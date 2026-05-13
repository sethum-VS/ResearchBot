"""
DataRefiner.py — Phase 2.5: Noise Reduction & Structured Refining.
Uses Gemini 2.5 Pro's 1M token context window to filter noise from
raw web and social scrapes, verify facts, and re-organize data.
"""

import os
from google import genai
from google.genai import types
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception


REFINER_SYSTEM_INSTRUCTION = (
    "You are an Academic Data Refiner. Ingest the provided corpus. Your primary goal is to preserve data lineage. "
    "web and social media data. Your task is to: "
    "1. Remove all advertisements, off-topic rants, and irrelevant marketing fluff. "
    "2. Verify facts against the provided academic context. "
    "3. Re-organize the useful data into a clean Markdown format, tagging every "
    "section with its origin (e.g., [Source: X/Twitter]). "
    "4. Extract any new high-value URLs for the next crawl phase."
    "For every finding, you MUST maintain the [Source: Origin] tag. If the input exceeds 500,000 tokens, "
    "prioritize sections relevant to 'Methodological Weaknesses' and 'Future Work'. "
    "Output a clean, structured Markdown summary that does not exceed 60,000 tokens."

)


def is_resource_exhausted(exc: Exception) -> bool:
    """Check if the exception is a 429 ResourceExhausted."""
    exc_str = str(exc).lower()
    return "429" in exc_str or "resourceexhausted" in exc_str or "resource_exhausted" in exc_str


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    retry=retry_if_exception(is_resource_exhausted)
)
def _call_gemini_with_retry(client: genai.Client, model: str, contents: list, config: types.GenerateContentConfig):
    return client.models.generate_content(
        model=model,
        contents=contents,
        config=config,
    )


def refine_scraped_data(raw_data: str) -> str:
    """
    Filter noise from raw scraped data using Gemini 2.5 Pro.

    Args:
        raw_data: Combined raw output from Social, Web, and Academic scrapers.

    Returns:
        Cleaned, re-organized Markdown string.
    """
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT_ID")
    if not project_id:
        return "# Refiner Error\nGOOGLE_CLOUD_PROJECT_ID not set."

    if not raw_data or not raw_data.strip():
        return "# Refiner Warning\nNo raw data provided for refinement."

    try:
        client = genai.Client(vertexai=True, project=project_id, location="global")
        model = "gemini-2.5-pro"

        config = types.GenerateContentConfig(
            max_output_tokens=65536,
            system_instruction=REFINER_SYSTEM_INSTRUCTION,
        )

        response = _call_gemini_with_retry(
            client=client,
            model=model,
            contents=[raw_data],
            config=config,
        )
        return response.text or "# Refiner Warning\nGemini returned an empty response."
    except Exception as e:
        return f"# Refiner Error\nGemini API call failed: {e}"
