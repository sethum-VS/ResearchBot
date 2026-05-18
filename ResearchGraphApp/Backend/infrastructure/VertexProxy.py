"""
VertexProxy.py — Local FastAPI proxy that bridges the Graphify CLI
(which speaks the Google AI Studio / OpenAI-compatible REST protocol)
to Google Cloud Vertex AI via the ``google-genai`` SDK and ADC.

Graphify's ``gemini`` backend sends OpenAI-compatible chat.completions
requests to ``https://generativelanguage.googleapis.com/v1beta/openai/``.
This proxy intercepts those requests on localhost:8000, translates them
to Vertex AI calls using ADC credentials, and returns a conforming
OpenAI-compatible response.

Model Routing
─────────────
- ``llama-4-scout``  → Meta Llama 4 Scout on Vertex AI (us-east5 primary)
  with max_output_tokens clamped to 8192 (Meta hard cap) and temperature=0.1.
  On 429 exhaustion, fails over through STABLE_REGIONS.
- ``gemini-2.5-pro`` → Pinned; always routed directly, never pool-rotated.
  On 429 exhaustion, fails over through STABLE_REGIONS.
- All other models   → Gemini via global endpoint with caller-specified
  or default generation parameters.

Model Load Balancer
───────────────────
To mitigate 429 ResourceExhausted / ClientError rate-limiting when
Phase 2.6 fires multiple concurrent worker threads, requests are
distributed across MODEL_POOL via an atomic round-robin counter.
On ClientError, the proxy transparently falls back to the next model
in the pool before propagating failure.

Pinned Model Guard
──────────────────
Models listed in PINNED_MODELS are NEVER pool-rotated or substituted.
This protects Phase 4 (Graphify / Llama 4 Scout) and synthesis tasks
(gemini-2.5-pro) from being silently downgraded to flash/lite variants,
which cannot reliably produce the structured JSON graph output required
by the Swift bridging contract.

Lifecycle: started by ``test_backend.sh`` before ``main.py`` runs,
           killed on script exit via ``trap``.
"""

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

app = FastAPI(title="ResearchBot Vertex AI Proxy")

# Path resolution: infrastructure/VertexProxy.py -> infrastructure -> Backend -> ResearchGraphApp -> ResearchBot (root)
root_dir = Path(__file__).resolve().parent.parent.parent.parent
env_path = root_dir / ".env"
load_dotenv(env_path)

# 2. Get project ID with a fallback check
_PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT_ID")

if not _PROJECT_ID:
    print("⚠️ WARNING: GOOGLE_CLOUD_PROJECT_ID not found in environment.")
    print("  Proxy will reject requests until the variable is set.")

# Initialise the Vertex AI client once at module level.
_client = genai.Client(
    vertexai=True,
    project=_PROJECT_ID,
    location="global",
)

# ── Pinned Models (Never Pool-Rotated) ──────────────────────────────────────
# These models are called explicitly by high-order pipeline stages (Phase 4
# Graphify, Phase 3 synthesis) and must NEVER be substituted with lower-tier
# variants. Routing them through the pool would cause JSON parse failures and
# context-window truncation errors in the Swift bridging contract.

PINNED_MODELS: frozenset[str] = frozenset({
    "llama-4-scout",
    "gemini-2.5-pro",
})


def _is_pinned_model(model_name: str) -> bool:
    """
    Return True if *model_name* must bypass pool rotation.

    - Exact membership in PINNED_MODELS.
    - Any ``llama-4`` prefix variant (catches future Llama 4 sub-models).
    """
    if model_name in PINNED_MODELS:
        return True
    if model_name.startswith("llama-4"):
        return True
    return False


# ── Model Load Balancer ─────────────────────────────────────────────────────
# Equivalent Gemini model pool for distributing concurrent Phase 2.6 requests.
# Thread-safe atomic counter rotates through the pool to avoid hammering a
# single endpoint and triggering 429 rate limits.
# IMPORTANT: PINNED_MODELS are EXCLUDED from this pool so they can never be
# selected as a rotation target or fallback destination.

MODEL_POOL = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-001",
    "gemini-2.5-flash-preview-09-2025",
    "gemini-2.5-flash-lite-preview-09-2025",
    "gemini-2.0-flash-lite-001",
]

_pool_counter = 0
_pool_lock = threading.Lock()


def _next_pool_model() -> str:
    """Return the next model from MODEL_POOL via thread-safe round-robin."""
    global _pool_counter
    with _pool_lock:
        model = MODEL_POOL[_pool_counter % len(MODEL_POOL)]
        _pool_counter += 1
    return model


