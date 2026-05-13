# Project Specification: Autonomous Research Graph (macOS)

## 1. Project Vision
This project is a native macOS application designed to automate deep-dive domain research for academic and industrial projects. It ingests seeds (URLs, ideas, or files), expands them using an autonomous Python-based agent, synthesizes the findings into local Markdown files, and generates an interactive, queryable knowledge graph using Graphify. 

The application serves as an always-on "academic gap-hunting engine," blending native UI performance with local, privacy-first data processing.

## 2. Technology Stack & Environment
* **Target Platform:** macOS (optimized for Apple Silicon).
* **Frontend UI:** SwiftUI (App lifecycle, Menu Bar integration, WKWebView for graph rendering).
* **Backend Orchestrator:** Python 3.10+ (Subprocess execution from Swift).
* **AI Models:** Google Gen AI SDK (Vertex AI ADC) using Gemini 2.5 Flash (Routing/Parsing) and Gemini 2.5 Pro (Synthesis).
* **Knowledge Graph Engine:** Graphify (`graphifyy`).
* **Data Ingestion APIs:** Tavily Search API, Local Firecrawl Docker Container, MediaWiki REST API, Semantic Scholar API, arXiv API.

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
    * **Social Scraper:** Uses Tavily API (scoped to `reddit.com`, `x.com`) to find recent conversations, extract public sentiment, and capture new leads (like Wikipedia page links). Output MUST explicitly tag data with its source (e.g., "[Source: Reddit]").
    * **Wiki Context:** Utilizes the MediaWiki Action API to extract structured background data and definitions, saved as Markdown.
    * **Academic Deep Dive:** Uses the Semantic Scholar API, Tavily's strict academic mode and arXiv to query the top 20 research papers. Extracts the "Current Work", "Limitations", and "Future Work" sections. Output MUST include formal citations and URLs for the real papers(articals).
* **Phase 3: Synthesis & Storage (Python -> File System)**
    * `AgentSynthesizer.py` (Gemini 2.5 Pro) synthesizes the scraped context, anchored by the `core_context` and `user_intent`. It MUST output its findings strictly adhering to the following Markdown headers:
        - `## Problem Background`
        - `## Existing Solutions/Competitors (Literature)`
        - `## Methodological Weaknesses (The Gap)`
        - `## Proposed Novelty`
    * All outputs are saved strictly as `.md` files in the local `/research_knowledge_base` directory.
        ├── /raw_ingestion (Social/Reddit/Twitter dumps)
        ├── /agent_scrapes (Wiki, Academic, and Web Markdown)
        └── /processed_summaries (Gemini 2.5 Pro final synthesis)
* **Phase 4: Knowledge Graph Generation (Python Shell)**
    * Python triggers the `graphify ../research_knowledge_base` shell command via `subprocess`, utilizing the local FastAPI proxy to route to Vertex AI.
    * Graphify outputs `graph.html`, `GRAPH_REPORT.md`, and `graph.json` into the `graphify-out/` directory.
* **Phase 5: Visual Presentation (SwiftUI)**
    * SwiftUI detects the successful process exit.
    * A `WKWebView` component loads `graphify-out/graph.html` to provide interactive visualization.

## 4. Development Rules for Google Antigravity
When operating in this repository, Google Antigravity MUST adhere to the following directives:

### A. Code Generation Rules
1.  **Strict Separation:** Do not mix Python logic into Swift files or vice versa. 
2.  **SwiftUI Standards:** Use modern SwiftUI state management (`@Observable`, `@State`). 
3.  **Python Standards:** Write modular Python scripts. Ensure all dependencies are documented in `requirements.txt`.
4.  **Error Handling:** Python scripts must return clear exit codes and JSON-formatted error strings to stdout.

### B. Graphify Integration Rules
1.  **Always Consult the Graph:** Before proposing architectural changes, Antigravity MUST read `graphify-out/GRAPH_REPORT.md`.

### C. File System Safety
1.  Never delete files in the `/research_knowledge_base` directory without explicit user permission.
2.  Always append or create new files with timestamped naming conventions.

## 5. Definition of Done for Features
1. The SwiftUI interface is responsive and non-blocking.
2. The Python script executes without environment path errors.
3. The resulting data is successfully ingested into the Graphify pipeline and visually updates in the `WKWebView`.