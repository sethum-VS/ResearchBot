"""
AcademicScraper.py — Multi-source academic discovery + Two-Tiered Phase 2.2
full-text triage.

Sources:
  1. Semantic Scholar Academic Graph API (optional x-api-key, multi-keyword)
  2. arXiv Atom API (multi-keyword, rate-paced)
  3. Tavily domain search (arxiv.org, researchgate.net, scholar.google.com)

Phase 2.2 — Two-Tiered Paper Pool Filtration:
  Tier 1 (Mega-Pool + Matching Engine):
    • Gemini 2.5 Flash generates 5 semantic search queries + core_criteria
    • Casts wide net across S2/arXiv/Tavily → 50-80 candidate papers
    • Concurrent PDF bookend extraction (Abstract + Conclusion)
    • SemanticMatcher rubric scoring → discard papers < 75%
  Tier 2 (Triage Critic):
    • Top 15 survivors fed to Evaluator Agent (Triage Critic)
    • Critic selects best 5 for full-text extraction → processed_summaries/
  (metadata-only academic scrape stays in agent_scrapes/academic_scrape.md).
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

import requests
from google import genai
from google.genai import types
from tavily import TavilyClient
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from pydantic import BaseModel, Field

from infrastructure.PdfExtractor import extract_full_text_from_url
from infrastructure.SemanticMatcher import calculate_relevance_score
from infrastructure.TextChunker import extract_academic_bookends


logger = logging.getLogger(__name__)

_PDF_TEXT_CACHE: dict[str, str] = {}


S2_BASE_URL = "https://api.semanticscholar.org/graph/v1"
S2_SEARCH_PATH = "/paper/search"
S2_FIELDS = "title,url,abstract,year,citationCount,isOpenAccess,openAccessPdf,externalIds,tldr,paperId"
S2_USER_AGENT = "ResearchBot/1.0 (ResearchGraphApp academic pipeline; mailto:admin@researchgraph.app)"
S2_TIMEOUT_SEC = 30
S2_PER_KEYWORD_LIMIT = 10

ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_PER_KEYWORD_LIMIT = 10
ARXIV_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
_ARXIV_CIRCUIT_OPEN = False

ACADEMIC_DOMAINS = ["arxiv.org", "researchgate.net", "scholar.google.com"]
TAVILY_PER_KEYWORD_LIMIT = 5
KEYWORD_SLICE = 3
_PDF_EXTRACT_WORKERS = 5
_FULLTEXT_TARGET = 5
_FULLTEXT_MAX_PDF_ATTEMPTS = _FULLTEXT_TARGET + 3
_TRIAGE_MODEL = "gemini-2.5-flash"

# ── Two-Tiered Mega-Pool Constants ─────────────────────────────────────────
_MEGA_POOL_KEYWORD_SLICE = 5               # Use all 5 LLM-generated queries
_MEGA_POOL_SCORER_WORKERS = 5              # Concurrent scoring workers
_MEGA_POOL_BOOKEND_WORKERS = 5             # Concurrent bookend extraction workers
_MEGA_POOL_THRESHOLD = 75.0                # Minimum score to survive Tier 1
_MEGA_POOL_CRITIC_INPUT_CAP = 15           # Max papers sent to Triage Critic

_S2_PAPER_URL_RE = re.compile(
    r"https?://(?:www\.)?semanticscholar\.org/paper/([0-9a-fA-F]{38,40})\b",
    re.IGNORECASE,
)

_STABLE_REGIONS: list[str] = [
    "europe-west4",
    "us-east4",
    "asia-northeast1",
    "us-central1",
]

_ARXIV_ID_RE = re.compile(
    r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)",
    re.IGNORECASE,
)


@dataclass
class AcademicPaper:
    title: str
    url: str
    snippet: str
    source: str
    paper_id: str | None = None
    arxiv_id: str | None = None
    citation_count: int = 0
    pdf_url: str | None = None
    triage_id: str = ""


@dataclass
class FullTextArtifact:
    triage_id: str
    title: str
    url: str
    pdf_url: str
    body: str
    source: str


@dataclass
class AcademicSearchResult:
    markdown: str
    fulltext_artifacts: list[FullTextArtifact] = field(default_factory=list)


def _is_resource_exhausted(exc: Exception) -> bool:
    exc_str = str(exc).lower()
    return (
        "429" in exc_str
        or "resourceexhausted" in exc_str
        or "resource_exhausted" in exc_str
        or "timeout" in exc_str
        or "connection" in exc_str
    )


# ── Semantic Expansion Pydantic Schema ─────────────────────────────────────


class SemanticExpansion(BaseModel):
    """Structured output from Gemini 2.5 Flash for semantic query expansion."""

    search_queries: list[str] = Field(
        description="An array of exactly 5 highly specific academic search queries "
        "targeting different aspects of the research topic (domain, methodology, "
        "limitations, architecture, constraints)."
    )
    core_criteria: str = Field(
        description="A concise 2-sentence definition of what makes an academic "
        "paper relevant to this specific research topic."
    )


_SEMANTIC_EXPANSION_PROMPT = """\
You are a research query expansion specialist for academic paper discovery. Given \
a research topic and the user's intent, generate:
1. Exactly 5 highly specific, diverse academic search queries. Each query should \
target a DIFFERENT aspect of the research: domain-specific terms, methodology, \
known limitations, architectural patterns, and real-world constraints. These \
queries will be sent to Semantic Scholar and arXiv, so make them precise.
2. A concise 2-sentence definition of the "core criteria" for what makes an \
academic paper relevant to this specific research topic. This will be used to \
score every candidate paper against the user's needs.

RESEARCH TOPIC: {primary_keyword}

