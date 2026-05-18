"""
GraphifyRunner.py — Shell executor for the Graphify knowledge-graph pipeline.

Routes LLM extraction through the local VertexProxy (localhost:8000) by
using Graphify's ``ollama`` backend — the only backend whose base_url is
configurable via the ``OLLAMA_BASE_URL`` environment variable.

After a successful run, ``post_process_artifacts`` enhances the generated
graph.html (resizable sidebar) and replaces integer community IDs with
short, AI-generated descriptive titles via Gemini 2.5 Flash.
"""

import json
import logging
import os
import re
import shutil
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv
from infrastructure.FileStorage import get_kb_root

load_dotenv()

logger = logging.getLogger(__name__)


class GraphifyError(Exception):
    """Raised when the graphify subprocess exits with a non-zero code."""

    def __init__(self, exit_code: int, stderr: str):
        self.exit_code = exit_code
        self.stderr = stderr
        super().__init__(
            f"graphify exited with code {exit_code}: {stderr}"
        )


# ── Resizable Sidebar (Task 2) ──────────────────────────────────────────────

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

  // Prepend the resizer handle to the left edge of the sidebar
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
    // Sidebar is on the right, so dragging left increases width
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


# ── Community Naming Helpers (Task 3) ────────────────────────────────────────

def _compute_degrees(graph_data: dict) -> Counter:
    """Return a Counter mapping node-id → degree from the links array."""
    degrees: Counter = Counter()
    for link in graph_data.get("links", []):
        degrees[link["source"]] += 1
        degrees[link["target"]] += 1
    return degrees


def _group_communities(graph_data: dict, degrees: Counter) -> dict[int, list[tuple[str, int]]]:
    """
    Group nodes by their integer community ID.

    Returns
    -------
    { community_int: [(node_label, degree), ...] }  sorted by degree desc.
    """
    communities: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for node in graph_data.get("nodes", []):
        cid = node.get("community")
        if cid is None:
            continue
        communities[cid].append((node.get("label", ""), degrees.get(node["id"], 0)))

    # Sort each community's nodes by degree descending
    for cid in communities:
        communities[cid].sort(key=lambda x: -x[1])

    return dict(communities)


def _generate_community_names(
    communities: dict[int, list[tuple[str, int]]],
) -> dict[int, str]:
    """
    Call Gemini 2.5 Flash to generate a short 2-3 word title for each
    community based on its top-5 highest-degree nodes.

    Falls back to "Community N" if the API is unreachable.
    """
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT_ID")
    if not project_id:
        logger.warning("GOOGLE_CLOUD_PROJECT_ID not set; skipping smart community naming.")
        return {cid: f"Community {cid}" for cid in communities}

    # Build the prompt
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
        # Strip markdown fences if present
        raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
        raw_text = re.sub(r"\s*```$", "", raw_text)

        name_map = json.loads(raw_text)
        result = {}
        for cid in communities:
            title = name_map.get(str(cid), f"Community {cid}")
            # Sanitise: strip any stray special characters that slipped through
            title = re.sub(r"[\"'\\<>&]", "", title).strip()
            if not title:
                title = f"Community {cid}"
            result[cid] = title
        return result

    except Exception as e:
        logger.warning("Gemini community naming failed (%s); using defaults.", e)
        return {cid: f"Community {cid}" for cid in communities}


def _patch_graph_json(graph_data: dict, name_map: dict[int, str]) -> dict:
    """
    Replace the integer ``community`` property on every node with the
    smart string title from *name_map*.
    """
    for node in graph_data.get("nodes", []):
        old_cid = node.get("community")
        if old_cid is not None and old_cid in name_map:
            node["community"] = name_map[old_cid]
    return graph_data


