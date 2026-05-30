"""
GraphifyRunner.py — Shell executor for the Graphify knowledge-graph pipeline.

Routes LLM extraction through the local VertexProxy (localhost:8000) by using
Graphify's ``ollama`` backend — the only backend whose base_url is configurable
via the ``OLLAMA_BASE_URL`` environment variable.

SESSION ISOLATION CONTRACT
──────────────────────────
Every Graphify invocation writes its artefacts into the caller-supplied
session directory:

    research_knowledge_base/runs/session_<TIMESTAMP>/graphify-out/

No artefacts are ever written to the legacy shared root. The session path is
accepted as an immutable absolute string (``session_dir``) and propagated
verbatim to every subprocess call.

Interactive Console
───────────────────
``execute_graph_query`` and ``execute_graph_path`` shell out to the Graphify
CLI's native query / path tooling, scoped to a specific historical session,
returning their raw stdout for the SwiftUI ``GraphTerminalView`` console.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv
from infrastructure.FileStorage import (
    get_kb_root,
    get_session_dir_from_env,
    resolve_session_dir,
)

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_ENV_PATH = _BACKEND_DIR.parent.parent / ".env"
load_dotenv(_ENV_PATH)

logger = logging.getLogger(__name__)


class GraphifyError(Exception):
    """Raised when the graphify subprocess exits with a non-zero code."""

    def __init__(self, exit_code: int, stderr: str):
        self.exit_code = exit_code
        self.stderr = stderr
        super().__init__(
            f"graphify exited with code {exit_code}: {stderr}"
        )


# ── Resizable Sidebar (post-process injection) ──────────────────────────────

_RESIZER_INJECTION = """
<!-- Resizable sidebar injected by GraphifyRunner post-processing -->
<style>
  .resizer {
    width: 5px;
    cursor: col-resize;
    background: transparent;
    position: absolute;
    left: 0;
    top: 0;
    bottom: 0;
    z-index: 100;
    transition: background 0.15s ease;
  }
  .resizer:hover,
  .resizer.active {
    background: rgba(78, 121, 167, 0.6);
  }
  #sidebar {
    position: relative;
    min-width: 180px;
    max-width: 50vw;
  }