USER INTENT: {user_intent}
"""


def _mega_pool_bookend_max_chars() -> int:
    """Maximum characters for Abstract+Conclusion bookend extraction."""
    return _env_int("MEGA_POOL_BOOKEND_MAX_CHARS", 8000)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    retry=retry_if_exception(_is_resource_exhausted),
)
def _call_flash_expansion(client: genai.Client, prompt: str) -> str:
    """Single Gemini 2.5 Flash call for semantic expansion; tenacity retries on 429."""
    response = client.models.generate_content(
        model=_TRIAGE_MODEL,
        contents=[prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=SemanticExpansion,
        ),
    )
    if not response or not response.text:
        raise RuntimeError("Gemini Flash returned empty semantic expansion response.")
    return response.text.strip()


def _generate_semantic_expansion(
    primary_keyword: str,
    user_intent: str,
) -> tuple[list[str], str]:
    """
    Use Gemini 2.5 Flash to generate 5 semantic search queries and a core_criteria
    string from the primary keyword and user intent.

    Returns:
        (search_queries, core_criteria) — on failure, returns template-based fallbacks.
    """
    fallback_queries = [
        primary_keyword,
        f"{primary_keyword} methodology limitations",
        f"{primary_keyword} architecture challenges",
        f"{primary_keyword} research findings constraints",
        f"{primary_keyword} state of the art evaluation",
    ]
    fallback_criteria = (
        f"Papers that directly address the domain, methodology, or limitations of "
        f"{primary_keyword}. The paper must provide actionable findings or demonstrate "
        f"approaches relevant to: {user_intent}."
    )

    project_id = os.getenv("GOOGLE_CLOUD_PROJECT_ID", "").strip()
    if not project_id:
        logger.warning(
            "_generate_semantic_expansion: GOOGLE_CLOUD_PROJECT_ID not set; "
            "using fallback queries."
        )
        return fallback_queries, fallback_criteria

    prompt = _SEMANTIC_EXPANSION_PROMPT.format(
        primary_keyword=primary_keyword,
        user_intent=user_intent,
    )

    last_exc: Exception | None = None
    raw_text = ""

    # Primary: global endpoint
    try:
        client = genai.Client(vertexai=True, project=project_id, location="global")
        raw_text = _call_flash_expansion(client, prompt)
    except Exception as exc:
        last_exc = exc

    # Regional failover
    if not raw_text:
        for region in _STABLE_REGIONS:
            try:
                client = genai.Client(
                    vertexai=True, project=project_id, location=region
                )
                raw_text = _call_flash_expansion(client, prompt)
                if raw_text:
                    break
            except Exception as exc:
                last_exc = exc
                continue

    if not raw_text:
        logger.error(
            "_generate_semantic_expansion: all regions exhausted: %s", last_exc
        )
        return fallback_queries, fallback_criteria

    # Parse the structured JSON response
    try:
        text = raw_text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            text = text.strip()

        parsed = SemanticExpansion.model_validate_json(text)
        queries = parsed.search_queries[:_MEGA_POOL_KEYWORD_SLICE]
        criteria = parsed.core_criteria.strip()

        if len(queries) < 3:
            logger.warning(
                "_generate_semantic_expansion: Flash returned only %d queries; "
                "padding with fallbacks.",
                len(queries),
            )
            seen = {q.lower() for q in queries}
            for fq in fallback_queries:
                if fq.lower() not in seen:
                    queries.append(fq)
                    seen.add(fq.lower())
                if len(queries) >= _MEGA_POOL_KEYWORD_SLICE:
                    break

        if not criteria:
            criteria = fallback_criteria

        return queries, criteria
    except Exception as parse_exc:
        logger.warning(
            "_generate_semantic_expansion: failed to parse Flash JSON: %s",
            parse_exc,
        )
        return fallback_queries, fallback_criteria


def _enhanced_query(keyword: str) -> str:
    keyword = keyword.strip().replace('"', '')
    words = keyword.split()
    if len(words) > 3:
        return keyword
    return f"{keyword} research paper methodology findings"


def _top_keywords(keywords: list[str], max_slice: int = KEYWORD_SLICE) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in keywords:
        kw = (raw or "").strip()
        if not kw:
            continue
        key = kw.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(kw)
        if len(ordered) >= max_slice:
            break
    return ordered


def _normalize_title(title: str) -> str:
    lowered = (title or "").lower()
    cleaned = re.sub(r"[^\w\s]", " ", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


def _paper_triage_id(paper: AcademicPaper) -> str:
    if paper.paper_id:
        return f"s2:{paper.paper_id}"
    if paper.arxiv_id:
        return f"arxiv:{paper.arxiv_id}"
    if paper.pdf_url:
        return f"url:{paper.pdf_url}"
    return f"title:{_normalize_title(paper.title)}"


def _best_s2_paper_id_match(corrupt_id: str, known_ids: set[str]) -> str | None:
    """Map a typo'd paperId to a known id from Phase 2 metadata (length-safe fuzzy match)."""
    corrupt = corrupt_id.lower()
    if corrupt in known_ids:
        return corrupt
    matches = difflib.get_close_matches(corrupt, list(known_ids), n=1, cutoff=0.92)
    return matches[0] if matches else None


def normalize_semanticscholar_crawl_url(
    url: str,
    known_paper_ids: set[str] | None = None,
) -> str | None:
    """
    Validate or repair semanticscholar.org/paper/{id} URLs before Firecrawl.
    Returns None when the id cannot be normalized (skip crawl).
    """
    url = (url or "").strip()
    match = _S2_PAPER_URL_RE.search(url)
    if not match:
        return url
    raw_id = match.group(1).lower()
    paper_id = raw_id
    if known_paper_ids:
        if raw_id in known_paper_ids:
            paper_id = raw_id
        else:
            fixed = _best_s2_paper_id_match(raw_id, known_paper_ids)
            if fixed:
                paper_id = fixed
                logger.info(
                    "Repaired Semantic Scholar crawl URL paper id %s → %s",
                    raw_id[:12] + "...",
                    fixed[:12] + "...",
                )
            else:
                logger.info(
                    "Skipping unknown Semantic Scholar paper URL: %s",
                    url[:120],
                )
                return None
    elif not re.fullmatch(r"[0-9a-f]{40}", raw_id):
        return None
    return url[: match.start(1)] + paper_id + url[match.end(1) :]


def s2_paper_ids_from_academic_markdown(markdown: str) -> set[str]:
    """Extract s2:{paperId} values from academic_scrape.md for URL repair in Phase 2.6."""
    return {pid.lower() for pid in re.findall(r"s2:([0-9a-f]{40})", markdown or "", re.IGNORECASE)}


def _extract_arxiv_id(url: str) -> str | None:
    if not url:
        return None
    match = _ARXIV_ID_RE.search(url)
    if match:
        return match.group(1).split("v")[0]
    return None


def _abs_to_pdf_url(abs_url: str) -> str:
    pdf = abs_url.replace("/abs/", "/pdf/")
    if not pdf.endswith(".pdf"):
        pdf = f"{pdf}.pdf"
    return pdf


def _ensure_pdf_url(paper: AcademicPaper) -> None:
    if paper.pdf_url:
        return
    if paper.arxiv_id:
        paper.pdf_url = f"https://arxiv.org/pdf/{paper.arxiv_id}.pdf"
        return
    if paper.url and "arxiv.org/abs/" in paper.url:
        paper.pdf_url = _abs_to_pdf_url(paper.url)


def _paper_url(paper: dict[str, Any]) -> str:
    url = paper.get("url") or ""
    if url:
        return url
    oa = paper.get("openAccessPdf") or {}
    if isinstance(oa, dict) and oa.get("url"):
        return oa["url"]
    paper_id = paper.get("paperId") or ""
    if paper_id:
        return f"https://www.semanticscholar.org/paper/{paper_id}"
    return ""


def _open_access_pdf_url(paper: dict[str, Any]) -> str | None:
    oa = paper.get("openAccessPdf") or {}
    if isinstance(oa, dict):
        url = (oa.get("url") or "").strip()
        if url:
            return url
    return None


def _paper_snippet(paper: dict[str, Any]) -> str:
    meta_parts: list[str] = []
    year = paper.get("year")
    if year is not None:
        meta_parts.append(str(year))
    cites = paper.get("citationCount")
    if cites is not None:
        meta_parts.append(f"{cites} citations")

    body = (paper.get("abstract") or "").strip()
    if not body:
        tldr = paper.get("tldr")
        if isinstance(tldr, dict):
            body = (tldr.get("text") or "").strip()
    if not body:
        body = "No abstract available."

    if meta_parts:
        return f"*({', '.join(meta_parts)})*\n\n{body}"
    return body


def _assign_triage_ids(papers: list[AcademicPaper]) -> None:
    for paper in papers:
        _ensure_pdf_url(paper)
        paper.triage_id = _paper_triage_id(paper)


