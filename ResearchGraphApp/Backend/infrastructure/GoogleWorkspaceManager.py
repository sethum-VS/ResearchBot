"""
GoogleWorkspaceManager.py — OAuth 2.0 Desktop flow + Drive/Docs export.

Authenticates the end-user (not a service account), uploads session scrapes,
creates a topic Google Doc from academic_gap_analysis, and updates the
Master Tracking Document.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from infrastructure.FileStorage import resolve_session_dir

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
]

MASTER_DOC_TITLE = "ResearchBot — Master Tracking Document"
APP_SUPPORT_DIR_NAME = "ResearchBot"


def app_support_dir() -> Path:
    """macOS Application Support directory for ResearchBot (token + config)."""
    base = Path.home() / "Library" / "Application Support" / APP_SUPPORT_DIR_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def token_path() -> Path:
    return app_support_dir() / "token.json"


def credentials_path() -> Path:
    """
    Resolve OAuth client secrets (Desktop app JSON).

    Priority:
      1. RESEARCHBOT_OAUTH_CREDENTIALS env (set by Swift from app bundle)
      2. Application Support credentials.json (optional user override)
      3. Dev fallback: ResearchGraphApp/App/ResearchBot/credentials.json
    """
    env = os.environ.get("RESEARCHBOT_OAUTH_CREDENTIALS", "").strip()
    if env:
        p = Path(env).expanduser()
        if p.is_file():
            return p

    support_creds = app_support_dir() / "credentials.json"
    if support_creds.is_file():
        return support_creds

    backend_dir = Path(__file__).resolve().parent.parent
    dev = backend_dir.parent / "App" / "ResearchBot" / "credentials.json"
    if dev.is_file():
        return dev

    raise FileNotFoundError(
        "OAuth credentials.json not found. Bundle it in the macOS app or set "
        "RESEARCHBOT_OAUTH_CREDENTIALS."
    )


def has_saved_token() -> bool:
    return token_path().is_file()


def get_credentials() -> Credentials:
    """Load or obtain OAuth credentials (opens browser on first run)."""
    creds: Credentials | None = None
    tok = token_path()

    if tok.is_file():
        creds = Credentials.from_authorized_user_file(str(tok), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        tok.write_text(creds.to_json(), encoding="utf-8")
        return creds

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path()), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    tok.write_text(creds.to_json(), encoding="utf-8")
    logger.info("OAuth token saved to %s", tok)
    return creds


def _drive_service(creds: Credentials):
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _docs_service(creds: Credentials):
    return build("docs", "v1", credentials=creds, cache_discovery=False)


def _web_link(file_id: str, drive, mime_type: str | None = None) -> str:
    meta = (
        drive.files()
        .get(fileId=file_id, fields="webViewLink,mimeType")
        .execute()
    )
    if meta.get("webViewLink"):
        return meta["webViewLink"]
    mt = mime_type or meta.get("mimeType") or ""
    if mt == "application/vnd.google-apps.document":
        return f"https://docs.google.com/document/d/{file_id}/edit"
    return f"https://drive.google.com/file/d/{file_id}/view"


def create_topic_folder(drive, session_id: str, topic_label: str) -> tuple[str, str]:
    """Create ``Run_<TIMESTAMP>_<slug>`` in the user's Drive root."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = re.sub(r"[^\w]+", "_", (topic_label or session_id).lower()).strip("_")[:48]
    name = f"Run_{ts}_{slug or 'export'}"
    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    folder = drive.files().create(body=meta, fields="id,webViewLink").execute()
    return folder["id"], folder.get("webViewLink") or _web_link(folder["id"], drive)


def upload_agent_scrapes(
    drive,
    session_dir: Path,
    folder_id: str,
) -> list[dict[str, str]]:
    """Upload all ``agent_scrapes/*.md`` and return {name, url} entries."""
    scrapes = session_dir / "agent_scrapes"
    if not scrapes.is_dir():
        return []

    uploaded: list[dict[str, str]] = []
    for md in sorted(scrapes.glob("*.md")):
        media = MediaFileUpload(
            str(md),
            mimetype="text/markdown",
            resumable=True,
        )
        body = {"name": md.name, "parents": [folder_id]}
        created = (
            drive.files()
            .create(body=body, media_body=media, fields="id")
            .execute()
        )
        uploaded.append({"name": md.name, "url": _web_link(created["id"], drive)})
    return uploaded


def _format_gap_analysis(analysis: dict[str, Any]) -> str:
    """Plain-text body for the topic Google Doc."""
    lines: list[str] = []
    summary = (analysis.get("summary") or "").strip()
    if summary:
        lines.extend(["Executive Summary", summary, ""])

    def section(title: str, items: list[dict], formatter) -> None:
        if not items:
            return
        lines.append(title)
        for i, item in enumerate(items, 1):
            lines.append(f"{i}. {formatter(item)}")
            lines.append("")
        lines.append("")

    lines.append("Structural Holes")
    for hole in analysis.get("structural_holes") or []:
        lines.append(f"• {hole.get('title', 'Untitled')}")
        if hole.get("description"):
            lines.append(f"  {hole['description']}")
        if hole.get("bridging_opportunity"):
            lines.append(f"  Opportunity: {hole['bridging_opportunity']}")
        lines.append("")

    lines.append("High-Degree Limitations")
    for lim in analysis.get("high_degree_limitations") or []:
        lines.append(f"• {lim.get('title', 'Untitled')}")
        if lim.get("description"):
            lines.append(f"  {lim['description']}")
        if lim.get("evidence"):
            lines.append(f"  Evidence: {lim['evidence']}")
        lines.append("")

    lines.append("Orphaned Solutions")
    for sol in analysis.get("orphaned_solutions") or []:
        lines.append(f"• {sol.get('title', 'Untitled')}")
        if sol.get("description"):
            lines.append(f"  {sol['description']}")
        if sol.get("technical_contribution"):
            lines.append(f"  Contribution: {sol['technical_contribution']}")
        lines.append("")

    if analysis.get("error"):
        lines.extend(["Analysis Warning", str(analysis["error"]), ""])

    return "\n".join(lines).strip() + "\n"


