"""
SemanticMatcher.py — Phase 5: Semantic relevance scoring for proposal papers.

Evaluates individual papers against a scoped research query using Gemini 2.5
Flash. Each paper is scored on Task, Domain, and Constraint alignment with
strict penalties for tangential matches.

Concurrency: asyncio.gather with Semaphore(5) for batch scoring.
Resilience: global + STABLE_REGIONS failover, tenacity retry on 429.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re

from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception,
)

logger = logging.getLogger(__name__)

# ── Pydantic Schema for Rubric-Based Scoring ──────────────────────────────────

class RubricEvaluation(BaseModel):
    """Structured relevance scoring output returned by Gemini 2.5 Flash."""

    domain_alignment: int = Field(
        description="Does the paper operate in the same field? Score from 0 to 10."
    )
    task_alignment: int = Field(
        description="Is it trying to solve a similar problem? Score from 0 to 10."
    )
    method_relevance: int = Field(
        description="Is the technology or approach relevant? Score from 0 to 10."
    )
    total_percentage: float = Field(
        description="The total relevance percentage score, calculated as (domain_alignment + task_alignment + method_relevance) / 30 * 100. Range: 0 to 100."
    )
    reasoning: str = Field(
        description="A 1-sentence justification of the scores."
    )


_STABLE_REGIONS: list[str] = [
    "europe-west4",
    "us-east4",
    "asia-northeast1",
    "us-central1",
]

_MAX_CONCURRENT_SCORES = 5

_RUBRIC_SCORE_PROMPT_TEMPLATE = """\
You are an academic research relevance evaluator. Evaluate this paper against \
the specified core research criteria using a strict scoring rubric. You are \
provided with the paper's Abstract (what the authors set out to do) and, when \
available, its Conclusion (what they actually achieved, their limitations and \
future work recommendations). Use BOTH sections for a precise evaluation.

CORE RELEVANCE CRITERIA:
{core_criteria}

PAPER TITLE: {title}

PAPER CONTEXT (Abstract + Conclusion):
{paper_context}

Evaluation Rubric (Score each from 0 to 10):
1. Domain Alignment (0-10): Does the paper operate in the same field or scientific domain?
2. Task Alignment (0-10): Is the paper trying to solve a similar problem, task, or research gap?
   - Evaluate BOTH what the paper set out to do (Abstract) AND what it \
actually achieved or failed at (Conclusion/Limitations).
3. Method Relevance (0-10): Is the methodology, technology, or approach used in the paper relevant or applicable to our project?
   - Pay special attention to the Conclusion's stated limitations, as \
these may represent direct opportunities for our proposed work.

Calculate the total percentage score as: (domain_alignment + task_alignment + method_relevance) / 30 * 100.