def _has_s2_api_key() -> bool:
    return bool(os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip())


def _s2_headers() -> dict[str, str]:
    headers = {"User-Agent": S2_USER_AGENT}
    api_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip()
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _parse_retry_after(response: requests.Response) -> float:
    header = (response.headers.get("Retry-After") or "").strip()
    if not header:
        return 0.0
    try:
        return max(0.0, float(header))
    except ValueError:
        pass
    try:
        retry_dt = parsedate_to_datetime(header)
        if retry_dt.tzinfo is None:
            retry_dt = retry_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return max(0.0, (retry_dt - now).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _s2_default_429_wait_sec() -> float:
    return 3.0 if _has_s2_api_key() else 10.0


def _s2_post_429_cooldown_sec() -> float:
    explicit = os.getenv("S2_POST_429_COOLDOWN_SEC", "").strip()
    if explicit:
        return _env_float("S2_POST_429_COOLDOWN_SEC", 15.0)
    return 5.0 if _has_s2_api_key() else 15.0


def _s2_429_max_retries() -> int:
    return _env_int("S2_429_MAX_RETRIES", 2)


def _s2_circuit_breaker_enabled() -> bool:
    return _env_bool("S2_CIRCUIT_BREAKER", True)


def _s2_keyword_delay_sec() -> float:
    return _env_float("S2_KEYWORD_DELAY_SEC", 3.0)


_S2_LOCK = threading.Lock()
_LAST_S2_REQUEST_TIME = 0.0


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=3, max=15),
    retry=retry_if_exception(_is_resource_exhausted),
    reraise=True,
)
def _s2_search_request(query: str, limit: int) -> dict[str, Any]:
    """Individual S2 search with global lock enforcing strict 1 req/sec rate limit and tenacity retries."""
    global _LAST_S2_REQUEST_TIME

    url = f"{S2_BASE_URL}{S2_SEARCH_PATH}"
    params = {"limit": limit, "fields": S2_FIELDS, "query": query}

    with _S2_LOCK:
        now = time.time()
        elapsed = now - _LAST_S2_REQUEST_TIME
        if elapsed < 1.1:
            time.sleep(1.1 - elapsed)
        
        response = requests.get(
            url,
            params=params,
            headers=_s2_headers(),
            timeout=S2_TIMEOUT_SEC,
        )
        _LAST_S2_REQUEST_TIME = time.time()

    response.raise_for_status()
    return response.json()


def _dedupe_s2_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    by_title: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for paper in records:
        if not isinstance(paper, dict):
            continue
        pid = (paper.get("paperId") or "").strip()
        title_key = _normalize_title(paper.get("title") or "")
        key = pid or f"title:{title_key}" if title_key else ""
        if not key:
            continue
        if key in by_id or (title_key and title_key in by_title):
            continue
        by_id[key] = paper
        if title_key:
            by_title[title_key] = paper
        order.append(key)

    return [by_id[k] for k in order]


def _fetch_semantic_scholar_keywords(
    keywords: list[str],
    max_keyword_slice: int = KEYWORD_SLICE,
) -> list[AcademicPaper]:
    merged: list[dict[str, Any]] = []
    has_api_key = _has_s2_api_key()
    top = _top_keywords(keywords, max_slice=max_keyword_slice)

    if not top:
        return []

    # Individual per-keyword queries with strict 1 req/sec rate limit.
    # S2's /paper/search does NOT support boolean operators (|, OR, AND)
    # in the query parameter — they are treated as literal text.
    consecutive_429s = 0
    circuit_open = False
    total = len(top)

    for idx, kw in enumerate(top, 1):
        if circuit_open:
            print(
                f"PROGRESS: Phase 2 — Semantic Scholar [{idx}/{total}] skipped (circuit open).",
                flush=True,
            )
            continue

        query = _enhanced_query(kw)
        try:
            payload = _s2_search_request(query, S2_PER_KEYWORD_LIMIT)
            batch = payload.get("data") or []
            count = len(batch)
            merged.extend(batch)
            consecutive_429s = 0  # Reset on success
            print(
                f"PROGRESS: Phase 2 — Semantic Scholar [{idx}/{total}] ok ({count} papers).",
                flush=True,
            )
        except Exception as exc:
            if _is_resource_exhausted(exc):
                consecutive_429s += 1
                hint = (
                    " Set SEMANTIC_SCHOLAR_API_KEY in .env for dedicated 1 RPS."
                    if not has_api_key
                    else ""
                )
                if consecutive_429s >= _s2_429_max_retries() and _s2_circuit_breaker_enabled():
                    circuit_open = True
                    remaining = total - idx
                    print(
                        f"PROGRESS: Phase 2 — Semantic Scholar circuit open — "
                        f"skipping {remaining} remaining query(ies). "
                        f"arXiv and Tavily will still supply academic metadata.{hint}",
                        flush=True,
                    )
                    logger.info("Semantic Scholar circuit open after %d consecutive 429s: %s", consecutive_429s, exc)
                else:
                    print(
                        f"PROGRESS: Phase 2 — Semantic Scholar [{idx}/{total}] rate-limited (429).{hint}",
                        flush=True,
                    )
                    logger.info("Semantic Scholar 429 on query %d/%d: %s", idx, total, exc)
            else:
                logger.warning(
                    "Semantic Scholar search failed for query %r: %s",
                    query,
                    exc,
                )

    papers: list[AcademicPaper] = []
    for paper in _dedupe_s2_records(merged):
        title = paper.get("title") or "Untitled Paper"
        url = _paper_url(paper)
        pdf_url = _open_access_pdf_url(paper)
        
        # Check for ArXiv ID in externalIds first, then fallback to URL extraction
        ext_ids = paper.get("externalIds") or {}
        arxiv_id = ext_ids.get("ArXiv") if isinstance(ext_ids, dict) else None
        if not arxiv_id:
            arxiv_id = _extract_arxiv_id(url) or _extract_arxiv_id(pdf_url or "")
            
        papers.append(
            AcademicPaper(
                title=title,
                url=url,
                snippet=_paper_snippet(paper),
                source="semantic_scholar",
                paper_id=(paper.get("paperId") or "").strip() or None,
                arxiv_id=arxiv_id,
                citation_count=int(paper.get("citationCount") or 0),
                pdf_url=pdf_url,
            )
        )
    return papers


def _arxiv_delay_sec() -> float:
    raw = os.getenv("ARXIV_REQUEST_DELAY_SEC", "5")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 5.0


def _parse_arxiv_atom(xml_text: str) -> list[AcademicPaper]:
    root = ET.fromstring(xml_text)
    papers: list[AcademicPaper] = []
    for entry in root.findall("atom:entry", ARXIV_ATOM_NS):
        title_el = entry.find("atom:title", ARXIV_ATOM_NS)
        summary_el = entry.find("atom:summary", ARXIV_ATOM_NS)
        published_el = entry.find("atom:published", ARXIV_ATOM_NS)
        id_el = entry.find("atom:id", ARXIV_ATOM_NS)

        title = (title_el.text if title_el is not None else "Untitled Paper") or "Untitled Paper"
        title = re.sub(r"\s+", " ", title).strip()
        summary = (summary_el.text if summary_el is not None else "") or ""
        summary = re.sub(r"\s+", " ", summary).strip()
        published = (published_el.text if published_el is not None else "")[:10]
        abs_url = (id_el.text if id_el is not None else "").strip()

        arxiv_id = _extract_arxiv_id(abs_url)
        pdf_url = _abs_to_pdf_url(abs_url) if abs_url else None
        meta = f"*({published})*" if published else ""
        snippet = f"{meta}\n\n{summary}" if meta else summary

        papers.append(
            AcademicPaper(
                title=title,
                url=abs_url,
                snippet=snippet or "No abstract available.",
                source="arxiv",
                arxiv_id=arxiv_id,
                pdf_url=pdf_url,
            )
        )
    return papers


