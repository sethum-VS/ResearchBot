# ResearchBot: Autonomous Research Graph

[![Platform: macOS](https://img.shields.io/badge/Platform-macOS%2014+-blue.svg)](https://developer.apple.com/macos/)
[![Language: Swift](https://img.shields.io/badge/Language-Swift%206.0-orange.svg)](https://developer.apple.com/swift/)
[![Language: Python](https://img.shields.io/badge/Language-Python%203.10+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

An academic gap-hunting and proposal synthesis engine designed to automate domain research for university-level Final Year Projects (FYP). The platform correlates societal needs, academic limitations, and knowledge-graph topologies to generate structured, source-verifiable academic proposals.

ResearchBot works by isolating each execution session, scraping real-time academic and social data, building interactive network graphs, running Map-Reduce topology analysis, and exporting natively formatted documents directly to Google Workspace.

---

## Architectural Overview

ResearchBot is split into a native macOS interface and a highly concurrent Python command-line engine. The frontend manages layouts, execution processes, and graph views, while the backend coordinates data ingestion, extraction, vector routing, and workspace synchronization.

```mermaid
graph TD
    %% Define components in a top-down, clean vertical stack

    subgraph ClientLayer [Client Layer: macOS SwiftUI Application]
        UI[SwiftUI Interface]
        Bridge[PythonBridge Process Wrapper]
        WebView[WKWebView vis-network Explorer]
        MDViewer[Native Markdown Viewer]
    end

    subgraph InterfaceLayer [Local Communication & Bridging APIs]
        Shell[execute_pipeline.sh Shell Script]
        Proxy[VertexProxy FastAPI Server :8000]
    end

    subgraph ProcessEngine [Python 3.10+ Orchestration Engine]
        IngestUC[IngestSeedUseCase Ingestion Pipeline]
        ProposalUC[ProposalOrchestrator Synthesis Engine]
        ExportUC[ExportWorkspaceUseCase Workspace Exporter]
    end

    subgraph Scrapers [Concurrent Discovery & Scrapers]
        Social[SocialScraper PyTavily]
        Wiki[WikiAPI Definition Crawler]
        Academic[AcademicScraper arXiv & Semantic Scholar]
        Firecrawl[Firecrawl Docker Container :3002]
        PDF[PdfExtractor PyMuPDF]
    end

    subgraph Analysis [Data Analysis & Extraction Layers]
        Refiner[DataRefiner Gemini 2.5 Pro]
        Graphify[Graphify CLI & Llama 4 Scout]
        GapAnalyzer[GraphAnalyzer Map-Reduce Gemini]
        Matcher[SemanticMatcher Relevance Scoring]
        Synthesizer[ProposalSynthesizer Roman Numerals]
    end

    subgraph Storage [Isolated Session Storage - Local File System]
        Runs[research_knowledge_base/runs/]
        Manifest[session_manifest.json Metadata]
        GapJson[academic_gap_analysis.json Payload]
        Scrapes[agent_scrapes/ Refined Ledgers]
        Processed[processed_summaries/ PDFs & Synthesis]
        GraphOut[graphify-out/ Visual Assets]
        Proposals[proposals/ Proposals & Matched Papers]
    end

    subgraph External [External Web Services]
        VertexAI[Google Vertex AI API Gemini & Llama]
        GoogleDocs[Google Workspace APIs Drive & Docs]
    end

    %% Vertical Data Flow & Control
    UI -->|Spawn process| Bridge
    Bridge -->|Activate env| Shell
    Shell -->|Trigger command| IngestUC
    Shell -->|Trigger command| ProposalUC
    Shell -->|Trigger command| ExportUC

    %% Scrapers Execution
    IngestUC -->|Parallel scrapes| Social & Wiki & Academic
    Social & Wiki -->|Refine ledger| Refiner
    Academic -->|PDF triage| PDF
    IngestUC -->|Crawl web| Firecrawl

    %% Processing & Analysis Flow
    PDF & Refiner -->|Save processed docs| Processed
    Processed -->|Ingest corpus| Graphify
    Graphify -->|Compile network| GraphOut
    GraphOut -->|Map topology| GapAnalyzer
    
    %% Scoping and Proposal Flow
    ProposalUC -->|Scoping & scoring| Matcher
    Matcher -->|Synthesize proposal| Synthesizer
    Synthesizer -->|Write document| Proposals

    %% Local Disk Storage mapping
    IngestUC & ProposalUC & ExportUC -->|Session-isolated writes| Runs
    Runs --> Manifest & GapJson & Scrapes & Processed & GraphOut & Proposals

    %% External APIs Integration
    Refiner & GapAnalyzer & Matcher & Synthesizer -->|HTTPS REST| VertexAI
    ExportUC -->|OAuth Consent| GoogleDocs

    %% UI Interactive Links
    UI -->|URLSession POST queries| Proxy
    Proxy -->|Local interrogations| Graphify
    UI -->|Render HTML visualization| WebView
    WebView -->|Read local graph| GraphOut
    UI -->|Render raw files| MDViewer
    MDViewer -->|Read documents| Processed & Proposals
```

---

## Core Capabilities

### Multi-Source Academic & Social Ingestion
The ingestion scraper pulls concurrently from Semantic Scholar, arXiv, and Tavily to build a multi-dimensional foundation for analysis. The scraper implements rate-pacing protocols, backoffs based on HTTP Retry-After headers, and an automatic circuit-breaker behavior that skips degraded endpoints while preserving successful threads.

### Open-Access PDF Triage
For all located papers featuring valid PDF URLs, ResearchBot executes a high-speed triage phase. Using Gemini 2.5 Flash, the engine filters up to 25 titles and abstracts, selects the top 5 most relevant targets, and pulls down full-document text via a PyMuPDF extraction pool.

### Interactive Knowledge Graphs
Ingested documents are compiled into structural visual networks using the Graphify entity-relation extraction framework. Graphify parses primary research documents into a typed graph schema using Llama 4 Scout (10M token context window) via the local FastAPI VertexProxy bridge. 

Network nodes are grouped via Louvain-style modular community clustering, programmatically named using Gemini 2.5 Flash, and rendered interactively inside the SwiftUI interface using the vis-network engine. The interface also exposes an interactive console connected directly to the VertexProxy API to calculate node paths and execute graph queries.

### Map-Reduce Topology Analyzer
The engine executes a parallel Map-Reduce topology analysis pipeline orchestrated with Python's asyncio framework and Gemini 2.5 Pro models. The analysis extracts key final-year project academic gaps using structural network algorithms combined with full-text source document books:

* **Structural Holes (Burt's Structural Holes Theory)**: The algorithm parses the modular communities of the network to locate bridging opportunities—disconnected or loosely-coupled sub-graphs that can be synthetically integrated to provide novel research contributions.
* **High-Degree Constraint Analysis (Degree Centrality)**: Computes edge counts and inbound citation weights for nodes classified as limitations or method weaknesses. Central constraints backed by multi-source primary evidence denote consensus technical bottlenecks.
* **Orphaned Solutions Detection**: Filters for solution nodes displaying outgoing edges to failure/drawback nodes but lacking integrated target-system relationships. These denote proven engineering components that have not yet been successfully applied to current bottlenecks.

To optimize token consumption, documents larger than 60,000 characters are processed using a Semantic Bookends Algorithm (infrastructure/TextChunker.py), which extracts and pairs abstract/introduction headers and discussion/conclusion headers while dropping middle sections. To avoid regional rate limits, Map tasks are sharded across parallel API pools (europe-west4, us-east4, and asia-northeast1) with automated regional failover cascades, before a consolidated Reduce step merges the findings into a structured JSON executive summary.

### Automated Proposal Synthesis
A post-analysis workflow takes student ideas and correlates them with the active session history to write complete project proposals. Proposals are organized into a strict Roman numeral hierarchy:
* **I. Executive Summary**: Narrative problem and proposed innovation.
* **II. Project Definition**: Clear boundary conditions, scopes, and project constraints.
* **III. Matched Literature Review**: Critical assessment of top-scoring Open Access references (filtered through a strict 75% semantic threshold).
* **IV. Academic Gap Alignment**: Technical alignment with the diagnosed graph topology.
* **V. Architecture & Methodology**: Separated systematically into Feature Sets (V.A) and Technical Challenges (V.B).
* **VI. Verification & Execution**: Empirical testing plan.

### Upgraded Workspace Exporter
The native Google Workspace Exporter avoids simple plain-text dumps. It parses Markdown inline markers (headers, bullet points, tables, and bold style ranges) and writes native Google Doc API styling instructions. The engine searches user Drive folders, creates runs-isolated directories, populates document text, auto-generates tabular literature sections, and links reference citations directly back to their academic web source URLs.

---

## Advanced Corpus Allocation & Token Budgeting

To run deep-dive Map-Reduce analysis without hitting model limits or regional quota walls, ResearchBot implements a multi-tiered token gating and dynamic allocation pipeline. The system enforces a rigorous target budget of **180,000 characters** (approximately 45,000 to 60,000 tokens), leaving safe headroom for massive structural topologies, prompts, and system instructions:

### 1. Architectural File Filtering (Stage 1)
To ensure the pipeline is grounded strictly in verified research rather than social media noise, ResearchBot filters incoming files at the directory level:
* **Allowed Directories**: Only files residing in `processed_summaries/` and `agent_scrapes/` are loaded.
* **Bypassed Directories**: Raw web crawled indexes and unfiltered social media data residing in `raw_ingestion/` are completely skipped during topology and gap analysis.

### 2. Semantic Bookends Compression Algorithm (Stage 2)
For individual files exceeding **60,000 characters**, ResearchBot executes an in-memory semantic compression process (`infrastructure/TextChunker.py`):
* **Header Matching**: The chunker uses case-insensitive regular expressions to detect primary academic sections:
  * **Intro Headers**: `Abstract`, `Introduction`
  * **Outro Headers**: `Conclusion`, `Discussion`, `Future Work`, `Limitations`
* **Section Extraction**: It extracts the text body starting from the abstract/introduction (up to the next Markdown heading level 1-6) and joins it with the entire conclusion/outro section through a marked break (`\n\n[... Middle sections omitted for brevity ...]\n\n`).
* **Double Fallback**: If the section headers are missing or out of order, the chunker takes the first 30,000 characters and the last 30,000 characters of the file and joins them, guaranteeing the processed file fits under the **60,000 character limit** without breaking document schema.

### 3. Protected Dynamic Bucket Allocation (Stage 3)
Eligible files are sorted chronologically (newest-first) and mapped into dedicated, protected character budgets to prevent one massive document from starving other categories:
* **Synthesis Budget (`_SYNTHESIS_BUDGET`)**: `40,000` characters baseline cap. Reserved for Phase 3 syntheses.
* **Web Scrape Budget (`_WEB_SCRAPE_BUDGET`)**: `80,000` characters baseline cap. Reserved for web scrapers and refined ledgers.
* **Academic Budget (`_ACADEMIC_BUDGET`)**: `120,000` characters baseline cap. Reserved for open-access PDF full texts.

### 4. Asymmetric Budget Rollover (Stage 4)
If a high-priority bucket consumes less than its allocated cap, the remaining headroom is rolled over to downstream categories:
* **Synthesis Rollover**: Any unused synthesis character capacity is rolled over to the **Academic budget first**.
* **Secondary Rollover**: If the Academic budget is completely satisfied, any remaining rollover is added to the **Web Scrape budget**.
* **Integrity Packing**: Files are packed as complete blocks. If a file is larger than the remaining budget of its target bucket, the file is omitted entirely from the run rather than being head-truncated, maintaining complete document integrity.

### 5. Global Character Safety Net (Stage 5)
Once the dynamic buckets are packed, the entire merged corpus is audited. If the total length exceeds the global cap of **180,000 characters**, an eviction algorithm drops files lowest-priority first until the budget is met:
1. `_urlrefiner` files and generic `agent_scrapes/`
2. `academic_fulltext_` documents
3. Phase 3 synthesis documents (never evicted)

### 6. Reference Hygiene Validation (Stage 6)
Any file omitted during Stage 3 (due to bucket caps) or evicted during Stage 5 (safety net) is stripped from the active loading manifests. The python script (`GraphAnalyzer.py`) compares the LLM output array against the list of surviving files. If the model cites a document that did not survive the allocation filters, the citation reference is automatically scrubbed, preventing model hallucinations.

---

## Technical Prerequisites

To compile and execute ResearchBot, your local workstation requires:

| Component | Minimum Version | Installation Source / Note |
|-----------|-----------------|----------------------------|
| macOS | macOS Sonoma 14.0+ | Apple Silicon optimization supported |
| Xcode | Xcode 15.0+ | Required for SwiftUI compile targets |
| Python | Python 3.10+ | Primary backend processing |
| Firecrawl | Local Docker instance | Required for recursive web scraping |
| Graphify | Local CLI utility | Required for knowledge graph extraction |
| Google Cloud | Active Project | Required for Vertex AI API access |

---

## Getting Started

### 1. Backend Environment Setup
Navigate to the backend directory, create a virtual environment, and install package dependencies:

```bash
cd ResearchGraphApp/Backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` configuration file at the repository root:

```env
GOOGLE_CLOUD_PROJECT_ID=your-gcp-project-id
TAVILY_API_KEY=your-tavily-api-key
SEMANTIC_SCHOLAR_API_KEY=optional-api-key
GRAPHIFY_TOKEN_BUDGET=8192
```

Ensure your local gcloud CLI is authenticated and Application Default Credentials (ADC) are generated:

```bash
gcloud auth application-default login
```

### 2. Launch Local Scraper & Proxy
Launch your local Firecrawl scraper inside its Docker environment, then initiate the VertexProxy helper script. This handles FastAPI endpoints and launches the background server on port 8000:

```bash
# Verify firecrawl docker status
docker compose -f ResearchGraphApp/Backend/infrastructure/firecrawl/docker-compose.yml up -d

# Start VertexProxy
./ResearchGraphApp/ensure_vertex_proxy.sh
```

### 3. SwiftUI App Compilation
Open the Swift project using Xcode:

```bash
open ResearchGraphApp/App/ResearchBot.xcodeproj
```

* Drag your Google Cloud OAuth client secrets (`credentials.json`) directly into the app bundle (`App/ResearchBot/credentials.json`).
* Select the **ResearchBot** scheme, set the target device to your local macOS workstation, and execute the Build & Run command (`Cmd + R`).

---

## Execution Interface & Command Line

The backend can be driven programmatically or tested using the execution wrapper `execute_pipeline.sh`:

### Run Ingestion Pipeline
```bash
./execute_pipeline.sh --idea "Decentralized episodic memory systems in multi-agent workflows"
```

### Run Proposal Synthesis
```bash
./execute_pipeline.sh --command generate_proposal \
  --session-id "session_20260525T004344Z_episodic_memory_analysis" \
  --project-idea "Provide localized vector memory capsules for time-travel state transitions"
```

### Run Google Workspace Export
```bash
./execute_pipeline.sh --command export_to_workspace \
  --session-id "session_20260525T004344Z_episodic_memory_analysis" \
  --kb-root "/Users/user/Documents/Dev/ResearchBot/research_knowledge_base"
```

---

## Backend Module Index

The Python backend is organized cleanly around UseCase abstractions and infrastructure providers:

```
Backend/
├── main.py                          # Unified CLI entrypoint
├── application/
│   ├── IngestSeedUseCase.py         # Main pipeline coordinator
│   ├── ExportWorkspaceUseCase.py    # Workspace exporter coordination
│   ├── InputAnalyzer.py             # Seed parsing and keyword extraction
│   ├── DataRefiner.py               # Gemini 2.5 Pro noise reduction
│   ├── AgentSynthesizer.py          # Abstract synthesis compiler
│   ├── GraphAnalyzer.py             # Map-Reduce topology calculations
│   ├── ProposalOrchestrator.py      # Scoping and paper matching coordinator
│   └── ProposalSynthesizer.py       # Roman numeral academic writer
└── infrastructure/
    ├── GoogleWorkspaceManager.py    # Native Google Docs formatting and OAuth
    ├── VertexProxy.py               # FastAPI proxy and local endpoint router
    ├── GraphifyRunner.py            # Graphify shell bridge and CLI manager
    ├── SemanticMatcher.py           # Gemini 2.5 Flash relevance validation
    ├── TextChunker.py               # Segment-based academic bookend compressor
    ├── FileStorage.py               # Session directories and file structures
    ├── WebScraper.py                # Firecrawl integration
    ├── SocialScraper.py             # Social leads fetcher
    ├── AcademicScraper.py           # arXiv, Semantic Scholar, and triage logic
    ├── PdfExtractor.py              # PyMuPDF processing
    └── WikiAPI.py                   # Contextual term definition crawler
```

---

## Isolated Workspace Layout

Every pipeline run operates with strict isolation. Data is written directly inside timestamped session folders within the knowledge base:

```
/research_knowledge_base/
└── runs/
    └── session_<UTC_TIMESTAMP>_<slug>/
        ├── agent_scrapes/         # Academic metadata, web scrapes, wiki, url refiners
        ├── raw_ingestion/         # Excluded social feeds (Phase 2.5 inputs only)
        ├── processed_summaries/   # Phase 3 summaries and Phase 2.2 full PDFs
        ├── graphify-out/          # Local Graphify outputs (json, html, reports)
        ├── proposals/             # Synthesized academic proposal markdown files
        ├── session_manifest.json  # Ingestion metrics and details
        └── academic_gap_analysis.json  # Persisted map-reduce payloads
```

---

## Detailed Environment Reference

The following environment variables control thresholds, timing parameters, and failovers across the system:

| Variable Name | Default Value | Role and Technical Description |
|---------------|---------------|--------------------------------|
| `GOOGLE_CLOUD_PROJECT_ID` | Required | Google Cloud project ID hosting your Vertex API resources. |
| `RESEARCHBOT_SESSION_DIR` | Auto-set | Absolute path to the active timestamped folder. Set at startup. |
| `GRAPHIFY_TOKEN_BUDGET` | `8192` | Budget allocations passed to the Graphify CLI compiler. |
| `S2_KEYWORD_DELAY_SEC` | `3` | Pacing delay between distinct Semantic Scholar API keyword calls. |
| `S2_POST_429_COOLDOWN_SEC` | `15` | Cooldown period applied after receiving an HTTP 429 rate limit. |
| `S2_429_MAX_RETRIES` | `2` | Number of attempts allowed for S2 calls after rate-limit backoffs. |
| `S2_CIRCUIT_BREAKER` | `true` | When true, skips S2 calls on subsequent keywords if a 429 is exhausted. |
| `ARXIV_REQUEST_DELAY_SEC` | `3` | Pacing delay between distinct arXiv atom query executions. |
| `ACADEMIC_TRIAGE_POOL_MAX` | `25` | Target quantity of Open Access papers sent to initial Flash triage. |
| `ACADEMIC_FULLTEXT_MAX_CHARS` | `100000` | Hard cap on characters extracted from a single open-access PDF. |
| `ACADEMIC_FULLTEXT_MAX_BYTES` | `52428800` | Maximum size limits (50 MB) allowed for triaged PDF downloads. |

---

## System Design & Bridging Contracts

### Session Isolation & Data Integrity
To preserve integrity, the core pipeline only loads files that land under explicit paths. Files under `raw_ingestion/` are excluded from Graphify and GraphAnalyzer tasks. All output paths must utilize the `RESEARCHBOT_SESSION_DIR` path environment, and temporary directory buffers must remain session-scoped.

### SwiftUI Stdout Communication
The SwiftUI frontend processes backend subprocess executions using specific boundary tags printed on stdout. This allows reliable asynchronous JSON decoding:

#### Ingestion Pipeline Finish Tag
```
---PIPELINE_RESULT_START---
{
  "status": "success",
  "message": "Completed pipeline execution successfully.",
  "graph_path": "/path/to/runs/session_xyz/graphify-out/graph.html",
  "kb_root": "/path/to/research_knowledge_base",
  "session_id": "session_xyz",
  "session_path": "/path/to/runs/session_xyz",
  "phase": "4.5",
  "seed_analysis": { ... },
  "saved_files": [ ... ],
  "synthesis_preview": "...",
  "graphify": { "ran": true, "stdout": "...", "error": null },
  "academic_gap_analysis": { ... }
}
---PIPELINE_RESULT_END---
```

#### Proposal Synthesis Finish Tag
```
---PROPOSAL_RESULT_START---
{
  "status": "success",
  "message": "Academic proposal compiled.",
  "proposal_path": "/path/to/proposals/proposal_123.md",
  "matched_papers_path": "/path/to/proposals/proposal_123_matched_papers.json",
  "session_id": "session_xyz",
  "scoped_query": "...",
  "matched_paper_count": 15,
  "proposal_id": "proposal_123"
}
---PROPOSAL_RESULT_END---
```

#### Workspace Export Finish Tag
```
---WORKSPACE_EXPORT_RESULT_START---
{
  "status": "success",
  "message": "Session documents exported to Google Drive.",
  "master_document_url": "https://docs.google.com/document/d/...",
  "topic_document_url": "https://docs.google.com/document/d/...",
  "topic_folder_url": "https://drive.google.com/drive/folders/...",
  "session_id": "session_xyz"
}
---WORKSPACE_EXPORT_RESULT_END---
```

### Rate-Limiting & Local Backoffs
API requests to upstream entities are isolated within distinct execution units. 429 exhaustion within a specific Map-Reduce task triggers regional sharding failovers only within that thread. Sibling processes continue without interruptions.

---

## Contributing Protocols

To contribute features or modify ResearchBot:
1. **Maintain Interface Boundaries**: All layout and SwiftUI operations belong in the frontend application. Script executions, web scraping, and API proxy routing belong in the Python backend. Do not bridge UI code and processing logic.
2. **Adhere to Session Rules**: All file writes and database states must target individual timestamped sessions under `/research_knowledge_base/runs/`. Do not perform global database updates or write files to root directories.
3. **No Direct Commits**: Do not run automated git commits or push code to remote branches without explicit developer review and verification. All changes must be manually approved and authorized.
