"""
ProposalOrchestrator.py — Phase 5: Proposal generation pipeline orchestrator.

Manages input scoping, local/external paper filtering via SemanticMatcher,
and coordinates ProposalSynthesizer for final document generation.

This is a STANDALONE pipeline — it does not alter or re-run Phases 1.5–4.5.
It reads from an existing session's artifacts and produces proposals inside
that session's ``proposals/`` subdirectory.

Invoked via: main.py --command generate_proposal --session-id ... --project-idea ...
"""

from __future__ import annotations

import json
import logging
import os
import re
import concurrent.futures
from datetime import datetime, timezone
from pathlib import Path

from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

from infrastructure.FileStorage import resolve_session_dir, get_kb_root
from infrastructure.SemanticMatcher import calculate_relevance_score
from infrastructure.AcademicScraper import (
    _fetch_semantic_scholar_keywords,
    _fetch_tavily_keywords,
)
from infrastructure.PdfExtractor import extract_full_text_from_url
from infrastructure.TextChunker import extract_academic_bookends

logger = logging.getLogger(__name__)

_STABLE_REGIONS: list[str] = [
    "europe-west4",
    "us-east4",
    "asia-northeast1",
    "us-central1",
]

_MIN_MATCH_THRESHOLD = 75.0
_TARGET_PAPER_COUNT = 15
_MIN_PAPER_COUNT = 10


def _is_resource_exhausted(exc: Exception) -> bool:
    exc_str = str(exc).lower()
    return (
        "429" in exc_str
        or "resourceexhausted" in exc_str
        or "resource_exhausted" in exc_str
    )


# ── Pydantic Schema for Scoping ──────────────────────────────────────────────

class ProposalScopingAnalysis(BaseModel):
    """Structured research scoping analysis."""
    scoped_query: str = Field(
        description="A single precise, highly specific research query defining Task, Domain, and Constraint."
    )
    search_queries: list[str] = Field(
        description="An array of exactly 5 distinct academic search queries targeting different aspects of the idea."
    )
    core_criteria: str = Field(
        description="A concise 2-sentence definition of what makes an academic paper relevant to this specific project."
    )


# ── Input Scoping ────────────────────────────────────────────────────────────


_SCOPE_PROMPT = """\
You are a research scoping specialist for Final Year Projects. Analyze the user's \
raw project idea and the session context to define a precise scope and search strategy.

SESSION CONTEXT (from prior research):
{core_context}

USER'S RAW PROJECT IDEA:
{user_idea}

Tasks:
1. Transform the vague project idea into a single precise, highly specific research query defining the Task, Domain, and Constraints.
2. Generate an array of exactly 5 distinct, highly-optimized academic search queries. Each query should target different aspects of the proposal (e.g., domain-specific, methodology, technology, architectural limitations, and constraints).
3. Create a concise 2-sentence definition of the 'core criteria' for what makes an academic paper relevant to this specific project.
"""


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    retry=retry_if_exception(_is_resource_exhausted),
)
def _call_flash_scope(client: genai.Client, prompt: str) -> str:
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ProposalScopingAnalysis,
        ),
    )
    if not response or not response.text:
        raise RuntimeError("Gemini Flash returned empty scoping response.")
    return response.text.strip()


