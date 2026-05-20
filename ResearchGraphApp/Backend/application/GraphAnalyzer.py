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

from infrastructure.FileStorage import get_kb_root

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

_MAX_CHARS_PER_FILE = 120_000

_SHARED_CONTEXT = """You are an Academic Graph Analyzer helping university students discover Final Year Project (FYP) research gaps.

You will receive TWO inputs:

  (A) The COMPLETE knowledge-graph topology (every node, every edge, every community).
  (B) The FULL set of source Markdown documents. Each document is delimited with its exact filename.

Cross-reference topology against the source text. Every claim MUST cite exact filename(s) in `references` from the SOURCE DOCUMENTS section only. If a signal cannot be grounded, omit that entry.

Rules for `references`:
  - Use ONLY filenames from SOURCE DOCUMENTS. Do not invent or rename them.
  - Include 1-4 strongest supporting filenames per entry.
"""

_PROMPT_STRUCTURAL_HOLES = f"""{_SHARED_CONTEXT}

Analyze ONLY for **Structural Holes** — disconnected or loosely connected communities (e.g., societal-problem cluster vs. technology cluster with few bridging edges). For each hole: name communities involved, explain the disconnect using source evidence, and suggest a bridging FYP angle.

Return ONLY valid JSON (no markdown fences) with this shape:

{{
  "structural_holes": [
    {{
      "title": "short title",
      "communities_involved": ["community A", "community B"],
      "description": "why this is a structural hole, source-grounded",
      "bridging_opportunity": "concrete FYP angle",
      "references": ["exact_filename_1.md"]
    }}
  ]
}}

If no defensible structural holes exist, return {{"structural_holes": []}}. Be specific to this graph — no generic advice.
"""

_PROMPT_HIGH_DEGREE = f"""{_SHARED_CONTEXT}

Analyze ONLY for **High-Degree Limitation Nodes** — limitation/challenge/weakness nodes (e.g., latency, privacy, scalability) with multiple incoming edges from different sources. Confirm in source text that gaps are multi-source validated, not single-paper opinions.

Return ONLY valid JSON (no markdown fences) with this shape:

{{
  "high_degree_limitations": [
    {{
      "title": "limitation theme",
      "node_labels": ["label1", "label2"],
      "degree": 0,
      "description": "why this is a validated multi-source gap",
      "evidence": "quote, paraphrase, or pattern across cited sources",
      "references": ["exact_filename_1.md"]
    }}
  ]
}}

If none exist, return {{"high_degree_limitations": []}}. Be specific to this graph.
"""

_PROMPT_ORPHANED = f"""{_SHARED_CONTEXT}

Analyze ONLY for **Orphaned Solutions** — solution/method nodes whose outgoing edges point to failure conditions, drawbacks, or "fails when X". Verify failures in source text; describe a concrete technical FYP contribution.

Return ONLY valid JSON (no markdown fences) with this shape:

{{
  "orphaned_solutions": [
    {{
      "title": "solution node label",
      "failure_conditions": ["condition 1", "condition 2"],
      "description": "why the solution is undermined in the literature",
      "technical_contribution": "what an FYP could build or fix",
      "references": ["exact_filename_1.md"]
    }}
  ]
}}

If none exist, return {{"orphaned_solutions": []}}. Be specific to this graph.
"""

_SUMMARY_PROMPT = """You are summarizing academic graph gap analysis for a university FYP student.

You will receive a digest of structural holes, high-degree limitations, and orphaned solutions already extracted from their research graph.

Write a 2-4 sentence executive summary covering the single most actionable FYP angle. Be concrete; do not repeat every item.

Return ONLY valid JSON: {"summary": "your 2-4 sentences here"}
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
    if len(text) > _MAX_CHARS_PER_FILE:
        text = text[:_MAX_CHARS_PER_FILE] + "\n\n[... truncated for context window ...]"
    block = f"<<<FILE: {raw_path.name}>>>\n{text}\n<<<END FILE: {raw_path.name}>>>"
    return raw_path.name, block


async def _load_source_corpus_async(
    current_run_files: Iterable[Path],
) -> tuple[str, list[str]]:
    """
    Concurrently read every Markdown file and build the SOURCE DOCUMENTS block.
    """
    paths: list[Path] = []
    seen: set[str] = set()
    for raw_path in current_run_files or []:
        try:
            p = Path(raw_path)
        except Exception:
            continue
        if p.name in seen:
            continue
        if not p.is_file() or p.suffix.lower() != ".md":
            continue
        seen.add(p.name)
        paths.append(p)

    if not paths:
        return "(no source documents available for this run)", []

    results = await asyncio.gather(
        *[asyncio.to_thread(_read_single_markdown, p) for p in paths],
        return_exceptions=True,
    )

    blocks: list[str] = []
    filenames: list[str] = []
    for item in results:
        if isinstance(item, Exception):
            logger.warning("GraphAnalyzer: corpus read error — %s", item)
            continue
        if item is None:
            continue
        name, block = item
        filenames.append(name)
        blocks.append(block)

    if not blocks:
        return "(no source documents available for this run)", []

    print(
        f"PROGRESS: Phase 4.5 — loaded {len(filenames)} source files (async I/O).",
        flush=True,
    )
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
) -> str:
    """Lightweight fourth call — small payload, fast summary synthesis."""
    digest = _build_findings_digest(
        structural_holes, high_degree_limitations, orphaned_solutions
    )
    contents = [
        f"{_SUMMARY_PROMPT}\n\n"
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
) -> dict[str, Any]:
    """Concurrent Map-Reduce over three gap categories + executive summary."""
    print(
        "PROGRESS: Phase 4.5 — Map-Reduce: 3 parallel category analyses "
        "+ executive summary...",
        flush=True,
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
            _PROMPT_STRUCTURAL_HOLES,
            topology_block,
            source_block,
            allowed,
        ),
        _analyze_category_async(
            "high_degree_limitations",
            _PROMPT_HIGH_DEGREE,
            topology_block,
            source_block,
            allowed,
        ),
        _analyze_category_async(
            "orphaned_solutions",
            _PROMPT_ORPHANED,
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

    if not any([structural_holes, high_degree_limitations, orphaned_solutions]):
        raise RuntimeError(
            "All three category analyses failed. "
            + (partial_errors[0] if partial_errors else "unknown")
        )

    summary = await _generate_executive_summary_async(
        structural_holes,
        high_degree_limitations,
        orphaned_solutions,
        _topology_stats_line(graph_data),
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
) -> dict[str, Any]:
    """
    Phase 4.5 entry point (sync wrapper around asyncio Map-Reduce).

    Returns academic_gap_analysis payload for Swift bridging.
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

    print(
        "PROGRESS: Phase 4.5 — analyzing graph topology + source corpus (Map-Reduce)...",
        flush=True,
    )

    try:
        result = asyncio.run(
            _run_map_reduce_analysis_async(graph_data, current_run_files or [])
        )
        print("PROGRESS: Phase 4.5 — ✓ academic gap analysis complete.", flush=True)
        return result
    except Exception as e:
        logger.error("GraphAnalyzer failed: %s", e)
        print(f"PROGRESS: Phase 4.5 — ✗ analysis error: {e}", flush=True)
        _, filenames = asyncio.run(
            _load_source_corpus_async(current_run_files or [])
        )
        return _empty_analysis(str(e), source_files=filenames)