def create_topic_document(
    drive,
    docs,
    folder_id: str,
    session_id: str,
    analysis: dict[str, Any],
    references: list[dict[str, str]],
) -> tuple[str, str]:
    """Create a Google Doc in ``folder_id`` with gap analysis + reference links."""
    title = f"Gap Analysis — {session_id}"
    meta = {
        "name": title,
        "mimeType": "application/vnd.google-apps.document",
        "parents": [folder_id],
    }
    doc_file = drive.files().create(body=meta, fields="id").execute()
    doc_id = doc_file["id"]

    full = _format_gap_analysis(analysis)
    full += "\nReference Materials\n"
    for ref in references:
        full += f"• {ref['name']}\n  {ref['url']}\n"

    docs.documents().batchUpdate(
        documentId=doc_id,
        body={
            "requests": [
                {"insertText": {"location": {"index": 1}, "text": full}},
            ]
        },
    ).execute()

    return doc_id, _web_link(
        doc_id,
        drive,
        mime_type="application/vnd.google-apps.document",
    )


def _find_master_document(drive) -> str | None:
    q = (
        f"name = '{MASTER_DOC_TITLE}' and "
        "mimeType = 'application/vnd.google-apps.document' and "
        "trashed = false"
    )
    resp = (
        drive.files()
        .list(q=q, spaces="drive", fields="files(id)", pageSize=1)
        .execute()
    )
    files = resp.get("files") or []
    return files[0]["id"] if files else None


def _create_master_document(drive) -> str:
    meta = {
        "name": MASTER_DOC_TITLE,
        "mimeType": "application/vnd.google-apps.document",
    }
    created = drive.files().create(body=meta, fields="id").execute()
    return created["id"]


def append_run_to_master(
    docs,
    drive,
    *,
    session_id: str,
    topic_label: str,
    topic_doc_url: str,
    folder_url: str,
    summary: str,
) -> str:
    """Prepend a new run section at the top of the Master Tracking Document."""
    doc_id = _find_master_document(drive)
    if not doc_id:
        doc_id = _create_master_document(drive)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    block = (
        f"Run: {session_id}\n"
        f"Topic: {topic_label}\n"
        f"Exported: {ts}\n"
        f"Summary: {summary[:500]}{'…' if len(summary) > 500 else ''}\n"
        f"Topic Document: {topic_doc_url}\n"
        f"Topic Folder: {folder_url}\n"
        f"{'—' * 40}\n\n"
    )

    docs.documents().batchUpdate(
        documentId=doc_id,
        body={
            "requests": [
                {"insertText": {"location": {"index": 1}, "text": block}},
            ]
        },
    ).execute()

    return _web_link(doc_id, drive)


def _load_topic_label(session_dir: Path, session_id: str) -> str:
    manifest = session_dir / "session_manifest.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            return (
                (data.get("primary_keyword") or data.get("topic") or "")
                .strip()
            )
        except (json.JSONDecodeError, OSError):
            pass
    parts = session_id.replace("session_", "", 1).split("_", 1)
    if len(parts) == 2:
        return parts[1].replace("_", " ")
    return session_id


def export_session_to_workspace(
    session_id: str,
    kb_root: str | None = None,
) -> dict[str, Any]:
    """
    Full export pipeline for one historical or active session.

    Returns a dict suitable for JSON serialization to Swift.
    """
    session_dir: Path | None
    if kb_root:
        runs = Path(kb_root).expanduser() / "runs"
        direct = runs / session_id.strip()
        prefixed = runs / f"session_{session_id.strip()}"
        if direct.is_dir():
            session_dir = direct
        elif prefixed.is_dir():
            session_dir = prefixed
        else:
            session_dir = None
            if runs.is_dir():
                sid = session_id.strip()
                for p in runs.iterdir():
                    if p.is_dir() and (
                        p.name == sid
                        or p.name.endswith(sid)
                        or sid in p.name
                    ):
                        session_dir = p
                        break
    else:
        session_dir = resolve_session_dir(session_id)

    if session_dir is None or not session_dir.is_dir():
        return {
            "status": "error",
            "message": f"Session not found: {session_id}",
        }

    gap_path = session_dir / "academic_gap_analysis.json"
    if not gap_path.is_file():
        return {
            "status": "error",
            "message": f"No academic_gap_analysis.json in {session_dir}",
        }

    try:
        analysis = json.loads(gap_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return {"status": "error", "message": f"Invalid gap analysis JSON: {e}"}

    creds = get_credentials()
    drive = _drive_service(creds)
    docs = _docs_service(creds)

    sid = session_dir.name
    topic_label = _load_topic_label(session_dir, sid)

    folder_id, folder_url = create_topic_folder(drive, sid, topic_label)
    references = upload_agent_scrapes(drive, session_dir, folder_id)
    _, topic_doc_url = create_topic_document(
        drive,
        docs,
        folder_id,
        sid,
        analysis,
        references,
    )
    master_url = append_run_to_master(
        docs,
        drive,
        session_id=sid,
        topic_label=topic_label,
        topic_doc_url=topic_doc_url,
        folder_url=folder_url,
        summary=(analysis.get("summary") or ""),
    )

    return {
        "status": "success",
        "message": "Exported to Google Workspace.",
        "master_document_url": master_url,
        "topic_document_url": topic_doc_url,
        "topic_folder_url": folder_url,
        "session_id": sid,
    }
