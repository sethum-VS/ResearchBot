"""
GraphAnalyzer.py — Phase 4.5: Academic Graph Topology Analysis (Full-Corpus).

Reads the compiled graphify-out/graph.json AND the raw Markdown source
documents from the current pipeline run, then sends both to Gemini 2.5 Pro
(Vertex AI with STABLE_REGIONS failover — same contract as DataRefiner /
VertexProxy).

The LLM is asked to reason about the FULL graph topology cross-referenced
against the source corpus, and to cite the exact source filenames that
support each insight. This enables the SwiftUI front-end to navigate from
a research gap straight to the underlying scrape for verification.

Output is a JSON object with three academic gap categories, each entry
including a `references` array of source filenames, plus an executive
summary and the list of available source filenames the LLM was given.
"""

from __future__ import annotations

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

from infrastructure.FileStorage import get_kb_root

logger = logging.getLogger(__name__)

_STABLE_REGIONS: list[str] = [
    "europe-west4",
    "us-east4",
    "asia-northeast1",
    "us-central1",
]

_ANALYSIS_MODEL = "gemini-2.5-pro"

# Defensive per-file cap to keep individual outlier files from blowing past
# the 1M-token context window. The LLM still sees every file — only extreme
# multi-megabyte scrapes get tail-trimmed. No artificial node/edge limits.
_MAX_CHARS_PER_FILE = 120_000

_TOPOLOGY_PROMPT = """You are an Academic Graph Analyzer helping university students discover Final Year Project (FYP) research gaps.

You will receive TWO inputs:

  (A) The COMPLETE knowledge-graph topology (every node, every edge, every community) compiled by Graphify from the student's research corpus.
  (B) The FULL set of source Markdown documents the graph was extracted from. Each document is delimited and labeled with its exact filename.

Your task is to analyze the topology IN THE CONTEXT OF the source documents — do not treat them as separate. Use the graph to find structural signals, then verify and enrich each signal against the source text. Every claim you make MUST be supported by at least one source document, and you MUST cite the exact filename(s) of that document in the `references` array.

Identify FYP opportunities using these three academic indicators:

1. **Structural Holes** — Disconnected or loosely connected communities in the graph (e.g., a "Societal Problem" cluster and a "New Technology" cluster with few or no bridging edges). For each hole, name the communities involved, explain the disconnect using evidence from the source text, and suggest how a novel FYP could bridge them.

2. **High-Degree "Limitation" Nodes** — Nodes representing limitations, challenges, weaknesses (e.g., "High Latency", "Privacy Risks", "Scalability Issues") with multiple incoming edges from different source documents. Use the source text to confirm these are multi-source, validated gaps — not single-paper opinions.

3. **Orphaned Solutions** — Existing solutions/methods/approaches in the graph whose outgoing edges point to failure conditions, drawbacks, or "fails when X" situations. Verify in the source text that the failure is real and underexplored, then describe a concrete technical contribution.

Return ONLY valid JSON (no markdown fences, no preamble) matching this schema:

{
  "summary": "3-5 sentence executive summary for the student covering the most actionable FYP angle",
  "structural_holes": [
    {
      "title": "short title",
      "communities_involved": ["community A", "community B"],
      "description": "why this is a structural hole, with source-grounded reasoning",
      "bridging_opportunity": "concrete FYP angle to connect the clusters",
      "references": ["exact_filename_1.md", "exact_filename_2.md"]
    }
  ],
  "high_degree_limitations": [
    {
      "title": "limitation node label or theme",
      "node_labels": ["label1", "label2"],
      "degree": 0,
      "description": "why this is a validated multi-source gap",
      "evidence": "concrete quote, paraphrase, or pattern observed across the cited sources",
      "references": ["exact_filename_1.md", "exact_filename_2.md"]
    }
  ],
  "orphaned_solutions": [
    {
      "title": "solution node label",
      "failure_conditions": ["condition or drawback 1", "condition 2"],
      "description": "why this solution is undermined in the literature",
      "technical_contribution": "what an FYP could build or fix",
      "references": ["exact_filename_1.md"]
    }
  ]
}

Rules for `references`:
  - Use ONLY filenames that appear in the SOURCE DOCUMENTS section below. Do not invent or rename them.
  - Include 1-4 of the strongest supporting filenames per entry.
  - If a claim cannot be grounded in any provided source, omit that entry entirely.

If a category has no defensible signal in this run, return an empty array for that key. Be specific to the actual graph nodes and source content — do not produce generic FYP advice.
"""

