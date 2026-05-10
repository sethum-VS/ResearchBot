"""
FileStorage.py — Local file system storage for research artifacts.
Dynamically resolves the knowledge base path as a sibling to /Backend,
ensures the required subdirectory structure exists, and provides
cross-platform safe file-write helpers.
"""

import os
import re
from datetime import datetime, timezone
from pathlib import Path


# ── Path Resolution ──────────────────────────────────────────────────────────
# Backend/ lives at ResearchGraphApp/Backend/.
# research_knowledge_base/ is a sibling: ResearchGraphApp/research_knowledge_base/
_BACKEND_DIR = Path(__file__).resolve().parent.parent          # .../Backend
_KB_ROOT = _BACKEND_DIR.parent / "research_knowledge_base"     # .../ResearchGraphApp/research_knowledge_base

SUBDIRS = {
    "raw_ingestion":       _KB_ROOT / "raw_ingestion",
    "agent_scrapes":       _KB_ROOT / "agent_scrapes",
    "processed_summaries": _KB_ROOT / "processed_summaries",
}


def _sanitize(name: str, max_len: int = 60) -> str:
    """Turn an arbitrary topic string into a safe filename fragment."""
    slug = re.sub(r"[^\w\s-]", "", name.lower())
    slug = re.sub(r"[\s_-]+", "_", slug).strip("_")
    return slug[:max_len] if slug else "untitled"


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def ensure_structure() -> Path:
    """Create the knowledge-base tree if it doesn't exist. Returns KB root."""
    for d in SUBDIRS.values():
        d.mkdir(parents=True, exist_ok=True)
    return _KB_ROOT


def save_markdown(subdir_key: str, topic: str, content: str) -> Path:
    """
    Write *content* to ``<subdir>/<topic>_<timestamp>.md``.

    Parameters
    ----------
    subdir_key : one of ``raw_ingestion``, ``agent_scrapes``, ``processed_summaries``
    topic      : seed topic (used for the filename)
    content    : raw Markdown text

    Returns
    -------
    Path to the written file.
    """
    ensure_structure()
    folder = SUBDIRS.get(subdir_key)
    if folder is None:
        raise ValueError(
            f"Unknown subdir_key '{subdir_key}'. "
            f"Choose from {list(SUBDIRS.keys())}."
        )

    filename = f"{_sanitize(topic)}_{_timestamp()}.md"
    filepath = folder / filename
    filepath.write_text(content, encoding="utf-8")
    return filepath


def get_kb_root() -> Path:
    """Return the absolute path to the knowledge-base root."""
    return _KB_ROOT
