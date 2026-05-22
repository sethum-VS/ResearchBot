"""
AcademicScraper.py — Multi-source academic discovery + Phase 2.2 full-text triage.

Sources:
  1. Semantic Scholar Academic Graph API (optional x-api-key, multi-keyword)
  2. arXiv Atom API (multi-keyword, rate-paced)
  3. Tavily domain search (arxiv.org, researchgate.net, scholar.google.com)

Phase 2.2: Gemini 2.5 Flash selects top 5 OA papers → concurrent full PDF
extraction → artifacts returned for direct injection into processed_summaries/
(metadata-only academic scrape stays in agent_scrapes/academic_scrape.md).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
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

from infrastructure.PdfExtractor import extract_full_text_from_url


logger = logging.getLogger(__name__)

S2_BASE_URL = "https://api.semanticscholar.org/graph/v1"
S2_SEARCH_PATH = "/paper/search"
S2_FIELDS = "title,url,abstract,year,citationCount,openAccessPdf,tldr,paperId"
S2_USER_AGENT = "ResearchBot/1.0 (ResearchGraphApp academic pipeline)"
S2_TIMEOUT_SEC = 30
S2_PER_KEYWORD_LIMIT = 10

ARXIV_API_URL = "http://export.arxiv.org/api/query"
ARXIV_PER_KEYWORD_LIMIT = 10
ARXIV_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

ACADEMIC_DOMAINS = ["arxiv.org", "researchgate.net", "scholar.google.com"]
TAVILY_PER_KEYWORD_LIMIT = 5
KEYWORD_SLICE = 3
_PDF_EXTRACT_WORKERS = 5
_FULLTEXT_TARGET = 5
_TRIAGE_MODEL = "gemini-2.5-flash"

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


def _enhanced_query(keyword: str) -> str:
    return f"{keyword} research paper methodology findings"


def _top_keywords(keywords: list[str]) -> list[str]:
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
        if len(ordered) >= KEYWORD_SLICE:
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


def _s2_headers() -> dict[str, str]:
    headers = {"User-Agent": S2_USER_AGENT}
    api_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip()
    if api_key:
        headers["x-api-key"] = api_key
    return headers


def _is_retryable_http_error(exc: BaseException) -> bool:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return False


def _is_resource_exhausted(exc: Exception) -> bool:
    exc_str = str(exc).lower()
    return "429" in exc_str or "resourceexhausted" in exc_str or "resource_exhausted" in exc_str


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
        headers=_s2_headers(),
        timeout=S2_TIMEOUT_SEC,
    )
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


def _fetch_semantic_scholar_keywords(keywords: list[str]) -> list[AcademicPaper]:
    merged: list[dict[str, Any]] = []
    has_api_key = bool(os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip())
    saw_429 = False
    for idx, keyword in enumerate(_top_keywords(keywords)):
        delay = _s2_keyword_delay_sec()
        if idx > 0 and delay > 0:
            time.sleep(delay)
        try:
            payload = _s2_search_request(_enhanced_query(keyword), S2_PER_KEYWORD_LIMIT)
            merged.extend(payload.get("data") or [])
        except Exception as exc:
            exc_str = str(exc)
            if "429" in exc_str:
                saw_429 = True
            logger.warning(
                "Semantic Scholar search failed for keyword %r after retries: %s",
                keyword,
                exc,
            )
    if saw_429 and not has_api_key:
        print(
            "PROGRESS: Phase 2 — Semantic Scholar rate-limited (429). "
            "Set SEMANTIC_SCHOLAR_API_KEY in .env for higher quotas.",
            flush=True,
        )

    papers: list[AcademicPaper] = []
    for paper in _dedupe_s2_records(merged):
        title = paper.get("title") or "Untitled Paper"
        url = _paper_url(paper)
        pdf_url = _open_access_pdf_url(paper)
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
    raw = os.getenv("ARXIV_REQUEST_DELAY_SEC", "3")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 3.0


def _s2_keyword_delay_sec() -> float:
    raw = os.getenv("S2_KEYWORD_DELAY_SEC", "4")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 4.0


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


def _fetch_arxiv_keyword(keyword: str, max_results: int) -> list[AcademicPaper]:
    try:
        response = requests.get(
            ARXIV_API_URL,
            params={
                "search_query": f"all:{keyword}",
                "start": 0,
                "max_results": max_results,
            },
            timeout=30,
            headers={"User-Agent": S2_USER_AGENT},
        )
        response.raise_for_status()
        return _parse_arxiv_atom(response.text)
    except Exception as exc:
        logger.warning("arXiv search failed for keyword %r: %s", keyword, exc)
        return []


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


def search_arxiv_papers(keywords: list[str], max_results: int = ARXIV_PER_KEYWORD_LIMIT) -> str:
    """Query arXiv for the top keywords and return a Markdown subsection (## arXiv)."""
    all_papers: list[AcademicPaper] = []
    top = _top_keywords(keywords)
    delay = _arxiv_delay_sec()

    for i, keyword in enumerate(top):
        if i > 0 and delay > 0:
            time.sleep(delay)
        all_papers.extend(_fetch_arxiv_keyword(keyword, max_results))

    deduped = _dedupe_arxiv_papers(all_papers)
    if not deduped:
        return ""
    return _format_subsection("arXiv", deduped)


def _fetch_arxiv_papers_list(keywords: list[str]) -> list[AcademicPaper]:
    all_papers: list[AcademicPaper] = []
    top = _top_keywords(keywords)
    delay = _arxiv_delay_sec()

    for i, keyword in enumerate(top):
        if i > 0 and delay > 0:
            time.sleep(delay)
        all_papers.extend(_fetch_arxiv_keyword(keyword, ARXIV_PER_KEYWORD_LIMIT))

    return _dedupe_arxiv_papers(all_papers)


def _fetch_tavily_keywords(keywords: list[str]) -> tuple[list[AcademicPaper], str | None]:
    api_key = os.getenv("TAVILY_API_KEY", "")
    if not api_key:
        return [], "TAVILY_API_KEY not set. Cannot search academic papers via Tavily."

    client = TavilyClient(api_key=api_key)
    rows: list[AcademicPaper] = []
    seen_urls: set[str] = set()

    for keyword in _top_keywords(keywords):
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


def _papers_to_triage_dicts(papers: list[AcademicPaper]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for paper in papers:
        abstract = paper.snippet
        if "\n\n" in abstract:
            abstract = abstract.split("\n\n", 1)[-1].strip()
        rows.append(
            {
                "triage_id": paper.triage_id,
                "title": paper.title,
                "abstract": abstract,
                "pdf_url": paper.pdf_url or "",
                "citation_count": paper.citation_count,
                "source": paper.source,
            }
        )
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


def _build_triage_prompt(papers: list[dict[str, Any]], keywords: list[str]) -> str:
    kw_text = ", ".join(_top_keywords(keywords)) or "research"
    catalog = json.dumps(papers, ensure_ascii=False, indent=2)
    return (
        "You are an academic triage specialist for university Final Year Project research.\n\n"
        f"Evaluate these paper records against the research keywords: {kw_text}\n\n"
        "Rules:\n"
        "- Only consider entries that include a non-empty pdf_url (open-access PDF).\n"
        "- Identify the 5 most critical papers for a deep methodological review.\n"
        "- Return ONLY a raw JSON array of the exact triage_id strings (no markdown fences).\n"
        "- Example: [\"s2:abc123\", \"arxiv:1706.03762\"]\n\n"
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


def _parse_triage_ids(raw: str, valid_ids: set[str]) -> list[str]:
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
        parsed = json.loads(match.group(0))

    if isinstance(parsed, dict):
        for key in ("triage_ids", "ids", "papers", "selected"):
            if key in parsed and isinstance(parsed[key], list):
                parsed = parsed[key]
                break

    if not isinstance(parsed, list):
        return []

    out: list[str] = []
    for item in parsed:
        tid = ""
        if isinstance(item, str):
            tid = item.strip()
        elif isinstance(item, dict):
            tid = str(
                item.get("triage_id")
                or item.get("id")
                or item.get("paper_id")
                or ""
            ).strip()
        if tid and tid in valid_ids and tid not in out:
            out.append(tid)
    return out


def triage_top_papers(papers: list[dict], keywords: list[str]) -> list[str]:
    """
    Use Gemini 2.5 Flash to pick up to 5 triage_id values from the OA pool.

    Falls back to top citations if Flash is unavailable or returns invalid JSON.
    """
    if not papers:
        return []

    valid_ids = {p["triage_id"] for p in papers if p.get("triage_id")}
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT_ID", "").strip()
    if not project_id:
        logger.warning("triage_top_papers: GOOGLE_CLOUD_PROJECT_ID not set; using citation fallback.")
        return [p["triage_id"] for p in papers[:_FULLTEXT_TARGET] if p.get("triage_id")]

    prompt = _build_triage_prompt(papers, keywords)
    last_exc: Exception | None = None

    try:
        client = genai.Client(vertexai=True, project=project_id, location="global")
        raw = _call_flash_triage(client, prompt)
        ids = _parse_triage_ids(raw, valid_ids)
        if ids:
            print(
                f"PROGRESS: Phase 2.2 — Flash selected {len(ids)} papers for full-text review.",
                flush=True,
            )
            return ids[:_FULLTEXT_TARGET]
    except Exception as exc:
        logger.warning("triage_top_papers: global Flash failed: %s", exc)
        last_exc = exc

    for region in _STABLE_REGIONS:
        try:
            client = genai.Client(vertexai=True, project=project_id, location=region)
            raw = _call_flash_triage(client, prompt)
            ids = _parse_triage_ids(raw, valid_ids)
            if ids:
                print(
                    f"PROGRESS: Phase 2.2 — Flash selected {len(ids)} papers "
                    f"(region {region}).",
                    flush=True,
                )
                return ids[:_FULLTEXT_TARGET]
        except Exception as exc:
            logger.warning("triage_top_papers: region %s failed: %s", region, exc)
            last_exc = exc

    logger.warning(
        "triage_top_papers: Flash exhausted (%s); citation fallback.",
        last_exc,
    )
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
        body = extract_full_text_from_url(pdf_url)
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


def _run_phase_22_fulltext(
    all_papers: list[AcademicPaper],
    keywords: list[str],
) -> list[FullTextArtifact]:
    pool = _build_triage_pool(all_papers)
    if not pool:
        print("PROGRESS: Phase 2.2 — no open-access PDFs in corpus; skipping full text.", flush=True)
        return []

    print(
        f"PROGRESS: Phase 2.2 — triaging {len(pool)} OA candidates (max pool "
        f"{_triage_pool_max()})...",
        flush=True,
    )

    triage_dicts = _papers_to_triage_dicts(pool)
    selected_ids = triage_top_papers(triage_dicts, keywords)

    by_id = {p.triage_id: p for p in pool}
    attempt_order: list[AcademicPaper] = []
    seen: set[str] = set()

    for tid in selected_ids:
        paper = by_id.get(tid)
        if paper and tid not in seen:
            attempt_order.append(paper)
            seen.add(tid)

    for paper in pool:
        if len(attempt_order) >= _FULLTEXT_TARGET * 3:
            break
        if paper.triage_id not in seen:
            attempt_order.append(paper)
            seen.add(paper.triage_id)

    if not attempt_order:
        return []

    # Deduplicate download work by PDF URL.
    url_to_paper: dict[str, AcademicPaper] = {}
    for paper in attempt_order:
        pdf_url = (paper.pdf_url or "").strip()
        if pdf_url and pdf_url not in url_to_paper:
            url_to_paper[pdf_url] = paper

    work = list(url_to_paper.items())
    target = _FULLTEXT_TARGET
    print(
        f"PROGRESS: Phase 2.2 — fetching full text for up to {target} papers "
        f"({len(work)} unique PDFs, {_PDF_EXTRACT_WORKERS} workers)...",
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
    max_results: int = S2_PER_KEYWORD_LIMIT,
) -> AcademicSearchResult:
    """
    Multi-source academic discovery with Phase 2.2 Flash triage + full PDF text.

    Returns metadata markdown for agent_scrapes/ and full-text artifacts for
    processed_summaries/ (Graphify + Phase 4.5 bypass DataRefiner).
    """
    del max_results

    if isinstance(keywords, str):
        keywords = [keywords]

    top = _top_keywords(keywords if keywords else ["research"])
    topic = top[0] if top else "research"

    s2_papers = _fetch_semantic_scholar_keywords(keywords)
    arxiv_papers = _fetch_arxiv_papers_list(keywords)
    tavily_papers, tavily_error = _fetch_tavily_keywords(keywords)

    s2_papers, arxiv_papers, tavily_papers = _cross_source_dedup(
        s2_papers, arxiv_papers, tavily_papers,
    )

    markdown = _merge_academic_markdown(
        topic, s2_papers, arxiv_papers, tavily_papers, tavily_error,
    )

    all_papers = s2_papers + arxiv_papers + tavily_papers
    fulltext_artifacts = _run_phase_22_fulltext(all_papers, keywords)

    return AcademicSearchResult(
        markdown=markdown,
        fulltext_artifacts=fulltext_artifacts,
    )
