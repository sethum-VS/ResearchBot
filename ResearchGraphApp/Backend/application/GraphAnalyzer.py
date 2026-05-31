"""
GraphAnalyzer.py — Phase 4.5: Academic Graph Topology Analysis (Full-Corpus).

Reads graphify-out/graph.json and the Markdown corpus from the current pipeline
run, then runs three concurrent Gemini 2.5 Pro calls (Map-Reduce) — one per gap
category — each pinned to a different Vertex region with independent failover.

A lightweight fourth call synthesizes the executive summary from merged findings.
Source corpus files are loaded concurrently via asyncio.to_thread.

Output matches the Swift `academic_gap_analysis` schema (summary + three
categories with per-entry `references`, plus `source_files`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from google import genai
from google.genai import types
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

from infrastructure.FileStorage import get_session_dir_from_env
from infrastructure.TextChunker import extract_academic_bookends

logger = logging.getLogger(__name__)

_STABLE_REGIONS: list[str] = [
    "europe-west4",
    "us-east4",
    "asia-northeast1",
    "us-central1",
]

# Per-category starting region + failover chain (Map-Reduce load balancer).
_CATEGORY_REGION_PLANS: dict[str, list[str]] = {
    "structural_holes": ["europe-west4", "us-central1", "us-east4", "asia-northeast1"],
    "high_degree_limitations": ["us-east4", "asia-northeast1", "us-central1", "europe-west4"],
    "orphaned_solutions": ["asia-northeast1", "us-central1", "europe-west4", "us-east4"],
}

_CATEGORY_LABELS: dict[str, str] = {
    "structural_holes": "Structural Holes",
    "high_degree_limitations": "High-Degree Limitations",
    "orphaned_solutions": "Orphaned Solutions",
}

_SUMMARY_REGION_PLAN: list[str] = ["global", "us-central1", "europe-west4", "us-east4"]

_ANALYSIS_MODEL = "gemini-2.5-pro"

# Phase 4.5 corpus must mirror Graphify inputs (no raw_ingestion).
_ALLOWED_CORPUS_PARENTS = frozenset({"processed_summaries", "agent_scrapes"})

# Per-file semantic chunking threshold (see TextChunker.extract_academic_bookends).
_LARGE_FILE_THRESHOLD = 60_000
_MAX_CHARS_PER_ACADEMIC_FILE = 75_000

# Graphify graphs with fewer nodes get a document-derived topology fallback.
_MIN_GRAPH_NODES = 2

# Keep map prompts under Gemini's per-request input limit (topology + system + corpus).
# 600k chars utilises enterprise-tier quotas; leaves ample headroom for topology + prompts.
_MAX_TOTAL_CORPUS_CHARS = 600_000

# Dynamic corpus allocation — protected buckets (Phase 4.5).
_SYNTHESIS_BUDGET = 50_000
_WEB_SCRAPE_BUDGET = 100_000
_ACADEMIC_BUDGET = 450_000

_CorpusEntry = tuple[Path, str, str]  # path, filename, delimited block

_SHARED_CONTEXT = """You are an Academic Graph Analyzer helping university students discover Final Year Project (FYP) research gaps.

--- USER RESEARCH INTENT ---
Core Topic: {core_topic}
User Intent: {user_intent}

You will receive TWO inputs:

  (A) The COMPLETE knowledge-graph topology (every node, every edge, every community).
  (B) The FULL set of source Markdown documents. Each document is delimited with its exact filename.

Cross-reference topology against the source text. Every claim MUST cite exact filename(s) in `references` from the SOURCE DOCUMENTS section only. If a signal cannot be grounded, omit that entry.

CRITICAL DIRECTIVE: You must analyze the topology and documents specifically through the lens of the User Research Intent above. Prioritize gaps, limitations, and solutions that are directly relevant to solving the user's core topic. Do not highlight generic academic gaps that do not serve the user's specific research goal.

Rules for `references`:
  - Use ONLY filenames from SOURCE DOCUMENTS. Do not invent or rename them.
  - Include 1-4 strongest supporting filenames per entry.
"""

_PROMPT_STRUCTURAL_HOLES = _SHARED_CONTEXT + """
Analyze ONLY for **Structural Holes** — disconnected or loosely connected communities (e.g., societal-problem cluster vs. technology cluster with few bridging edges). For each hole: name communities involved, explain the disconnect using source evidence, and suggest a bridging FYP angle. Focus on bridging opportunities that directly serve the student's proposed project described in the USER RESEARCH INTENT.

Return ONLY valid JSON (no markdown fences) with this shape:

{{{{
  "structural_holes": [
    {{{{
      "title": "short title",
      "communities_involved": ["community A", "community B"],
      "description": "why this is a structural hole, source-grounded",
      "bridging_opportunity": "concrete FYP angle",
      "references": ["exact_filename_1.md"]
    }}}}
  ]
}}}}

