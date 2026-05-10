# Project Specification: Autonomous Research Graph (macOS)

## 1. Project Vision
This project is a native macOS application designed to automate deep-dive domain research. It ingest seeds (URLs, ideas, or files), expands them using an autonomous Python-based agent, synthesizes the findings into local Markdown files, and generates an interactive, queryable knowledge graph using Graphify. 

The application serves as an always-on "research co-pilot," blending native UI performance with local, privacy-first data processing.

## 2. Technology Stack & Environment
* **Target Platform:** macOS (optimized for Apple Silicon).
* **Frontend UI:** SwiftUI (App lifecycle, Menu Bar integration, WKWebView for graph rendering).
* **Backend Orchestrator:** Python 3.10+ (Subprocess execution from Swift).
* **AI Models:** Google Gen AI SDK (Vertex AI ADC) using Gemini 2.5 Flash (Routing/Parsing) and Gemini 2.5 Pro (Synthesis).
* **Knowledge Graph Engine:** Graphify (`graphifyy`).
* **Data Ingestion APIs:** Tavily Search API (Social/Academic), Local Firecrawl Docker Container (Web Scraping), MediaWiki REST API (Wiki Context).

## 3. Core Architecture & Data Flow
The system operates in a strict, sequential pipeline. Antigravity must respect these boundaries and not conflate frontend state with backend script logic.

* **Phase 1: Dual-Entry Ingestion (SwiftUI -> Python)**
    * User inputs a raw text idea or a URL via the SwiftUI interface.
    * SwiftUI passes this data as arguments to a background Python process (`main.py`).
* **Phase 1.5: AI Pre-processing (Python)**
    * The raw input is routed to `InputAnalyzer.py` powered by Gemini 2.5 Flash.
    * The model parses the input into a strict Pydantic schema extracting: `core_context`, `search_keywords`, `extracted_urls`, and `user_intent`.
* **Phase 2: Context Expansion & Scraping (Python)**
    * **Web Scraper:** Uses Firecrawl's advanced `/search` or `/crawl` endpoint (running locally via Docker) driven by the extracted keywords and URLs.
    * **Social Scraper:** Uses Tavily API (scoped to `reddit.com`, `x.com`) modified to specifically hunt for high-engagement discussions based on the primary search keyword.
    * **Wiki Context:** Utilizes the MediaWiki Action API to extract structured background data and definitions, saved as Markdown.
    * **Academic Deep Dive:** Uses Tavily API (scoped to academic domains like `arxiv.org`, `scholar.google.com`) to extract methodologies and findings into Markdown.
* **Phase 3: Synthesis & Storage (Python -> File System)**
    * `AgentSynthesizer.py` (Gemini 2.5 Pro) synthesizes the scraped context, anchored by the `core_context` and `user_intent` generated in Phase 1.5, to identify "Competitors" and "Research Gaps."
    * All outputs are saved strictly as `.md` files in the local `/research_knowledge_base` directory.
    /research_knowledge_base
        ├── /raw_ingestion (Social/Reddit/Twitter dumps)
        ├── /agent_scrapes (Wiki, Academic, and Web Markdown)
        └── /processed_summaries (Gemini 2.5 Pro final synthesis)
* **Phase 4: Knowledge Graph Generation (Python Shell)**
    * Python triggers the `graphify ../research_knowledge_base` shell command via `subprocess`.
    * Graphify outputs `graph.html`, `GRAPH_REPORT.md`, and `graph.json` into the `graphify-out/` directory.
    * The Python script completes and returns a structured JSON success payload to stdout.
* **Phase 5: Visual Presentation (SwiftUI)**
    * SwiftUI detects the successful process exit.
    * A `WKWebView` component loads `graphify-out/graph.html` to provide interactive visualization.

## 4. Development Rules for Google Antigravity
When operating in this repository, Google Antigravity MUST adhere to the following directives:

### A. Code Generation Rules
1.  **Strict Separation:** Do not mix Python logic into Swift files or vice versa. Swift handles UI and process management; Python handles all API calls, scraping, and LLM orchestration.
2.  **SwiftUI Standards:** Use modern SwiftUI state management (`@Observable`, `@State`). Avoid outdated Combine patterns unless strictly necessary for Python process bridging. 
3.  **Python Standards:** Write modular Python scripts. Ensure all dependencies are documented in `requirements.txt`.
4.  **Error Handling:** Python scripts must return clear exit codes and JSON-formatted error strings to stdout so the Swift `Process()` can display native UI alerts on failure. Never crash silently.

### B. Graphify Integration Rules
1.  **Always Consult the Graph:** Before proposing architectural changes or answering questions about the existing codebase, Antigravity MUST read `graphify-out/GRAPH_REPORT.md` to understand current file connections.
2.  **Graphify Hook:** Ensure the `graphify antigravity install` hook is active. 

### C. File System Safety
1.  Never delete files in the `/research_knowledge_base` directory without explicit user permission.
2.  Always append or create new files with timestamped naming conventions.

## 5. Definition of Done for Features
A new feature is only considered complete when:
1. The SwiftUI interface is responsive and non-blocking (process runs on background threads).
2. The Python script executes without environment path errors.
3. The resulting data is successfully ingested into the Graphify pipeline and visually updates in the `WKWebView`.