_ARXIV_LOCK = threading.Lock()
_LAST_ARXIV_REQUEST_TIME = 0.0


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=5, max=30),
    retry=retry_if_exception(_is_resource_exhausted),
    reraise=True,
)
def _arxiv_search_request(query: str, max_results: int) -> str:
    global _LAST_ARXIV_REQUEST_TIME

    with _ARXIV_LOCK:
        now = time.time()
        elapsed = now - _LAST_ARXIV_REQUEST_TIME
        delay = _arxiv_delay_sec()
        if elapsed < delay:
            time.sleep(delay - elapsed)

        response = requests.get(
            ARXIV_API_URL,
            params={
                "search_query": query,
                "start": 0,
                "max_results": max_results,
            },
            timeout=30,
            headers={"User-Agent": S2_USER_AGENT},
        )
        _LAST_ARXIV_REQUEST_TIME = time.time()

    response.raise_for_status()
    if "rate exceeded" in response.text.lower():
        raise RuntimeError("arXiv API 429 ResourceExhausted: rate limit exceeded")
    return response.text


def _dedupe_arxiv_papers(papers: list[AcademicPaper]) -> list[AcademicPaper]:
    seen_ids: set[str] = set()
    seen_titles: set[str] = set()
    out: list[AcademicPaper] = []
    for paper in papers:
        aid = (paper.arxiv_id or "").strip()
        title_key = _normalize_title(paper.title)
        if aid and aid in seen_ids:
            continue
        if title_key and title_key in seen_titles:
            continue
        if aid:
            seen_ids.add(aid)
        if title_key:
            seen_titles.add(title_key)
        out.append(paper)
    return out


def _format_arxiv_query_term(kw: str) -> str:
    kw = kw.strip()
    # Strip any existing quotes
    kw = kw.replace('"', '')
    words = [
        w for w in kw.split()
        if w.lower() not in {
            "of", "in", "and", "or", "the", "for", "with", "on", "at", "by",
            "an", "a", "to", "is", "within", "about", "into", "through",
            "during", "under"
        }
    ]
    if not words:
        return ""
    if len(words) <= 2:
        return f'all:"{" ".join(words)}"'
    else:
        # AND search for high-value terms in the query
        return " AND ".join([f'all:{w}' for w in words])


def search_arxiv_papers(keywords: list[str], max_results: int = ARXIV_PER_KEYWORD_LIMIT) -> str:
    """Query arXiv for the top keywords and return a Markdown subsection (## arXiv)."""
    global _ARXIV_CIRCUIT_OPEN
    if _ARXIV_CIRCUIT_OPEN:
        return ""

    top = _top_keywords(keywords)
    if not top:
        return ""

    # Individual per-keyword queries with strict 1 req/3sec rate limit.
    all_papers: list[AcademicPaper] = []
    total = len(top)

    for idx, kw in enumerate(top, 1):
        if _ARXIV_CIRCUIT_OPEN:
            print(
                f"PROGRESS: Phase 2 — arXiv [{idx}/{total}] skipped (circuit open).",
                flush=True,
            )
            continue

        query_term = _format_arxiv_query_term(kw)
        if not query_term:
            continue

        try:
            xml_text = _arxiv_search_request(query_term, max_results)
            batch = _parse_arxiv_atom(xml_text)
            all_papers.extend(batch)
            print(
                f"PROGRESS: Phase 2 — arXiv [{idx}/{total}] ok ({len(batch)} papers).",
                flush=True,
            )
        except Exception as exc:
            if _is_resource_exhausted(exc):
                _ARXIV_CIRCUIT_OPEN = True
                remaining = total - idx
                print(
                    f"PROGRESS: Phase 2 — arXiv circuit open — skipping "
                    f"{remaining} remaining query(ies) due to rate limit exhaustion.",
                    flush=True,
                )
                logger.warning("arXiv circuit open after 429 exhaustion: %s", exc)
            else:
                logger.warning("arXiv search failed for query %r: %s", query_term, exc)

    deduped = _dedupe_arxiv_papers(all_papers)
    if not deduped:
        return ""
    return _format_subsection("arXiv", deduped)


def _fetch_arxiv_papers_list(
    keywords: list[str],
    max_keyword_slice: int = KEYWORD_SLICE,
) -> list[AcademicPaper]:
    global _ARXIV_CIRCUIT_OPEN
    if _ARXIV_CIRCUIT_OPEN:
        return []

    top = _top_keywords(keywords, max_slice=max_keyword_slice)
    if not top:
        return []

    # Individual per-keyword queries with strict 1 req/3sec rate limit.
    all_papers: list[AcademicPaper] = []
    total = len(top)

    for idx, kw in enumerate(top, 1):
        if _ARXIV_CIRCUIT_OPEN:
            print(
                f"PROGRESS: Phase 2 — arXiv [{idx}/{total}] skipped (circuit open).",
                flush=True,
            )
            continue

        query_term = _format_arxiv_query_term(kw)
        if not query_term:
            continue

        try:
            xml_text = _arxiv_search_request(query_term, ARXIV_PER_KEYWORD_LIMIT)
            batch = _parse_arxiv_atom(xml_text)
            all_papers.extend(batch)
            print(
                f"PROGRESS: Phase 2 — arXiv [{idx}/{total}] ok ({len(batch)} papers).",
                flush=True,
            )
        except Exception as exc:
            if _is_resource_exhausted(exc):
                _ARXIV_CIRCUIT_OPEN = True
                remaining = total - idx
                print(
                    f"PROGRESS: Phase 2 — arXiv circuit open — skipping "
                    f"{remaining} remaining query(ies) due to rate limit exhaustion.",
                    flush=True,
                )
                logger.warning("arXiv circuit open after 429 exhaustion: %s", exc)
            else:
                logger.warning("arXiv search failed for query %r: %s", query_term, exc)

    return _dedupe_arxiv_papers(all_papers)


