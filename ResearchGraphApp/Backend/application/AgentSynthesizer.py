"""
AgentSynthesizer.py — Gemini 2.5 Pro LLM synthesis via Vertex AI.
Uses google-genai SDK with Application Default Credentials (ADC).
Analyzes scraped context to extract competitors, research gaps, and core findings.
"""

import os
from google import genai
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

def _is_resource_exhausted(exc: Exception) -> bool:
    """Check if the exception is a 429 ResourceExhausted."""
    exc_str = str(exc).lower()
    return "429" in exc_str or "resourceexhausted" in exc_str or "resource_exhausted" in exc_str

_STABLE_REGIONS = [
    "us-central1",
    "us-east4",
    "us-west1",
    "europe-west1",
    "europe-west4",
    "asia-southeast1",
]

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    retry=retry_if_exception(_is_resource_exhausted)
)
def _call_gemini_with_retry(client: genai.Client, model: str, prompt: str):
    return client.models.generate_content(
        model=model,
        contents=[prompt],
    )

def synthesize_context(context_text: str) -> str:
    """Synthesize research context using Gemini 2.5 Pro via Vertex AI."""
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT_ID")
    if not project_id:
        return "# Synthesis Error\nGOOGLE_CLOUD_PROJECT_ID not set."

    model = "gemini-2.5-pro"
    prompt = (
        "Analyze the following research context and synthesize the core findings, "
        "competitors, and research gaps:\n\n"
        f"{context_text}"
    )

    # ── Primary attempt: global endpoint ─────────────────────────────────
    try:
        client = genai.Client(vertexai=True, project=project_id, location="global")
        response = _call_gemini_with_retry(client, model, prompt)
        return response.text
    except Exception as primary_exc:
        print(f"AgentSynthesizer: global endpoint failed ({primary_exc}). Attempting regional failover...")
        last_exc = primary_exc

    # ── Regional failover: cycle through STABLE_REGIONS ──────────────────
    for region in _STABLE_REGIONS:
        try:
            print(f"AgentSynthesizer: regional failover → {region}")
            client = genai.Client(vertexai=True, project=project_id, location=region)
            response = _call_gemini_with_retry(client, model, prompt)
            return response.text
        except Exception as region_exc:
            print(f"AgentSynthesizer: region {region} failed: {region_exc}")
            last_exc = region_exc
            continue

    return f"# Synthesis Error\nGemini API call failed across all regions: {last_exc}"