def _patch_graph_html(html: str, name_map: dict[int, str]) -> str:
    """
    Patch the LEGEND array and RAW_NODES community_name values in graph.html.

    Strategy:
      1. In LEGEND: replace each ``"label": "..."`` for matching cid entries.
      2. In RAW_NODES: replace each ``"community_name": "..."`` occurrence.
    Both replacements are done via precise regex to avoid corrupting the
    surrounding JavaScript.
    """
    # --- Patch LEGEND entries ---
    # LEGEND items look like: {"cid": 0, "color": "...", "label": "Old Name", "count": N}
    for cid, new_name in name_map.items():
        # Escape for JSON embedding (no unescaped quotes/backslashes)
        safe_name = json.dumps(new_name)[1:-1]  # strip outer quotes from json.dumps
        # Match: "cid": <cid>, "color": "...", "label": "<old>"
        pattern = (
            r'("cid":\s*' + str(cid) + r',\s*"color":\s*"[^"]*",\s*"label":\s*)"[^"]*"'
        )
        html = re.sub(pattern, r'\1"' + safe_name + '"', html)

    # --- Patch RAW_NODES community_name values ---
    # RAW_NODES items contain: "community": <int>, "community_name": "<old>"
    for cid, new_name in name_map.items():
        safe_name = json.dumps(new_name)[1:-1]
        pattern = (
            r'("community":\s*' + str(cid) + r',\s*"community_name":\s*)"[^"]*"'
        )
        html = re.sub(pattern, r'\1"' + safe_name + '"', html)

    return html


# ── Post-Processing Orchestrator ─────────────────────────────────────────────

