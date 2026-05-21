"""
AcademicScraper.py — Dual-source academic paper discovery.

1. Semantic Scholar Academic Graph API (keyless, retried on 429/5xx)
2. Tavily domain search (arxiv.org, researchgate.net, scholar.google.com)

Returns merged Markdown with per-source subsections in a shared per-paper format.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests
from tavily import TavilyClient
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)


logger = logging.getLogger(__name__)

S2_BASE_URL = "https://api.semanticscholar.org/graph/v1"
S2_SEARCH_PATH = "/paper/search"
S2_FIELDS = "title,url,abstract,year,citationCount,openAccessPdf,tldr,paperId"
S2_USER_AGENT = "ResearchBot/1.0 (ResearchGraphApp academic pipeline)"
S2_TIMEOUT_SEC = 30

ACADEMIC_DOMAINS = ["arxiv.org", "researchgate.net", "scholar.google.com"]

PaperRow = tuple[str, str, str]  # title, url, snippet


def _enhanced_query(topic: str) -> str:
    return f"{topic} research paper methodology findings"


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


def _format_paper_block(index: int, title: str, url: str, snippet: str) -> list[str]:
    return [
        f"## {index}. {title}",
        f"**Source:** {url}\n",
        f"{snippet}\n",
        "---\n",
    ]


def _format_subsection(source_label: str, papers: list[PaperRow]) -> str:
    if not papers:
        return ""
    lines = [f"## {source_label}\n"]
    for i, (title, url, snippet) in enumerate(papers, 1):
        lines.extend(_format_paper_block(i, title, url, snippet))
    return "\n".join(lines)


def _is_retryable_http_error(exc: BaseException) -> bool:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return False


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=3, min=5, max=60),
    retry=retry_if_exception(_is_retryable_http_error),
    reraise=True,
)
def _s2_search_request(query: str, limit: int) -> dict[str, Any]:
    response = requests.get(
        f"{S2_BASE_URL}{S2_SEARCH_PATH}",
        params={
            "query": query,
            "limit": limit,
            "fields": S2_FIELDS,
        },
        headers={"User-Agent": S2_USER_AGENT},
        timeout=S2_TIMEOUT_SEC,
    )
    response.raise_for_status()
    return response.json()


def _fetch_semantic_scholar(topic: str, max_results: int) -> list[PaperRow]:
    """Return paper rows from Semantic Scholar; [] on failure (no error markdown)."""
    try:
        payload = _s2_search_request(_enhanced_query(topic), max_results)
    except Exception as exc:
        logger.warning(
            "Semantic Scholar search failed for topic %r after retries: %s",
            topic,
            exc,
        )
        return []

    papers: list[PaperRow] = []
    for paper in (payload.get("data") or [])[:max_results]:
        if not isinstance(paper, dict):
            continue
        title = paper.get("title") or "Untitled Paper"
        papers.append((title, _paper_url(paper), _paper_snippet(paper)))
    return papers


def _fetch_tavily_academic(topic: str, max_results: int) -> tuple[list[PaperRow], str | None]:
    """
    Return (paper rows, optional error message for Tavily subsection only).
    """
    api_key = os.getenv("TAVILY_API_KEY", "")
    if not api_key:
        return [], "TAVILY_API_KEY not set. Cannot search academic papers via Tavily."

    try:
        client = TavilyClient(api_key=api_key)
        response = client.search(
            query=_enhanced_query(topic),
            include_domains=ACADEMIC_DOMAINS,
            max_results=max_results,
        )
        results = response.get("results", [])
        papers: list[PaperRow] = []
        for r in results:
            title = r.get("title", "Untitled Paper")
            url = r.get("url", "")
            snippet = r.get("content", "No abstract available.")
            papers.append((title, url, snippet))
        return papers, None
    except Exception as exc:
        return [], f"Failed to search for `{topic}`: {exc}"


def _merge_academic_markdown(
    topic: str,
    s2_papers: list[PaperRow],
    tavily_papers: list[PaperRow],
    tavily_error: str | None,
) -> str:
    sections: list[str] = []

    s2_block = _format_subsection("Semantic Scholar", s2_papers)
    if s2_block:
        sections.append(s2_block)

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


def search_academic_papers(topic: str, max_results: int = 5) -> str:
    """Search Semantic Scholar then Tavily; return merged Markdown."""
    s2_papers = _fetch_semantic_scholar(topic, max_results)
    tavily_papers, tavily_error = _fetch_tavily_academic(topic, max_results)
    return _merge_academic_markdown(topic, s2_papers, tavily_papers, tavily_error)