If no defensible structural holes exist, return {{{{"structural_holes": []}}}}. Be specific to this graph — no generic advice.
"""

_PROMPT_HIGH_DEGREE = _SHARED_CONTEXT + """
Analyze ONLY for **High-Degree Limitation Nodes** — limitation/challenge/weakness nodes (e.g., latency, privacy, scalability) with multiple incoming edges from different sources. Confirm in source text that gaps are multi-source validated, not single-paper opinions. Confirm limitations are validated across multiple sources AND are relevant to the user's stated research intent.

Return ONLY valid JSON (no markdown fences) with this shape:

{{{{
  "high_degree_limitations": [
    {{{{
      "title": "limitation theme",
      "node_labels": ["label1", "label2"],
      "degree": 0,
      "description": "why this is a validated multi-source gap",
      "evidence": "quote, paraphrase, or pattern across cited sources",
      "references": ["exact_filename_1.md"]
    }}}}
  ]
}}}}

If none exist, return {{{{"high_degree_limitations": []}}}}. Be specific to this graph.
"""

_PROMPT_ORPHANED = _SHARED_CONTEXT + """
Analyze ONLY for **Orphaned Solutions** — solution/method nodes whose outgoing edges point to failure conditions, drawbacks, or "fails when X". Verify failures in source text; describe a concrete technical FYP contribution that addresses the user's core topic.

Return ONLY valid JSON (no markdown fences) with this shape:

{{{{
  "orphaned_solutions": [
    {{{{
      "title": "solution node label",
      "failure_conditions": ["condition 1", "condition 2"],
      "description": "why the solution is undermined in the literature",
      "technical_contribution": "what an FYP could build or fix",
      "references": ["exact_filename_1.md"]
    }}}}
  ]
}}}}

If none exist, return {{{{"orphaned_solutions": []}}}}. Be specific to this graph.
"""

_SUMMARY_PROMPT = """You are summarizing academic graph gap analysis for a university FYP student.

The student's research topic is: {core_topic}
Their intent: {user_intent}

You will receive a digest of structural holes, high-degree limitations, and orphaned solutions already extracted from their research graph.

Write a 2-4 sentence executive summary covering the single most actionable FYP angle specifically for the student's topic. Be concrete; do not repeat every item. Anchor your summary to the student's stated research intent.

