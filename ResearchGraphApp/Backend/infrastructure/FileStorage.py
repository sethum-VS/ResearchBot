"""
FileStorage.py — Local file-system storage with strict session isolation.

Every pipeline invocation lives inside its own dedicated, timestamped folder
under ``research_knowledge_base/runs/session_<TIMESTAMP>[_<slug>]/``.
This guarantees absolute historical preservation and zero cross-run artifact
bleed: Phase 2 scrapers, Phase 2.6 URLRefiners, Phase 3 synthesizers, and
Phase 4 Graphify all write *exclusively* to their matching session subtree.

Layout
──────
research_knowledge_base/
├── runs/
│   ├── session_20260520T231844Z_ai_ethics/
│   │   ├── agent_scrapes/
│   │   ├── raw_ingestion/
│   │   ├── processed_summaries/
│   │   └── graphify-out/
│   │       ├── graph.html
│   │       ├── graph.json
│   │       └── GRAPH_REPORT.md
│   └── session_20260521T091233Z_quantum_routing/
│       └── ...
└── .archive_legacy/   (untouched: pre-session shared folders)
"""

import os
import re
from datetime import datetime, timezone
from pathlib import Path

# ── Path Resolution ──────────────────────────────────────────────────────────
# Backend/ lives at ResearchGraphApp/Backend/.
# research_knowledge_base/ is a sibling: ResearchGraphApp/research_knowledge_base/
_BACKEND_DIR = Path(__file__).resolve().parent.parent
_KB_ROOT = _BACKEND_DIR.parent / "research_knowledge_base"
_RUNS_ROOT = _KB_ROOT / "runs"

# All session-scoped subdirectories created up front for every new run.
SESSION_SUBDIRS: tuple[str, ...] = (
    "agent_scrapes",
    "raw_ingestion",
    "processed_summaries",
    "graphify-out",
)

# Environment variable that the orchestrator pins for the lifetime of one run
# so any deeply-nested helper can recover the immutable session path.
SESSION_DIR_ENV = "RESEARCHBOT_SESSION_DIR"


def _sanitize(name: str, max_len: int = 60) -> str:
    """Turn an arbitrary topic string into a safe filename fragment."""
    slug = re.sub(r"[^\w\s-]", "", (name or "").lower())
    slug = re.sub(r"[\s_-]+", "_", slug).strip("_")
    return slug[:max_len] if slug else "untitled"


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ── Public API ───────────────────────────────────────────────────────────────

def get_kb_root() -> Path:
    """Return the absolute path to the knowledge-base root."""
    return _KB_ROOT


def get_runs_root() -> Path:
    """Return the absolute path to ``research_knowledge_base/runs/``."""
    _RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    return _RUNS_ROOT


def create_session_dir(topic: str = "") -> Path:
    """
    Create and return a fresh, isolated session directory:

        research_knowledge_base/runs/session_<UTC_TIMESTAMP>[_<slug>]/

    Every standard subdirectory (``agent_scrapes``, ``raw_ingestion``,
    ``processed_summaries``, ``graphify-out``) is pre-created so every
    downstream writer can blindly target ``session_dir / subdir``.
    """
    runs_root = get_runs_root()
    slug = _sanitize(topic) if topic else ""
    name = f"session_{_timestamp()}" + (f"_{slug}" if slug else "")
    session = runs_root / name

    # Defensive uniqueness — sub-second collisions fall back to a counter.
    suffix = 0
    while session.exists():
        suffix += 1
        session = runs_root / f"{name}_{suffix:02d}"

    for sub in SESSION_SUBDIRS:
        (session / sub).mkdir(parents=True, exist_ok=True)

    # Pin the absolute session path into the process environment so any
    # helper invoked deeper in the call stack can recover it without
    # re-plumbing every signature.
    os.environ[SESSION_DIR_ENV] = str(session.resolve())
    return session


def get_session_dir_from_env() -> Path | None:
    """Return the immutable session dir pinned by ``create_session_dir``."""
    raw = os.environ.get(SESSION_DIR_ENV)
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_dir() else None


def list_sessions() -> list[Path]:
    """
    Enumerate every recorded session, newest first.

    A *session* is any directory directly under ``runs/`` whose name starts
    with ``session_``.
    """
    runs_root = get_runs_root()
    if not runs_root.is_dir():
        return []
    sessions = [
        p for p in runs_root.iterdir()
        if p.is_dir() and p.name.startswith("session_")
    ]
    sessions.sort(key=lambda p: p.name, reverse=True)
    return sessions


def resolve_session_dir(session_id: str) -> Path | None:
    """
    Resolve a Swift-supplied session identifier to an absolute path.

    Accepts any of:
      - the full directory name           (``session_20260520T231844Z_ai_ethics``)
      - the timestamp portion only        (``20260520T231844Z``)
      - a partial prefix / suffix match   (best-effort)
    """
    if not session_id:
        return None

    runs_root = get_runs_root()
    sid = session_id.strip()

    direct = runs_root / sid
    if direct.is_dir():
        return direct

    prefixed = runs_root / f"session_{sid}"
    if prefixed.is_dir():
        return prefixed

    if not runs_root.is_dir():
        return None

    for p in runs_root.iterdir():
        if not p.is_dir():
            continue
        if p.name == sid or p.name.endswith(sid) or sid in p.name:
            return p
    return None


def session_id_from_path(session_dir: Path) -> str:
    """Return the canonical session id (its directory basename)."""
    return Path(session_dir).name


def ensure_session_structure(session_dir: Path) -> Path:
    """Create all standard subdirectories inside an existing session path."""
    session_dir = Path(session_dir)
    session_dir.mkdir(parents=True, exist_ok=True)
    for sub in SESSION_SUBDIRS:
        (session_dir / sub).mkdir(parents=True, exist_ok=True)
    return session_dir


def save_markdown(
    subdir_key: str,
    topic: str,
    content: str,
    session_dir: Path | None = None,
) -> Path:
    """
    Write ``content`` to ``<session_dir>/<subdir_key>/<topic>_<timestamp>.md``.

    Parameters
    ----------
    subdir_key  : one of ``raw_ingestion``, ``agent_scrapes``,
                  ``processed_summaries`` (must be in ``SESSION_SUBDIRS``)
    topic       : seed topic — used to build the filename
    content     : raw Markdown text
    session_dir : absolute path to the active session. If ``None``, falls
                  back to the env-pinned session created by
                  ``create_session_dir``. Raises if neither is available.
    """
    if subdir_key not in SESSION_SUBDIRS:
        raise ValueError(
            f"Unknown subdir_key '{subdir_key}'. "
            f"Choose from {list(SESSION_SUBDIRS)}."
        )

    target_session = session_dir or get_session_dir_from_env()
    if target_session is None:
        raise RuntimeError(
            "No active session directory. Call FileStorage.create_session_dir() "
            "at orchestrator entry before writing markdown artifacts."
        )

    target_session = Path(target_session)
    ensure_session_structure(target_session)

    folder = target_session / subdir_key
    filename = f"{_sanitize(topic)}_{_timestamp()}.md"
    filepath = folder / filename
    filepath.write_text(content, encoding="utf-8")
    return filepath
