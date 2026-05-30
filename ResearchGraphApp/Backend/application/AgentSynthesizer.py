"""
AgentSynthesizer.py — Phase 3: Gemini 2.5 Pro synthesis via Vertex AI.
Uses google-genai SDK with Application Default Credentials (ADC).
Produces FYP-oriented Markdown with rubric sections per PROJECT_SPEC.
"""

import logging
import os

from google import genai
from google.genai import types
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

import json

logger = logging.getLogger(__name__)

# Mirrors DataRefiner / VertexProxy / PROJECT_SPEC §9
_STABLE_REGIONS: list[str] = [
    "europe-west4",
    "us-east4",
    "asia-northeast1",
    "us-central1",
]

_SYNTHESIS_PROMPT = """You are an academic research synthesizer for university Final Year Projects (FYP).

Analyze the research context below and write a structured Markdown report with EXACTLY these sections (use ## headings):

## Problem Background
## Existing Solutions
## Methodological Weaknesses (The Gap)
## Proposed Novelty

Be specific to the provided context. Cite patterns from the refined data where possible. Do not invent sources.

--- RESEARCH CONTEXT ---
{context}
"""

_CRITIQUE_PROMPT = """You are a Synthesis Critic.
Review the following synthesis draft against the full research context.
Does this synthesis explicitly link the 'Existing Solutions' to the 'Methodological Weaknesses'?
Does it avoid hallucinating facts not in the context?

If the draft passes, return JSON with "passed": true and "revised_draft": "".
If it fails, output a revised version of the synthesis fixing its own logical gaps, returning JSON with "passed": false and "revised_draft": "<the revised text>".

--- FULL RESEARCH CONTEXT ---
{context}

--- SYNTHESIS DRAFT ---
{draft}
"""


def _is_resource_exhausted(exc: Exception) -> bool:
    exc_str = str(exc).lower()
    return "429" in exc_str or "resourceexhausted" in exc_str or "resource_exhausted" in exc_str


def _get_project_id() -> str:
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT_ID")
    if not project_id:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT_ID not set.")
    return project_id


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    retry=retry_if_exception(_is_resource_exhausted),
)
def _call_gemini_with_retry(client: genai.Client, model: str, prompt: str, config: types.GenerateContentConfig | None = None):
    kwargs = {
        "model": model,
        "contents": [prompt],
    }
    if config is not None:
        kwargs["config"] = config
    return client.models.generate_content(**kwargs)


def _generate_initial_draft(context_text: str) -> str:
    """
    Generate initial research context synthesis using Gemini 2.5 Pro.

    Raises:
        RuntimeError: If project ID is missing or all regions are exhausted.
    """
    if not context_text or not context_text.strip():
        raise RuntimeError("No context provided for synthesis.")

    project_id = _get_project_id()
    model = "gemini-2.5-pro"
    prompt = _SYNTHESIS_PROMPT.format(context=context_text)

    last_exc: Exception | None = None

    try:
        client = genai.Client(vertexai=True, project=project_id, location="global")
        response = _call_gemini_with_retry(client, model, prompt)
        text = response.text if response else None
        if text and text.strip():
            return text
        raise RuntimeError("Gemini returned an empty synthesis on the global endpoint.")
    except Exception as primary_exc:
        print(
            f"PROGRESS: Phase 3 — global endpoint failed ({primary_exc}). "
            "Attempting regional failover...",
            flush=True,
        )
        logger.warning("AgentSynthesizer: global failed (%s)", primary_exc)
        last_exc = primary_exc

    for region in _STABLE_REGIONS:
        try:
            print(f"PROGRESS: Phase 3 — regional failover → {region}", flush=True)
            client = genai.Client(vertexai=True, project=project_id, location=region)
            response = _call_gemini_with_retry(client, model, prompt)
            text = response.text if response else None
            if text and text.strip():
                return text
            raise RuntimeError(f"Gemini returned an empty synthesis on {region}.")
        except Exception as region_exc:
            print(f"PROGRESS: Phase 3 — region {region} failed: {region_exc}", flush=True)
            logger.warning("AgentSynthesizer: region %s failed: %s", region, region_exc)
            last_exc = region_exc
            continue

    raise RuntimeError(
        f"Synthesis failed after exhausting all regions (global + "
        f"{', '.join(_STABLE_REGIONS)}). Last error: {last_exc}"
    )


def _critique_and_revise(draft: str, full_context: str) -> str:
    project_id = _get_project_id()
    model = "gemini-2.5-flash"
    prompt = _CRITIQUE_PROMPT.format(context=full_context, draft=draft)
    config = types.GenerateContentConfig(response_mime_type="application/json")

    try:
        client = genai.Client(vertexai=True, project=project_id, location="global")
        response = _call_gemini_with_retry(client, model, prompt, config)
        if response and response.text:
            result = json.loads(response.text)
            if result.get("passed"):
                print("PROGRESS: Phase 3 — Critic approved the draft.", flush=True)
                return draft
            revised = (result.get("revised_draft") or "").strip()
            if revised:
                print("PROGRESS: Phase 3 — Critic revised the draft.", flush=True)
                return revised
            # Critic flagged issues but provided no revision text
            print("PROGRESS: Phase 3 — Critic flagged issues but provided no revision. Using original draft.", flush=True)
            return draft
    except Exception as exc:
        logger.warning("AgentSynthesizer critique failed on global: %s", exc)

    for region in _STABLE_REGIONS:
        try:
            client = genai.Client(vertexai=True, project=project_id, location=region)
            response = _call_gemini_with_retry(client, model, prompt, config)
            if response and response.text:
                result = json.loads(response.text)
                if result.get("passed"):
                    print(f"PROGRESS: Phase 3 — Critic approved the draft (region {region}).", flush=True)
                    return draft
                revised = (result.get("revised_draft") or "").strip()
                if revised:
                    print(f"PROGRESS: Phase 3 — Critic revised the draft (region {region}).", flush=True)
                    return revised
                print(f"PROGRESS: Phase 3 — Critic flagged issues but provided no revision (region {region}). Using original draft.", flush=True)
                return draft
        except Exception as exc:
            logger.warning("AgentSynthesizer critique failed on %s: %s", region, exc)

    logger.warning("AgentSynthesizer: All regions failed for critique, returning original draft.")
    return draft


def synthesize_context(context_text: str) -> str:
    """
    Synthesize research context by generating an initial draft and then critiquing/revising it.
    """
    draft = _generate_initial_draft(context_text)
    print("PROGRESS: Phase 3 — running Critic evaluation...", flush=True)
    return _critique_and_revise(draft, context_text)
