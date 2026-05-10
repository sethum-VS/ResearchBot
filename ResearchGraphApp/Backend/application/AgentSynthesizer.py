"""
AgentSynthesizer.py — Gemini 2.5 Pro LLM synthesis via Vertex AI.
Uses google-genai SDK with Application Default Credentials (ADC).
Analyzes scraped context to extract competitors, research gaps, and core findings.
"""

import os
from google import genai


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