def post_process_artifacts(target_dir: Path) -> None:
    """
    Enhance Graphify artifacts after generation:
      1. Inject a resizable sidebar into graph.html.
      2. Generate intelligent community names via Gemini 2.5 Flash.
      3. Patch both graph.json and graph.html with the new names.

    Handles missing files gracefully — if Graphify failed to produce
    an artifact, the corresponding step is silently skipped.
    """
    out_dir = target_dir / "graphify-out"
    html_path = out_dir / "graph.html"
    json_path = out_dir / "graph.json"

    # ── Task 2: Resizable sidebar ────────────────────────────────────────
    if html_path.is_file():
        try:
            html = html_path.read_text(encoding="utf-8")
            if "class=\"resizer\"" not in html:  # idempotent
                html = html.replace("</body>", _RESIZER_INJECTION + "\n</body>")
                html_path.write_text(html, encoding="utf-8")
                logger.info("Injected resizable sidebar into graph.html.")
            else:
                logger.info("Resizable sidebar already present; skipping injection.")
        except Exception as e:
            logger.warning("Failed to inject resizable sidebar: %s", e)
    else:
        logger.warning("graph.html not found at %s; skipping sidebar injection.", html_path)

    # ── Task 3: Intelligent community naming ─────────────────────────────
    if json_path.is_file():
        try:
            graph_data = json.loads(json_path.read_text(encoding="utf-8"))
            degrees = _compute_degrees(graph_data)
            communities = _group_communities(graph_data, degrees)

            if not communities:
                logger.info("No communities found in graph.json; skipping naming.")
                return

            name_map = _generate_community_names(communities)
            logger.info("Generated community names: %s", name_map)

            # Patch graph.json
            _patch_graph_json(graph_data, name_map)
            json_path.write_text(
                json.dumps(graph_data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.info("Patched graph.json with smart community names.")

            # Patch graph.html (if it exists)
            if html_path.is_file():
                html = html_path.read_text(encoding="utf-8")
                html = _patch_graph_html(html, name_map)
                html_path.write_text(html, encoding="utf-8")
                logger.info("Patched graph.html legend and node labels.")

        except json.JSONDecodeError as e:
            logger.warning("graph.json is not valid JSON: %s", e)
        except Exception as e:
            logger.warning("Community naming failed: %s", e)
    else:
        logger.warning("graph.json not found at %s; skipping community naming.", json_path)


# ── Semantic Input Filtering ─────────────────────────────────────────────────

def _prepare_filtered_kb(kb_path: Path, current_run_files: list[Path]) -> Path:
    """
    Build a temporary directory containing only high-quality, refined
    Markdown files suitable for Graphify extraction.

    Inclusion rules (from current_run_files)
    ─────────────────────────────────────────
    - The primary Academic Data Refinement summary.
    - All processed_summaries.
    - Files starting with '# Wiki:'.
    - All individual _URLRefiner.md files.

    Returns the path to the temporary directory.
    """
    temp_dir = kb_path / "temp_graph_input"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True)

    for f in current_run_files:
        if not f.is_file():
            continue

        name = f.name.lower()
        include = False

        # Include _URLRefiner.md files
        if "_urlrefiner" in name:
            include = True
        # Include processed_summaries (parent dir check)
        elif f.parent.name == "processed_summaries":
            include = True
        else:
            # Check file content for Academic Data Refinement or Wiki prefix
            try:
                header = f.read_text(encoding="utf-8")[:200]
                if header.startswith("# Wiki:") or "Academic Data Refin" in header:
                    include = True
            except OSError:
                pass

        # Fallback: include any file explicitly in current_run_files
        # from agent_scrapes (refinement outputs)
        if not include and f.parent.name == "agent_scrapes":
            include = True

        if include:
            try:
                shutil.copy2(f, temp_dir / f.name)
            except OSError as e:
                logger.warning("Could not copy %s: %s", f, e)

    return temp_dir


# ── Main Runner ──────────────────────────────────────────────────────────────

def run_graphify(current_run_files: list[Path], kb_path: Path | None = None) -> str:
    """
    Execute the full Graphify pipeline and return stdout.

    Steps
    -----
    1. Build a filtered temp directory with only current run data.
    2. Run ``graphify extract`` against the temp directory (llama-4-scout).
    3. Run ``graphify cluster-only`` against the temp directory.
    4. Move resulting ``graphify-out/`` into the canonical KB location.
    5. Run post-processing (resizable sidebar + smart community naming).
    6. Clean up the temp directory.

    Raises
    ------
    GraphifyError
        If graphify returns a non-zero exit code.
    FileNotFoundError
        If the target directory doesn't exist.
    """
    target = kb_path or get_kb_root()
    if not target.is_dir():
        raise FileNotFoundError(
            f"Knowledge base directory not found: {target}"
        )

    # ── Build filtered input set ─────────────────────────────────────────
    filtered_dir = _prepare_filtered_kb(target, current_run_files)
    logger.info("Filtered KB prepared at %s (%d files).",
                filtered_dir, sum(1 for _ in filtered_dir.iterdir()))

    env = os.environ.copy()
    # Graphify's ollama backend is the only one whose base_url is
    # configurable via an env var.  Point it at our local VertexProxy.
    env["OLLAMA_BASE_URL"] = "http://localhost:8000/v1"
    env["OLLAMA_API_KEY"] = "dummy-proxy-key"

    cwd = str(target.parent)  # run from ResearchGraphApp/

    try:
        # 1. Headless Extraction (produces graph.json)
        #    Uses Llama 4 Scout via VertexProxy. Token budget forces Graphify
        #    to split the corpus into semantic windows so the model extracts
        #    micro-details per chunk instead of macro-summarising the entire
        #    corpus into a handful of nodes.
        result_extract = subprocess.run(
            [
                "graphify", "extract", str(filtered_dir),
                "--backend", "ollama",
                "--model", "llama-4-scout",
                "--token-budget", "8000",
            ],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=1800,
            env=env,
        )

        if result_extract.returncode != 0:
            raise GraphifyError(result_extract.returncode, result_extract.stderr.strip())

        # 2. Visual Artifact Generation (produces graph.html and GRAPH_REPORT.md)
        result_viz = subprocess.run(
            [
                "graphify", "cluster-only", str(filtered_dir),
            ],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=600,
            env=env,
        )

        if result_viz.returncode != 0:
            raise GraphifyError(result_viz.returncode, result_viz.stderr.strip())

    except subprocess.TimeoutExpired as e:
        # Clean up even on timeout
        shutil.rmtree(filtered_dir, ignore_errors=True)
        raise GraphifyError(1, f"Graphify pipeline timed out after {e.timeout} seconds.")

    # ── Move artifacts to canonical location ─────────────────────────────
    temp_out = filtered_dir / "graphify-out"
    canonical_out = target / "graphify-out"

    if temp_out.is_dir():
        if canonical_out.exists():
            shutil.rmtree(canonical_out)
        shutil.move(str(temp_out), str(canonical_out))
        logger.info("Moved graphify-out to canonical location: %s", canonical_out)
    else:
        logger.warning("graphify-out not found in temp dir; artifacts may be missing.")

    # ── Clean up temp directory ──────────────────────────────────────────
    shutil.rmtree(filtered_dir, ignore_errors=True)
    logger.info("Cleaned up temporary filtered KB.")

    # ── Post-process generated artifacts ─────────────────────────────────
    try:
        post_process_artifacts(target)
    except Exception as e:
        # Post-processing is best-effort — never fail the overall pipeline
        logger.warning("Post-processing completed with warnings: %s", e)

    return result_extract.stdout + "\n" + result_viz.stdout
