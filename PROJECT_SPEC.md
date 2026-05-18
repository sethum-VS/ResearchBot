# Project Specification: Autonomous Research Graph (macOS)

## 1. Project Vision
An **Academic Gap-Hunting Engine** designed to automate deep-dive domain research for university-level Final Year Projects (FYP). The system identifies "Structural Holes" in existing literature by correlating societal problems (extracted from social media) with academic limitations (from peer-reviewed papers), providing a clear path for a novel technical contribution.

## 2. Technology Stack & Environment
* **Platform:** macOS (optimized for Apple Silicon).
* **Frontend UI:** SwiftUI (Modern `@Observable` state management, WKWebView for visualization).
* **Backend Orchestrator:** Python 3.10+ (Subprocess execution from Swift).
* **AI Models:**
    * **Gemini 2.5 Flash:** Input analysis, lead extraction, and high-speed URL parsing.
    * **Gemini 2.5 Pro:** Data refinement, noise reduction, and final synthesis (utilizing 64k output window).
    * **Llama 4 Scout:** 10M-context Knowledge Graph generation via Vertex AI Model-as-a-Service (MaaS).
* **Data Ingestion APIs:** * **Firecrawl:** Local Docker container for deep web crawling (`/crawl` and `/scrape`).
    * **Semantic Scholar API:** Direct extraction of academic citations, limitations, and future work.
    * **Tavily API:** Discovery of social leads (Reddit/X) and academic fallbacks.
    * **MediaWiki API:** Foundational definitions and Wiki context.

## 3. Core Architecture & Data Flow
The system operates as a recursive, concurrent pipeline with strict session isolation.

### Phase 1 & 1.5: Ingestion & Intent Analysis
* SwiftUI passes raw input to `InputAnalyzer.py`.
* Gemini 2.5 Flash generates `core_context`, `search_keywords`, and `user_intent`.

### Phase 2: Discovery Scraping (Concurrent Execution)
* **Social Scraper:** Captures Reddit/X threads; extracts "leads" (Wikipedia/GitHub/Research terms).
* **Academic Scraper:** Queries Semantic Scholar/arXiv for the top 20 papers; extracts Current Work, Limitations, and Citations.
* **Wiki Context:** Extracts background logic from MediaWiki.

### Phase 2.5: Noise Reduction & Primary Refinement
* **DataRefiner:** Ingests raw Discovery data into Gemini 2.5 Pro.
* **Logic:** Strips ads and fluff; organizes findings into a "Research Ledger" with source tags.
* **Output:** Generates the "High-Value URLs for Next Crawl Phase."

### Phase 2.6: Recursive Deep-Crawl (High-Concurrency)
* **URL Extraction:** Gemini 2.5 Flash extracts URLs from Phase 2.5.
* **Recursive Loop:** Every URL is deep-crawled via Firecrawl and individually refined via Gemini 2.5 Pro, saved as `_URLRefiner.md` files.

### Phase 3: Synthesis & Storage
* **AgentSynthesizer:** Ingests `core_context`, `user_intent`, and `refined_data`.
* **Output:** Strictly follows the University Rubric: `## Problem Background`, `## Existing Solutions`, `## Methodological Weaknesses (The Gap)`, and `## Proposed Novelty`.
* Raw data and syntheses are saved strictly as `.md` files in the local `/research_knowledge_base` directory.
    /research_knowledge_base
        ├── /raw_ingestion (Reddit/Twitter dumps)
        ├── /agent_scrapes (Raw Markdown from websites)
        ├── /processed_summaries (Intermediate JSON files)
* **Phase 4: Knowledge Graph Generation (Python Shell)**
    * Python triggers the `graphify ./research_knowledge_base` shell command.
    * Graphify outputs `graph.html`, `GRAPH_REPORT.md`, and `graph.json` into the `graphify-out/` directory.

### Phase 4: Knowledge Graph Generation (Llama 4 Scout)
* **Session Isolation:** A temporary directory is created containing ONLY the current run's refined artifacts.
* **Extraction:** Llama 4 Scout (10M context) processes the entire corpus to generate `graph.json` and `graph.html`.
* **Post-Processing:** Injects resizable UI scripts and applies "Smart Community Naming" to clusters.

## 4. Performance & Concurrency Protocol
To maximize efficiency on Apple Silicon, the following optimization goals are enforced:
1.  **Multithreading (`asyncio` / `ThreadPoolExecutor`):** Applied to all I/O-bound tasks in Phase 2 and Phase 2.6. Scrapers and URL Refiners must run in parallel with a `MaxWorkers` cap to respect API rate limits.
2.  **Multiprocessing:** Utilized for CPU-bound post-processing of HTML/JSON files to ensure the UI remains responsive.
3.  **Real-time Streaming:** Backend logs must stream to `stdout` to provide live progress updates to the SwiftUI frontend during long concurrent operations.

## 5. Development Rules

### Code Generation Rules
1.  **Strict Separation:** Do not mix Python logic into Swift files or vice versa. Swift handles UI and process management; Python handles all API calls, scraping, and LLM orchestration.
2.  **SwiftUI Standards:** Use modern SwiftUI state management (`@Observable`, `@State`). Avoid outdated Combine patterns unless strictly necessary for Python process bridging. 
3.  **Python Standards:** Write modular Python scripts. Ensure all dependencies are documented in a `requirements.txt` or `pyproject.toml`.
4.  **Error Handling:** Python scripts must return clear exit codes and JSON-formatted error strings to stdout so the Swift `Process()` can display native UI alerts on failure.

* **Regional Accuracy:** All Llama 4 Scout requests MUST be routed to the `us-east5` region.
* **Output Capacity:** Gemini 2.5 Pro calls must explicitly set `max_output_tokens=65536`.
* **Source Integrity:** Data lineage must be preserved via `[Source: Origin]` tags.
* **Strict Separation:** Swift handles the UI/Process; Python handles the logic. No logic cross-contamination.

### . Graphify Integration Rules
1.  **Always Consult the Graph:** Before proposing architectural changes or answering questions about the existing codebase, Antigravity MUST read `graphify-out/GRAPH_REPORT.md` to understand current file connections.
2.  **Graphify Hook:** Ensure the `graphify antigravity install` hook is active. 

### . File System Safety
1.  Never delete files in the `/research_knowledge_base` directory without explicit user permission.
2.  Always append or create new files. 

## . Definition of Done for Features
A new feature is only considered complete when:
1. The SwiftUI interface is responsive and non-blocking (process runs on background threads).
2. The Python script executes without environment path errors.
3. The resulting data is successfully ingested into the Graphify pipeline and visually updates in the `WKWebView`.
4.  The system identifies a verifiable research gap between social needs and academic limitations.
5.  Recursive crawling generates individual `_URLRefiner.md` files successfully.
6.  The Knowledge Graph is generated using Llama 4 Scout without truncation errors.
7.  The final UI allows for resizable panels and displays smart-named communities.