# ── Model-Specific Configuration ────────────────────────────────────────────

# Maps model-name prefixes to their primary Vertex AI region.
# Llama 4 partner models are available via the us-east5 MaaS endpoint.
MODEL_REGION_MAP: dict[str, str] = {
    "llama-4-scout": "us-east5",
}

# Meta Llama 4 Scout hard cap: the MaaS endpoint rejects any request
# with maxOutputTokens > 8192 (returns 400 INVALID_ARGUMENT).
_LLAMA_MAX_OUTPUT_TOKENS = 8192

# Full Vertex AI resource-name template for Meta Llama 4 Scout.
# The {location} placeholder allows regional failover.
_LLAMA_SCOUT_RESOURCE_TPL = (
    "projects/{project}/locations/{location}"
    "/publishers/meta/models/llama-4-scout-17b-16e-instruct-maas"
)

# ── Regional Redundancy Pool ────────────────────────────────────────────────
# Prioritised fallback regions for heavy reasoning models when the primary
# region returns 429 RESOURCE_EXHAUSTED. Ordered by observed capacity and
# latency for Apple Silicon workstations.
STABLE_REGIONS: list[str] = [
    "europe-west4",
    "us-east4",
    "asia-northeast1",
    "us-central1",
]


def _get_client_and_model(
    model_alias: str,
    location_override: str | None = None,
) -> tuple[genai.Client, str]:
    """
    Return the correct (client, resolved_model_name) pair.

    Parameters
    ----------
    model_alias : str
        The logical model name (e.g. ``llama-4-scout``, ``gemini-2.5-pro``).
    location_override : str | None
        If provided, forces the client to this specific region instead of
        the default. Used by the regional failover loop.

    Returns
    -------
    tuple[genai.Client, str]
        A (client, resolved_model_name) pair ready for ``generate_content``.
    """
    if model_alias.startswith("llama-4"):
        location = location_override or MODEL_REGION_MAP.get(model_alias, "us-east5")
        regional_client = genai.Client(
            vertexai=True,
            project=_PROJECT_ID,
            location=location,
        )
        resource_name = _LLAMA_SCOUT_RESOURCE_TPL.format(
            project=_PROJECT_ID, location=location,
        )
        return regional_client, resource_name

    # Gemini pinned models (gemini-2.5-pro) — use override region if provided
    if location_override:
        regional_client = genai.Client(
            vertexai=True,
            project=_PROJECT_ID,
            location=location_override,
        )
        return regional_client, model_alias

    # Default: global endpoint
    return _client, model_alias


def _clamp_llama_tokens(gen_config: types.GenerateContentConfig) -> None:
    """
    Enforce Meta's hard cap of 8192 max_output_tokens for Llama 4 Scout.

    Graphify (and other upstream callers) may request up to 16384 tokens.
    The Vertex AI MaaS endpoint rejects anything > 8192 with a
    ``400 INVALID_ARGUMENT`` error. This function silently clamps the
    value to prevent that failure without requiring upstream code changes.
    """
    current = getattr(gen_config, "max_output_tokens", None)
    if current is not None and current > _LLAMA_MAX_OUTPUT_TOKENS:
        logger.info(
            "🔧 Proxy Intercept: Clamping max_output_tokens from %d to %d "
            "for Llama 4 Scout compatibility.",
            current, _LLAMA_MAX_OUTPUT_TOKENS,
        )
        gen_config.max_output_tokens = _LLAMA_MAX_OUTPUT_TOKENS


def _strip_json_fences(text: str) -> str:
    """
    Remove markdown code fences that models sometimes emit around JSON.

    Handles BOTH complete fences (```json ... ```) and TRUNCATED output
    where the closing fence is missing because max_output_tokens was hit.
    This prevents ``Extra data: line 2 column 1`` JSON parse errors in
    the Swift bridging layer.
    """
    stripped = text.strip()

    # 1) Try complete fence first: ```json\n{...}\n```
    complete_fence = re.compile(
        r'^```(?:json|JSON)?\s*\n(.*?)\n?```\s*$',
        re.DOTALL,
    )
    m = complete_fence.match(stripped)
    if m:
        return m.group(1).strip()

    # 2) Handle truncated fence: opening ``` exists but closing ``` is missing
    #    (model hit max_output_tokens mid-generation)
    opening_fence = re.compile(r'^```(?:json|JSON)?\s*\n', re.DOTALL)
    m = opening_fence.match(stripped)
    if m:
        return stripped[m.end():].strip()

    return stripped