Return ONLY valid JSON: {{"summary": "your 2-4 sentences here"}}
"""

_SYSTEM_INSTRUCTION = (
    "You analyze knowledge-graph topology against source documents for university "
    "Final Year Project gap hunting. Every insight must cite real source filenames. "
    "Respond with strict JSON only."
)

_SYSTEM_INSTRUCTION_SUMMARY = (
    "You write concise executive summaries for FYP gap analysis. Respond with strict JSON only."
)


def _is_resource_exhausted(exc: Exception) -> bool:
    exc_str = str(exc).lower()
    return "429" in exc_str or "resourceexhausted" in exc_str or "resource_exhausted" in exc_str


def _get_project_id() -> str:
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT_ID")
    if not project_id:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT_ID not set.")
    return project_id


def _make_client(location: str = "global") -> genai.Client:
    return genai.Client(
        vertexai=True,
        project=_get_project_id(),
        location=location,
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    retry=retry_if_exception(_is_resource_exhausted),
)
def _call_gemini(client: genai.Client, contents: list, config: types.GenerateContentConfig):
    return client.models.generate_content(
        model=_ANALYSIS_MODEL,
        contents=contents,
        config=config,
    )


def _compute_degrees(graph_data: dict) -> Counter:
    degrees: Counter = Counter()
    for link in graph_data.get("links", []):
        degrees[link["source"]] += 1
        degrees[link["target"]] += 1
    return degrees


def _node_index(graph_data: dict) -> dict[str, dict]:
    return {node["id"]: node for node in graph_data.get("nodes", []) if "id" in node}


def _build_topology_summary(graph_data: dict) -> str:
    """Emit the complete topology — every node, community, and edge."""
    nodes = graph_data.get("nodes", [])
    links = graph_data.get("links", [])
    degrees = _compute_degrees(graph_data)
    by_id = _node_index(graph_data)

    communities: dict[str, list[dict]] = defaultdict(list)
    for node in nodes:
        comm = node.get("community_name") or node.get("community") or "Unclustered"
        communities[str(comm)].append({
            "id": node.get("id"),
            "label": node.get("label", ""),
            "degree": degrees.get(node.get("id"), 0),
            "type": node.get("type") or node.get("category") or "",
        })

    community_blocks: list[str] = []
    for comm, members in sorted(communities.items(), key=lambda x: -len(x[1])):
        members_sorted = sorted(members, key=lambda m: -m["degree"])
        labels = [
            f"{m['label']}[deg={m['degree']}]"
            for m in members_sorted
            if m["label"]
        ]
        community_blocks.append(
            f"Community '{comm}' ({len(members_sorted)} nodes): " + ", ".join(labels)
        )

    nodes_full = [
        {
            "id": n.get("id"),
            "label": n.get("label", ""),
            "degree": degrees.get(n.get("id"), 0),
            "community": n.get("community_name") or n.get("community"),
            "type": n.get("type") or n.get("category") or "",
        }
        for n in nodes
    ]
    nodes_full.sort(key=lambda x: -x["degree"])

    link_lines: list[str] = []
    for link in links:
        src = by_id.get(link["source"], {})
        tgt = by_id.get(link["target"], {})
        src_l = src.get("label") or link["source"]
        tgt_l = tgt.get("label") or link["target"]
        rel = link.get("type") or link.get("relation") or link.get("label") or "related_to"
        link_lines.append(f"  {src_l} --[{rel}]--> {tgt_l}")

    limitation_keywords = re.compile(
        r"limit|weak|fail|risk|challenge|drawback|bottleneck|latency|privacy|"
        r"scalab|error|gap|problem|issue|constraint|barrier|shortcoming",
        re.I,
    )
    limitation_candidates = [
        n for n in nodes_full
        if limitation_keywords.search(n.get("label") or "")
    ]

    parts = [
        f"Graph stats: {len(nodes)} nodes, {len(links)} edges, {len(communities)} communities.",
        "",
        "=== Communities (ALL nodes per cluster) ===",
        *community_blocks,
        "",
        "=== All nodes (sorted by degree desc) ===",
        json.dumps(nodes_full, ensure_ascii=False),
        "",
        "=== Limitation-themed nodes (auto-tagged candidates) ===",
        json.dumps(limitation_candidates, ensure_ascii=False) if limitation_candidates else "  (none auto-tagged)",
        "",
        f"=== All edges ({len(link_lines)}) ===",
        *link_lines,
    ]
    return "\n".join(parts)


def _bucket_from_filename(name: str) -> str:
    """Infer corpus bucket from a source filename (basename only)."""
    lower = name.lower()
    if lower.startswith("academic_fulltext_"):
        return "academic"
    if "_urlrefiner" in lower or lower == "academic_scrape.md":
        return "web"
    return "synthesis"


def _build_document_fallback_graph(filenames: list[str]) -> dict[str, Any]:
    """
    Synthetic topology when Graphify fails or returns a degenerate graph.
    One node per source filename; light same-bucket edges for Map-Reduce context.
    """
    nodes: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    by_bucket: dict[str, list[str]] = defaultdict(list)

    for name in filenames[:40]:
        stem = Path(name).stem.replace("_", " ")[:72]
        node_id = re.sub(r"[^\w.-]", "_", name)
        bucket = _bucket_from_filename(name)

        nodes.append({
            "id": node_id,
            "label": stem or name,
            "community": {"synthesis": 0, "web": 1, "academic": 2}.get(bucket, 3),
            "community_name": f"{bucket.title()} Sources",
            "type": "document",
            "degree": 0,
        })
        by_bucket[bucket].append(node_id)

    for bucket_ids in by_bucket.values():
        for i in range(len(bucket_ids) - 1):
            links.append({
                "source": bucket_ids[i],
                "target": bucket_ids[i + 1],
                "type": "same_bucket",
            })

    for n in nodes:
        n["degree"] = sum(
            1 for link in links
            if link["source"] == n["id"] or link["target"] == n["id"]
        )

    return {
        "directed": False,
        "multigraph": False,
        "graph": {"fallback": True},
        "nodes": nodes,
        "links": links,
        "hyperedges": [],
    }


def _list_eligible_corpus_paths(
    current_run_files: Iterable[Path],
) -> list[Path]:
    """List corpus paths without reading file bodies (for fallback topology only)."""
    paths: list[Path] = []
    seen: set[Path] = set()
    for raw_path in current_run_files or []:
        try:
            p = Path(raw_path).resolve()
        except (TypeError, OSError, ValueError):
            continue
        if p in seen or not p.is_file() or p.suffix.lower() != ".md":
            continue
        if p.parent.name not in _ALLOWED_CORPUS_PARENTS:
            continue
        seen.add(p)
        paths.append(p)
    return paths


def _resolve_graph_data(
    graph_json_path: Path | None,
    graphify_error: str | None,
    current_run_files: Iterable[Path],
) -> tuple[dict[str, Any], str | None]:
    """
    Load graph.json or build document fallback when missing / sparse / Graphify failed.
    Returns (graph_data, warning_message).
    """
    warning: str | None = graphify_error
    graph_data: dict[str, Any] = {"nodes": [], "links": []}

    if graph_json_path is not None and graph_json_path.is_file():
        try:
            graph_data = json.loads(graph_json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            warning = f"Invalid graph.json: {e}"
            graph_data = {"nodes": [], "links": []}

    node_count = len(graph_data.get("nodes") or [])
    if node_count >= _MIN_GRAPH_NODES:
        if graphify_error:
            return graph_data, f"Degraded graph ({node_count} nodes): {graphify_error}"
        return graph_data, None

    if node_count > 0 and node_count < _MIN_GRAPH_NODES:
        warning = (
            f"Sparse graph ({node_count} node(s)); using document topology fallback."
        )
    elif graphify_error:
        warning = f"Graphify failed ({graphify_error}); using document topology fallback."

    corpus_paths = _list_eligible_corpus_paths(current_run_files)
    filenames = [p.name for p in corpus_paths]
    if not filenames:
        return graph_data, warning or "No source documents for fallback topology."

    fallback = _build_document_fallback_graph(filenames)
    print(
        f"PROGRESS: Phase 4.5 — document topology fallback: "
        f"{len(fallback['nodes'])} nodes, {len(fallback['links'])} edges "
        f"from {len(filenames)} source file(s).",
        flush=True,
    )
    return fallback, warning


def _topology_stats_line(graph_data: dict) -> str:
    return (
        f"{len(graph_data.get('nodes', []))} nodes, "
        f"{len(graph_data.get('links', []))} edges"
    )


def _read_single_markdown(raw_path: Path) -> tuple[str, str] | None:
    """Sync helper: read one .md file; returns (filename, block) or None."""
    if not raw_path.is_file() or raw_path.suffix.lower() != ".md":
        return None
    try:
        text = raw_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        logger.warning("GraphAnalyzer: failed to read %s — %s", raw_path, e)
        return None
    if not text.strip():
        return None
    bucket = _classify_corpus_bucket(raw_path) or "corpus"
    original_len = len(text)
    
    if bucket == "academic":
        text = extract_academic_bookends(text, max_chars=_MAX_CHARS_PER_ACADEMIC_FILE)
        if len(text) == _MAX_CHARS_PER_ACADEMIC_FILE:
            text += "\n\n[WARNING: Paper truncated to meet 75,000 character limit]"
    else:
        text = extract_academic_bookends(text, max_chars=_LARGE_FILE_THRESHOLD)

    if len(text) < original_len:
        print(
            f"PROGRESS: Phase 4.5 — intelligently chunking {bucket} file "
            f"{raw_path.name} ({original_len:,} → {len(text):,} chars).",
            flush=True,
        )
    block = f"<<<FILE: {raw_path.name}>>>\n{text}\n<<<END FILE: {raw_path.name}>>>"
    return raw_path.name, block


def _classify_corpus_bucket(path: Path) -> str | None:
    """Return synthesis | web | academic, or None if not a corpus file."""
    parent = path.parent.name
    name = path.name.lower()
    if parent == "processed_summaries":
        if name.startswith("academic_fulltext_"):
            return "academic"
        return "synthesis"
    if parent == "agent_scrapes":
        return "web"
    return None


def _synthesis_sort_key(path: Path) -> tuple[float, str]:
    try:
        return (-path.stat().st_mtime, path.name)
    except OSError:
        return (0.0, path.name)


def _web_scrape_sort_key(path: Path) -> tuple[int, float, str]:
    """URLRefiner first, then other agent_scrapes; newest first within each tier."""
    url_tier = 0 if "_urlrefiner" in path.name.lower() else 1
    try:
        mtime = -path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (url_tier, mtime, path.name)


def _academic_sort_key(path: Path) -> tuple[float, str]:
    try:
        return (-path.stat().st_mtime, path.name)
    except OSError:
        return (0.0, path.name)


def _pack_bucket(
    entries: list[_CorpusEntry],
    budget: int,
) -> tuple[list[_CorpusEntry], int, int]:
    """Include whole delimited files only; skip any that exceed remaining budget."""
    included: list[_CorpusEntry] = []
    used = 0
    skipped = 0
    for entry in entries:
        block_len = len(entry[2])
        if used + block_len <= budget:
            included.append(entry)
            used += block_len
        else:
            skipped += 1
    return included, used, skipped


def _trim_priority(path: Path) -> int:
    """Lower value = dropped first by the global 240k safety net."""
    bucket = _classify_corpus_bucket(path)
    name = path.name.lower()
    if bucket == "web":
        return 0 if "_urlrefiner" not in name else 1
    if bucket == "academic":
        return 2
    if bucket == "synthesis":
        return 3
    return 0


def _apply_global_corpus_safety_net(
    included: list[_CorpusEntry],
) -> tuple[list[_CorpusEntry], int]:
    """Drop whole files (lowest priority first) if bucket totals exceed the 600k cap."""
    total = sum(len(entry[2]) for entry in included)
    if total <= _MAX_TOTAL_CORPUS_CHARS:
        return included, 0

    drop_order = sorted(
        range(len(included)),
        key=lambda i: (_trim_priority(included[i][0]), -i),
    )
    dropped: set[int] = set()
    for idx in drop_order:
        if total <= _MAX_TOTAL_CORPUS_CHARS:
            break
        total -= len(included[idx][2])
        dropped.add(idx)

    if dropped:
        print(
            f"PROGRESS: Phase 4.5 — global corpus safety net: dropped {len(dropped)} "
            f"file(s) to stay within {_MAX_TOTAL_CORPUS_CHARS:,} chars.",
            flush=True,
        )

    surviving = [entry for i, entry in enumerate(included) if i not in dropped]
    return surviving, len(dropped)


def _apply_dynamic_corpus_allocation(
    entries: list[_CorpusEntry],
) -> tuple[str, list[str], int]:
    """
    Pack SOURCE DOCUMENTS into protected synthesis, web, and academic buckets.
    """
    synthesis_entries: list[_CorpusEntry] = []
    web_entries: list[_CorpusEntry] = []
    academic_entries: list[_CorpusEntry] = []

    for entry in entries:
        bucket = _classify_corpus_bucket(entry[0])
        if bucket == "synthesis":
            synthesis_entries.append(entry)
        elif bucket == "web":
            web_entries.append(entry)
        elif bucket == "academic":
            academic_entries.append(entry)

    synthesis_entries.sort(key=lambda e: _synthesis_sort_key(e[0]))
    web_entries.sort(key=lambda e: _web_scrape_sort_key(e[0]))
    academic_entries.sort(key=lambda e: _academic_sort_key(e[0]))

    syn_inc, syn_used, syn_skip = _pack_bucket(synthesis_entries, _SYNTHESIS_BUDGET)
    synthesis_spare = _SYNTHESIS_BUDGET - syn_used

    # Rollover: unused synthesis -> Web (up to hard ceiling of 120_000 for web)
    web_cap_max = 120_000
    web_rollover_allowed = max(0, web_cap_max - _WEB_SCRAPE_BUDGET)
    web_rollover_actual = min(synthesis_spare, web_rollover_allowed)
    web_cap = _WEB_SCRAPE_BUDGET + web_rollover_actual
    
    web_inc, web_used, web_skip = _pack_bucket(web_entries, web_cap)
    web_spare = web_cap - web_used

    # All remaining unused capacity across both Synthesis and Web must roll entirely into Academic
    academic_cap = _ACADEMIC_BUDGET + (synthesis_spare - web_rollover_actual) + web_spare
    acad_inc, acad_used, acad_skip = _pack_bucket(academic_entries, academic_cap)

    if synthesis_spare > 0 or web_spare > 0:
        rollover_parts = []
        if web_rollover_actual > 0:
            rollover_parts.append(f"{web_rollover_actual:,} chars synthesis spare → web (+{web_rollover_actual:,} cap)")
        acad_bonus = (synthesis_spare - web_rollover_actual) + web_spare
        if acad_bonus > 0:
            rollover_parts.append(f"{acad_bonus:,} chars spare → academic (+{acad_bonus:,} cap)")
        print(
            f"PROGRESS: Phase 4.5 — corpus rollover: {'; '.join(rollover_parts)}.",
            flush=True,
        )

    def _bucket_line(
        label: str,
        included: list[_CorpusEntry],
        used: int,
        cap: int,
        eligible: int,
        skipped: int,
    ) -> str:
        return (
            f"{label} {len(included)}/{eligible} file(s) "
            f"({used:,}/{cap:,} chars"
            + (f", {skipped} skipped" if skipped else "")
            + ")"
        )

    print(
        f"PROGRESS: Phase 4.5 — dynamic corpus (cap={_MAX_TOTAL_CORPUS_CHARS:,}): "
        + _bucket_line(
            "synthesis",
            syn_inc,
            syn_used,
            _SYNTHESIS_BUDGET,
            len(synthesis_entries),
            syn_skip,
        )
        + "; "
        + _bucket_line(
            "web",
            web_inc,
            web_used,
            web_cap,
            len(web_entries),
            web_skip,
        )
        + "; "
        + _bucket_line(
            "academic",
            acad_inc,
            acad_used,
            academic_cap,
            len(academic_entries),
            acad_skip,
        )
        + ".",
        flush=True,
    )

    # SOURCE DOCUMENTS block order: synthesis → web → academic (load narrative).
    all_inc = syn_inc + web_inc + acad_inc
    all_inc, safety_dropped = _apply_global_corpus_safety_net(all_inc)

    blocks = [entry[2] for entry in all_inc]
    filenames = [entry[1] for entry in all_inc]
    bucket_omitted = syn_skip + web_skip + acad_skip + safety_dropped

    if bucket_omitted and not safety_dropped:
        print(
            f"PROGRESS: Phase 4.5 — corpus buckets: omitted {bucket_omitted} "
            f"whole file(s) that exceeded bucket caps.",
            flush=True,
        )

    return "\n\n".join(blocks), filenames, bucket_omitted


async def _load_source_corpus_async(
    current_run_files: Iterable[Path],
) -> tuple[str, list[str]]:
    """
    Concurrently read every Markdown file and build the SOURCE DOCUMENTS block.
    Uses resolved paths (not basenames) so duplicate filenames are not dropped.
    """
    paths: list[Path] = []
    seen: set[Path] = set()
    skipped_non_graphify = 0
    for raw_path in current_run_files or []:
        try:
            p = Path(raw_path).resolve()
        except Exception:
            continue
        if p in seen:
            continue
        if not p.is_file() or p.suffix.lower() != ".md":
            continue
        if p.parent.name not in _ALLOWED_CORPUS_PARENTS:
            skipped_non_graphify += 1
            continue
        seen.add(p)
        paths.append(p)

    if skipped_non_graphify:
        print(
            f"PROGRESS: Phase 4.5 — corpus aligned with Graphify: skipped "
            f"{skipped_non_graphify} file(s) outside processed_summaries/ "
            f"and agent_scrapes/ (e.g. raw_ingestion/).",
            flush=True,
        )

    if not paths:
        return "(no source documents available for this run)", []

    results = await asyncio.gather(
        *[asyncio.to_thread(_read_single_markdown, p) for p in paths],
        return_exceptions=True,
    )

    entries: list[tuple[Path, str, str]] = []
    for path, item in zip(paths, results):
        if isinstance(item, Exception):
            logger.warning("GraphAnalyzer: corpus read error — %s", item)
            continue
        if item is None:
            continue
        name, block = item
        entries.append((path, name, block))

    if not entries:
        return "(no source documents available for this run)", []

    corpus, filenames, _ = _apply_dynamic_corpus_allocation(entries)

    print(
        f"PROGRESS: Phase 4.5 — loaded {len(filenames)} source files "
        f"({len(corpus):,} chars, async I/O, dynamic buckets).",
        flush=True,
    )
    return corpus, filenames


def _parse_json_response(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def _empty_analysis(
    error: str | None = None,
    source_files: list[str] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "summary": "Academic gap analysis was not available for this run.",
        "structural_holes": [],
        "high_degree_limitations": [],
        "orphaned_solutions": [],
        "source_files": source_files or [],
    }
    if error:
        out["error"] = error
    return out


def _sanitize_references(
    refs: Any,
    allowed_filenames: set[str],
) -> list[str]:
    if not isinstance(refs, list):
        return []
    cleaned: list[str] = []
    for r in refs:
        if not isinstance(r, str):
            continue
        name = Path(r).name.strip()
        if name in allowed_filenames and name not in cleaned:
            cleaned.append(name)
    return cleaned


def _scrub_category_list(
    items: Any,
    allowed_filenames: set[str],
) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item = dict(item)
        item["references"] = _sanitize_references(
            item.get("references"), allowed_filenames
        )
        cleaned.append(item)
    return cleaned


def _build_llm_contents(prompt: str, topology_block: str, source_block: str) -> list[str]:
    return [
        f"{prompt}\n\n"
        f"--- GRAPH TOPOLOGY ---\n{topology_block}\n\n"
        f"--- SOURCE DOCUMENTS ---\n{source_block}"
    ]


async def _analyze_category_async(
    category_key: str,
    prompt: str,
    topology_block: str,
    source_block: str,
    allowed_filenames: set[str],
) -> list[dict[str, Any]]:
    """
    Run a single category analysis with regional sharding and per-task failover.
    """
    label = _CATEGORY_LABELS[category_key]
    regions = _CATEGORY_REGION_PLANS[category_key]
    config = types.GenerateContentConfig(
        max_output_tokens=16384,
        system_instruction=_SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
    )
    contents = _build_llm_contents(prompt, topology_block, source_block)
    last_exc: Exception | None = None

    for region in regions:
        print(
            f"PROGRESS: Phase 4.5 — routing {label} to {region}...",
            flush=True,
        )
        try:
            client = await asyncio.to_thread(_make_client, region)
            response = await asyncio.to_thread(_call_gemini, client, contents, config)
            if not response.text:
                raise RuntimeError(f"Empty response for {category_key} in {region}")
            parsed = _parse_json_response(response.text)
            items = _scrub_category_list(parsed.get(category_key), allowed_filenames)
            print(
                f"PROGRESS: Phase 4.5 — ✓ {label} complete via {region} "
                f"({len(items)} entries).",
                flush=True,
            )
            return items
        except Exception as exc:
            last_exc = exc
            if _is_resource_exhausted(exc):
                logger.warning(
                    "GraphAnalyzer: %s 429/resource exhausted at %s — failover",
                    category_key,
                    region,
                )
                print(
                    f"PROGRESS: Phase 4.5 — {label} quota hit at {region}, "
                    f"retrying next region...",
                    flush=True,
                )
            else:
                logger.warning(
                    "GraphAnalyzer: %s failed at %s: %s",
                    category_key,
                    region,
                    exc,
                )
                print(
                    f"PROGRESS: Phase 4.5 — {label} error at {region}: {exc}",
                    flush=True,
                )

    raise RuntimeError(
        f"{label} analysis failed after exhausting regions "
        f"({', '.join(regions)}). Last error: {last_exc}"
    )


def _build_findings_digest(
    structural_holes: list[dict[str, Any]],
    high_degree_limitations: list[dict[str, Any]],
    orphaned_solutions: list[dict[str, Any]],
) -> str:
    digest = {
        "structural_holes": [
            {
                "title": h.get("title"),
                "communities_involved": h.get("communities_involved"),
                "bridging_opportunity": h.get("bridging_opportunity"),
            }
            for h in structural_holes[:6]
        ],
        "high_degree_limitations": [
            {
                "title": h.get("title"),
                "node_labels": h.get("node_labels"),
                "degree": h.get("degree"),
            }
            for h in high_degree_limitations[:6]
        ],
        "orphaned_solutions": [
            {
                "title": s.get("title"),
                "failure_conditions": s.get("failure_conditions"),
                "technical_contribution": s.get("technical_contribution"),
            }
            for s in orphaned_solutions[:6]
        ],
    }
    return json.dumps(digest, ensure_ascii=False, indent=2)


async def _generate_executive_summary_async(
    structural_holes: list[dict[str, Any]],
    high_degree_limitations: list[dict[str, Any]],
    orphaned_solutions: list[dict[str, Any]],
    topology_stats: str,
    seed_context: dict[str, Any] | None = None,
) -> str:
    """Lightweight fourth call — small payload, fast summary synthesis."""
    core_topic = (
        seed_context.get("core_context", "General FYP Research")
        if seed_context
        else "General FYP Research"
    )
    user_intent = (
        seed_context.get("user_intent", "General Inquiry")
        if seed_context
        else "General Inquiry"
    )
    formatted_summary_prompt = _SUMMARY_PROMPT.format(
        core_topic=core_topic, user_intent=user_intent,
    )
    digest = _build_findings_digest(
        structural_holes, high_degree_limitations, orphaned_solutions
    )
    contents = [
        f"{formatted_summary_prompt}\n\n"
        f"--- FINDINGS DIGEST ---\n{digest}\n\n"
        f"--- TOPOLOGY STATS ---\n{topology_stats}"
    ]
    config = types.GenerateContentConfig(
        max_output_tokens=2048,
        system_instruction=_SYSTEM_INSTRUCTION_SUMMARY,
        response_mime_type="application/json",
    )
    last_exc: Exception | None = None

    for region in _SUMMARY_REGION_PLAN:
        print(
            f"PROGRESS: Phase 4.5 — routing Executive Summary to {region}...",
            flush=True,
        )
        try:
            client = await asyncio.to_thread(_make_client, region)
            response = await asyncio.to_thread(_call_gemini, client, contents, config)
            if not response.text:
                raise RuntimeError(f"Empty summary response in {region}")
            parsed = _parse_json_response(response.text)
            summary = str(parsed.get("summary") or "").strip()
            if summary:
                print(
                    f"PROGRESS: Phase 4.5 — ✓ Executive Summary via {region}.",
                    flush=True,
                )
                return summary
        except Exception as exc:
            last_exc = exc
            logger.warning("GraphAnalyzer: summary failed at %s: %s", region, exc)
            if _is_resource_exhausted(exc):
                print(
                    f"PROGRESS: Phase 4.5 — Executive Summary quota hit at {region}, "
                    f"retrying...",
                    flush=True,
                )

    logger.warning(
        "GraphAnalyzer: executive summary fallback after regions exhausted: %s",
        last_exc,
    )
    return _fallback_summary_from_findings(
        structural_holes, high_degree_limitations, orphaned_solutions
    )


def _fallback_summary_from_findings(
    structural_holes: list[dict[str, Any]],
    high_degree_limitations: list[dict[str, Any]],
    orphaned_solutions: list[dict[str, Any]],
) -> str:
    parts: list[str] = []
    if structural_holes:
        parts.append(
            f"Structural bridging opportunity: {structural_holes[0].get('title', 'see holes')}."
        )
    if high_degree_limitations:
        parts.append(
            f"Validated multi-source gap: {high_degree_limitations[0].get('title', 'see limitations')}."
        )
    if orphaned_solutions:
        parts.append(
            f"Orphaned solution angle: {orphaned_solutions[0].get('title', 'see solutions')}."
        )
    if parts:
        return " ".join(parts)
    return "Topology analyzed; see category lists below for FYP opportunities."


def _merge_analysis_results(
    structural_holes: list[dict[str, Any]],
    high_degree_limitations: list[dict[str, Any]],
    orphaned_solutions: list[dict[str, Any]],
    summary: str,
    allowed_filenames: set[str],
    partial_errors: list[str] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "summary": summary.strip() or "Topology analyzed; see category lists below.",
        "structural_holes": structural_holes,
        "high_degree_limitations": high_degree_limitations,
        "orphaned_solutions": orphaned_solutions,
        "source_files": sorted(allowed_filenames),
    }
    if partial_errors:
        out["error"] = "; ".join(partial_errors)
    return out


async def _run_map_reduce_analysis_async(
    graph_data: dict,
    current_run_files: Iterable[Path],
    seed_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Concurrent Map-Reduce over three gap categories + executive summary."""
    print(
        "PROGRESS: Phase 4.5 — Map-Reduce: 3 parallel category analyses "
        "+ executive summary...",
        flush=True,
    )

    # ── Extract seed context for prompt anchoring ────────────────────────
    core_topic = (
        seed_context.get("core_context", "General FYP Research")
        if seed_context
        else "General FYP Research"
    )
    user_intent = (
        seed_context.get("user_intent", "General Inquiry")
        if seed_context
        else "General Inquiry"
    )
    print(
        f"PROGRESS: Phase 4.5 — seed context anchored: "
        f"topic={core_topic[:80]!r}, intent={user_intent[:60]!r}",
        flush=True,
    )

    # Format prompts with the user's research intent
    formatted_structural = _PROMPT_STRUCTURAL_HOLES.format(
        core_topic=core_topic, user_intent=user_intent,
    )
    formatted_high_degree = _PROMPT_HIGH_DEGREE.format(
        core_topic=core_topic, user_intent=user_intent,
    )
    formatted_orphaned = _PROMPT_ORPHANED.format(
        core_topic=core_topic, user_intent=user_intent,
    )

    topology_block = _build_topology_summary(graph_data)
    source_block, filenames = await _load_source_corpus_async(current_run_files)
    allowed = set(filenames)

    print(
        f"PROGRESS: Phase 4.5 — corpus: {len(filenames)} source files, "
        f"{len(graph_data.get('nodes', []))} nodes, "
        f"{len(graph_data.get('links', []))} edges.",
        flush=True,
    )

    map_tasks = [
        _analyze_category_async(
            "structural_holes",
            formatted_structural,
            topology_block,
            source_block,
            allowed,
        ),
        _analyze_category_async(
            "high_degree_limitations",
            formatted_high_degree,
            topology_block,
            source_block,
            allowed,
        ),
        _analyze_category_async(
            "orphaned_solutions",
            formatted_orphaned,
            topology_block,
            source_block,
            allowed,
        ),
    ]

    raw_results = await asyncio.gather(*map_tasks, return_exceptions=True)

    partial_errors: list[str] = []
    structural_holes: list[dict[str, Any]] = []
    high_degree_limitations: list[dict[str, Any]] = []
    orphaned_solutions: list[dict[str, Any]] = []

    for key, result in zip(
        ("structural_holes", "high_degree_limitations", "orphaned_solutions"),
        raw_results,
    ):
        label = _CATEGORY_LABELS[key]
        if isinstance(result, Exception):
            msg = f"{label}: {result}"
            partial_errors.append(msg)
            logger.error("GraphAnalyzer Map task failed: %s", msg)
            print(f"PROGRESS: Phase 4.5 — ✗ {msg}", flush=True)
            continue
        if key == "structural_holes":
            structural_holes = result
        elif key == "high_degree_limitations":
            high_degree_limitations = result
        else:
            orphaned_solutions = result

    # Only fail the reduce step when every map task raised (not when models return []).
    if len(partial_errors) >= 3:
        raise RuntimeError(
            "All three category analyses failed. "
            + (partial_errors[0] if partial_errors else "unknown")
        )

    summary = await _generate_executive_summary_async(
        structural_holes,
        high_degree_limitations,
        orphaned_solutions,
        _topology_stats_line(graph_data),
        seed_context=seed_context,
    )

    return _merge_analysis_results(
        structural_holes,
        high_degree_limitations,
        orphaned_solutions,
        summary,
        allowed,
        partial_errors if partial_errors else None,
    )