_SYSTEM_INSTRUCTION = (
    "You analyze knowledge-graph topology against source documents for university "
    "Final Year Project gap hunting. Every insight must cite real source filenames. "
    "Respond with strict JSON only."
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
    """
    Emit the COMPLETE topology — every node, every community, every edge.
    No truncation; the LLM sees the full graph.
    """
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


def _load_source_corpus(current_run_files: Iterable[Path]) -> tuple[str, list[str]]:
    """
    Read every Markdown file in *current_run_files* and emit a single
    LLM-friendly block with explicit filename delimiters.

    Returns (corpus_text, list_of_filenames). Missing or unreadable files
    are silently skipped and excluded from the filename list.
    """
    blocks: list[str] = []
    filenames: list[str] = []
    seen: set[str] = set()

    for raw_path in current_run_files or []:
        try:
            p = Path(raw_path)
        except Exception:
            continue
        if not p.is_file():
            continue
        if p.suffix.lower() != ".md":
            continue
        if p.name in seen:
            continue

        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            logger.warning("GraphAnalyzer: failed to read %s — %s", p, e)
            continue

        if not text.strip():
            continue

        if len(text) > _MAX_CHARS_PER_FILE:
            text = text[:_MAX_CHARS_PER_FILE] + "\n\n[... truncated for context window ...]"

        seen.add(p.name)
        filenames.append(p.name)
        blocks.append(
            f"<<<FILE: {p.name}>>>\n{text}\n<<<END FILE: {p.name}>>>"
        )

    if not blocks:
        return "(no source documents available for this run)", []

    return "\n\n".join(blocks), filenames


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
    """Keep only references that point to a real filename we provided."""
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


def _normalize_analysis(
    parsed: dict[str, Any],
    allowed_filenames: set[str],
) -> dict[str, Any]:
    """Ensure required keys exist, list types, and references are validated."""
    def _scrub_list(items: Any) -> list[dict[str, Any]]:
        if not isinstance(items, list):
            return []
        cleaned: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item["references"] = _sanitize_references(
                item.get("references"), allowed_filenames
            )
            cleaned.append(item)
        return cleaned

    return {
        "summary": str(parsed.get("summary") or "").strip()
        or "Topology analyzed; see category lists below.",
        "structural_holes": _scrub_list(parsed.get("structural_holes")),
        "high_degree_limitations": _scrub_list(parsed.get("high_degree_limitations")),
        "orphaned_solutions": _scrub_list(parsed.get("orphaned_solutions")),
        "source_files": sorted(allowed_filenames),
    }


def _call_gemini_with_failover(
    topology_block: str,
    source_block: str,
    allowed_filenames: set[str],
) -> dict[str, Any]:
    config = types.GenerateContentConfig(
        max_output_tokens=16384,
        system_instruction=_SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
    )
    contents = [
        f"{_TOPOLOGY_PROMPT}\n\n"
        f"--- GRAPH TOPOLOGY ---\n{topology_block}\n\n"
        f"--- SOURCE DOCUMENTS ---\n{source_block}"
    ]

    last_exc: Exception | None = None

    try:
        client = _make_client("global")
        response = _call_gemini(client, contents, config)
        if response.text:
            return _normalize_analysis(
                _parse_json_response(response.text), allowed_filenames
            )
    except Exception as primary_exc:
        logger.warning("GraphAnalyzer: global endpoint failed (%s)", primary_exc)
        last_exc = primary_exc

    for region in _STABLE_REGIONS:
        try:
            print(f"PROGRESS: Phase 4.5 — regional failover → {region}", flush=True)
            client = _make_client(region)
            response = _call_gemini(client, contents, config)
            if response.text:
                logger.info("GraphAnalyzer: regional failover SUCCESS → %s", region)
                return _normalize_analysis(
                    _parse_json_response(response.text), allowed_filenames
                )
        except Exception as region_exc:
            logger.warning("GraphAnalyzer: region %s failed: %s", region, region_exc)
            last_exc = region_exc

    raise RuntimeError(
        f"Graph topology analysis failed after exhausting all regions. Last error: {last_exc}"
    )


def analyze_graph_topology(
    current_run_files: Iterable[Path] | None = None,
    graph_json_path: Path | None = None,
) -> dict[str, Any]:
    """
    Phase 4.5 entry point.

    Parameters
    ----------
    current_run_files : iterable of Path
        The exact set of Markdown files compiled in this pipeline run.
        Their text content is appended to the LLM prompt under
        `--- SOURCE DOCUMENTS ---` so the model can cross-reference
        topology against original source text and cite filenames.
    graph_json_path : Path, optional
        Override for graphify-out/graph.json. Defaults to the canonical KB
        location.

    Returns
    -------
    dict
        academic_gap_analysis payload (summary + three categories with
        per-entry `references`, plus `source_files` and optional `error`).
    """
    path = graph_json_path or (get_kb_root() / "graphify-out" / "graph.json")

    if not path.is_file():
        msg = f"graph.json not found at {path}"
        logger.warning(msg)
        print(f"PROGRESS: Phase 4.5 — ⚠ {msg}", flush=True)
        return _empty_analysis(msg)

    try:
        graph_data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return _empty_analysis(f"Invalid graph.json: {e}")

    if not graph_data.get("nodes"):
        return _empty_analysis("graph.json contains no nodes.")

    print("PROGRESS: Phase 4.5 — analyzing graph topology + source corpus...", flush=True)
    topology_block = _build_topology_summary(graph_data)
    source_block, filenames = _load_source_corpus(current_run_files or [])
    allowed = set(filenames)

    print(
        f"PROGRESS: Phase 4.5 — corpus: {len(filenames)} source files, "
        f"{len(graph_data.get('nodes', []))} nodes, "
        f"{len(graph_data.get('links', []))} edges.",
        flush=True,
    )

    try:
        result = _call_gemini_with_failover(topology_block, source_block, allowed)
        print("PROGRESS: Phase 4.5 — ✓ academic gap analysis complete.", flush=True)
        return result
    except Exception as e:
        logger.error("GraphAnalyzer failed: %s", e)
        print(f"PROGRESS: Phase 4.5 — ✗ analysis error: {e}", flush=True)
        return _empty_analysis(str(e), source_files=filenames)
