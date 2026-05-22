"""
IngestSeedUseCase.py — Full pipeline orchestrator (Phases 1.5 → 4.5).

Wires InputAnalyzer → Phase 2 scrapers → Phase 2.5 refiner → Phase 2.6
recursive URL refinement → Phase 3 storage → Phase 4 graphify → Phase 4.5
gap analysis into a single, high-concurrency pipeline invoked from main.py.

WORKSPACE RUN ISOLATION CONTRACT
────────────────────────────────
Every invocation of ``execute()`` immediately allocates a dedicated, timestamped
session directory:

    research_knowledge_base/runs/session_<UTC_TIMESTAMP>_<slug>/
        ├── agent_scrapes/
        ├── raw_ingestion/
        ├── processed_summaries/
        └── graphify-out/

That absolute path is treated as an immutable execution-environment string and
is threaded down to every save_markdown / Graphify subprocess in this run.
Phase 2 scrapers, Phase 2.6 URLRefiners, Phase 3 synthesizers, and Phase 4
artefacts therefore CANNOT bleed into any prior or future run. Historical
sessions are preserved on disk forever and surfaced in the SwiftUI HistoryView.

SESSION ISOLATION CONTRACT (synthesis context)
──────────────────────────────────────────────
The ``full_context`` variable passed to AgentSynthesizer is STRICTLY constructed
from three in-memory variables produced in the CURRENT execution run:

    1. core_context  — Phase 1.5 InputAnalyzer (Gemini Flash parsed schema)
    2. user_intent   — Phase 1.5 InputAnalyzer (Gemini Flash parsed schema)
    3. refined_data  — Phase 2.5 DataRefiner   (direct string output, current run)

NO raw scraped Markdown strings (web_md, social_md, wiki_md, academic_md,
deep_crawl_md) and NO file system reads of legacy shared folders may enter
full_context.

CONCURRENCY MODEL
─────────────────
Phase 2   — ThreadPoolExecutor (3 workers): Social, Academic, Wiki in parallel.
             Firecrawl runs first (sequential) since it seeds the URL list.
Phase 2.6 — ThreadPoolExecutor (max_workers=5): URLs crawled + refined
             simultaneously with a threading.Lock on current_run_files.
"""

import os
import asyncio
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from google import genai
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

from application.InputAnalyzer import analyze_seed
from application.AgentSynthesizer import synthesize_context
from application.DataRefiner import refine_scraped_data
from application.GraphAnalyzer import analyze_graph_topology
from infrastructure.WebScraper import firecrawl_advanced_search, deep_crawl_urls
from infrastructure.SocialScraper import search_social_threads
from infrastructure.WikiAPI import get_wiki_summary
from infrastructure.AcademicScraper import (
    AcademicSearchResult,
    format_fulltext_artifact_markdown,
    normalize_semanticscholar_crawl_url,
    s2_paper_ids_from_academic_markdown,
    search_academic_papers,
)
from infrastructure.FileStorage import (
    create_session_dir,
    ensure_session_structure,
    save_markdown,
    get_kb_root,
    session_id_from_path,
)
from infrastructure.GraphifyRunner import run_graphify, GraphifyError

logger = logging.getLogger(__name__)


# ── Concurrency Tuning ──────────────────────────────────────────────────────
_PHASE2_WORKERS = 3        # Social, Academic, Wiki in parallel
_PHASE26_MAX_WORKERS = 5   # Cap for recursive URL refinement

_STABLE_REGIONS: list[str] = [
    "europe-west4",
    "us-east4",
    "asia-northeast1",
    "us-central1",
]

_URL_EXTRACT_PROMPT = (
    "You are a data extraction specialist. Look at the provided research summary "
    "and find the section regarding 'High-Value URLs for Next Crawl Phase'. "
    "Extract every title and its associated URL. Format the output strictly as one "
    "entry per line: Title [https://full-url]. "
    "Do NOT use section headings, bullet categories, markdown links, or bare URLs "
    "without the bracketed https URL. "
    "Example line: Google ADK Code Review Codelab [https://codelabs.developers.google.com/adk-code-reviewer-assistant/instructions]\n"
    "Return ONLY the list, with no preamble or explanation."
)

