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
        safe_name = json.dumps(new_name)[1:-1]
        pattern = (
            r'("cid":\s*' + str(cid) + r',\s*"color":\s*"[^"]*",\s*"label":\s*)"[^"]*"'
        )
        html = re.sub(pattern, r'\1"' + safe_name + '"', html)

    for cid, new_name in name_map.items():
        safe_name = json.dumps(new_name)[1:-1]
        pattern = (
            r'("community":\s*' + str(cid) + r',\s*"community_name":\s*)"[^"]*"'
        )
        html = re.sub(pattern, r'\1"' + safe_name + '"', html)

    return html


# ── Post-Processing Orchestrator ────────────────────────────────────────────

def post_process_artifacts(out_dir: Path) -> None:
    """
    Enhance Graphify artefacts produced inside ``out_dir`` (a session's
    ``graphify-out/`` folder):

      1. Inject a resizable sidebar into graph.html.
      2. Generate intelligent community names via Gemini 2.5 Flash.
      3. Patch both graph.json and graph.html with the new names.
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
            degrees = _compute_degrees(graph_data)
            communities = _group_communities(graph_data, degrees)

            if not communities:
                logger.info("No communities found in graph.json; skipping naming.")
                return

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

    try:
        result_extract = subprocess.run(
            [
                "graphify", "extract", str(filtered_dir),
                "--backend", "ollama",
                "--model", "llama-4-scout",
                "--token-budget", "1500",
            ],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=1800,
            env=env,
        )

        if result_extract.returncode != 0:
            raise GraphifyError(result_extract.returncode, result_extract.stderr.strip())

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

    return result_extract.stdout + "\n" + result_viz.stdout


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
