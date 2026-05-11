"""
IngestSeedUseCase.py — Full pipeline orchestrator (Phases 1.5 → 4).
Wires InputAnalyzer → Phase 2 scrapers → Phase 3 storage → Phase 4 graphify
into a single, sequential pipeline invoked from main.py.
"""

from application.InputAnalyzer import analyze_seed
from application.AgentSynthesizer import synthesize_context
from infrastructure.WebScraper import firecrawl_advanced_search
from infrastructure.SocialScraper import search_social_threads
from infrastructure.WikiAPI import get_wiki_summary
from infrastructure.AcademicScraper import search_academic_papers
from infrastructure.FileStorage import save_markdown, ensure_structure, get_kb_root
from infrastructure.GraphifyRunner import run_graphify, GraphifyError


def execute(idea: str, url: str) -> dict:
    """
    Run the full research pipeline:
      Phase 1.5 — AI pre-processing (Gemini Flash)
      Phase 2   — scrape & expand (using optimized keywords)
      Phase 3   — store to knowledge base
      Phase 4   — trigger graphify
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
    ensure_structure()

    # ── Phase 1.5: AI Pre-processing ─────────────────────────────────────
    seed_analysis = analyze_seed(raw_seed)

    core_context = seed_analysis.get("core_context", raw_seed)
    search_keywords = seed_analysis.get("search_keywords", [raw_seed])
    extracted_urls = seed_analysis.get("extracted_urls", [])
    user_intent = seed_analysis.get("user_intent", "General Inquiry")

    # If the user explicitly passed a --url, ensure it's in the list
    if url.strip() and url.strip() not in extracted_urls:
        extracted_urls.insert(0, url.strip())

    # Use the first optimized keyword for downstream scrapers
    primary_keyword = search_keywords[0] if search_keywords else raw_seed

    # ── Phase 2: Context Expansion (keyword-driven) ──────────────────────
    # 1. Advanced Firecrawl (crawl target URLs or search with keywords)
    web_md = firecrawl_advanced_search(search_keywords, extracted_urls)
    path = save_markdown("agent_scrapes", primary_keyword, web_md)
    saved_files.append(str(path))

    # 2. Social threads → raw_ingestion (using optimized keyword)
    social_md = search_social_threads(primary_keyword)
    path = save_markdown("raw_ingestion", primary_keyword, social_md)
    saved_files.append(str(path))

    # 3. Wikipedia → agent_scrapes
    wiki_md = get_wiki_summary(primary_keyword)
    path = save_markdown("agent_scrapes", primary_keyword, wiki_md)
    saved_files.append(str(path))

    # 4. Academic papers → agent_scrapes (using optimized keyword)
    academic_md = search_academic_papers(primary_keyword)
    path = save_markdown("agent_scrapes", primary_keyword, academic_md)
    saved_files.append(str(path))

    # ── Phase 3: Synthesis & Storage ─────────────────────────────────────
    context_parts = [p for p in [web_md, social_md, wiki_md, academic_md] if p]
    full_context = (
        f"## Core Context (from AI pre-processing)\n{core_context}\n\n"
        f"## User Intent: {user_intent}\n\n---\n\n"
        + "\n\n---\n\n".join(context_parts)
    )

    synthesis = synthesize_context(full_context)
    path = save_markdown("processed_summaries", primary_keyword, synthesis)
    saved_files.append(str(path))

    # ── Phase 4: Knowledge Graph Generation ──────────────────────────────
    graphify_output = ""
    graphify_error = None
    try:
        graphify_output = run_graphify()
    except GraphifyError as e:
        graphify_error = str(e)
    except FileNotFoundError as e:
        graphify_error = str(e)

    graph_path_abs = get_kb_root() / "graphify-out" / "graph.html"

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