def _extract_finish_reason(response) -> str:
    """
    Map the Vertex AI FinishReason enum to an OpenAI-compatible string.

    Critical for downstream consumers (Graphify) to detect truncated JSON
    output and avoid parsing incomplete payloads.

    Returns
    -------
    str
        ``"stop"``   — model completed naturally.
        ``"length"`` — model hit max_output_tokens (output is truncated).
        ``"stop"``   — fallback if the enum value is unrecognised.
    """
    try:
        candidate = response.candidates[0]
        reason = candidate.finish_reason

        # The google-genai SDK exposes FinishReason as an enum.
        # Compare against known values robustly (enum or string).
        reason_str = str(reason).upper() if reason else "STOP"

        if "MAX_TOKENS" in reason_str:
            return "length"
        if "SAFETY" in reason_str:
            return "content_filter"
        if "STOP" in reason_str:
            return "stop"

        # Unknown reason — log but don't crash
        logger.warning("Unrecognised finish_reason: %s — defaulting to 'stop'", reason)
        return "stop"
    except (IndexError, AttributeError) as exc:
        logger.warning("Could not extract finish_reason: %s — defaulting to 'stop'", exc)
        return "stop"


@app.post("/{path:path}")
async def proxy(path: str, request: Request):
    """
    Catch-all POST handler.  Graphify sends OpenAI-style
    ``/chat/completions`` requests here.  We forward the
    contents to Vertex AI and return an OpenAI-shaped response.
    """
    body = await request.json()

    # --- Extract model --------------------------------------------------
    model_name = body.get("model", "gemini-2.5-flash")

    # --- Build contents from OpenAI messages ----------------------------
    messages = body.get("messages", [])
    system_text = None
    contents = []

    for msg in messages:
        role = msg.get("role", "user")
        text = msg.get("content", "")

        if role == "system":
            system_text = text
            continue

        contents.append(
            types.Content(
                role="user" if role == "user" else "model",
                parts=[types.Part.from_text(text=text)],
            )
        )

    # --- Build generation config ----------------------------------------
    # Model-specific overrides: Llama 4 Scout is capped at 8192 output
    # tokens by Meta's MaaS contract (400 INVALID_ARGUMENT above that).
    is_llama = model_name.startswith("llama-4")

    if is_llama:
        gen_config = types.GenerateContentConfig(
            temperature=0.1,
            max_output_tokens=_LLAMA_MAX_OUTPUT_TOKENS,
        )
    else:
        gen_config = types.GenerateContentConfig(
            temperature=body.get("temperature", 0),
            max_output_tokens=65536,
        )

    # --- System instruction routing -------------------------------------
    # Vertex AI Llama MaaS endpoints ignore the system_instruction config
    # field. For Llama models, we prepend the system text into the first
    # user message so the model actually sees Graphify's strict JSON
    # formatting rules.  Gemini models use the native config field.
    if system_text:
        if is_llama:
            # Inject system prompt into the first user message
            if contents:
                first_user = contents[0]
                original_text = first_user.parts[0].text if first_user.parts else ""
                merged_text = f"[System Instructions]\n{system_text}\n\n[User Message]\n{original_text}"
                contents[0] = types.Content(
                    role=first_user.role,
                    parts=[types.Part.from_text(text=merged_text)],
                )
            else:
                # No user messages yet — create one from system text
                contents.insert(0, types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=system_text)],
                ))
            logger.info("Llama model: system prompt prepended to first user message.")
        else:
            gen_config.system_instruction = system_text

    # Check for JSON mode request
    if body.get("response_format", {}).get("type") == "json_object":
        gen_config.response_mime_type = "application/json"

    # --- Sanitise Llama token parameters --------------------------------
    # Intercept and clamp any max_output_tokens value that exceeds Meta's
    # hard cap of 8192. This is evaluated BEFORE the call so it applies
    # consistently to both the primary path and regional failover loop.
    if is_llama:
        _clamp_llama_tokens(gen_config)

    # --- Resolve client + model for Vertex AI ---------------------------
    # PINNED models (llama-4-scout, gemini-2.5-pro) bypass pool rotation
    # entirely and are routed to their exact regional/global endpoint.
    # All other models are distributed through MODEL_POOL to balance load
    # across concurrent Phase 2.6 workers and mitigate 429 rate-limiting.
    if _is_pinned_model(model_name):
        client, resolved_model = _get_client_and_model(model_name)
        logger.info("Pinned model routing: %s → %s", model_name, resolved_model)
    else:
        resolved_model = _next_pool_model()
        client = _client
        logger.info("Model pool rotation: requested=%s → routed=%s", model_name, resolved_model)

    # --- Call Vertex AI (with retry + model fallback) -------------------
    def _is_retryable(exc: Exception) -> bool:
        s = str(exc).lower()
        return any(code in s for code in ("429", "503", "resourceexhausted", "resource_exhausted"))

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception(_is_retryable),
    )
    def _generate_with_model(use_client: genai.Client, target_model: str):
        return use_client.models.generate_content(
            model=target_model,
            contents=contents,
            config=gen_config,
        )

    finish_reason = "stop"  # default; overwritten by actual response

    try:
        response = _generate_with_model(client, resolved_model)
        text_out = response.text or ""
        finish_reason = _extract_finish_reason(response)
    except Exception as primary_exc:
        # ── Pinned Model: Regional Failover ──────────────────────────────
        # Pinned models are NOT downgraded to the pool. Instead, they
        # cycle through STABLE_REGIONS with fresh clients to route around
        # 429 RESOURCE_EXHAUSTED on the primary region.
        if _is_pinned_model(model_name):
            logger.warning(
                "Pinned model %s primary region failed (%s). "
                "Attempting regional failover through STABLE_REGIONS...",
                resolved_model, primary_exc,
            )
            text_out = None
            last_exc = primary_exc

            for region in STABLE_REGIONS:
                try:
                    logger.info(
                        "Regional failover: trying %s in %s", model_name, region,
                    )
                    failover_client, failover_model = _get_client_and_model(
                        model_name, location_override=region,
                    )
                    response = failover_client.models.generate_content(
                        model=failover_model,
                        contents=contents,
                        config=gen_config,
                    )
                    text_out = response.text or ""
                    finish_reason = _extract_finish_reason(response)
                    resolved_model = failover_model
                    logger.info(
                        "Regional failover SUCCESS: %s in %s", model_name, region,
                    )
                    break
                except Exception as region_exc:
                    logger.warning(
                        "Regional failover %s failed: %s", region, region_exc,
                    )
                    last_exc = region_exc
                    continue

            if text_out is None:
                logger.error(
                    "Pinned model %s exhausted all regions. Last error: %s",
                    model_name, last_exc,
                )
                return JSONResponse(
                    status_code=500,
                    content={"error": {"message": str(last_exc), "type": "proxy_error"}},
                )
        else:
            # ── Pool-Rotated Model: Model Fallback ───────────────────────
            logger.warning(
                "Primary model %s failed (%s), attempting pool fallback...",
                resolved_model, primary_exc,
            )
            text_out = None
            last_exc = primary_exc
            for _ in range(len(MODEL_POOL)):
                fallback_model = _next_pool_model()
                if fallback_model == resolved_model:
                    continue  # skip the one that just failed
                try:
                    logger.info("Fallback attempt with model: %s", fallback_model)
                    response = _generate_with_model(client, fallback_model)
                    text_out = response.text or ""
                    finish_reason = _extract_finish_reason(response)
                    resolved_model = fallback_model
                    break
                except Exception as fallback_exc:
                    logger.warning("Fallback model %s also failed: %s", fallback_model, fallback_exc)
                    last_exc = fallback_exc
                    continue

            if text_out is None:
                return JSONResponse(
                    status_code=500,
                    content={"error": {"message": str(last_exc), "type": "proxy_error"}},
                )

    # ── JSON fence sanitisation ──────────────────────────────────────────
    # Strip markdown code fences that flash/lite models sometimes emit even
    # when response_mime_type="application/json" is set. This prevents the
    # "Extra data: line 2 column 1" parse error in the Swift bridging layer.
    is_json_mode = (
        body.get("response_format", {}).get("type") == "json_object"
        or getattr(gen_config, "response_mime_type", None) == "application/json"
    )
    if is_json_mode and text_out:
        text_out = _strip_json_fences(text_out)

    # Log a warning if the output was truncated — helps diagnose graph
    # generation failures without diving into Graphify's error stream.
    if finish_reason == "length":
        logger.warning(
            "Model %s hit max_output_tokens — output is TRUNCATED. "
            "Downstream JSON parsing will likely fail.",
            resolved_model,
        )

    # --- Return OpenAI-compatible response ------------------------------
    return JSONResponse(content={
        "id": f"proxy-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": resolved_model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": text_out,
                },
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    })