_URL_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")
_URL_BRACKET_RE = re.compile(r"\[(https?://[^\]\s]+)\]")
_BARE_URL_LINE_RE = re.compile(r"^\s*(https?://\S+)\s*$", re.MULTILINE)
_SECTION_HEADER_RE = re.compile(r"^[A-Za-z0-9][^\n]{0,120}:\s*$")


def _ensure_all_saved_md_in_run_queue(
    saved_files: list[str],
    current_run_files: list[Path],
) -> None:
    """Register every .md from this run so Phase 4 / 4.5 see the full corpus."""
    seen = {p.resolve() for p in current_run_files}
    for entry in saved_files:
        try:
            p = Path(entry).resolve()
        except (TypeError, OSError, ValueError):
            continue
        if p.suffix.lower() != ".md" or not p.is_file():
            continue
        if p not in seen:
            current_run_files.append(p)
            seen.add(p)


def _is_resource_exhausted(exc: Exception) -> bool:
    """Check if the exception is a 429 ResourceExhausted."""
    exc_str = str(exc).lower()
    return "429" in exc_str or "resourceexhausted" in exc_str or "resource_exhausted" in exc_str


def _title_from_url(url: str) -> str:
    """Derive a short crawl title from a URL path or host."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = (parsed.netloc or "source").replace("www.", "")
    path = (parsed.path or "").strip("/").split("/")[-1] or host
    slug = re.sub(r"[^\w\s-]", "", path.replace("-", " ").replace("_", " ")).strip()
    return slug[:60] if slug else host.replace(".", "_")


def _parse_url_entry(entry: str) -> tuple[str, str] | None:
    """
    Parse one high-value URL line into (title, url).
    Supports Title [URL], markdown links, and bare https lines.
    """
    line = entry.strip()
    if not line:
        return None
    if _SECTION_HEADER_RE.match(line) and "http" not in line.lower():
        return None

    md = _URL_MARKDOWN_LINK_RE.search(line)
    if md:
        title, url = md.group(1).strip(), md.group(2).strip()
        if title and url and not title.endswith(":"):
            return title, url

    bracket = _URL_BRACKET_RE.search(line)
    if bracket:
        url = bracket.group(1).strip()
        title_m = re.search(r"^[-*\d.\s]*(.+?)\s*\[https?://", line)
        title = title_m.group(1).strip() if title_m else _title_from_url(url)
        if title.endswith(":"):
            title = _title_from_url(url)
        return title, url

    bare = _BARE_URL_LINE_RE.match(line)
    if bare:
        url = bare.group(1).strip().rstrip(".,;)")
        return _title_from_url(url), url

    return None


def _format_url_entry(title: str, url: str) -> str:
    return f"{title} [{url}]"


def _dedupe_url_entries(
    entries: list[str],
    s2_paper_ids: set[str] | None = None,
) -> list[str]:
    """Keep first occurrence per URL; preserve Title [URL] format."""
    seen: set[str] = set()
    out: list[str] = []
    for entry in entries:
        parsed = _parse_url_entry(entry)
        if not parsed:
            continue
        title, url = parsed
        url = normalize_semanticscholar_crawl_url(url, s2_paper_ids) or ""
        if not url:
            continue
        key = url.rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(_format_url_entry(title, url))
    return out


def _extract_urls_from_refined_section(refined_text: str) -> list[str]:
    """Regex fallback when Flash returns section headers or bare URLs."""
    if not refined_text or not refined_text.strip():
        return []

    lower = refined_text.lower()
    marker = "high-value urls"
    idx = lower.find(marker)
    section = refined_text[idx:] if idx >= 0 else refined_text

    entries: list[str] = []
    for title, url in _URL_MARKDOWN_LINK_RE.findall(section):
        title = title.strip()
        if title and not title.endswith(":"):
            entries.append(_format_url_entry(title, url))

    for match in _URL_BRACKET_RE.finditer(section):
        url = match.group(1).strip()
        line_start = section.rfind("\n", 0, match.start()) + 1
        line_end = section.find("\n", match.end())
        if line_end == -1:
            line_end = len(section)
        line = section[line_start:line_end].strip()
        parsed = _parse_url_entry(line)
        if parsed:
            entries.append(_format_url_entry(*parsed))

    for bare in _BARE_URL_LINE_RE.findall(section):
        url = bare.strip().rstrip(".,;)")
        entries.append(_format_url_entry(_title_from_url(url), url))

    return entries


def _parse_url_extraction_response(response) -> list[str]:
    """Turn Gemini response into normalized 'Title [URL]' lines."""
    if not response or not response.text:
        return []
    lines = [line.strip() for line in response.text.strip().split("\n") if line.strip()]
    return _dedupe_url_entries(lines)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    retry=retry_if_exception(_is_resource_exhausted),
)
async def _call_flash_async(client: genai.Client, contents: str):
    """Gemini 2.5 Flash call with tenacity retry on 429 only."""
    return await client.aio.models.generate_content(
        model="gemini-2.5-flash",
        contents=contents,
    )


async def _extract_high_value_urls(
    refined_text: str,
    s2_paper_ids: set[str] | None = None,
) -> list[str]:
    """
    Extract high-value URLs for next crawl using Gemini 2.5 Flash.
    Global → STABLE_REGIONS failover, identical to DataRefiner pattern.
    """
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT_ID")
    if not project_id:
        logger.warning("GOOGLE_CLOUD_PROJECT_ID not set. Cannot extract URLs.")
        return []

    if not refined_text or not refined_text.strip():
        return []

    contents = f"{_URL_EXTRACT_PROMPT}\n\n{refined_text}"
    last_exc: Exception | None = None

    try:
        client = genai.Client(vertexai=True, project=project_id, location="global")
        response = await _call_flash_async(client, contents)
        entries = _parse_url_extraction_response(response)
        return _merge_high_value_url_entries(entries, refined_text, s2_paper_ids)
    except Exception as primary_exc:
        print(
            f"PROGRESS: Phase 2.6 — global endpoint failed ({primary_exc}). "
            "Attempting regional failover...",
            flush=True,
        )
        last_exc = primary_exc

    for region in _STABLE_REGIONS:
        try:
            print(f"PROGRESS: Phase 2.6 — regional failover → {region}", flush=True)
            client = genai.Client(vertexai=True, project=project_id, location=region)
            response = await _call_flash_async(client, contents)
            lines = _parse_url_extraction_response(response)
            if lines:
                return _merge_high_value_url_entries(lines, refined_text, s2_paper_ids)
        except Exception as region_exc:
            print(f"PROGRESS: Phase 2.6 — region {region} failed: {region_exc}", flush=True)
            last_exc = region_exc
            continue

    logger.error(
        "Failed to extract URLs via LLM after all regions. Last error: %s",
        last_exc,
    )
    fallback = _extract_urls_from_refined_section(refined_text)
    if fallback:
        print(
            f"PROGRESS: Phase 2.6 — LLM extraction failed; using {len(fallback)} "
            f"URL(s) from refined Markdown fallback.",
            flush=True,
        )
        return _dedupe_url_entries(fallback, s2_paper_ids)
    return []


def _merge_high_value_url_entries(
    llm_entries: list[str],
    refined_text: str,
    s2_paper_ids: set[str] | None = None,
) -> list[str]:
    """Combine Flash output with regex fallback; prefer parseable entries."""
    fallback = _extract_urls_from_refined_section(refined_text)
    merged = _dedupe_url_entries(llm_entries + fallback, s2_paper_ids)
    llm_ok = sum(1 for e in llm_entries if _parse_url_entry(e))
    if llm_entries and llm_ok < max(3, len(llm_entries) // 2):
        print(
            f"PROGRESS: Phase 2.6 — Flash returned {len(llm_entries)} lines but only "
            f"{llm_ok} parseable; merged with {len(fallback)} regex fallback URL(s) "
            f"→ {len(merged)} total.",
            flush=True,
        )
    return merged


def _refine_single_url(
    index: int,
    entry: str,
    primary_keyword: str,
    saved_files: list[str],
    current_run_files: list[Path],
    lock: threading.Lock,
    session_dir: Path,
) -> None:
    """
    Phase 2.6 worker: scrape one high-value URL, refine it, and thread-safely
    append to the session-scoped output queues.

    Every artifact is written *exclusively* into ``session_dir/agent_scrapes/``.
    """
    try:
        parsed = _parse_url_entry(entry)
        if not parsed:
            logger.warning("Phase 2.6: Could not parse URL from entry: %s", entry)
            return

        hv_title, hv_url = parsed

        clean_title = re.sub(r'[^\w\s-]', '', hv_title).strip().replace(' ', '_')
        if not clean_title:
            clean_title = f"Secondary_Crawl_{index}"

        print(f"PROGRESS: Phase 2.6 — crawling [{index}] {hv_url}", flush=True)

        url_raw_md = deep_crawl_urls([hv_url])
        if not url_raw_md or not url_raw_md.strip():
            print(f"PROGRESS: Phase 2.6 — [{index}] empty scrape, skipped.", flush=True)
            return

        _ANTIBOT_INDICATORS = (
            "document_antibot",
            "internal server error",
            "scrape failed",
            "access denied",
            "403 forbidden",
            "captcha",
            "just a moment",
            "checking your browser",
        )
        lowered_payload = url_raw_md.lower()
        if any(indicator in lowered_payload for indicator in _ANTIBOT_INDICATORS):
            print(f"[Crawl Skipped] Anti-bot detected for URL: {hv_url}", flush=True)
            return

        print(f"PROGRESS: Phase 2.6 — refining [{index}] {clean_title}", flush=True)

        try:
            url_refined = refine_scraped_data(url_raw_md)
        except RuntimeError as refine_err:
            print(
                f"[Crawl Failed] Skipping file generation for URL "
                f"due to API exhaustion: {hv_url} — {refine_err}",
                flush=True,
            )
            return

        topic_with_suffix = f"{primary_keyword}_{clean_title}_URLRefiner"
        path = save_markdown(
            "agent_scrapes",
            topic_with_suffix,
            url_refined,
            session_dir=session_dir,
        )
        resolved_path = path.resolve()

        with lock:
            saved_files.append(str(resolved_path))
            current_run_files.append(resolved_path)

        print(f"PROGRESS: Phase 2.6 — [{index}] ✓ saved {path.name}", flush=True)

    except Exception as e:
        logger.warning("Phase 2.6: failed to process %s — %s", entry, e)
        print(f"PROGRESS: Phase 2.6 — [{index}] ✗ error: {e}", flush=True)


def execute(idea: str, url: str) -> dict:
    """
    Run the full research pipeline inside a freshly allocated, isolated
    session directory.  All artefacts written this run live exclusively under
    ``research_knowledge_base/runs/session_<TIMESTAMP>_<slug>/``.
    """
    if not idea.strip():
        return {
            "status": "error",
            "code": 1,
            "message": "Idea cannot be empty. Provide a research topic via --idea.",
        }

    raw_seed = idea.strip()
    if url.strip():
        raw_seed += f"\n\nReference URL: {url.strip()}"

    saved_files: list[str] = []
    current_run_files: list[Path] = []
    lock = threading.Lock()

    # ── Workspace Run Isolation: allocate a dedicated session directory ──
    # The path is pinned into RESEARCHBOT_SESSION_DIR (immutable for this
    # process) so every helper — including subprocess-spawned post-processing
    # — can recover it without re-plumbing every signature.
    session_dir = create_session_dir(idea)
    session_id = session_id_from_path(session_dir)
    print(
        f"PROGRESS: Session — workspace allocated at {session_dir}",
        flush=True,
    )

    # ── Phase 1.5: AI Pre-processing ─────────────────────────────────────
    print("PROGRESS: Phase 1.5 — analyzing seed input...", flush=True)
    seed_analysis = analyze_seed(raw_seed)

    core_context: str = seed_analysis.get("core_context", raw_seed)
    search_keywords: list = seed_analysis.get("search_keywords", [raw_seed])
    extracted_urls: list = seed_analysis.get("extracted_urls", [])
    user_intent: str = seed_analysis.get("user_intent", "General Inquiry")

    if url.strip() and url.strip() not in extracted_urls:
        extracted_urls.insert(0, url.strip())

    primary_keyword = search_keywords[0] if search_keywords else raw_seed
    print("PROGRESS: Phase 1.5 — ✓ complete.", flush=True)

    # ── Phase 2: Context Expansion (CONCURRENT) ──────────────────────────
    print("PROGRESS: Phase 2 — running Firecrawl advanced search...", flush=True)
    web_md: str = firecrawl_advanced_search(search_keywords, extracted_urls)
    path = save_markdown("agent_scrapes", primary_keyword, web_md, session_dir=session_dir)
    saved_files.append(str(path))
    current_run_files.append(path.resolve())
    print("PROGRESS: Phase 2 — ✓ Firecrawl complete.", flush=True)

    print("PROGRESS: Phase 2 — launching parallel scrapers (Social, Academic, Wiki)...", flush=True)

    social_md: str = ""
    wiki_md: str = ""
    academic_md: str = ""
    academic_fulltext_artifacts: list = []

    with ThreadPoolExecutor(max_workers=_PHASE2_WORKERS, thread_name_prefix="phase2") as pool:
        future_social = pool.submit(search_social_threads, primary_keyword)
        future_academic = pool.submit(search_academic_papers, search_keywords)
        future_wiki = pool.submit(get_wiki_summary, primary_keyword)

        futures_map = {
            future_social: "Social",
            future_academic: "Academic",
            future_wiki: "Wiki",
        }

        for future in as_completed(futures_map):
            label = futures_map[future]
            try:
                result = future.result()
                if label == "Social":
                    social_md = result
                elif label == "Academic":
                    if isinstance(result, AcademicSearchResult):
                        academic_md = result.markdown
                        academic_fulltext_artifacts = result.fulltext_artifacts
                    else:
                        academic_md = result or ""
                else:
                    wiki_md = result
                print(f"PROGRESS: Phase 2 — ✓ {label} scraper complete.", flush=True)
            except Exception as e:
                logger.error("Phase 2 %s scraper failed: %s", label, e)
                print(f"PROGRESS: Phase 2 — ✗ {label} scraper error: {e}", flush=True)

    path = save_markdown("raw_ingestion", primary_keyword, social_md, session_dir=session_dir)
    saved_files.append(str(path))
    current_run_files.append(path.resolve())

    path = save_markdown("agent_scrapes", primary_keyword, wiki_md, session_dir=session_dir)
    saved_files.append(str(path))
    current_run_files.append(path.resolve())

    ensure_session_structure(session_dir)
    academic_scrape_path = session_dir / "agent_scrapes" / "academic_scrape.md"
    academic_scrape_path.write_text(
        academic_md or "# No academic papers found.\n",
        encoding="utf-8",
    )
    saved_files.append(str(academic_scrape_path))
    current_run_files.append(academic_scrape_path.resolve())

    if academic_fulltext_artifacts:
        fulltext_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        summaries_dir = session_dir / "processed_summaries"
        summaries_dir.mkdir(parents=True, exist_ok=True)
        print(
            f"PROGRESS: Phase 2.2 — saving {len(academic_fulltext_artifacts)} full-text "
            f"papers to processed_summaries/ (DataRefiner bypass)...",
            flush=True,
        )
        for artifact in academic_fulltext_artifacts:
            safe_id = re.sub(r"[^\w.-]", "_", artifact.triage_id).strip("_")[:48]
            fname = f"academic_fulltext_{safe_id}_{fulltext_ts}.md"
            ft_path = summaries_dir / fname
            ft_path.write_text(
                format_fulltext_artifact_markdown(artifact),
                encoding="utf-8",
            )
            resolved = ft_path.resolve()
            saved_files.append(str(resolved))
            current_run_files.append(resolved)
            print(f"PROGRESS: Phase 2.2 — saved {fname}", flush=True)

    print("PROGRESS: Phase 2 — ✓ all scrapers complete.", flush=True)

    # ── Phase 2.5: Deep Crawl + Noise Refinement ─────────────────────────
    print("PROGRESS: Phase 2.5 — deep crawling discovered URLs...", flush=True)
    discovered_urls: list = list(extracted_urls)

    deep_crawl_md: str = deep_crawl_urls(discovered_urls)
    if deep_crawl_md:
        path = save_markdown(
            "agent_scrapes", primary_keyword, deep_crawl_md, session_dir=session_dir,
        )
        saved_files.append(str(path))
        current_run_files.append(path.resolve())

    raw_corpus: str = "\n\n---\n\n".join(
        p for p in [web_md, social_md, wiki_md, academic_md, deep_crawl_md] if p
    )

    print("PROGRESS: Phase 2.5 — refining raw corpus via Gemini 2.5 Pro...", flush=True)
    try:
        refined_data: str = refine_scraped_data(raw_corpus)
    except RuntimeError as refine_err:
        print(
            f"[Refinement Failed] Primary refinement exhausted all regions: {refine_err}",
            flush=True,
        )
        logger.error("Phase 2.5: DataRefiner exhausted all regions — %s", refine_err)
        refined_data = ""

    if refined_data and refined_data.strip():
        path = save_markdown(
            "agent_scrapes", primary_keyword, refined_data, session_dir=session_dir,
        )
        saved_files.append(str(path))
        current_run_files.append(path.resolve())
        print("PROGRESS: Phase 2.5 — ✓ refinement complete.", flush=True)
    else:
        print("PROGRESS: Phase 2.5 — ⚠ refinement skipped (API exhaustion).", flush=True)

    # ── Phase 2.6: Recursive URL Extraction & Per-URL Refinement ─────────
    print("PROGRESS: Phase 2.6 — extracting high-value URLs...", flush=True)
    s2_paper_ids = s2_paper_ids_from_academic_markdown(academic_md)
    high_value_urls = asyncio.run(
        _extract_high_value_urls(refined_data, s2_paper_ids),
    )
    print(
        f"PROGRESS: Phase 2.6 — found {len(high_value_urls)} URLs for concurrent crawling.",
        flush=True,
    )

    if high_value_urls:
        with ThreadPoolExecutor(
            max_workers=_PHASE26_MAX_WORKERS,
            thread_name_prefix="phase26",
        ) as pool:
            futures = {
                pool.submit(
                    _refine_single_url,
                    i,
                    entry,
                    primary_keyword,
                    saved_files,
                    current_run_files,
                    lock,
                    session_dir,
                ): i
                for i, entry in enumerate(high_value_urls)
            }

            for future in as_completed(futures):
                idx = futures[future]
                try:
                    future.result()
                except Exception as e:
                    logger.warning("Phase 2.6 worker [%d] raised: %s", idx, e)

        print(
            f"PROGRESS: Phase 2.6 — ✓ all {len(high_value_urls)} URLs processed.",
            flush=True,
        )
    else:
        print("PROGRESS: Phase 2.6 — no URLs to process, skipping.", flush=True)

    # ── Phase 3: Synthesis & Storage ─────────────────────────────────────
    print("PROGRESS: Phase 3 — synthesizing research context...", flush=True)
    full_context: str = (
        f"## Core Context (from AI pre-processing)\n{core_context}\n\n"
        f"## User Intent\n{user_intent}\n\n"
        f"---\n\n"
        f"## Refined Research Data (current session)\n{refined_data}"
    )

    synthesis: str = ""
    try:
        synthesis = synthesize_context(full_context)
        path = save_markdown(
            "processed_summaries", primary_keyword, synthesis, session_dir=session_dir,
        )
        saved_files.append(str(path))
        current_run_files.append(path.resolve())
        print("PROGRESS: Phase 3 — ✓ synthesis saved.", flush=True)
    except RuntimeError as syn_err:
        print(f"PROGRESS: Phase 3 — ⚠ synthesis skipped: {syn_err}", flush=True)
        logger.error("Phase 3: AgentSynthesizer exhausted — %s", syn_err)

    _ensure_all_saved_md_in_run_queue(saved_files, current_run_files)
    print(
        f"PROGRESS: Session corpus — {len(current_run_files)} Markdown files "
        f"queued for graph + gap analysis.",
        flush=True,
    )

    # ── Persist a lightweight session manifest for HistoryView ───────────
    try:
        import json as _json
        manifest = {
            "session_id": session_id,
            "topic": idea.strip(),
            "primary_keyword": primary_keyword,
            "user_intent": user_intent,
            "saved_files": saved_files,
            "url_refiner_count": sum(
                1 for p in current_run_files if "_urlrefiner" in p.name.lower()
            ),
            "created_at": session_id.replace("session_", "").split("_")[0],
        }
        (session_dir / "session_manifest.json").write_text(
            _json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("Could not write session_manifest.json: %s", e)

    # ── Phase 4: Knowledge Graph Generation ──────────────────────────────
    print("PROGRESS: Phase 4 — generating knowledge graph...", flush=True)
    graphify_output = ""
    graphify_error = None
    try:
        graphify_output = run_graphify(current_run_files, session_dir=session_dir)
    except GraphifyError as e:
        graphify_error = str(e)
    except FileNotFoundError as e:
        graphify_error = str(e)

    graph_path_abs = session_dir / "graphify-out" / "graph.html"
    graph_json_abs = session_dir / "graphify-out" / "graph.json"
    graph_path_str: str | None = None
    if graph_path_abs.is_file():
        graph_path_str = str(graph_path_abs.resolve())
    elif graph_json_abs.is_file() and graphify_error:
        # Partial salvage: graph.json without html still enables HistoryView graph reload.
        graph_path_str = str(graph_json_abs.resolve())

    if graphify_error:
        print(f"PROGRESS: Phase 4 — ✗ graphify error: {graphify_error}", flush=True)
    else:
        print("PROGRESS: Phase 4 — ✓ knowledge graph generated.", flush=True)

    # ── Phase 4.5: Academic Graph Topology Analysis ──────────────────────
    # Run even when Graphify fails or returns a sparse graph — corpus Map-Reduce
    # still delivers value; topology falls back to document-derived nodes.
    academic_gap_analysis = analyze_graph_topology(
        current_run_files=current_run_files,
        graph_json_path=graph_json_abs if graph_json_abs.is_file() else None,
        graphify_error=graphify_error,
    )

    # Persist gap analysis next to the graph for HistoryView re-rendering.
    try:
        import json as _json
        (session_dir / "academic_gap_analysis.json").write_text(
            _json.dumps(academic_gap_analysis, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("Could not write academic_gap_analysis.json: %s", e)

    gap_error = academic_gap_analysis.get("error")
    if graphify_error is None and not gap_error:
        phase_label = "Phase 4.5 — Academic Gap Analysis Complete"
        message = "Research pipeline completed successfully."
    elif graphify_error and academic_gap_analysis.get("structural_holes"):
        phase_label = "Phase 4.5 — Academic Gap Analysis Complete (degraded graph)"
        message = (
            "Research pipeline finished; knowledge graph was degraded or missing "
            "but gap analysis completed using document topology fallback."
        )
    elif graphify_error:
        phase_label = "Phase 4 — Knowledge Graph failed"
        message = (
            "Research pipeline finished; knowledge graph was not generated. "
            f"{graphify_error}"
        )
    else:
        phase_label = "Phase 4.5 — Academic Gap Analysis Complete"
        message = "Research pipeline completed with warnings."

    return {
        "status": "success",
        "message": message,
        "graph_path": graph_path_str,
        "kb_root": str(get_kb_root().resolve()),
        "session_id": session_id,
        "session_path": str(session_dir.resolve()),
        "phase": phase_label,
        "seed_analysis": {
            "core_context": core_context,
            "search_keywords": search_keywords,
            "extracted_urls": extracted_urls,
            "user_intent": user_intent,
        },
        "saved_files": saved_files,
        "synthesis_preview": synthesis[:500] if synthesis else "",
        "graphify": {
            "ran": graphify_error is None,
            "stdout": graphify_output[:1000] if graphify_output else "",
            "error": graphify_error,
        },
        "academic_gap_analysis": academic_gap_analysis,
    }
