"""
ExportWorkspaceUseCase.py — CLI entry for Google Workspace export.
"""

from __future__ import annotations

from infrastructure.GoogleWorkspaceManager import export_session_to_workspace


def export_to_workspace(session_id: str, kb_root: str = "") -> dict:
    """Load session artifacts and push them to the user's Google Drive."""
    kb = kb_root.strip() or None
    return export_session_to_workspace(session_id, kb_root=kb)