def _fetch_tavily_keywords(
    keywords: list[str],
    max_keyword_slice: int = KEYWORD_SLICE,
) -> tuple[list[AcademicPaper], str | None]:
    api_key = os.getenv("TAVILY_API_KEY", "")
    if not api_key:
        return [], "TAVILY_API_KEY not set. Cannot search academic papers via Tavily."

    client = TavilyClient(api_key=api_key)
    rows: list[AcademicPaper] = []
    seen_urls: set[str] = set()

    for keyword in _top_keywords(keywords, max_slice=max_keyword_slice):
        try:
            response = client.search(
                query=_enhanced_query(keyword),
                include_domains=ACADEMIC_DOMAINS,
                max_results=TAVILY_PER_KEYWORD_LIMIT,
            )
            for r in response.get("results", []):
                url = (r.get("url") or "").strip()
                norm_url = url.lower().rstrip("/")
                if not url or norm_url in seen_urls:
                    continue
                seen_urls.add(norm_url)
                title = r.get("title", "Untitled Paper")
                snippet = r.get("content", "No abstract available.")
                rows.append(
                    AcademicPaper(
                        title=title,
                        url=url,
                        snippet=snippet,
                        source="tavily",
                        arxiv_id=_extract_arxiv_id(url),
                    )
                )
        except Exception as exc:
            logger.warning("Tavily search failed for keyword %r: %s", keyword, exc)

    return rows, None


def _cross_source_dedup(
    s2: list[AcademicPaper],
    arxiv: list[AcademicPaper],
    tavily: list[AcademicPaper],
) -> tuple[list[AcademicPaper], list[AcademicPaper], list[AcademicPaper]]:
    known_arxiv: set[str] = set()
    known_titles: set[str] = set()

    for paper in s2:
        if paper.arxiv_id:
            known_arxiv.add(paper.arxiv_id)
        title_key = _normalize_title(paper.title)
        if title_key:
            known_titles.add(title_key)

    arxiv_out: list[AcademicPaper] = []
    for paper in arxiv:
        aid = paper.arxiv_id or _extract_arxiv_id(paper.url)
        title_key = _normalize_title(paper.title)
        if aid and aid in known_arxiv:
            continue
        if title_key and title_key in known_titles:
            continue
        if aid:
            known_arxiv.add(aid)
        if title_key:
            known_titles.add(title_key)
        arxiv_out.append(paper)

    tavily_out: list[AcademicPaper] = []
    for paper in tavily:
        aid = paper.arxiv_id or _extract_arxiv_id(paper.url)
        title_key = _normalize_title(paper.title)
        if aid and aid in known_arxiv:
            continue
        if title_key and title_key in known_titles:
            continue
        if "arxiv.org" in paper.url.lower() and aid is None:
            extracted = _extract_arxiv_id(paper.url)
            if extracted and extracted in known_arxiv:
                continue
        tavily_out.append(paper)

    return s2, arxiv_out, tavily_out


def _triage_pool_max() -> int:
    raw = os.getenv("ACADEMIC_TRIAGE_POOL_MAX", "25")
    try:
        return max(5, int(raw))
    except ValueError:
        return 25