def _scope_input(user_idea: str, core_context: str) -> dict:
    """
    Use Gemini 2.5 Flash to transform a vague idea into precise research queries and criteria.
    """
    fallback_scoping = {
        "scoped_query": user_idea.strip(),
        "search_queries": [
            user_idea.strip(),
            f"{user_idea.strip()} methodology",
            f"{user_idea.strip()} core techniques",
            f"{user_idea.strip()} architectural limitations",
            f"{user_idea.strip()} challenges",
        ],
        "core_criteria": f"Papers discussing the domain or methodology of: {user_idea.strip()}.",
    }

    project_id = os.getenv("GOOGLE_CLOUD_PROJECT_ID")
    if not project_id:
        logger.warning("GOOGLE_CLOUD_PROJECT_ID not set; using fallback scoping.")
        return fallback_scoping

    prompt = _SCOPE_PROMPT.format(
        core_context=core_context[:4000],
        user_idea=user_idea.strip(),
    )

    last_exc: Exception | None = None
    raw_text = ""

    # Primary: global
    try:
        client = genai.Client(vertexai=True, project=project_id, location="global")
        raw_text = _call_flash_scope(client, prompt)
    except Exception as exc:
        last_exc = exc

    # Regional failover
    if not raw_text:
        for region in _STABLE_REGIONS:
            try:
                client = genai.Client(vertexai=True, project=project_id, location=region)
                raw_text = _call_flash_scope(client, prompt)
                if raw_text:
                    break
            except Exception as exc:
                last_exc = exc
                continue

    if not raw_text:
        logger.error("Input scoping exhausted all regions: %s", last_exc)
        return fallback_scoping

    try:
        text = raw_text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            text = text.strip()

        parsed = ProposalScopingAnalysis.model_validate_json(text)
        return parsed.model_dump()
    except Exception as parse_exc:
        logger.warning("Failed to parse scoping JSON. Error: %s", parse_exc)
        return fallback_scoping


# ── Paper Extraction from Session Artifacts ──────────────────────────────────

# Pattern: ## <N>. <Title>
_PAPER_HEADER_RE = re.compile(
    r"^##\s+(\d+)\.\s+(.+)$", re.MULTILINE
)


def _parse_academic_scrape(scrape_path: Path) -> list[dict]:
    """
    Parse academic_scrape.md into individual paper dicts.

    Expected format per paper:
        ## N. Title
        **Triage ID:** ...
        **Source:** ...
        (date)
        Abstract text...
        ---
    """
    if not scrape_path.is_file():
        return []

    text = scrape_path.read_text(encoding="utf-8")
    if not text.strip():
        return []

    papers: list[dict] = []

    # Find all section headers
    headers = list(_PAPER_HEADER_RE.finditer(text))
    if not headers:
        return []

    for i, match in enumerate(headers):
        title = match.group(2).strip()
        start = match.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        block = text[start:end].strip()

        # Extract triage_id
        triage_match = re.search(r"\*\*Triage ID:\*\*\s*(.+)", block)
        triage_id = triage_match.group(1).strip() if triage_match else ""

        # Extract source URL
        source_match = re.search(r"\*\*Source:\*\*\s*(.+)", block)
        source_url = source_match.group(1).strip() if source_match else ""

        # Extract year from date pattern *(YYYY-MM-DD)*
        year_match = re.search(r"\*\((\d{4})", block)
        year = year_match.group(1) if year_match else "N/A"

        # Everything after the metadata lines is the abstract
        # Remove metadata lines and separator
        abstract_text = block
        for pattern in [
            r"\*\*Triage ID:\*\*.*\n?",
            r"\*\*Source:\*\*.*\n?",
            r"\*\(\d{4}-\d{2}-\d{2}\)\*\n?",
            r"^---\s*$",
        ]:
            abstract_text = re.sub(pattern, "", abstract_text, flags=re.MULTILINE)
        abstract_text = abstract_text.strip()

        # Check for PDF URL in source
        pdf_url = ""
        if source_url and ("arxiv.org/pdf" in source_url or source_url.endswith(".pdf")):
            pdf_url = source_url
        elif source_url and "arxiv.org/abs/" in source_url:
            pdf_url = source_url.replace("/abs/", "/pdf/")

        papers.append({
            "title": title,
            "abstract": abstract_text,
            "triage_id": triage_id,
            "source_url": source_url,
            "source": "local_session",
            "year": year,
            "pdf_url": pdf_url,
        })

    return papers


