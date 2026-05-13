"""
AgentSynthesizer.py — Gemini 2.5 Pro LLM synthesis via Vertex AI.
Uses google-genai SDK with Application Default Credentials (ADC).
Analyzes scraped context to extract structural holes between social problems and academic limitations.
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
            "You are an expert academic research engine. Analyze the following scraped context, "
            "which contains social media problems and academic literature reviews.\n\n"
            "Your goal is to synthesize this information into a Final Year Project (FYP) rubric. "
            "Actively identify 'Structural Holes' between the societal problems discussed on social platforms "
            "and the limitations documented in the academic literature to propose a novel approach.\n\n"
            "You MUST strictly output your synthesis using the following Markdown headers:\n\n"
            "## Problem Background\n"
            "(Synthesize the societal/practical problems found)\n\n"
            "## Existing Solutions/Competitors (Literature)\n"
            "(Synthesize the current work in the domain)\n\n"
            "## Methodological Weaknesses (The Gap)\n"
            "(Identify structural holes, limitations, and areas for future work)\n\n"
            "## Proposed Novelty\n"
            "(Propose a novel FYP approach addressing the gap)\n\n"
            "Here is the context data:\n"
            f"{context_text}"
        )

        response = client.models.generate_content(
            model=model,
            contents=[prompt],
        )
        return response.text
    except Exception as e:
        return f"# Synthesis Error\nGemini API call failed: {e}"
