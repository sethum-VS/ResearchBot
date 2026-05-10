"""
GraphifyRunner.py — Shell executor for the Graphify knowledge-graph pipeline.
Runs ``graphify <knowledge_base_path>`` as a subprocess and captures output.
"""

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
    Execute ``graphify <path>`` and return its stdout.

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

    result = subprocess.run(
        ["graphify", str(target)],
        capture_output=True,
        text=True,
        cwd=str(target.parent),   # run from ResearchGraphApp/
        timeout=120,
    )

    if result.returncode != 0:
        raise GraphifyError(result.returncode, result.stderr.strip())

    return result.stdout
