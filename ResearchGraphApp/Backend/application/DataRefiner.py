"""
DataRefiner.py — Phase 2.5: Noise Reduction & Structured Refining.
Uses Gemini 2.5 Pro's 1M token context window to filter noise from
raw web and social scrapes, verify facts, and re-organize data.

Architecture alignment (v2)
───────────────────────────
Previously this module instantiated its own isolated genai.Client
pointing at the ``global`` location with NO regional failover.  Under
concurrent Phase 2.6 workloads this caused 429 RESOURCE_EXHAUSTED
errors that were silently swallowed and written to disk as error
strings, corrupting the knowledge base and producing empty Graphify
graphs.

The refactored version:
  1. Uses a resilient client factory that mirrors VertexProxy's
     STABLE_REGIONS failover pool (global → europe-west4 → us-east4
     → asia-northeast1 → us-central1).
  2. Raises RuntimeError on final exhaustion instead of returning
     error strings, enabling the orchestrator (IngestSeedUseCase) to
     skip corrupt file writes.

Thread-safety: Client construction is stateless (no shared mutable
singleton required).  Each call creates a lightweight client scoped
to the target region.  The google-genai SDK manages connection
pooling internally.
"""

import logging
import os

from google import genai
from google.genai import types
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception


logger = logging.getLogger(__name__)

REFINER_SYSTEM_INSTRUCTION = (
    "You are an Academic Data Refiner and Academic Rigor Critic. Ingest the provided corpus. Your primary goal is to preserve data lineage. "
    "Your task is to: "
    "1. Before summarizing a source, internally score it (0-10) based on methodological value and factual density. "
    "If a source appears to be corporate marketing, SEO fluff, or lacks verifiable evidence (Rigor Score < 5), you must completely discard it and omit it from the refined ledger. "
    "2. Verify facts against the provided academic context. "
    "3. Re-organize the useful data into a clean Markdown format, tagging every "
    "section with its origin (e.g., [Source: X/Twitter]). "
    "4. Extract any new high-value URLs for the next crawl phase. "
    "In that section use one line per URL in the exact form: Title [https://full-url] "
    "(no section headings, no bare URLs, no markdown [title](url) links)."
    "For every finding, you MUST maintain the [Source: Origin] tag. If the input exceeds 500,000 tokens, "
    "prioritize sections relevant to 'Methodological Weaknesses' and 'Future Work'. "
    "Output a clean, structured Markdown summary that does not exceed 60,000 tokens."
)


# ── Regional Redundancy Pool ────────────────────────────────────────────────
# Mirrors VertexProxy.STABLE_REGIONS so that DataRefiner threads benefit
# from the same multi-region failover when the primary global endpoint
# returns 429 RESOURCE_EXHAUSTED.
_STABLE_REGIONS: list[str] = [
    "europe-west4",
    "us-east4",
    "asia-northeast1",
    "us-central1",
]


def _get_project_id() -> str:
    """Return GOOGLE_CLOUD_PROJECT_ID or raise RuntimeError."""
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT_ID")
    if not project_id:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT_ID not set.")
    return project_id


def _make_client(location: str = "global") -> genai.Client:
    """Create a genai.Client for the given Vertex AI location."""
    return genai.Client(
        vertexai=True,
        project=_get_project_id(),
        location=location,
    )


def _is_resource_exhausted(exc: Exception) -> bool:
    """Check if the exception is a 429 ResourceExhausted."""
    exc_str = str(exc).lower()
    return "429" in exc_str or "resourceexhausted" in exc_str or "resource_exhausted" in exc_str


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    retry=retry_if_exception(_is_resource_exhausted)
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

    Raises:
        RuntimeError: If all regional endpoints are exhausted (429) or
            if GOOGLE_CLOUD_PROJECT_ID is not set.  The orchestrator
            must catch this and skip the file-writing step.
    """
    if not raw_data or not raw_data.strip():
        raise RuntimeError("No raw data provided for refinement — refusing to produce empty output.")

    # Validate environment early so the error is clear
    project_id = _get_project_id()
    model = "gemini-2.5-pro"

    config = types.GenerateContentConfig(
        max_output_tokens=65536,
        system_instruction=REFINER_SYSTEM_INSTRUCTION,
    )

    # ── Primary attempt: global endpoint ─────────────────────────────────
    try:
        client = _make_client("global")
        response = _call_gemini_with_retry(
            client=client,
            model=model,
            contents=[raw_data],
            config=config,
        )
        text = response.text
        if text and text.strip():
            return text
        raise RuntimeError("Gemini returned an empty response on the global endpoint.")
    except Exception as primary_exc:
        logger.warning(
            "DataRefiner: global endpoint failed (%s). "
            "Attempting regional failover through STABLE_REGIONS...",
            primary_exc,
        )
        last_exc = primary_exc

    # ── Regional failover: cycle through STABLE_REGIONS ──────────────────
    for region in _STABLE_REGIONS:
        try:
            logger.info("DataRefiner: regional failover → %s", region)
            client = _make_client(region)
            response = _call_gemini_with_retry(
                client=client,
                model=model,
                contents=[raw_data],
                config=config,
            )
            text = response.text
            if text and text.strip():
                logger.info("DataRefiner: regional failover SUCCESS → %s", region)
                return text
            raise RuntimeError(f"Gemini returned an empty response on {region}.")
        except Exception as region_exc:
            logger.warning("DataRefiner: region %s failed: %s", region, region_exc)
            last_exc = region_exc
            continue

    # ── All regions exhausted — FAIL FAST ────────────────────────────────
    raise RuntimeError(
        f"Refinement failed after exhausting all regions (global + "
        f"{', '.join(_STABLE_REGIONS)}). Last error: {last_exc}"
    )