def analyze_graph_topology(
    current_run_files: Iterable[Path] | None = None,
    graph_json_path: Path | None = None,
    graphify_error: str | None = None,
    seed_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Phase 4.5 entry point (sync wrapper around asyncio Map-Reduce).

    Returns academic_gap_analysis payload for Swift bridging.
    """
    path: Path | None
    if graph_json_path is not None:
        path = Path(graph_json_path)
    else:
        session_dir = get_session_dir_from_env()
        if session_dir is None:
            return _empty_analysis(
                "graph_json_path not provided and RESEARCHBOT_SESSION_DIR is unset."
            )
        path = session_dir / "graphify-out" / "graph.json"

    if path is not None and not path.is_file():
        path = None

    print(
        "PROGRESS: Phase 4.5 — analyzing graph topology + source corpus (Map-Reduce)...",
        flush=True,
    )

    graph_data, topology_warning = _resolve_graph_data(
        path, graphify_error, current_run_files or []
    )
    if not graph_data.get("nodes"):
        msg = topology_warning or "graph.json contains no nodes."
        print(f"PROGRESS: Phase 4.5 — ⚠ {msg}", flush=True)
        return _empty_analysis(msg)

    try:
        result = asyncio.run(
            _run_map_reduce_analysis_async(
                graph_data, current_run_files or [], seed_context=seed_context,
            )
        )
        if topology_warning:
            result["error"] = topology_warning
        print("PROGRESS: Phase 4.5 — ✓ academic gap analysis complete.", flush=True)
        return result
    except Exception as e:
        logger.error("GraphAnalyzer failed: %s", e)
        print(f"PROGRESS: Phase 4.5 — ✗ analysis error: {e}", flush=True)
        _, filenames = asyncio.run(
            _load_source_corpus_async(current_run_files or [])
        )
        return _empty_analysis(str(e), source_files=filenames)
