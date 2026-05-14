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
- ``llama-4-scout``  → Meta Llama 4 Scout on Vertex AI (us-east5)
  with hardcoded max_output_tokens=4096 and temperature=0.1.
- All other models   → Gemini via global endpoint with caller-specified
  or default generation parameters.

Lifecycle: started by ``test_backend.sh`` before ``main.py`` runs,
           killed on script exit via ``trap``.
"""

import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from google import genai
from google.genai import types

app = FastAPI(title="ResearchBot Vertex AI Proxy")

# Path resolution: infrastructure/VertexProxy.py -> infrastructure -> Backend -> ResearchGraphApp -> ResearchBot (root)
root_dir = Path(__file__).resolve().parent.parent.parent.parent
env_path = root_dir / ".env"
load_dotenv(env_path)

# 2. Get project ID with a fallback check
_PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT_ID")

if not _PROJECT_ID:
    # Optional: Log a warning or try to discover via ADC
    print("⚠️ WARNING: GOOGLE_CLOUD_PROJECT_ID not found in environment.")

# Initialise the Vertex AI client once at module level.
_client = genai.Client(
    vertexai=True,
    project=_PROJECT_ID,
    location="global",
)

# ── Model-Specific Configuration ────────────────────────────────────────────

# Maps model-name prefixes to their required Vertex AI region.
# Llama 4 partner models are only available via the us-east5 MaaS endpoint.
MODEL_REGION_MAP: dict[str, str] = {
    "llama-4-scout": "us-east5",
}

# Full Vertex AI resource-name template for Meta Llama 4 Scout.
_LLAMA_SCOUT_RESOURCE_TPL = (
    "projects/{project}/locations/us-east5"
    "/publishers/meta/models/llama-4-scout-17b-16e-instruct-maas"
)


def _get_client_and_model(model_alias: str) -> tuple[genai.Client, str]:
    """
    Return the correct (client, resolved_model_name) pair.

    - If the model starts with ``llama-4``, create a per-request client
      pointed at us-east5 and return the full Vertex AI resource name.
    - All other models use the module-level global client (Gemini/global).
    """
    if model_alias.startswith("llama-4"):
        location = MODEL_REGION_MAP.get(model_alias, "us-east5")
        regional_client = genai.Client(
            vertexai=True,
            project=_PROJECT_ID,
            location=location,
        )
        resource_name = _LLAMA_SCOUT_RESOURCE_TPL.format(project=_PROJECT_ID)
        return regional_client, resource_name

    return _client, model_alias


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
    # Model-specific overrides: Llama 4 Scout has strict limits
    if model_name == "llama-4-scout":
        gen_config = types.GenerateContentConfig(
            temperature=0.1,
            max_output_tokens=4096,
        )
    else:
        gen_config = types.GenerateContentConfig(
            temperature=body.get("temperature", 0),
            max_output_tokens=65536,
        )

    if system_text:
        gen_config.system_instruction = system_text

    # Check for JSON mode request
    if body.get("response_format", {}).get("type") == "json_object":
        gen_config.response_mime_type = "application/json"

    # --- Resolve client + model for Vertex AI ---------------------------
    client, resolved_model = _get_client_and_model(model_name)

    # --- Call Vertex AI -------------------------------------------------
    try:
        response = client.models.generate_content(
            model=resolved_model,
            contents=contents,
            config=gen_config,
        )
        text_out = response.text or ""
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"error": {"message": str(exc), "type": "proxy_error"}},
        )

    # --- Return OpenAI-compatible response ------------------------------
    return JSONResponse(content={
        "id": f"proxy-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": text_out,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    })