</style>
<script>
(function() {
  const sidebar = document.getElementById('sidebar');
  if (!sidebar) return;

  const resizer = document.createElement('div');
  resizer.className = 'resizer';
  sidebar.prepend(resizer);

  let startX, startWidth;

  function onMouseDown(e) {
    startX = e.clientX;
    startWidth = sidebar.offsetWidth;
    resizer.classList.add('active');
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
    e.preventDefault();
  }

  function onMouseMove(e) {
    const delta = startX - e.clientX;
    const newWidth = Math.max(180, Math.min(window.innerWidth * 0.5, startWidth + delta));
    sidebar.style.width = newWidth + 'px';
  }

  function onMouseUp() {
    resizer.classList.remove('active');
    document.removeEventListener('mousemove', onMouseMove);
    document.removeEventListener('mouseup', onMouseUp);
  }

  resizer.addEventListener('mousedown', onMouseDown);
})();
</script>
"""


# ── Subprocess Environment ──────────────────────────────────────────────────

def _graphify_env() -> dict:
    """Build the environment used for every graphify subprocess call."""
    env = os.environ.copy()
    env["OLLAMA_BASE_URL"] = "http://localhost:8000/v1"
    env["OLLAMA_API_KEY"] = "dummy-proxy-key"
    return env


def _graphify_token_budget() -> str:
    """Per-chunk extraction budget (higher → more entities per source file)."""
    return os.getenv("GRAPHIFY_TOKEN_BUDGET", "8192").strip() or "8192"


_MIN_GRAPHIFY_NODES = 1
_GRAPHIFY_EXTRACT_ATTEMPTS = 2


def _graph_json_node_count(graph_path: Path) -> int:
    if not graph_path.is_file():
        return 0
    try:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
        return len(data.get("nodes") or [])
    except (json.JSONDecodeError, OSError):
        return 0


def _run_graphify_extract(
    filtered_dir: Path,
    cwd: str,
    env: dict,
) -> subprocess.CompletedProcess[str]:
    """
    Run graphify extract with retry on failure or degenerate (sparse) graphs.
    """
    try:
        primary_budget = int(_graphify_token_budget())
    except ValueError:
        primary_budget = 8192
    budgets = [
        primary_budget,
        min(primary_budget * 2, 16384),
    ][: _GRAPHIFY_EXTRACT_ATTEMPTS]

    graph_path = filtered_dir / "graphify-out" / "graph.json"
    last_error = "graphify extract failed"

    for attempt, budget in enumerate(budgets, start=1):
        if attempt > 1:
            print(
                f"PROGRESS: Phase 4 — retrying graphify extract (attempt {attempt}, "
                f"token-budget={budget})...",
                flush=True,
            )
        result = subprocess.run(
            [
                "graphify", "extract", str(filtered_dir),
                "--backend", "ollama",
                "--model", "llama-4-scout",
                "--token-budget", str(budget),
            ],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=1800,
            env=env,
        )
        nodes = _graph_json_node_count(graph_path)
        err_body = "\n".join(
            part.strip()
            for part in (result.stderr, result.stdout)
            if part and part.strip()
        ).strip()

        if result.returncode == 0 and nodes >= _MIN_GRAPHIFY_NODES:
            if attempt > 1:
                print(
                    f"PROGRESS: Phase 4 — graphify extract recovered "
                    f"({nodes} nodes on attempt {attempt}).",
                    flush=True,
                )
            return result

        if nodes > 0 and nodes < _MIN_GRAPHIFY_NODES:
            last_error = (
                f"sparse graph ({nodes} nodes, need ≥{_MIN_GRAPHIFY_NODES}). "
                f"{err_body}"
            )
        else:
            last_error = err_body or f"graphify extract exit {result.returncode}"

        logger.warning(
            "Graphify extract attempt %d/%d failed: %s",
            attempt,
            len(budgets),
            last_error[:500],
        )

    raise GraphifyError(1, last_error)


def _promote_temp_graphify_out(filtered_dir: Path, session_dir: Path) -> bool:
    """Move temp graphify-out into the session when extract partially succeeded."""
    temp_out = filtered_dir / "graphify-out"
    graph_path = temp_out / "graph.json"
    if not graph_path.is_file():
        return False

    canonical_out = session_dir / "graphify-out"
    if canonical_out.exists():
        shutil.rmtree(canonical_out)
    shutil.move(str(temp_out), str(canonical_out))
    logger.info("Salvaged partial graphify-out to %s", canonical_out)
    return True


def _raw_node_ids_from_html(html: str) -> set[str]:
    """Parse vis-network RAW_NODES ids embedded in graph.html."""
    match = re.search(r"const RAW_NODES = (\[.*?\]);", html, re.DOTALL)
    if not match:
        return set()
    try:
        raw = json.loads(match.group(1))
    except json.JSONDecodeError:
        return set()
    return {n["id"] for n in raw if isinstance(n, dict) and n.get("id")}


def _update_html_stats(html_path: Path, node_count: int, edge_count: int, community_count: int) -> None:
    """Keep the sidebar stats footer aligned with graph.json."""
    html = html_path.read_text(encoding="utf-8")
    stats = (
        f'{node_count} nodes &middot; {edge_count} edges '
        f"&middot; {community_count} communities"
    )
    updated = re.sub(
        r'(<div id="stats">)(.*?)(</div>)',
        lambda m, s=stats: f"{m.group(1)}{s}{m.group(3)}",
        html,
        count=1,
        flags=re.DOTALL,
    )
    if updated != html:
        html_path.write_text(updated, encoding="utf-8")


def _regenerate_graph_html(out_dir: Path) -> bool:
    """
    Re-run ``graphify cluster-only`` so graph.html is rebuilt from graph.json.
    Returns True when the subprocess succeeds.
    """
    env = _graphify_env()
    result = subprocess.run(
        ["graphify", "cluster-only", str(out_dir)],
        capture_output=True,
        text=True,
        cwd=str(out_dir.parent),
        timeout=600,
        env=env,
    )
    if result.returncode != 0:
        logger.warning(
            "graphify cluster-only failed while syncing html (%s): %s",
            result.returncode,
            (result.stderr or result.stdout or "").strip(),
        )
        return False
    return True


def _ensure_html_includes_all_json_nodes(out_dir: Path, graph_data: dict) -> None:
    """
    Guarantee every node in graph.json appears in graph.html RAW_NODES.
    Regenerates the visualization when Graphify's html/json artefacts diverge.
    """
    html_path = out_dir / "graph.html"
    if not html_path.is_file():
        return

    nodes = graph_data.get("nodes") or []
    json_ids = {n["id"] for n in nodes if n.get("id")}
    if not json_ids:
        return

    html_ids = _raw_node_ids_from_html(html_path.read_text(encoding="utf-8"))
    if json_ids <= html_ids:
        communities = {n.get("community") for n in nodes if n.get("community") is not None}
        _update_html_stats(
            html_path,
            len(json_ids),
            len(graph_data.get("links") or []),
            len(communities),
        )
        return

    missing = len(json_ids - html_ids)
    logger.info(
        "graph.html missing %d node(s) from graph.json — regenerating visualization.",
        missing,
    )
    if not _regenerate_graph_html(out_dir):
        return

    html = html_path.read_text(encoding="utf-8")
    if 'class="resizer"' not in html:
        html = html.replace("</body>", _RESIZER_INJECTION + "\n</body>")
        html_path.write_text(html, encoding="utf-8")

    name_map: dict[int, str] = {}
    for node in nodes:
        cid = node.get("community")
        cname = node.get("community_name")
        if cid is not None and cname and cid not in name_map:
            name_map[int(cid)] = str(cname)

    if name_map:
        html = html_path.read_text(encoding="utf-8")
        html = _patch_graph_html(html, name_map)
        html_path.write_text(html, encoding="utf-8")

    html_ids = _raw_node_ids_from_html(html_path.read_text(encoding="utf-8"))
    if json_ids - html_ids:
        logger.warning(
            "graph.html still missing %d node(s) after cluster-only: %s",
            len(json_ids - html_ids),
            sorted(json_ids - html_ids)[:5],
        )
    else:
        communities = {n.get("community") for n in nodes if n.get("community") is not None}
        _update_html_stats(
            html_path,
            len(json_ids),
            len(graph_data.get("links") or []),
            len(communities),
        )
        logger.info("graph.html synced with graph.json (%d nodes).", len(json_ids))


# ── Community Naming Helpers ────────────────────────────────────────────────

def _compute_degrees(graph_data: dict) -> Counter:
    """Return a Counter mapping node-id → degree from the links array."""
    degrees: Counter = Counter()
    for link in graph_data.get("links", []):
        degrees[link["source"]] += 1
        degrees[link["target"]] += 1
    return degrees


def _group_communities(
    graph_data: dict, degrees: Counter,
) -> dict[int, list[tuple[str, int]]]:
    """Group nodes by integer community id, sorted by degree desc."""
    communities: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for node in graph_data.get("nodes", []):
        cid = node.get("community")
        if cid is None:
            continue
        communities[cid].append(
            (node.get("label", ""), degrees.get(node["id"], 0))
        )
    for cid in communities:
        communities[cid].sort(key=lambda x: -x[1])
    return dict(communities)


def _generate_community_names(
    communities: dict[int, list[tuple[str, int]]],
) -> dict[int, str]:
    """Call Gemini 2.5 Flash for short community titles; fallback on failure."""
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT_ID")
    if not project_id:
        logger.warning("GOOGLE_CLOUD_PROJECT_ID not set; skipping smart community naming.")
        return {cid: f"Community {cid}" for cid in communities}

    community_descriptions = []
    for cid, nodes in sorted(communities.items()):
        top_labels = [label for label, _ in nodes[:5]]
        community_descriptions.append(
            f"Community {cid}: {', '.join(top_labels)}"
        )

    prompt = (
        "You are a knowledge-graph analyst. For each community below, generate "
        "a short 2-to-3 word descriptive title that captures the theme of its "
        "top nodes. Return ONLY a valid JSON object mapping the community number "
        "(as a string key) to its title (as a string value). "
        "Do NOT use any special characters like quotes, apostrophes, ampersands, "
        "angle brackets, or backslashes in the titles — use only plain ASCII "
        "alphanumeric characters, spaces, and hyphens.\n\n"
        + "\n".join(community_descriptions)
    )

    try:
        from google import genai

        client = genai.Client(vertexai=True, project=project_id, location="global")
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[prompt],
        )

        raw_text = response.text.strip()
        raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
        raw_text = re.sub(r"\s*```$", "", raw_text)

        name_map = json.loads(raw_text)
        result = {}
        for cid in communities:
            title = name_map.get(str(cid), f"Community {cid}")
            title = re.sub(r"[\"'\\<>&]", "", title).strip()
            if not title:
                title = f"Community {cid}"
            result[cid] = title
        return result

    except Exception as e:
        logger.warning("Gemini community naming failed (%s); using defaults.", e)
        return {cid: f"Community {cid}" for cid in communities}


def _patch_graph_json(graph_data: dict, name_map: dict[int, str]) -> dict:
    """Add ``community_name`` labels while preserving integer ``community`` ids."""
    for node in graph_data.get("nodes", []):
        old_cid = node.get("community")
        if old_cid is not None and old_cid in name_map:
            node["community_name"] = name_map[old_cid]
    return graph_data


def _patch_graph_html(html: str, name_map: dict[int, str]) -> str:
    """Patch the LEGEND array and RAW_NODES community_name values in graph.html."""
    for cid, new_name in name_map.items():
        quoted = json.dumps(new_name)
        pattern = (
            r'("cid":\s*' + str(cid) + r',\s*"color":\s*"[^"]*",\s*"label":\s*)"[^"]*"'
        )
        html = re.sub(pattern, lambda m, q=quoted: f"{m.group(1)}{q}", html)

    for cid, new_name in name_map.items():
        quoted = json.dumps(new_name)
        pattern = (
            r'("community":\s*' + str(cid) + r',\s*"community_name":\s*)"[^"]*"'
        )
        html = re.sub(pattern, lambda m, q=quoted: f"{m.group(1)}{q}", html)

    return html


# ── Entity Resolution (Semantic Deduplication) ───────────────────────────────────

_ENTITY_RESOLUTION_BATCH_SIZE = 100
_ENTITY_RESOLUTION_MIN_NODES = 3

_ENTITY_RESOLUTION_REGIONS: list[str] = [
    "global",
    "us-central1",
    "europe-west4",
    "us-east4",
]


def _call_flash_for_entity_resolution(
    node_list_text: str,
    project_id: str,
) -> dict[str, str]:
    """
    Send a single batch of alphabetically sorted node labels to Gemini 2.5 Flash
    for synonym/co-reference detection. Returns a mapping of duplicate_id → primary_id.
    Uses global → regional failover.
    """
    from google import genai

    prompt = (
        "You are a graph topology optimizer. Below is an alphabetically sorted "
        "subset of knowledge graph nodes (ID and label). Identify groups of nodes "
        "that are semantic duplicates, near-synonyms, or co-references "
        '(e.g., "LLM" and "Large Language Model", or "EM-LLM" and '
        '"Human-Inspired Episodic Memory", or "AI Agent" and "AI Agents"). '
        "For each duplicate group, pick the node with the most descriptive label "
        "as the primary. Return ONLY a valid JSON object where each key is a "
        "duplicate node ID (to be removed) and the value is the primary node ID "
        "it should be merged into. If no duplicates exist, return {}.\n\n"
        f"{node_list_text}"
    )

    last_exc: Exception | None = None
    for region in _ENTITY_RESOLUTION_REGIONS:
        try:
            client = genai.Client(
                vertexai=True, project=project_id, location=region,
            )
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[prompt],
            )
            raw_text = (response.text or "").strip()
            raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
            raw_text = re.sub(r"\s*```$", "", raw_text)
            if not raw_text:
                return {}
            mapping = json.loads(raw_text)
            if isinstance(mapping, dict):
                return {str(k): str(v) for k, v in mapping.items()}
            return {}
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "Entity resolution Flash call failed at %s: %s", region, exc,
            )
            continue

    logger.error(
        "Entity resolution failed after all regions. Last error: %s", last_exc,
    )
    return {}


def _resolve_graph_entities(graph_data: dict) -> dict:
    """
    Merge semantically duplicate nodes in the knowledge graph using
    Gemini 2.5 Flash with alphabetical batching.

    Strategy:
      1. Sort nodes alphabetically by label (lowercased) so near-synonyms
         naturally cluster together.
      2. Chunk into batches of 100 nodes.
      3. Send each batch to Flash for synonym detection.
      4. Merge all batch mappings into a master dedup map.
      5. Rewire graph: delete duplicate nodes, rewrite links, remove
         self-loops and duplicate edges.

    Gracefully degrades: returns graph_data unmodified on any failure.
    """
    nodes = graph_data.get("nodes", [])
    if len(nodes) < _ENTITY_RESOLUTION_MIN_NODES:
        return graph_data

    project_id = os.getenv("GOOGLE_CLOUD_PROJECT_ID")
    if not project_id:
        logger.warning(
            "GOOGLE_CLOUD_PROJECT_ID not set; skipping entity resolution.",
        )
        return graph_data

    print(
        f"PROGRESS: Phase 4 — entity resolution: sorting {len(nodes)} nodes "
        f"alphabetically for batched deduplication...",
        flush=True,
    )

    # ── Step 1: Sort nodes alphabetically by label ────────────────────
    sorted_nodes = sorted(
        nodes,
        key=lambda n: (n.get("label") or n.get("id", "")).lower(),
    )

    # ── Step 2: Chunk into batches ───────────────────────────────────
    batches: list[list[dict]] = [
        sorted_nodes[i : i + _ENTITY_RESOLUTION_BATCH_SIZE]
        for i in range(0, len(sorted_nodes), _ENTITY_RESOLUTION_BATCH_SIZE)
    ]
    print(
        f"PROGRESS: Phase 4 — entity resolution: {len(batches)} batch(es) "
        f"of up to {_ENTITY_RESOLUTION_BATCH_SIZE} nodes each.",
        flush=True,
    )

    # ── Step 3: Send each batch to Flash ─────────────────────────────
    master_mapping: dict[str, str] = {}
    node_id_set = {n.get("id") for n in nodes if n.get("id")}

    for batch_idx, batch in enumerate(batches, start=1):
        node_lines = [
            f"  ID: {n.get('id', '?')}  |  Label: {n.get('label', '(unlabeled)')}"
            for n in batch
        ]
        node_list_text = "\n".join(node_lines)

        print(
            f"PROGRESS: Phase 4 — entity resolution batch {batch_idx}/{len(batches)} "
            f"({len(batch)} nodes)...",
            flush=True,
        )

        try:
            batch_mapping = _call_flash_for_entity_resolution(
                node_list_text, project_id,
            )
        except Exception as exc:
            logger.warning(
                "Entity resolution batch %d failed: %s", batch_idx, exc,
            )
            continue

        # Validate: both IDs must exist in the graph
        for dup_id, primary_id in batch_mapping.items():
            if dup_id in node_id_set and primary_id in node_id_set:
                if dup_id != primary_id:
                    master_mapping[dup_id] = primary_id
            else:
                logger.debug(
                    "Entity resolution: skipping invalid mapping %s → %s",
                    dup_id, primary_id,
                )

    if not master_mapping:
        print(
            "PROGRESS: Phase 4 — entity resolution: no duplicates found.",
            flush=True,
        )
        return graph_data

    # ── Step 4: Resolve transitive chains (A→B, B→C ⇒ A→C, B→C) ─────
    def _resolve_primary(node_id: str) -> str:
        visited: set[str] = set()
        current = node_id
        while current in master_mapping and current not in visited:
            visited.add(current)
            current = master_mapping[current]
        return current

    resolved_mapping = {
        dup: _resolve_primary(dup) for dup in master_mapping
    }
    # Remove identity mappings that may arise from transitive resolution
    resolved_mapping = {
        k: v for k, v in resolved_mapping.items() if k != v
    }

    print(
        f"PROGRESS: Phase 4 — entity resolution: merging "
        f"{len(resolved_mapping)} duplicate node(s)...",
        flush=True,
    )

    # ── Step 5: Rewire the graph ────────────────────────────────────
    duplicates_to_remove = set(resolved_mapping.keys())

    # 5a. Remove duplicate nodes
    graph_data["nodes"] = [
        n for n in graph_data["nodes"]
        if n.get("id") not in duplicates_to_remove
    ]

    # 5b. Rewrite link endpoints
    links = graph_data.get("links", [])
    for link in links:
        src = link.get("source", "")
        tgt = link.get("target", "")
        if src in resolved_mapping:
            link["source"] = resolved_mapping[src]
        if tgt in resolved_mapping:
            link["target"] = resolved_mapping[tgt]

    # 5c. Remove self-loops
    links = [lk for lk in links if lk.get("source") != lk.get("target")]

    # 5d. Remove duplicate edges (keep first occurrence)
    seen_edges: set[tuple[str, str]] = set()
    deduped_links: list[dict] = []
    for lk in links:
        edge_key = (lk.get("source", ""), lk.get("target", ""))
        if edge_key not in seen_edges:
            seen_edges.add(edge_key)
            deduped_links.append(lk)
    graph_data["links"] = deduped_links

    print(
        f"PROGRESS: Phase 4 — entity resolution complete: "
        f"{len(graph_data['nodes'])} nodes, {len(graph_data['links'])} edges "
        f"(removed {len(duplicates_to_remove)} duplicate(s)).",
        flush=True,
    )

    return graph_data


# ── Post-Processing Orchestrator ────────────────────────────────────────────

def post_process_artifacts(out_dir: Path) -> None:
    """
    Enhance Graphify artefacts produced inside ``out_dir`` (a session's
    ``graphify-out/`` folder):

      1. Inject a resizable sidebar into graph.html.
      2. Entity resolution: merge semantic duplicates via batched Flash calls.
      3. Generate intelligent community names via Gemini 2.5 Flash.
      4. Patch both graph.json and graph.html with the new names.
    """
    out_dir = Path(out_dir)
    html_path = out_dir / "graph.html"
    json_path = out_dir / "graph.json"

    if html_path.is_file():
        try:
            html = html_path.read_text(encoding="utf-8")
            if 'class="resizer"' not in html:
                html = html.replace("</body>", _RESIZER_INJECTION + "\n</body>")
                html_path.write_text(html, encoding="utf-8")
                logger.info("Injected resizable sidebar into graph.html.")
        except Exception as e:
            logger.warning("Failed to inject resizable sidebar: %s", e)
    else:
        logger.warning("graph.html not found at %s; skipping sidebar injection.", html_path)

    if json_path.is_file():
        try:
            graph_data = json.loads(json_path.read_text(encoding="utf-8"))

            # ── Entity resolution BEFORE community naming ─────────────
            try:
                graph_data = _resolve_graph_entities(graph_data)
                json_path.write_text(
                    json.dumps(graph_data, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception as e:
                logger.warning(
                    "Entity resolution failed (continuing with original graph): %s", e,
                )

            # ── Community naming (on deduplicated graph) ───────────────
            degrees = _compute_degrees(graph_data)
            communities = _group_communities(graph_data, degrees)

            if communities:
                name_map = _generate_community_names(communities)
                logger.info("Generated community names: %s", name_map)

                _patch_graph_json(graph_data, name_map)
                json_path.write_text(
                    json.dumps(graph_data, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )

                if html_path.is_file():
                    html = html_path.read_text(encoding="utf-8")
                    html = _patch_graph_html(html, name_map)
                    html_path.write_text(html, encoding="utf-8")
            else:
                logger.info("No communities found in graph.json; skipping naming.")

            _ensure_html_includes_all_json_nodes(out_dir, graph_data)

        except json.JSONDecodeError as e:
            logger.warning("graph.json is not valid JSON: %s", e)
        except Exception as e:
            logger.warning("Community naming failed: %s", e)
    else:
        logger.warning("graph.json not found at %s; skipping community naming.", json_path)


# ── Semantic Input Filtering ────────────────────────────────────────────────

def _prepare_filtered_kb(session_dir: Path, current_run_files: list[Path]) -> Path:
    """
    Build a temporary directory inside the session, containing only refined
    Markdown files suitable for Graphify extraction.

    Inclusion rules:
      - All ``*_URLRefiner.md`` files
      - Anything written to the session's ``processed_summaries/``
      - Files whose first 200 chars start with ``# Wiki:`` / ``# Wikipedia:``
        or contain ``Academic Data Refin``
      - Anything written to the session's ``agent_scrapes/`` (refinement outputs)
    """
    temp_dir = session_dir / "temp_graph_input"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True)

    for f in current_run_files:
        if not f.is_file():
            continue

        name = f.name.lower()
        include = False

        if "_urlrefiner" in name:
            include = True
        elif f.parent.name == "processed_summaries":
            include = True
        else:
            try:
                header = f.read_text(encoding="utf-8")[:200]
                if (
                    header.startswith("# Wikipedia:")
                    or header.startswith("# Wiki:")
                    or "Academic Data Refin" in header
                ):
                    include = True
            except OSError:
                pass

        if not include and f.parent.name == "agent_scrapes":
            include = True

        if include:
            try:
                digest = hashlib.sha256(str(f.resolve()).encode()).hexdigest()[:10]
                dest = temp_dir / f"{digest}_{f.name}"
                shutil.copy2(f, dest)
            except OSError as e:
                logger.warning("Could not copy %s: %s", f, e)

    return temp_dir


# ── Main Runner ─────────────────────────────────────────────────────────────

def run_graphify(
    current_run_files: list[Path],
    session_dir: Path | None = None,
) -> str:
    """
    Execute the full Graphify pipeline for the active session and return stdout.

    Parameters
    ----------
    current_run_files : every Markdown path produced this run.
    session_dir       : absolute path to ``runs/session_<ts>/``. When ``None``
                        the env-pinned session is used (set by
                        ``FileStorage.create_session_dir``).

    Steps
    -----
    1. Build a filtered temp directory under ``session_dir``.
    2. Run ``graphify extract`` against the temp directory (llama-4-scout).
    3. Run ``graphify cluster-only`` against the temp directory.
    4. Move the resulting ``graphify-out/`` to ``session_dir/graphify-out/``.
    5. Run post-processing (resizable sidebar + smart community naming).
    6. Clean up the temp directory.
    """
    session_dir = session_dir or get_session_dir_from_env()
    if session_dir is None:
        raise FileNotFoundError(
            "run_graphify called without an active session_dir. "
            "Create one via FileStorage.create_session_dir() first."
        )

    session_dir = Path(session_dir)
    if not session_dir.is_dir():
        raise FileNotFoundError(f"Session directory not found: {session_dir}")

    filtered_dir = _prepare_filtered_kb(session_dir, current_run_files)
    logger.info(
        "Filtered KB prepared at %s (%d files).",
        filtered_dir,
        sum(1 for _ in filtered_dir.iterdir()),
    )

    env = _graphify_env()
    cwd = str(session_dir)  # run from the session root

    extract_stdout = ""
    try:
        result_extract = _run_graphify_extract(filtered_dir, cwd, env)
        extract_stdout = result_extract.stdout or ""

        result_viz = subprocess.run(
            ["graphify", "cluster-only", str(filtered_dir)],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=600,
            env=env,
        )

        if result_viz.returncode != 0:
            raise GraphifyError(result_viz.returncode, result_viz.stderr.strip())

    except GraphifyError as e:
        salvaged = _promote_temp_graphify_out(filtered_dir, session_dir)
        if salvaged:
            print(
                "PROGRESS: Phase 4 — salvaged partial graphify-out for UI/history.",
                flush=True,
            )
            _regenerate_graph_html(session_dir / "graphify-out")
        shutil.rmtree(filtered_dir, ignore_errors=True)
        raise e

    except subprocess.TimeoutExpired as e:
        shutil.rmtree(filtered_dir, ignore_errors=True)
        raise GraphifyError(1, f"Graphify pipeline timed out after {e.timeout} seconds.")

    # ── Move artefacts into the session's canonical graphify-out folder ──
    temp_out = filtered_dir / "graphify-out"
    canonical_out = session_dir / "graphify-out"

    if temp_out.is_dir():
        if canonical_out.exists():
            shutil.rmtree(canonical_out)
        shutil.move(str(temp_out), str(canonical_out))
        logger.info("Moved graphify-out to %s", canonical_out)
    else:
        canonical_out.mkdir(parents=True, exist_ok=True)
        logger.warning(
            "graphify-out not found in temp dir; artifacts may be missing.",
        )

    shutil.rmtree(filtered_dir, ignore_errors=True)

    try:
        post_process_artifacts(canonical_out)
    except Exception as e:
        logger.warning("Post-processing completed with warnings: %s", e)

    return extract_stdout + "\n" + result_viz.stdout


# ── Interactive Console: query / path subprocesses ──────────────────────────

# Hard wall-clock timeout for any interactive Graphify CLI invocation.
_INTERACTIVE_TIMEOUT_S = 300


def _resolve_workspace(session_id: str) -> Path:
    """Resolve a session id to its ``graphify-out`` workspace path."""
    session_dir = resolve_session_dir(session_id)
    if session_dir is None:
        raise FileNotFoundError(f"No session found for id: {session_id}")

    workspace = session_dir / "graphify-out"
    if not workspace.is_dir():
        raise FileNotFoundError(
            f"Session {session_id} has no graphify-out/ workspace. "
            "Run the pipeline at least once before querying."
        )
    return workspace


def _run_graphify_subcommand(args: list[str], cwd: Path) -> str:
    """Shared subprocess wrapper for graphify CLI invocations."""
    env = _graphify_env()
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=_INTERACTIVE_TIMEOUT_S,
            env=env,
        )
    except subprocess.TimeoutExpired as e:
        raise GraphifyError(
            1, f"graphify {args[1]} timed out after {e.timeout}s",
        )

    if result.returncode != 0:
        raise GraphifyError(result.returncode, (result.stderr or "").strip())

    stdout = result.stdout or ""
    stderr = (result.stderr or "").strip()
    if stderr:
        stdout = stdout.rstrip() + "\n\n[stderr]\n" + stderr
    return stdout


def execute_graph_query(session_id: str, question: str) -> str:
    """
    Run ``graphify query <workspace> "<question>"`` against the historical
    session identified by ``session_id`` and return the raw console output.
    """
    if not question or not question.strip():
        raise ValueError("execute_graph_query requires a non-empty question.")

    workspace = _resolve_workspace(session_id)
    args = ["graphify", "query", str(workspace), question.strip()]
    logger.info("graphify query → session=%s", session_id)
    return _run_graphify_subcommand(args, cwd=workspace.parent)


def execute_graph_path(session_id: str, source: str, target: str) -> str:
    """
    Run ``graphify path <workspace> "<source>" "<target>"`` against the
    historical session and return the raw console output.
    """
    if not source.strip() or not target.strip():
        raise ValueError(
            "execute_graph_path requires non-empty source AND target nodes."
        )

    workspace = _resolve_workspace(session_id)
    args = [
        "graphify", "path", str(workspace),
        source.strip(), target.strip(),
    ]
    logger.info(
        "graphify path → session=%s | %s → %s",
        session_id, source, target,
    )
    return _run_graphify_subcommand(args, cwd=workspace.parent)