def _papers_to_triage_dicts(
    papers: list[AcademicPaper],
    bookends: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """
    Serialize AcademicPaper list to triage dicts for the Critic prompt.

    Args:
        papers:   Papers to serialize.
        bookends: Optional map of triage_id -> abstract+conclusion bookend text.
                  When provided, each paper's dict will include an
                  ``abstract_conclusion`` key so the Critic has richer evidence.
    """
    bookends = bookends or {}
    rows: list[dict[str, Any]] = []
    for paper in papers:
        abstract = paper.snippet
        if "\n\n" in abstract:
            abstract = abstract.split("\n\n", 1)[-1].strip()
        row: dict[str, Any] = {
            "triage_id": paper.triage_id,
            "title": paper.title,
            "abstract": abstract,
            "pdf_url": paper.pdf_url or "",
            "citation_count": paper.citation_count,
            "source": paper.source,
        }
        bookend_text = bookends.get(paper.triage_id, "")
        if bookend_text:
            row["abstract_conclusion"] = bookend_text
        rows.append(row)
    return rows


def _build_triage_pool(all_papers: list[AcademicPaper]) -> list[AcademicPaper]:
    _assign_triage_ids(all_papers)
    oa = [p for p in all_papers if (p.pdf_url or "").strip()]
    ranked = sorted(
        oa,
        key=lambda p: (p.citation_count, p.source == "semantic_scholar"),
        reverse=True,
    )
    return ranked[: _triage_pool_max()]


def _build_triage_prompt(papers: list[dict[str, Any]], keywords: list[str], user_intent: str) -> str:
    kw_text = ", ".join(_top_keywords(keywords)) or "research"
    catalog = json.dumps(papers, ensure_ascii=False, indent=2)
    return (
        "You are an Academic Triage Critic for university Final Year Project research.\n\n"
        f"Evaluate these paper records against the research keywords: {kw_text}\n"
        f"And the specific user intent: {user_intent}\n\n"
        "Rules:\n"
        "- Only consider entries that include a non-empty pdf_url (open-access PDF).\n"
        "- Evaluate each paper on three dimensions on a 0-10 scale:\n"
        "  1. domain_alignment (0-10): Does the paper operate in the exact scientific domain the user requested?\n"
        "  2. task_alignment (0-10): Is the paper trying to solve the specific problem the user is researching?\n"
        "  3. method_relevance (0-10): Is the proposed methodology highly relevant to the user's inquiry?\n"
        "- Return ONLY a structured JSON array containing objects with the following fields:\n"
        "  - triage_id: string\n"
        "  - domain_alignment: integer (0-10)\n"
        "  - task_alignment: integer (0-10)\n"
        "  - method_relevance: integer (0-10)\n"
        "  - total_score: integer (sum of the three scores, out of 30)\n"
        "  - critic_reasoning: string (1-sentence justification)\n\n"
        f"Papers:\n{catalog}"
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    retry=retry_if_exception(_is_resource_exhausted),
)
def _call_flash_triage(client: genai.Client, prompt: str) -> str:
    response = client.models.generate_content(
        model=_TRIAGE_MODEL,
        contents=[prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )
    if not response or not response.text:
        raise RuntimeError("Gemini Flash returned an empty triage response.")
    return response.text.strip()


def _parse_triage_results(raw: str, valid_ids: set[str]) -> list[dict]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*\]", text)
        if not match:
            return []
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []

    if isinstance(parsed, dict):
        for key in ("triage_ids", "ids", "papers", "selected", "results"):
            if key in parsed and isinstance(parsed[key], list):
                parsed = parsed[key]
                break

    if not isinstance(parsed, list):
        return []

    out = []
    for item in parsed:
        if isinstance(item, dict):
            tid = str(item.get("triage_id") or item.get("id") or item.get("paper_id") or "").strip()
            if tid in valid_ids:
                out.append(item)
    return out


def triage_top_papers(papers: list[dict], keywords: list[str], user_intent: str) -> list[str]:
    """
    Use Gemini 2.5 Flash to pick up to 5 triage_id values from the OA pool
    by acting as an Evaluator Agent.
    
    Falls back to top citations if Flash is unavailable.
    """
    if not papers:
        return []

    valid_ids = {p["triage_id"] for p in papers if p.get("triage_id")}
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT_ID", "").strip()
    if not project_id:
        logger.warning("triage_top_papers: GOOGLE_CLOUD_PROJECT_ID not set; using citation fallback.")
        return [p["triage_id"] for p in papers[:_FULLTEXT_TARGET] if p.get("triage_id")]

    prompt = _build_triage_prompt(papers, keywords, user_intent)
    last_exc: Exception | None = None

    def _safe_score(val) -> int:
        """Safely parse total_score from LLM output, handling floats and non-numeric strings."""
        try:
            return int(float(val or 0))
        except (ValueError, TypeError):
            return 0

    def _process_triage_response(raw: str) -> list[str]:
        results = _parse_triage_results(raw, valid_ids)
        if not results:
            return []
        
        # Sort by total_score descending
        results.sort(key=lambda x: _safe_score(x.get("total_score")), reverse=True)
        
        # Filter out < 15
        filtered_results = [r for r in results if _safe_score(r.get("total_score")) >= 15]
        
        if filtered_results:
            top_score = _safe_score(filtered_results[0].get("total_score"))
            print(f"PROGRESS: Phase 2.2 — Critic evaluated {len(papers)} papers. Top paper scored {top_score}/30. Downloading top {min(len(filtered_results), _FULLTEXT_TARGET)}...", flush=True)
        else:
            print(f"PROGRESS: Phase 2.2 — Critic evaluated {len(papers)} papers but none scored >= 15/30.", flush=True)
            
        return [r["triage_id"] for r in filtered_results[:_FULLTEXT_TARGET]]


    try:
        client = genai.Client(vertexai=True, project=project_id, location="global")
        raw = _call_flash_triage(client, prompt)
        ids = _process_triage_response(raw)
        if ids:
            return ids
    except Exception as exc:
        logger.warning("triage_top_papers: global Flash failed: %s", exc)
        last_exc = exc

    for region in _STABLE_REGIONS:
        try:
            client = genai.Client(vertexai=True, project=project_id, location=region)
            raw = _call_flash_triage(client, prompt)
            ids = _process_triage_response(raw)
            if ids:
                return ids
        except Exception as exc:
            logger.warning("triage_top_papers: region %s failed: %s", region, exc)
            last_exc = exc

    logger.warning("triage_top_papers: Flash exhausted (%s); citation fallback.", last_exc)
    fallback = [p["triage_id"] for p in papers[:_FULLTEXT_TARGET] if p.get("triage_id")]
    print(
        f"PROGRESS: Phase 2.2 — Flash unavailable; citation fallback selected "
        f"{len(fallback)} papers.",
        flush=True,
    )
    return fallback


def format_fulltext_artifact_markdown(artifact: FullTextArtifact) -> str:
    """Public Markdown serializer for processed_summaries/ injection."""
    return _format_fulltext_markdown(artifact)


def _format_fulltext_markdown(artifact: FullTextArtifact) -> str:
    return (
        f"# Academic Full Text: {artifact.title}\n\n"
        f"**Triage ID:** {artifact.triage_id}\n"
        f"**Source:** {artifact.url}\n"
        f"**PDF:** {artifact.pdf_url}\n"
        f"**Origin:** {artifact.source}\n\n"
        "## Full Document Text\n\n"
        f"```text\n{artifact.body}\n```\n"
    )


def _fetch_fulltext_for_paper(paper: AcademicPaper) -> FullTextArtifact | None:
    pdf_url = (paper.pdf_url or "").strip()
    if not pdf_url:
        return None
    try:
        if pdf_url in _PDF_TEXT_CACHE:
            body = _PDF_TEXT_CACHE[pdf_url]
            print(f"PROGRESS: Phase 2.2 — Cache hit for PDF {pdf_url[:50]}...", flush=True)
        else:
            body = extract_full_text_from_url(pdf_url)
            if body:
                _PDF_TEXT_CACHE[pdf_url] = body
    except Exception as exc:
        logger.warning(
            "Full-text worker failed for %s: %s",
            paper.triage_id,
            exc,
        )
        return None
    if not body:
        return None
    return FullTextArtifact(
        triage_id=paper.triage_id,
        title=paper.title,
        url=paper.url,
        pdf_url=pdf_url,
        body=body,
        source=paper.source,
    )


# ── Two-Tiered Mega-Pool: Bookend Extraction ──────────────────────────────


def _extract_bookend_for_paper(
    paper: AcademicPaper,
    max_chars: int,
) -> str:
    """
    Download a PDF and extract Abstract+Conclusion bookends.

    Thread-safe: uses only local variables and function-scoped handles.
    Returns the bookend text, or empty string on failure.
    """
    pdf_url = (paper.pdf_url or "").strip()
    if not pdf_url:
        return ""
    try:
        if pdf_url in _PDF_TEXT_CACHE:
            full_text = _PDF_TEXT_CACHE[pdf_url]
            print(f"PROGRESS: Phase 2.2 — Cache hit for PDF {pdf_url[:50]}...", flush=True)
        else:
            full_text = extract_full_text_from_url(pdf_url)
            if full_text:
                _PDF_TEXT_CACHE[pdf_url] = full_text
        if not full_text or len(full_text.strip()) < 200:
            return ""
        return extract_academic_bookends(full_text, max_chars=max_chars)
    except Exception as exc:
        logger.warning(
            "Bookend extraction failed for %s: %s",
            paper.triage_id,
            exc,
        )
        return ""


def _concurrent_bookend_extraction(
    papers: list[AcademicPaper],
) -> dict[str, str]:
    """
    Concurrently extract Abstract+Conclusion bookends for all papers with PDF URLs.

    Returns:
        Dict mapping triage_id -> bookend text (only for successful extractions).
    """
    max_chars = _mega_pool_bookend_max_chars()
    papers_with_pdf = [p for p in papers if (p.pdf_url or "").strip()]
    if not papers_with_pdf:
        return {}

    total = len(papers_with_pdf)
    bookends: dict[str, str] = {}
    completed = 0

    print(
        f"PROGRESS: Phase 2.2 — extracting bookends for {total} papers with PDF URLs "
        f"({_MEGA_POOL_BOOKEND_WORKERS} workers, max {max_chars:,} chars)...",
        flush=True,
    )

    with ThreadPoolExecutor(
        max_workers=_MEGA_POOL_BOOKEND_WORKERS,
        thread_name_prefix="bookend",
    ) as executor:
        future_to_paper = {
            executor.submit(_extract_bookend_for_paper, paper, max_chars): paper
            for paper in papers_with_pdf
        }
        for future in as_completed(future_to_paper):
            paper = future_to_paper[future]
            completed += 1
            try:
                text = future.result()
            except Exception as exc:
                logger.warning(
                    "Bookend future failed for %s: %s", paper.triage_id, exc
                )
                text = ""

            if text and text.strip():
                bookends[paper.triage_id] = text
                status = "✓"
            else:
                status = "✗"

            print(
                f'PROGRESS: Phase 2.2 — bookend [{completed}/{total}] {status}: '
                f'"{paper.title[:55]}"',
                flush=True,
            )

    print(
        f"PROGRESS: Phase 2.2 — bookends extracted for {len(bookends)}/{total} papers.",
        flush=True,
    )
    return bookends


# ── Two-Tiered Mega-Pool: Scoring Pipeline ────────────────────────────────


def _concurrent_mega_pool_scoring(
    papers: list[AcademicPaper],
    core_criteria: str,
    bookends: dict[str, str],
) -> list[tuple[AcademicPaper, float]]:
    """
    Score all papers against core_criteria using SemanticMatcher concurrently.

    Papers with bookend text get enriched scoring via abstract_conclusion.
    Papers without bookends are scored on abstract-only.

    Returns:
        List of (paper, score) tuples sorted by score descending.
    """
    if not papers:
        return []

    total = len(papers)
    results: list[tuple[AcademicPaper, float]] = []
    completed = 0

    print(
        f"PROGRESS: Phase 2.2 — scoring {total} papers via Semantic Matcher "
        f"({_MEGA_POOL_SCORER_WORKERS} workers)...",
        flush=True,
    )

    def _score_single(idx: int, paper: AcademicPaper) -> tuple[AcademicPaper, float]:
        """Score one paper; thread-safe via independent dict construction."""
        # Build a metadata dict for SemanticMatcher
        abstract = paper.snippet
        if "\n\n" in abstract:
            abstract = abstract.split("\n\n", 1)[-1].strip()

        paper_dict: dict[str, Any] = {
            "title": paper.title,
            "abstract": abstract,
        }

        # Enrich with bookend text if available
        bookend_text = bookends.get(paper.triage_id, "")
        if bookend_text:
            paper_dict["abstract_conclusion"] = bookend_text

        try:
            score = calculate_relevance_score(core_criteria, paper_dict)
        except Exception as exc:
            logger.warning(
                "Scoring failed for %s: %s", paper.triage_id, exc
            )
            score = 0.0

        status = "✓" if score > _MEGA_POOL_THRESHOLD else "✗"
        enriched = "⊕" if bookend_text else "○"
        print(
            f'PROGRESS: Phase 2.2 — [{idx}/{total}] "{paper.title[:55]}" → '
            f"{score:.0f}% {status} {enriched}",
            flush=True,
        )
        return paper, score

    with ThreadPoolExecutor(
        max_workers=_MEGA_POOL_SCORER_WORKERS,
        thread_name_prefix="scorer",
    ) as executor:
        futures = [
            executor.submit(_score_single, i + 1, paper)
            for i, paper in enumerate(papers)
        ]
        for future in as_completed(futures):
            completed += 1
            try:
                results.append(future.result())
            except Exception as exc:
                logger.error("Scoring worker error: %s", exc)

    results.sort(key=lambda x: x[1], reverse=True)
    return results


# ── Two-Tiered Phase 2.2 Pipeline ─────────────────────────────────────────


def _run_fulltext_download(
    attempt_order: list[AcademicPaper],
) -> list[FullTextArtifact]:
    """
    Download full-text PDFs for the selected papers (Step 5 of the pipeline).

    This is the final step after the Triage Critic has selected the best papers.
    Reuses the existing concurrent download logic.
    """
    if not attempt_order:
        return []

    # Deduplicate download work by PDF URL.
    url_to_paper: dict[str, AcademicPaper] = {}
    for paper in attempt_order:
        pdf_url = (paper.pdf_url or "").strip()
        if pdf_url and pdf_url not in url_to_paper:
            url_to_paper[pdf_url] = paper

    work = list(url_to_paper.items())[:_FULLTEXT_MAX_PDF_ATTEMPTS]
    target = _FULLTEXT_TARGET
    print(
        f"PROGRESS: Phase 2.2 — fetching full text for up to {target} papers "
        f"({len(work)} PDF attempt(s) max, {_PDF_EXTRACT_WORKERS} workers)...",
        flush=True,
    )

    artifacts: list[FullTextArtifact] = []
    completed = 0

    with ThreadPoolExecutor(
        max_workers=_PDF_EXTRACT_WORKERS,
        thread_name_prefix="fulltext_pdf",
    ) as executor:
        future_to_url = {
            executor.submit(_fetch_fulltext_for_paper, paper): url
            for url, paper in work
        }
        for future in as_completed(future_to_url):
            if len(artifacts) >= target:
                break
            url = future_to_url[future]
            completed += 1
            try:
                artifact = future.result()
            except Exception as exc:
                logger.warning("Full-text future failed for %s: %s", url, exc)
                artifact = None

            status = "ok" if artifact else "empty"
            short = url if len(url) <= 72 else f"{url[:69]}..."
            print(
                f"PROGRESS: Phase 2.2 — full-text [{completed}/{len(work)}] {status}: {short}",
                flush=True,
            )
            if artifact and len(artifacts) < target:
                artifacts.append(artifact)

    print(
        f"PROGRESS: Phase 2.2 — full-text complete ({len(artifacts)}/{target} saved).",
        flush=True,
    )
    return artifacts


def _run_phase_22_fulltext(
    all_papers: list[AcademicPaper],
    keywords: list[str],
    user_intent: str,
    core_criteria: str,
) -> list[FullTextArtifact]:
    """
    Two-Tiered Phase 2.2 Pipeline:
      Step 1: Build mega-pool (assign triage IDs, ensure PDF URLs)
      Step 2: Concurrent bookend extraction (Abstract + Conclusion)
      Step 3: Tier 1 — Semantic Matcher scoring + 75% threshold
      Step 4: Tier 2 — Triage Critic selects best 5
      Step 5: Full-text PDF download for selected papers
    """
    # ── Step 1: Build mega-pool ──────────────────────────────────────────
    _assign_triage_ids(all_papers)
    if not all_papers:
        print(
            "PROGRESS: Phase 2.2 — no papers in mega-pool; skipping full text.",
            flush=True,
        )
        return []

    mega_pool_size = len(all_papers)
    oa_count = sum(1 for p in all_papers if (p.pdf_url or "").strip())
    print(
        f"PROGRESS: Phase 2.2 — Mega-Pool built ({mega_pool_size} papers, "
        f"{oa_count} with PDF URLs).",
        flush=True,
    )

    if oa_count == 0 and mega_pool_size == 0:
        print(
            "PROGRESS: Phase 2.2 — no open-access PDFs in corpus; skipping.",
            flush=True,
        )
        return []

    # ── Step 2: Tier 1 — Semantic Matcher scoring (Abstract Only) ────────
    scored = _concurrent_mega_pool_scoring(all_papers, core_criteria, {})

    # Apply 75% threshold
    survivors = [
        (paper, score) for paper, score in scored if score > _MEGA_POOL_THRESHOLD
    ]

    if not survivors:
        print(
            f"PROGRESS: Phase 2.2 — no papers scored > {_MEGA_POOL_THRESHOLD}%. "
            "Falling back to top-scored papers for Tier 1.5.",
            flush=True,
        )
        # Fallback: use top papers even if below threshold
        survivors = scored[:_MEGA_POOL_CRITIC_INPUT_CAP]

    top_score = survivors[0][1] if survivors else 0.0
    critic_input = survivors[:_MEGA_POOL_CRITIC_INPUT_CAP]
    critic_papers = [paper for paper, _ in critic_input]
    
    print(
        f"PROGRESS: Phase 2.2 — Matcher retained {len(survivors)} papers "
        f"> {_MEGA_POOL_THRESHOLD}% (top score: {top_score:.0f}%). "
        f"Passing top {len(critic_papers)} to Tier 1.5...",
        flush=True,
    )

    # ── Step 3: Tier 1.5 — Concurrent bookend extraction ─────────────────
    # Extract Abstract+Conclusion for each surviving paper. The returned dict
    # maps triage_id -> bookend text and is forwarded to the Triage Critic so
    # the LLM can make richer, evidence-backed selection decisions.
    bookends: dict[str, str] = _concurrent_bookend_extraction(critic_papers)
    logger.info(
        "Phase 2.2 — bookend extraction complete: %d/%d papers enriched.",
        len(bookends),
        len(critic_papers),
    )

    # ── Step 4: Tier 2 — Triage Critic ───────────────────────────────────

    # Build triage pool from surviving papers (must have a PDF URL).
    triage_pool: list[AcademicPaper] = []
    for paper in critic_papers:
        _ensure_pdf_url(paper)
        if (paper.pdf_url or "").strip():
            triage_pool.append(paper)

    if not triage_pool:
        # No OA papers survived — skip triage, just download best available
        print(
            "PROGRESS: Phase 2.2 — no OA PDFs in surviving papers; "
            "downloading top-scored papers directly.",
            flush=True,
        )
        return _run_fulltext_download(critic_papers)

    # Pass bookend text into triage dicts so the Critic sees
    # abstract+conclusion evidence per paper (not just the snippet).
    triage_dicts = _papers_to_triage_dicts(triage_pool, bookends=bookends)
    selected_ids = triage_top_papers(triage_dicts, keywords, user_intent)

    # Build download order: critic-selected first, then remaining survivors
    by_id = {p.triage_id: p for p in triage_pool}
    attempt_order: list[AcademicPaper] = []
    seen: set[str] = set()

    for tid in selected_ids:
        paper = by_id.get(tid)
        if paper and tid not in seen:
            attempt_order.append(paper)
            seen.add(tid)

    # Pad with remaining critic pool papers not already selected
    for paper in triage_pool:
        if len(attempt_order) >= _FULLTEXT_TARGET * 3:
            break
        if paper.triage_id not in seen:
            attempt_order.append(paper)
            seen.add(paper.triage_id)

    # ── Step 5: Full-text download ───────────────────────────────────────
    return _run_fulltext_download(attempt_order)


def _format_paper_block(index: int, paper: AcademicPaper) -> list[str]:
    return [
        f"## {index}. {paper.title}",
        f"**Triage ID:** {paper.triage_id}",
        f"**Source:** {paper.url}\n",
        f"{paper.snippet}\n",
        "---\n",
    ]


def _format_subsection(source_label: str, papers: list[AcademicPaper]) -> str:
    if not papers:
        return ""
    lines = [f"## {source_label}\n"]
    for i, paper in enumerate(papers, 1):
        lines.extend(_format_paper_block(i, paper))
    return "\n".join(lines)


def _merge_academic_markdown(
    topic: str,
    s2_papers: list[AcademicPaper],
    arxiv_papers: list[AcademicPaper],
    tavily_papers: list[AcademicPaper],
    tavily_error: str | None,
) -> str:
    """Metadata-only scrape (abstracts); no embedded PDF excerpts."""
    all_papers = s2_papers + arxiv_papers + tavily_papers
    _assign_triage_ids(all_papers)

    sections: list[str] = []

    s2_block = _format_subsection("Semantic Scholar", s2_papers)
    if s2_block:
        sections.append(s2_block)

    arxiv_block = _format_subsection("arXiv", arxiv_papers)
    if arxiv_block:
        sections.append(arxiv_block)

    if tavily_papers:
        tavily_block = _format_subsection("Tavily", tavily_papers)
        if tavily_block:
            sections.append(tavily_block)
    elif tavily_error:
        sections.append(f"## Tavily\n\n{tavily_error}\n")

    if not sections:
        return f"# No academic papers found for: {topic}"

    body = "\n".join(sections)
    return f"# Academic Papers: {topic}\n\n{body}"


def search_academic_papers(
    keywords: list[str],
    user_intent: str = "General Inquiry",
    max_results: int = S2_PER_KEYWORD_LIMIT,
) -> AcademicSearchResult:
    """
    Multi-source academic discovery with Two-Tiered Phase 2.2 filtration.

    Pipeline:
      1. Semantic Expansion: Flash generates 5 search queries + core_criteria
      2. Mega-Pool: Fetch from S2/arXiv/Tavily using expanded queries (50-80 papers)
      3. Bookend Extraction: Concurrent Abstract+Conclusion PDF extraction
      4. Tier 1 — Semantic Matcher: Score papers, discard < 75%
      5. Tier 2 — Triage Critic: Select best 5 for full-text extraction
      6. Full-text download → processed_summaries/

    Returns metadata markdown for agent_scrapes/ and full-text artifacts for
    processed_summaries/ (Graphify + Phase 4.5 bypass DataRefiner).
    """
    del max_results

    if isinstance(keywords, str):
        keywords = [keywords]

    top = _top_keywords(keywords if keywords else ["research"])
    topic = top[0] if top else "research"
    primary_keyword = topic

    # ── Step 0: Semantic Expansion ───────────────────────────────────────
    print(
        "PROGRESS: Phase 2.2 — generating semantic search queries via Flash...",
        flush=True,
    )
    expanded_queries, core_criteria = _generate_semantic_expansion(
        primary_keyword, user_intent,
    )
    print(
        f"PROGRESS: Phase 2.2 — semantic expansion generated {len(expanded_queries)} "
        f"search queries + core criteria.",
        flush=True,
    )
    print(
        f'PROGRESS: Phase 2.2 — core criteria: "{core_criteria}"',
        flush=True,
    )

    # ── Step 1: Mega-Pool Fetch using expanded queries ───────────────────
    # S2 and arXiv run in parallel (each with its own lock + rate limiter).
    # Tavily runs sequentially after to avoid overwhelming external APIs.
    s2_papers: list[AcademicPaper] = []
    arxiv_papers: list[AcademicPaper] = []
    tavily_papers: list[AcademicPaper] = []
    tavily_error: str | None = None

    # Parallel: S2 (1 req/sec) + arXiv (1 req/3sec) — independent rate limiters
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="academic_src") as pool:
        s2_future = pool.submit(
            _fetch_semantic_scholar_keywords,
            expanded_queries,
            _MEGA_POOL_KEYWORD_SLICE,
        )
        arxiv_future = pool.submit(
            _fetch_arxiv_papers_list,
            expanded_queries,
            _MEGA_POOL_KEYWORD_SLICE,
        )
        s2_papers = s2_future.result()
        arxiv_papers = arxiv_future.result()

    # Sequential: Tavily with expanded queries (all 5 queries)
    tavily_papers, tavily_error = _fetch_tavily_keywords(
        expanded_queries, max_keyword_slice=_MEGA_POOL_KEYWORD_SLICE,
    )

    s2_papers, arxiv_papers, tavily_papers = _cross_source_dedup(
        s2_papers, arxiv_papers, tavily_papers,
    )

    print(
        "PROGRESS: Phase 2 — Academic sources (after dedup): "
        f"S2={len(s2_papers)}, arXiv={len(arxiv_papers)}, "
        f"Tavily={len(tavily_papers)} papers.",
        flush=True,
    )

    markdown = _merge_academic_markdown(
        topic, s2_papers, arxiv_papers, tavily_papers, tavily_error,
    )

    all_papers = s2_papers + arxiv_papers + tavily_papers

    # ── Step 2-5: Two-Tiered Phase 2.2 Pipeline ─────────────────────────
    fulltext_artifacts = _run_phase_22_fulltext(
        all_papers, keywords, user_intent, core_criteria,
    )

    return AcademicSearchResult(
        markdown=markdown,
        fulltext_artifacts=fulltext_artifacts,
    )