Return ONLY a raw JSON object with no markdown fencing, conforming exactly to this schema:
{{
  "domain_alignment": <integer 0-10>,
  "task_alignment": <integer 0-10>,
  "method_relevance": <integer 0-10>,
  "total_percentage": <number 0-100>,
  "reasoning": "<1-sentence justification of the scores>"
}}"""


def _is_resource_exhausted(exc: Exception) -> bool:
    exc_str = str(exc).lower()
    return (
        "429" in exc_str
        or "resourceexhausted" in exc_str
        or "resource_exhausted" in exc_str
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    retry=retry_if_exception(_is_resource_exhausted),
)
async def _call_flash_async(client: genai.Client, contents: str) -> str:
    """Single Gemini 2.5 Flash call; tenacity retries on 429 only."""
    response = await client.aio.models.generate_content(
        model="gemini-2.5-flash",
        contents=contents,
    )
    return response.text if response and response.text else ""


def _parse_score(raw_text: str) -> float:
    """
    Extract match_percentage from Flash response.

    Handles raw JSON, markdown-fenced JSON, and plain numeric responses.
    Returns 0.0 on parse failure.
    """
    if not raw_text or not raw_text.strip():
        return 0.0

    text = raw_text.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    # Try JSON parse
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "match_percentage" in data:
            val = float(data["match_percentage"])
            return max(0.0, min(100.0, val))
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # Fallback: find a bare number
    match = re.search(r"(\d{1,3})(?:\.\d+)?", text)
    if match:
        val = float(match.group(1))
        if 0 <= val <= 100:
            return val

    logger.warning("SemanticMatcher: could not parse score from: %s", text[:200])
    return 0.0


async def _score_single_paper(
    query: str,
    paper: dict,
    index: int,
    total: int,
    semaphore: asyncio.Semaphore,
) -> tuple[dict, float]:
    """
    Score one paper against the query. Tries global, then STABLE_REGIONS.
    """
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT_ID")
    if not project_id:
        logger.warning("GOOGLE_CLOUD_PROJECT_ID not set; cannot score.")
        return paper, 0.0

    title = paper.get("title", "Untitled")
    abstract = paper.get("abstract", "")[:3000]

    prompt = _SCORE_PROMPT_TEMPLATE.format(
        query=query,
        title=title,
        abstract=abstract,
    )

    async with semaphore:
        last_exc: Exception | None = None

        # Primary: global endpoint
        try:
            client = genai.Client(
                vertexai=True, project=project_id, location="global"
            )
            raw = await _call_flash_async(client, prompt)
            score = _parse_score(raw)
            status = "✓" if score >= 90 else "✗"
            print(
                f'PROGRESS: Phase 5 — [{index}/{total}] "{title[:60]}" → '
                f"{score:.0f}% match {status}",
                flush=True,
            )
            return paper, score
        except Exception as exc:
            last_exc = exc

        # Regional failover
        for region in _STABLE_REGIONS:
            try:
                client = genai.Client(
                    vertexai=True, project=project_id, location=region
                )
                raw = await _call_flash_async(client, prompt)
                score = _parse_score(raw)
                status = "✓" if score >= 90 else "✗"
                print(
                    f'PROGRESS: Phase 5 — [{index}/{total}] "{title[:60]}" → '
                    f"{score:.0f}% match {status} (via {region})",
                    flush=True,
                )
                return paper, score
            except Exception as exc:
                last_exc = exc
                continue

        logger.error(
            "SemanticMatcher: all regions exhausted for '%s'. Last: %s",
            title[:60],
            last_exc,
        )
        print(
            f'PROGRESS: Phase 5 — [{index}/{total}] "{title[:60]}" → '
            f"FAILED (all regions exhausted)",
            flush=True,
        )
        return paper, 0.0


async def _batch_score_async(
    query: str,
    papers: list[dict],
) -> list[tuple[dict, float]]:
    """Internal async implementation for batch scoring."""
    if not papers:
        return []

    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_SCORES)
    total = len(papers)

    tasks = [
        _score_single_paper(query, paper, i + 1, total, semaphore)
        for i, paper in enumerate(papers)
    ]

    return list(await asyncio.gather(*tasks))


def batch_score_papers(
    query: str,
    papers: list[dict],
) -> list[tuple[dict, float]]:
    """
    Score all papers against the query concurrently.

    Args:
        query: The scoped research query (Task + Domain + Constraint).
        papers: List of dicts with at least 'title' and 'abstract' keys.

    Returns:
        List of (paper_dict, score) tuples, sorted by score descending.
    """
    if not papers:
        return []

    results = asyncio.run(_batch_score_async(query, papers))
    results.sort(key=lambda x: x[1], reverse=True)
    return results


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    retry=retry_if_exception(_is_resource_exhausted),
)
def _call_flash_rubric(client: genai.Client, prompt: str) -> str:
    """Single synchronous Gemini 2.5 Flash call; tenacity retries on 429 only."""
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RubricEvaluation,
        ),
    )
    return response.text if response and response.text else ""


def calculate_relevance_score(core_criteria: str, paper_metadata: dict) -> float:
    """
    Score a single paper's relevance to the core criteria using a strict rubric.

    Prefers ``abstract_conclusion`` (Abstract + Conclusion extracted via
    TextChunker) when available; falls back to ``abstract`` metadata.

    Args:
        core_criteria: A 2-sentence definition of what constitutes a relevant paper.
        paper_metadata: Dict with 'title' and 'abstract' (and optionally
            'abstract_conclusion' from PdfExtractor + TextChunker enrichment).

    Returns:
        Float 0-100 representing total relevance percentage.
    """
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT_ID")
    if not project_id:
        logger.warning("GOOGLE_CLOUD_PROJECT_ID not set; cannot score.")
        return 0.0

    title = paper_metadata.get("title", "Untitled")

    # Prefer enriched Abstract+Conclusion text; fall back to abstract metadata
    abstract_conclusion = (paper_metadata.get("abstract_conclusion") or "").strip()
    if abstract_conclusion:
        paper_context = abstract_conclusion[:6000]
    else:
        abstract = paper_metadata.get("abstract", "") or ""
        paper_context = abstract[:3000]

    prompt = _RUBRIC_SCORE_PROMPT_TEMPLATE.format(
        core_criteria=core_criteria,
        title=title,
        paper_context=paper_context,
    )

    last_exc: Exception | None = None
    raw_text = ""

    # Primary: global
    try:
        client = genai.Client(vertexai=True, project=project_id, location="global")
        raw_text = _call_flash_rubric(client, prompt)
    except Exception as exc:
        last_exc = exc

    # Failover to stable regions
    if not raw_text:
        for region in _STABLE_REGIONS:
            try:
                client = genai.Client(vertexai=True, project=project_id, location=region)
                raw_text = _call_flash_rubric(client, prompt)
                if raw_text:
                    break
            except Exception as exc:
                last_exc = exc
                continue

    if not raw_text:
        logger.error(
            "SemanticMatcher: failed to score '%s' across all regions. Last error: %s",
            title[:50],
            last_exc,
        )
        return 0.0

    # Parse JSON
    try:
        text = raw_text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            text = text.strip()

        parsed = RubricEvaluation.model_validate_json(text)
        
        # Enrich metadata dictionary in-place
        paper_metadata["domain_alignment"] = parsed.domain_alignment
        paper_metadata["task_alignment"] = parsed.task_alignment
        paper_metadata["method_relevance"] = parsed.method_relevance
        paper_metadata["reasoning"] = parsed.reasoning
        paper_metadata["total_percentage"] = parsed.total_percentage
        paper_metadata["match_score"] = parsed.total_percentage
        
        return parsed.total_percentage
    except Exception as parse_exc:
        logger.warning(
            "SemanticMatcher: failed to parse rubric JSON for '%s'. Raw response: %s. Error: %s",
            title[:50],
            raw_text[:200],
            parse_exc,
        )
        # Regex fallback
        try:
            match = re.search(r'"total_percentage"\s*:\s*(\d+(?:\.\d+)?)', text)
            if match:
                val = float(match.group(1))
                paper_metadata["total_percentage"] = val
                paper_metadata["match_score"] = val
                return val
        except Exception:
            pass
        return 0.0
