"""
IngestSeedUseCase.py — Full pipeline orchestrator (Phases 1.5 → 4).
Wires InputAnalyzer → Phase 2 scrapers → Phase 2.5 refiner → Phase 2.6
recursive URL refinement → Phase 3 storage → Phase 4 graphify into a
single, sequential pipeline invoked from main.py.

SESSION ISOLATION CONTRACT
──────────────────────────
The `full_context` variable passed to AgentSynthesizer.synthesize() is STRICTLY
constructed from three in-memory variables produced in the CURRENT execution run:

    1. core_context  — from Phase 1.5 InputAnalyzer (Gemini Flash parsed schema)
    2. user_intent   — from Phase 1.5 InputAnalyzer (Gemini Flash parsed schema)
    3. refined_data  — from Phase 2.5 DataRefiner (direct string output, current run)

NO raw scraped Markdown strings (web_md, social_md, wiki_md, academic_md,
deep_crawl_md), NO file system reads from /research_knowledge_base, /agent_scrapes,
/raw_ingestion, or /processed_summaries directories are permitted to enter
full_context. Those variables are written to disk for Graphify but MUST NOT
be re-appended into the synthesis context.
"""

import logging
import re
from pathlib import Path

from application.InputAnalyzer import analyze_seed
from application.AgentSynthesizer import synthesize_context
from application.DataRefiner import refine_scraped_data
from infrastructure.WebScraper import firecrawl_advanced_search, deep_crawl_urls
from infrastructure.SocialScraper import search_social_threads
from infrastructure.WikiAPI import get_wiki_summary
from infrastructure.AcademicScraper import search_academic_papers
from infrastructure.FileStorage import save_markdown, ensure_structure, get_kb_root
from infrastructure.GraphifyRunner import run_graphify, GraphifyError

logger = logging.getLogger(__name__)

# ── Regex for extracting URLs from the refiner's "High-Value URLs" section ───

_HIGH_VALUE_URL_PATTERN = re.compile(
    r"###\s*5\.0\s+High-Value URLs for Next Crawl.*?(?=\n###|\Z)",
    re.DOTALL | re.IGNORECASE,
)
_URL_EXTRACTOR = re.compile(r"https?://[^\s\)\]\>\"]+")


def _extract_high_value_urls(refined_text: str) -> list[str]:
    """
    Parse the '### 5.0 High-Value URLs for Next Crawl' section from
    the DataRefiner output and return a deduplicated list of URLs.
    """
    match = _HIGH_VALUE_URL_PATTERN.search(refined_text)
    if not match:
        return []

    section = match.group(0)
    urls = _URL_EXTRACTOR.findall(section)
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return unique


