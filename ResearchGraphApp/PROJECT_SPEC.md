# Project Specification: Autonomous Research Graph (macOS)

## 1. Project Vision
This project is a native macOS application designed to automate deep-dive domain research. It ingest seeds (URLs, ideas, or files), expands them using an autonomous Python-based agent, synthesizes the findings into local Markdown files, and generates an interactive, queryable knowledge graph using Graphify. 

The application serves as an always-on "research co-pilot," blending native UI performance with local, privacy-first data processing.

## 2. Technology Stack & Environment
* **Target Platform:** macOS (optimized for Apple Silicon).
* **Frontend UI:** SwiftUI (App lifecycle, Menu Bar integration, WKWebView for graph rendering).
* **Backend Orchestrator:** Python 3.10+ (Subprocess execution from Swift).
* **Agentic Framework:** LangChain / CrewAI (or custom local bridge protocol).
* **Knowledge Graph Engine:** Graphify (`graphifyy`).
* **Data Ingestion APIs:** PRAW (Reddit), Tweepy/X API (X), Firecrawl/Jina (Web Scraping).

## 3. Core Architecture & Data Flow
The system operates in a strict, sequential pipeline. Antigravity must respect these boundaries and not conflate frontend state with backend script logic.

* **Phase 1: Dual-Entry Ingestion (SwiftUI -> Python)**
    * User inputs a raw text idea or a URL via the SwiftUI interface.
    * SwiftUI passes this data as arguments to a background Python process (`ingest.py`).
* **Phase 2: Context Expansion & Scraping (Python)**
    * If a general URL is provided, the Python scraper extracts the full Markdown.
    * The agent queries X/Reddit APIs to find 5-10 related threads to build a broader context pool.
    * **Wiki Context:** Utilizes the MediaWiki Action API to extract structured background data and definitions if the topic maps to Wikipedia or Wikidata entries, and saev as markdown format
    * **Academic Deep Dive:** Executes a targeted search for research papers (utilizing scholarly APIs like arXiv or Tavily Search), reads the top 5 academic links, and extracts the methodologies and findings into Markdown.
* **Phase 3: Synthesis & Storage (Python -> File System)**
    * The LLM synthesizes the scraped context to identify "Competitors" and "Research Gaps."
    * Raw data and syntheses are saved strictly as `.md` files in the local `/research_knowledge_base` directory.
    /research_knowledge_base
        ├── /raw_ingestion (Reddit/Twitter dumps)
        ├── /agent_scrapes (Raw Markdown from websites)
        ├── /processed_summaries (Intermediate JSON files)
* **Phase 4: Knowledge Graph Generation (Python Shell)**
    * Python triggers the `graphify ./research_knowledge_base` shell command.
    * Graphify outputs `graph.html`, `GRAPH_REPORT.md`, and `graph.json` into the `graphify-out/` directory.
* **Phase 5: Visual Presentation (SwiftUI)**
    * SwiftUI detects the updated `graphify-out/` directory.
    * A `WKWebView` component loads `graph.html` to provide interactive visualization.

## 4. Development Rules for Google Antigravity
When operating in this repository, Google Antigravity MUST adhere to the following directives:

### A. Code Generation Rules
1.  **Strict Separation:** Do not mix Python logic into Swift files or vice versa. Swift handles UI and process management; Python handles all API calls, scraping, and LLM orchestration.
2.  **SwiftUI Standards:** Use modern SwiftUI state management (`@Observable`, `@State`). Avoid outdated Combine patterns unless strictly necessary for Python process bridging. 
3.  **Python Standards:** Write modular Python scripts. Ensure all dependencies are documented in a `requirements.txt` or `pyproject.toml`.
4.  **Error Handling:** Python scripts must return clear exit codes and JSON-formatted error strings to stdout so the Swift `Process()` can display native UI alerts on failure.

### B. Graphify Integration Rules
1.  **Always Consult the Graph:** Before proposing architectural changes or answering questions about the existing codebase, Antigravity MUST read `graphify-out/GRAPH_REPORT.md` to understand current file connections.
2.  **Graphify Hook:** Ensure the `graphify antigravity install` hook is active. 

### C. File System Safety
1.  Never delete files in the `/research_knowledge_base` directory without explicit user permission.
2.  Always append or create new files. 

## 5. Definition of Done for Features
A new feature is only considered complete when:
1. The SwiftUI interface is responsive and non-blocking (process runs on background threads).
2. The Python script executes without environment path errors.
3. The resulting data is successfully ingested into the Graphify pipeline and visually updates in the `WKWebView`.