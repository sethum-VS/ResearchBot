"""
GraphifyRunner.py — Shell executor for the Graphify knowledge-graph pipeline.

Routes LLM extraction through the local VertexProxy (localhost:8000) by
using Graphify's ``ollama`` backend — the only backend whose base_url is
configurable via the ``OLLAMA_BASE_URL`` environment variable.
"""

import os
import subprocess
from pathlib import Path

from infrastructure.FileStorage import get_kb_root


class GraphifyError(Exception):
    """Raised when the graphify subprocess exits with a non-zero code."""

    def __init__(self, exit_code: int, stderr: str):
        self.exit_code = exit_code
        self.stderr = stderr
        super().__init__(
            f"graphify exited with code {exit_code}: {stderr}"
        )


def run_graphify(kb_path: Path | None = None) -> str:
    """
    Execute the full Graphify pipeline and return stdout.

    Parameters
    ----------
    kb_path : optional override for the knowledge-base directory.
              Defaults to the canonical ``research_knowledge_base`` folder.

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

    env = os.environ.copy()
    # Graphify's ollama backend is the only one whose base_url is
    # configurable via an env var.  Point it at our local VertexProxy.
    env["OLLAMA_BASE_URL"] = "http://localhost:8000/v1"
    env["OLLAMA_API_KEY"] = "dummy-proxy-key"
    env["GRAPHIFY_MAX_OUTPUT_TOKENS"] = "65536"

    cwd = str(target.parent)  # run from ResearchGraphApp/

    try:
        # 1. Headless Extraction (produces graph.json)
        result_extract = subprocess.run(
            [
                "graphify", "extract", str(target),
                "--backend", "ollama",
                "--model", "gemini-2.5-flash",
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
                "graphify", "cluster-only", str(target),
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
        raise GraphifyError(1, f"Graphify pipeline timed out after {e.timeout} seconds.")

    return result_extract.stdout + "\n" + result_viz.stdout
