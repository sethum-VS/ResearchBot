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

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    retry=retry_if_exception(_is_resource_exhausted)
)
def synthesize_context(context_text: str) -> str:
    """Synthesize research context using Gemini 2.5 Pro via Vertex AI."""
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT_ID")
    if not project_id:
        return "# Synthesis Error\nGOOGLE_CLOUD_PROJECT_ID not set."

    try:
        client = genai.Client(vertexai=True, project=project_id, location="global")
        model = "gemini-2.5-pro"

        prompt = (
            "Analyze the following research context and synthesize the core findings, "
            "competitors, and research gaps:\n\n"
            f"{context_text}"
        )

        response = client.models.generate_content(
            model=model,
            contents=[prompt],
        )
        return response.text
    except Exception as e:
        return f"# Synthesis Error\nGemini API call failed: {e}"
