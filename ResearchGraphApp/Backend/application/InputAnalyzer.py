"""
InputAnalyzer.py — Phase 1.5: AI Pre-processing via Gemini 2.5 Flash.
Parses the user's raw seed input into structured keywords, URLs, and
intent classification using Pydantic-based structured output.
"""

import os
from pydantic import BaseModel, Field
from google import genai
from google.genai import types


# ── Pydantic Schema for Structured Output ────────────────────────────────────

class SeedAnalysis(BaseModel):
    """Structured output returned by Gemini 2.5 Flash."""

    core_context: str = Field(
        description="A 1-2 sentence summary of what the user is really asking for."
    )
    search_keywords: list[str] = Field(
        description="3-5 highly optimized search queries for web scraping."
    )
    extracted_urls: list[str] = Field(
        description="Any URLs found in the raw input."
    )
    user_intent: str = Field(
        description=(
            "Classification of the request. One of: "
            "Competitor Analysis, Academic Research, General Inquiry, "
            "Market Research, Technical Deep-Dive."
        )
    )


# ── Analyzer Function ────────────────────────────────────────────────────────

def analyze_seed(raw_input: str) -> dict:
    """
    Pass the user's raw seed text through Gemini 2.5 Flash to extract
    structured keywords, URLs, and intent.

    Returns a dict matching the SeedAnalysis schema, or a fallback dict
    on failure so the pipeline can continue with the raw input.
    """
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT_ID")
    if not project_id:
        return _fallback(raw_input, reason="GOOGLE_CLOUD_PROJECT_ID not set")

    try:
        client = genai.Client(vertexai=True, project=project_id, location="global")

        prompt = (
            "You are a research-intent analyzer. Given the user's raw input below, "
            "extract structured information. Identify the core context, generate "
            "3-5 highly optimized search queries, extract any URLs, and classify "
            "the user's intent.\n\n"
            f"User Input:\n{raw_input}"
        )

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=SeedAnalysis,
            ),
        )

        parsed = SeedAnalysis.model_validate_json(response.text)
        return parsed.model_dump()

    except Exception as e:
        return _fallback(raw_input, reason=str(e))


def _fallback(raw_input: str, reason: str) -> dict:
    """
    Graceful degradation — if Flash fails, return a minimal dict
    so Phase 2 scrapers can still run with the raw input.
    """
    return {
        "core_context": raw_input.strip(),
        "search_keywords": [raw_input.strip()],
        "extracted_urls": [],
        "user_intent": "General Inquiry",
        "_fallback_reason": reason,
    }