def _collect_fulltext_papers(summaries_dir: Path) -> list[dict]:
    """
    Read academic_fulltext_*.md files and extract paper metadata.
    """
    if not summaries_dir.is_dir():
        return []

    papers: list[dict] = []
    for md_file in sorted(summaries_dir.glob("academic_fulltext_*.md")):
        try:
            text = md_file.read_text(encoding="utf-8")
        except OSError:
            continue

        if not text.strip():
            continue

        # Extract title from first heading
        title_match = re.search(r"^#\s+Academic Full Text:\s*(.+)$", text, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else md_file.stem

        # Extract triage ID
        triage_match = re.search(r"\*\*Triage ID:\*\*\s*(.+)", text)
        triage_id = triage_match.group(1).strip() if triage_match else ""

        # Extract source
        source_match = re.search(r"\*\*Source:\*\*\s*(.+)", text)
        source_url = source_match.group(1).strip() if source_match else ""

        # Extract PDF URL
        pdf_match = re.search(r"\*\*PDF:\*\*\s*(.+)", text)
        pdf_url = pdf_match.group(1).strip() if pdf_match else ""

        # Use first 2000 chars of body as abstract proxy
        body_match = re.search(r"## Full Document Text\s*```text\s*(.+?)```", text, re.DOTALL)
        abstract = ""
        if body_match:
            abstract = body_match.group(1).strip()[:2000]
        elif len(text) > 500:
            abstract = text[200:2200]

        papers.append({
            "title": title,
            "abstract": abstract,
            "triage_id": triage_id,
            "source_url": source_url,
            "source": "local_fulltext",
            "year": "N/A",
            "pdf_url": pdf_url,
        })

    return papers


# ── External Paper Search ────────────────────────────────────────────────────


def _external_search(scoped_query: str, needed: int) -> list[dict]:
    """
    Search for additional papers using the existing AcademicScraper infrastructure.
    Returns raw paper dicts suitable for SemanticMatcher scoring.
    """
    try:
        from infrastructure.AcademicScraper import (
            AcademicSearchResult,
            search_academic_papers,
        )
    except ImportError as exc:
        logger.error("Cannot import AcademicScraper: %s", exc)
        return []

    # Split scoped query into keyword-like chunks for the search
    keywords = _scoped_query_to_keywords(scoped_query)
    if not keywords:
        keywords = [scoped_query[:100]]

    print(
        f"PROGRESS: Phase 5 — external search with keywords: {keywords}",
        flush=True,
    )

    try:
        result = search_academic_papers(keywords)
    except Exception as exc:
        logger.error("External academic search failed: %s", exc)
        return []

    # Convert the AcademicSearchResult markdown back to structured papers
    if isinstance(result, AcademicSearchResult):
        papers = _parse_academic_scrape_text(result.markdown)
    else:
        papers = _parse_academic_scrape_text(result or "")

    print(
        f"PROGRESS: Phase 5 — external search returned {len(papers)} candidates.",
        flush=True,
    )
    return papers


def _scoped_query_to_keywords(scoped_query: str) -> list[str]:
    """Break a scoped query into 2-3 search-friendly keyword phrases."""
    # Remove common filler words, keep meaningful chunks
    cleaned = re.sub(r"\b(using|for|with|and|the|in|of|to|a|an|on)\b", " ", scoped_query.lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    words = cleaned.split()
    if len(words) <= 4:
        return [scoped_query]

    # Return 2-3 keyword chunks
    keywords = []
    chunk_size = max(3, len(words) // 3)
    for i in range(0, len(words), chunk_size):
        chunk = " ".join(words[i : i + chunk_size])
        if chunk.strip():
            keywords.append(chunk.strip())
        if len(keywords) >= 3:
            break

    return keywords


def _parse_academic_scrape_text(markdown_text: str) -> list[dict]:
    """Parse any academic scrape markdown text into paper dicts (reuses the same format)."""
    if not markdown_text or not markdown_text.strip():
        return []

    papers: list[dict] = []
    headers = list(_PAPER_HEADER_RE.finditer(markdown_text))
    if not headers:
        return []

    for i, match in enumerate(headers):
        title = match.group(2).strip()
        start = match.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(markdown_text)
        block = markdown_text[start:end].strip()

        triage_match = re.search(r"\*\*Triage ID:\*\*\s*(.+)", block)
        triage_id = triage_match.group(1).strip() if triage_match else ""

        source_match = re.search(r"\*\*Source:\*\*\s*(.+)", block)
        source_url = source_match.group(1).strip() if source_match else ""

        year_match = re.search(r"\*\((\d{4})", block)
        year = year_match.group(1) if year_match else "N/A"

        abstract_text = block
        for pattern in [
            r"\*\*Triage ID:\*\*.*\n?",
            r"\*\*Source:\*\*.*\n?",
            r"\*\(\d{4}-\d{2}-\d{2}\)\*\n?",
            r"^---\s*$",
        ]:
            abstract_text = re.sub(pattern, "", abstract_text, flags=re.MULTILINE)
        abstract_text = abstract_text.strip()

        pdf_url = ""
        if source_url and "arxiv.org/abs/" in source_url:
            pdf_url = source_url.replace("/abs/", "/pdf/")

        papers.append({
            "title": title,
            "abstract": abstract_text,
            "triage_id": triage_id,
            "source_url": source_url,
            "source": "external",
            "year": year,
            "pdf_url": pdf_url,
        })

    return papers


# ── Main Orchestrator ────────────────────────────────────────────────────────


def generate_proposal(
    session_id: str,
    user_project_idea: str,
    kb_root: str | None = None,
) -> dict:
    """
    Phase 5 entry point: generate an academic proposal from a historical session.

    Args:
        session_id: Session directory name (e.g., session_20260520T235831Z_topic).
        user_project_idea: Raw user idea text.
        kb_root: Optional absolute path to research_knowledge_base/.

    Returns:
        Result dict for JSON serialization to Swift.
    """
    if not user_project_idea.strip():
        return {
            "status": "error",
            "message": "Project idea cannot be empty.",
        }

    # ── Resolve session directory ────────────────────────────────────────
    session_dir: Path | None = None
    if kb_root:
        runs = Path(kb_root).expanduser() / "runs"
        for candidate_name in [session_id.strip(), f"session_{session_id.strip()}"]:
            candidate = runs / candidate_name
            if candidate.is_dir():
                session_dir = candidate
                break
        if session_dir is None and runs.is_dir():
            sid = session_id.strip()
            for p in runs.iterdir():
                if p.is_dir() and (p.name == sid or p.name.endswith(sid) or sid in p.name):
                    session_dir = p
                    break
    else:
        session_dir = resolve_session_dir(session_id)

    if session_dir is None or not session_dir.is_dir():
        return {
            "status": "error",
            "message": f"Session not found: {session_id}",
        }

    print(
        f"PROGRESS: Phase 5 — resolved session: {session_dir.name}",
        flush=True,
    )

    # ── Load session context ─────────────────────────────────────────────
    manifest_path = session_dir / "session_manifest.json"
    core_context = ""
    primary_keyword = ""
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            core_context = manifest.get("topic", "")
            primary_keyword = manifest.get("primary_keyword", "")
        except (json.JSONDecodeError, OSError):
            pass

    if not core_context:
        core_context = user_project_idea

    # Load gap analysis
    gap_path = session_dir / "academic_gap_analysis.json"
    gap_analysis: dict = {}
    if gap_path.is_file():
        try:
            gap_analysis = json.loads(gap_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    # ── Task 1: Deep Semantic Expansion ──────────────────────────────────
    print("PROGRESS: Phase 5 — scoping input idea...", flush=True)
    scoping_data = _scope_input(user_project_idea, core_context)
    scoped_query = scoping_data["scoped_query"]
    search_queries = scoping_data["search_queries"]
    core_criteria = scoping_data["core_criteria"]

    print(
        f'PROGRESS: Phase 5 — scoped query: "{scoped_query[:120]}"',
        flush=True,
    )
    print(
        f'PROGRESS: Phase 5 — academic search criteria defined: "{core_criteria[:120]}"',
        flush=True,
    )

    # ── Task 2: Mega-Pool Builder ────────────────────────────────────────
    scrape_path = session_dir / "agent_scrapes" / "academic_scrape.md"
    summaries_dir = session_dir / "processed_summaries"

    local_scrape_papers = _parse_academic_scrape(scrape_path)
    local_fulltext_papers = _collect_fulltext_papers(summaries_dir)

    candidate_pool = []
    for paper in local_scrape_papers:
        paper["source"] = "local_session"
        candidate_pool.append(paper)
    for paper in local_fulltext_papers:
        paper["source"] = "local_fulltext"
        candidate_pool.append(paper)

    local_count = len(candidate_pool)
    print(
        f"PROGRESS: Phase 5 — local pass collected {local_count} papers from session corpus.",
        flush=True,
    )

    # Concurrently execute 5 external search queries
    print(
        f"PROGRESS: Phase 5 — external pass: executing {len(search_queries)} concurrent queries via S2 + Tavily...",
        flush=True,
    )

    external_papers = []

    def fetch_for_query(q_str: str) -> list[dict]:
        results = []
        # S2 fetch
        try:
            s2_papers = _fetch_semantic_scholar_keywords([q_str])
            print(
                f"PROGRESS: Phase 5 — Semantic Scholar search ok for query: '{q_str[:40]}...' ({len(s2_papers)} papers)",
                flush=True,
            )
            for p in s2_papers:
                results.append({
                    "title": p.title,
                    "abstract": p.snippet or "",
                    "triage_id": p.triage_id or (f"s2:{p.paper_id}" if p.paper_id else ""),
                    "source_url": p.url,
                    "source": "semantic_scholar",
                    "year": "N/A",
                    "pdf_url": p.pdf_url or "",
                })
        except Exception as e:
            logger.warning("S2 query %r failed: %s", q_str, e)

        # Tavily fetch
        try:
            tav_papers, err = _fetch_tavily_keywords([q_str])
            if not err:
                print(
                    f"PROGRESS: Phase 5 — Tavily search ok for query: '{q_str[:40]}...' ({len(tav_papers)} papers)",
                    flush=True,
                )
                for p in tav_papers:
                    results.append({
                        "title": p.title,
                        "abstract": p.snippet or "",
                        "triage_id": p.triage_id or (f"tavily:{p.url}" if p.url else ""),
                        "source_url": p.url,
                        "source": "tavily",
                        "year": "N/A",
                        "pdf_url": p.pdf_url or "",
                    })
            else:
                logger.warning("Tavily query %r failed: %s", q_str, err)
        except Exception as e:
            logger.warning("Tavily query %r failed: %s", q_str, e)

        return results

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_for_query, q): q for q in search_queries}
        for future in concurrent.futures.as_completed(futures):
            try:
                external_papers.extend(future.result())
            except Exception as e:
                logger.error("External query worker encountered error: %s", e)

    # Deduplicate the entire pool based on title, URL, or triage ID
    seen_titles = set()
    seen_urls = set()
    seen_ids = set()
    deduped_pool = []

    for paper in candidate_pool + external_papers:
        title = paper.get("title", "")
        norm_title = re.sub(r"\s+", " ", title.strip().lower())
        url = paper.get("source_url", "")
        norm_url = url.strip().lower().rstrip("/") if url else ""
        tid = paper.get("triage_id", "")

        if not norm_title:
            continue
        if norm_title in seen_titles:
            continue
        if norm_url and norm_url in seen_urls:
            continue
        if tid and tid in seen_ids:
            continue

        seen_titles.add(norm_title)
        if norm_url:
            seen_urls.add(norm_url)
        if tid:
            seen_ids.add(tid)
        deduped_pool.append(paper)

    candidate_pool = deduped_pool
    local_count = sum(1 for p in candidate_pool if p.get("source") in ("local_session", "local_fulltext"))
    external_count = len(candidate_pool) - local_count

    print(
        f"PROGRESS: Phase 5 — built candidate mega-pool: {len(candidate_pool)} unique papers "
        f"({local_count} local + {external_count} external).",
        flush=True,
    )

    # ── Task 3.5: Abstract+Conclusion Enrichment (PDF extraction) ────────
    papers_with_pdf = [p for p in candidate_pool if p.get("pdf_url")]
    print(
        f"PROGRESS: Phase 5 — enriching {len(papers_with_pdf)} papers with "
        f"Abstract+Conclusion (PDF extraction)...",
        flush=True,
    )

    _ENRICHMENT_MAX_CHARS = 8000
    enrichment_success = 0
    enrichment_total = len(papers_with_pdf)

    def _enrich_single_paper(idx: int, paper: dict) -> dict:
        nonlocal enrichment_success
        pdf_url = paper.get("pdf_url", "")
        title_short = paper.get("title", "Untitled")[:60]
        try:
            full_text = extract_full_text_from_url(pdf_url)
            if full_text and len(full_text.strip()) > 200:
                bookends = extract_academic_bookends(
                    full_text, max_chars=_ENRICHMENT_MAX_CHARS
                )
                if bookends and bookends.strip():
                    paper["abstract_conclusion"] = bookends
                    enrichment_success += 1
                    print(
                        f'PROGRESS: Phase 5 — [{idx}/{enrichment_total}] '
                        f'✓ extracted bookends for "{title_short}" '
                        f'({len(bookends):,} chars)',
                        flush=True,
                    )
                    return paper
            print(
                f'PROGRESS: Phase 5 — [{idx}/{enrichment_total}] '
                f'✗ PDF unavailable for "{title_short}"',
                flush=True,
            )
        except Exception as e:
            logger.warning(
                "Enrichment failed for '%s': %s", title_short, e
            )
            print(
                f'PROGRESS: Phase 5 — [{idx}/{enrichment_total}] '
                f'✗ PDF extraction failed for "{title_short}"',
                flush=True,
            )
        return paper

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [
            executor.submit(_enrich_single_paper, i + 1, paper)
            for i, paper in enumerate(papers_with_pdf)
        ]
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except Exception as e:
                logger.error("Enrichment worker error: %s", e)

    print(
        f"PROGRESS: Phase 5 — enrichment complete: {enrichment_success}/"
        f"{enrichment_total} papers have Abstract+Conclusion.",
        flush=True,
    )

    # ── Task 3 & 4: Rubric-Based Scoring & Filtering ─────────────────────
    print(
        f"PROGRESS: Phase 5 — scoring candidate pool of {len(candidate_pool)} papers concurrently...",
        flush=True,
    )

    scored_papers = []
    total_papers = len(candidate_pool)

    def score_single_paper_task(idx: int, paper: dict) -> dict:
        try:
            score = calculate_relevance_score(core_criteria, paper)
            status = "✓" if score > _MIN_MATCH_THRESHOLD else "✗"
            print(
                f"PROGRESS: Phase 5 — [{idx}/{total_papers}] \"{paper['title'][:60]}\" → "
                f"{score:.0f}% match {status}",
                flush=True,
            )
            paper["match_score"] = score
        except Exception as e:
            logger.warning("Error scoring paper %r: %s", paper.get("title"), e)
            paper["match_score"] = 0.0
        return paper

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(score_single_paper_task, i + 1, paper) for i, paper in enumerate(candidate_pool)]
        for future in concurrent.futures.as_completed(futures):
            try:
                scored_papers.append(future.result())
            except Exception as e:
                logger.error("Scoring worker encountered error: %s", e)

    # Sort in descending order of relevance score
    scored_papers.sort(key=lambda x: x.get("match_score", 0.0), reverse=True)

    # Filter by strict > 75% threshold
    matched_papers = [p for p in scored_papers if p.get("match_score", 0.0) > _MIN_MATCH_THRESHOLD]

    # Select the Top 15
    matched_papers = matched_papers[:15]

    local_count = sum(1 for p in matched_papers if p.get("source") in ("local_session", "local_fulltext"))
    external_count = len(matched_papers) - local_count

    # Extract top scoring percentages to print in stdout
    top_scores_str = ", ".join(f"\"{p['title'][:25]}...\" ({p['match_score']:.0f}%)" for p in matched_papers[:5])
    print(
        f"PROGRESS: Phase 5 — final pool curated: {len(matched_papers)} papers "
        f"({local_count} local + {external_count} external) filtered > 75% match.",
        flush=True,
    )
    if matched_papers:
        print(f"PROGRESS: Phase 5 — top matches: {top_scores_str}", flush=True)

    if len(matched_papers) < 10:
        print(
            f"PROGRESS: Phase 5 — ⚠ warning: only {len(matched_papers)} papers matched "
            f"the 75% threshold (minimum 10 requested). Proceeding with available papers.",
            flush=True,
        )

    if not matched_papers:
        return {
            "status": "error",
            "message": (
                "No papers scored > 75% relevance. The project idea may be too "
                "different from the session's research domain. Try a more specific "
                "idea or run new research first."
            ),
        }

    # ── Step 5: Synthesize proposal ──────────────────────────────────────
    from application.ProposalSynthesizer import synthesize_proposal

    try:
        proposal_md = synthesize_proposal(
            scoped_idea=scoped_query,
            matched_papers=matched_papers,
            gap_analysis=gap_analysis,
        )
    except RuntimeError as exc:
        return {
            "status": "error",
            "message": f"Proposal synthesis failed: {exc}",
        }

    # ── Step 6: Persist the proposal ─────────────────────────────────────
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    proposals_dir = session_dir / "proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)

    proposal_filename = f"proposal_{ts}.md"
    proposal_path = proposals_dir / proposal_filename
    proposal_path.write_text(proposal_md, encoding="utf-8")

    # Write proposal manifest
    proposal_id = f"proposal_{ts}"
    manifest_data = {
        "proposal_id": proposal_id,
        "session_id": session_dir.name,
        "user_idea": user_project_idea.strip(),
        "scoped_query": scoped_query,
        "matched_paper_count": len(matched_papers),
        "matched_papers": [
            {
                "title": p.get("title", ""),
                "match_score": p.get("match_score", 0),
                "source": p.get("source", ""),
                "domain_alignment": p.get("domain_alignment", 0),
                "task_alignment": p.get("task_alignment", 0),
                "method_relevance": p.get("method_relevance", 0),
                "reasoning": p.get("reasoning", ""),
            }
            for p in matched_papers
        ],
        "created_at": ts,
        "proposal_file": proposal_filename,
    }

    manifest_path = proposals_dir / f"{proposal_id}_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # Write matched_papers.json for the export pipeline
    matched_papers_path = proposals_dir / f"{proposal_id}_matched_papers.json"
    matched_papers_path.write_text(
        json.dumps(matched_papers, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(
        f"PROGRESS: Phase 5 — ✓ proposal saved to proposals/{proposal_filename}",
        flush=True,
    )

    return {
        "status": "success",
        "message": "Proposal generated successfully.",
        "proposal_path": str(proposal_path.resolve()),
        "matched_papers_path": str(matched_papers_path.resolve()),
        "session_id": session_dir.name,
        "scoped_query": scoped_query,
        "matched_paper_count": len(matched_papers),
        "proposal_id": proposal_id,
    }