def execute(idea: str, url: str) -> dict:
    """
    Run the full research pipeline:
      Phase 1.5  — AI pre-processing (Gemini Flash)
      Phase 2    — scrape & expand (using optimised keywords)
      Phase 2.5  — deep crawl + noise refinement (Gemini 2.5 Pro)
      Phase 2.6  — recursive URL extraction & per-URL refinement
      Phase 3    — synthesize & store to knowledge base
      Phase 4    — trigger graphify (Llama 4 Scout 10M context)

    The AgentSynthesizer receives ONLY three in-memory variables from the
    current run.  No disk reads are performed at the synthesis stage.
    """
    if not idea.strip():
        return {
            "status": "error",
            "code": 1,
            "message": "Idea cannot be empty. Provide a research topic via --idea.",
        }

    raw_seed = idea.strip()
    # Append CLI URL into the raw seed so Flash can extract it too
    if url.strip():
        raw_seed += f"\n\nReference URL: {url.strip()}"

    saved_files: list[str] = []
    current_run_files: list[Path] = []
    ensure_structure()

    # ── Phase 1.5: AI Pre-processing ─────────────────────────────────────
    # Produces: core_context, search_keywords, extracted_urls, user_intent
    # These are in-memory variables for this run only.
    seed_analysis = analyze_seed(raw_seed)

    core_context: str = seed_analysis.get("core_context", raw_seed)
    search_keywords: list = seed_analysis.get("search_keywords", [raw_seed])
    extracted_urls: list = seed_analysis.get("extracted_urls", [])
    user_intent: str = seed_analysis.get("user_intent", "General Inquiry")

    # If the user explicitly passed a --url, ensure it's in the list
    if url.strip() and url.strip() not in extracted_urls:
        extracted_urls.insert(0, url.strip())

    # Use the first optimised keyword for downstream scrapers
    primary_keyword = search_keywords[0] if search_keywords else raw_seed

    # ── Phase 2: Context Expansion (keyword-driven) ──────────────────────
    # Raw outputs are collected into LOCAL variables then WRITTEN TO DISK.
    # They serve as inputs to the DataRefiner and to Graphify only.
    # They must NOT be appended directly to full_context.

    # 1. Advanced Firecrawl (crawl target URLs or search with keywords)
    web_md: str = firecrawl_advanced_search(search_keywords, extracted_urls)
    path = save_markdown("agent_scrapes", primary_keyword, web_md)
    saved_files.append(str(path))

    # 2. Social threads → raw_ingestion (using optimised keyword)
    social_md: str = search_social_threads(primary_keyword)
    path = save_markdown("raw_ingestion", primary_keyword, social_md)
    saved_files.append(str(path))

    # 3. Wikipedia → agent_scrapes
    wiki_md: str = get_wiki_summary(primary_keyword)
    path = save_markdown("agent_scrapes", primary_keyword, wiki_md)
    saved_files.append(str(path))
    current_run_files.append(path.resolve())

    # 4. Academic papers → agent_scrapes (using optimised keyword)
    academic_md: str = search_academic_papers(primary_keyword)
    path = save_markdown("agent_scrapes", primary_keyword, academic_md)
    saved_files.append(str(path))
    current_run_files.append(path.resolve())

    # ── Phase 2.5: Deep Crawl + Noise Refinement ─────────────────────────
    # Collect all discovered URLs from Phase 1.5 for deep crawling.
    discovered_urls: list = list(extracted_urls)

    deep_crawl_md: str = deep_crawl_urls(discovered_urls)
    if deep_crawl_md:
        path = save_markdown("agent_scrapes", primary_keyword, deep_crawl_md)
        saved_files.append(str(path))

    # Combine ALL raw data collected in this run into a single corpus for
    # the DataRefiner.  Only in-memory strings from this execution are joined.
    raw_corpus: str = "\n\n---\n\n".join(
        p for p in [web_md, social_md, wiki_md, academic_md, deep_crawl_md] if p
    )

    # refined_data is the sole output of DataRefiner for this run.
    refined_data: str = refine_scraped_data(raw_corpus)

    # Save the primary refinement to disk for Graphify.
    path = save_markdown("agent_scrapes", primary_keyword, refined_data)
    saved_files.append(str(path))
    current_run_files.append(path.resolve())

    # ── Phase 2.6: Recursive URL Extraction & Per-URL Refinement ─────────
    # Parse the refined output for high-value URLs identified by the refiner,
    # scrape each one individually, refine it, and add to the input set.
    high_value_urls = _extract_high_value_urls(refined_data)
    logger.info("Phase 2.6: found %d high-value URLs for recursive refinement.", len(high_value_urls))

    for hv_url in high_value_urls:
        try:
            url_raw_md = deep_crawl_urls([hv_url])
            if not url_raw_md or not url_raw_md.strip():
                logger.warning("Phase 2.6: empty scrape for %s — skipping.", hv_url)
                continue

            url_refined = refine_scraped_data(url_raw_md)

            # Save with _URLRefiner suffix for clear provenance
            path = save_markdown("agent_scrapes", f"{primary_keyword}_URLRefiner", url_refined)
            saved_files.append(str(path))
            current_run_files.append(path.resolve())
            logger.info("Phase 2.6: refined and saved %s", path.name)

        except Exception as e:
            logger.warning("Phase 2.6: failed to process %s — %s", hv_url, e)

    # ── Phase 3: Synthesis & Storage ─────────────────────────────────────
    # STRICT CONTEXT ENFORCEMENT:
    # full_context is assembled from EXACTLY three in-memory variables:
    #   • core_context  — Phase 1.5 InputAnalyzer output (current run)
    #   • user_intent   — Phase 1.5 InputAnalyzer output (current run)
    #   • refined_data  — Phase 2.5 DataRefiner output   (current run)
    full_context: str = (
        f"## Core Context (from AI pre-processing)\n{core_context}\n\n"
        f"## User Intent\n{user_intent}\n\n"
        f"---\n\n"
        f"## Refined Research Data (current session)\n{refined_data}"
    )

    synthesis: str = synthesize_context(full_context)
    path = save_markdown("processed_summaries", primary_keyword, synthesis)
    saved_files.append(str(path))
    current_run_files.append(path.resolve())

    # ── Phase 4: Knowledge Graph Generation ──────────────────────────────
    graphify_output = ""
    graphify_error = None
    try:
        graphify_output = run_graphify(current_run_files)
    except GraphifyError as e:
        graphify_error = str(e)
    except FileNotFoundError as e:
        graphify_error = str(e)

    graph_path_abs = get_kb_root() / "graphify-out" / "graph.html"

    # Swift bridging contract — JSON stdout schema must remain unchanged.
    return {
        "status": "success",
        "message": "Research pipeline completed successfully.",
        "graph_path": str(graph_path_abs.resolve()),
        "phase": "Phase 4 — Knowledge Graph Generation Complete",
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
    }
