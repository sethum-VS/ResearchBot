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
from infrastructure.AcademicScraper import search_academic_papers
from infrastructure.FileStorage import (
    create_session_dir,
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
    "Extract every title and its associated URL. Format the output strictly as a "
    "simple list of 'Title [URL]'. Return ONLY the list, with no preamble or explanation."
)


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


def _parse_url_extraction_response(response) -> list[str]:
    """Turn Gemini response into a list of 'Title [URL]' lines."""
    if not response or not response.text:
        return []
    return [line.strip() for line in response.text.strip().split("\n") if line.strip()]


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


async def _extract_high_value_urls(refined_text: str) -> list[str]:
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
        return _parse_url_extraction_response(response)
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
                return lines
        except Exception as region_exc:
            print(f"PROGRESS: Phase 2.6 — region {region} failed: {region_exc}", flush=True)
            last_exc = region_exc
            continue

    logger.error(
        "Failed to extract URLs via LLM after all regions. Last error: %s",
        last_exc,
    )
    return []


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
        url_match = re.search(r'\[(https?://[^\]]+)\]', entry)
        title_match = re.search(r'^(.+?)\s*\[', entry)

        if not url_match:
            logger.warning("Phase 2.6: Could not parse URL from entry: %s", entry)
            return

        hv_url = url_match.group(1)
        hv_title = title_match.group(1).strip() if title_match else f"Secondary_Crawl_{index}"

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

    with ThreadPoolExecutor(max_workers=_PHASE2_WORKERS, thread_name_prefix="phase2") as pool:
        future_social = pool.submit(search_social_threads, primary_keyword)
        future_academic = pool.submit(search_academic_papers, primary_keyword)
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
                    academic_md = result
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

    path = save_markdown("agent_scrapes", primary_keyword, academic_md, session_dir=session_dir)
    saved_files.append(str(path))
    current_run_files.append(path.resolve())

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
    high_value_urls = asyncio.run(_extract_high_value_urls(refined_data))
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
    if graphify_error is None and graph_path_abs.is_file():
        graph_path_str = str(graph_path_abs.resolve())

    if graphify_error:
        print(f"PROGRESS: Phase 4 — ✗ graphify error: {graphify_error}", flush=True)
    else:
        print("PROGRESS: Phase 4 — ✓ knowledge graph generated.", flush=True)

    # ── Phase 4.5: Academic Graph Topology Analysis ──────────────────────
    if graphify_error is None:
        academic_gap_analysis = analyze_graph_topology(
            current_run_files=current_run_files,
            graph_json_path=graph_json_abs if graph_json_abs.is_file() else None,
        )
    else:
        academic_gap_analysis = {
            "summary": "Academic gap analysis requires a successful knowledge graph.",
            "structural_holes": [],
            "high_degree_limitations": [],
            "orphaned_solutions": [],
            "source_files": [],
            "error": graphify_error,
        }

    # Persist gap analysis next to the graph for HistoryView re-rendering.
    try:
        import json as _json
        (session_dir / "academic_gap_analysis.json").write_text(
            _json.dumps(academic_gap_analysis, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("Could not write academic_gap_analysis.json: %s", e)

    if graphify_error is None:
        phase_label = "Phase 4.5 — Academic Gap Analysis Complete"
        message = "Research pipeline completed successfully."
    else:
        phase_label = "Phase 4 — Knowledge Graph failed"
        message = (
            "Research pipeline finished; knowledge graph was not generated. "
            f"{graphify_error}"
        )

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
