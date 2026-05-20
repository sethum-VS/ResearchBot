"""
InputAnalyzer.py — Phase 1.5: AI Pre-processing via Gemini 2.5 Flash.
Parses the user's raw seed input into structured keywords, URLs, and
intent classification using Pydantic-based structured output.

Resilience: global endpoint with tenacity retry on 429, then STABLE_REGIONS
regional failover (mirrors DataRefiner / IngestSeedUseCase URL extraction).
"""

import logging
import os

from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

logger = logging.getLogger(__name__)


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


# Mirrors DataRefiner / VertexProxy — regional failover when global 429s.
_STABLE_REGIONS: list[str] = [
    "europe-west4",
    "us-east4",
    "asia-northeast1",
    "us-central1",
]


def _is_resource_exhausted(exc: Exception) -> bool:
    """Check if the exception is a 429 ResourceExhausted."""
    exc_str = str(exc).lower()
    return "429" in exc_str or "resourceexhausted" in exc_str or "resource_exhausted" in exc_str


def _analysis_config() -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=SeedAnalysis,
    )


def _build_prompt(raw_input: str) -> str:
    return (
        "You are a research-intent analyzer. Given the user's raw input below, "
        "extract structured information. Identify the core context, generate "
        "3-5 highly optimized search queries, extract any URLs, and classify "
        "the user's intent.\n\n"
        f"User Input:\n{raw_input}"
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    retry=retry_if_exception(_is_resource_exhausted),
)
def _call_flash_with_retry(
    client: genai.Client,
    prompt: str,
    config: types.GenerateContentConfig,
):
    return client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[prompt],
        config=config,
    )


def _parse_analysis_response(response) -> dict:
    if not response or not response.text:
        raise RuntimeError("Gemini returned an empty response.")
    parsed = SeedAnalysis.model_validate_json(response.text)
    return parsed.model_dump()


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

    prompt = _build_prompt(raw_input)
    config = _analysis_config()
    last_exc: Exception | None = None

    # ── Primary: global endpoint ─────────────────────────────────────────
    try:
        client = genai.Client(vertexai=True, project=project_id, location="global")
        response = _call_flash_with_retry(client, prompt, config)
        return _parse_analysis_response(response)
    except Exception as primary_exc:
        logger.warning(
            "InputAnalyzer: global endpoint failed (%s). Attempting regional failover...",
            primary_exc,
        )
        last_exc = primary_exc

    # ── Regional failover ─────────────────────────────────────────────────
    for region in _STABLE_REGIONS:
        try:
            logger.info("InputAnalyzer: regional failover → %s", region)
            client = genai.Client(vertexai=True, project=project_id, location=region)
            response = _call_flash_with_retry(client, prompt, config)
            result = _parse_analysis_response(response)
            logger.info("InputAnalyzer: regional failover SUCCESS → %s", region)
            return result
        except Exception as region_exc:
            logger.warning("InputAnalyzer: region %s failed: %s", region, region_exc)
            last_exc = region_exc
            continue

    return _fallback(
        raw_input,
        reason=f"All regions exhausted ({', '.join(_STABLE_REGIONS)}). Last error: {last_exc}",
    )


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
