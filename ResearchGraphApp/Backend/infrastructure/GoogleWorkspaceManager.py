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
APP_SUPPORT_DIR_NAME = "AutonomousResearchGraph"


def app_support_dir() -> Path:
    """macOS Application Support (`.env`, OAuth token)."""
    env_dir = os.environ.get("APP_SUPPORT_DIR", "").strip()
    if env_dir:
        base = Path(env_dir).expanduser()
    else:
        base = (
            Path.home()
            / "Library"
            / "Application Support"
            / APP_SUPPORT_DIR_NAME
        )
    base.mkdir(parents=True, exist_ok=True)
    return base


def token_path() -> Path:
    return app_support_dir() / "token.json"


def credentials_path() -> Path:
    """
    Resolve OAuth client secrets (Desktop app JSON).

    Priority:
      1. RESEARCHBOT_OAUTH_CREDENTIALS env (set by Swift from app bundle)
      2. APP_BUNDLE_DIR/credentials.json (shipped inside the .dmg)
      3. Application Support credentials.json (optional user override)
      4. Dev fallback: ResearchGraphApp/App/ResearchBot/credentials.json
    """
    env = os.environ.get("RESEARCHBOT_OAUTH_CREDENTIALS", "").strip()
    if env:
        p = Path(env).expanduser()
        if p.is_file():
            return p

    bundle_dir = os.environ.get("APP_BUNDLE_DIR", "").strip()
    if bundle_dir:
        bundled = Path(bundle_dir).expanduser() / "credentials.json"
        if bundled.is_file():
            return bundled

    support_creds = app_support_dir() / "credentials.json"
    if support_creds.is_file():
        return support_creds

    backend_dir = Path(__file__).resolve().parent.parent
    dev = backend_dir.parent / "App" / "ResearchBot" / "credentials.json"
    if dev.is_file():
        return dev

    raise FileNotFoundError(
        "OAuth credentials.json not found. Bundle it in the macOS app or set "
        "RESEARCHBOT_OAUTH_CREDENTIALS / APP_BUNDLE_DIR."
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


# ── Markdown-to-Google-Docs Native Formatting ────────────────────────────────


def _parse_markdown_table(lines: list[str]) -> list[list[str]]:
    """Parse consecutive markdown table lines into a 2D list of cell strings."""
    rows: list[list[str]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            break
        cells = [c.strip() for c in stripped.split("|")]
        # Remove empty first/last from leading/trailing pipes
        if cells and cells[0] == "":
            cells = cells[1:]
        if cells and cells[-1] == "":
            cells = cells[:-1]
        # Skip separator rows (|---|---|)
        if cells and all(re.match(r"^:?-+:?$", c) for c in cells):
            continue
        if cells:
            rows.append(cells)
    return rows


def _markdown_to_docs_requests(markdown_text: str) -> list[dict]:
    """
    Convert proposal Markdown into Google Docs API batchUpdate requests.

    Handles:
      - # / ## / ### headings → HEADING_1 / HEADING_2 / HEADING_3
      - * / - bullet items → createParagraphBullets
      - **bold** → updateTextStyle bold
      - |...|...| tables → insertTable + insertText + header bold
      - Plain text → NORMAL_TEXT
    """
    requests: list[dict] = []
    # Track formatting operations to apply after all text is inserted
    heading_ranges: list[tuple[int, int, str]] = []  # (start, end, style)
    bullet_ranges: list[tuple[int, int]] = []  # (start, end)
    bold_ranges: list[tuple[int, int]] = []  # (start, end)
    table_insertions: list[tuple[int, list[list[str]]]] = []  # (index, rows)

    cursor = 1  # Google Docs is 1-indexed
    lines = markdown_text.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # ── Table detection ──────────────────────────────────────────────
        if stripped.startswith("|") and "|" in stripped[1:]:
            table_lines = []
            j = i
            while j < len(lines) and lines[j].strip().startswith("|"):
                table_lines.append(lines[j])
                j += 1
            table_data = _parse_markdown_table(table_lines)
            if table_data and len(table_data) > 0 and len(table_data[0]) > 0:
                table_insertions.append((cursor, table_data))
                # Reserve space — tables are inserted separately
                # Add a newline placeholder for spacing
                text = "\n"
                requests.append({
                    "insertText": {
                        "location": {"index": cursor},
                        "text": text,
                    }
                })
                cursor += len(text)
            i = j
            continue

        # ── Heading detection ────────────────────────────────────────────
        if stripped.startswith("### "):
            heading_text = stripped[4:].strip() + "\n"
            requests.append({
                "insertText": {
                    "location": {"index": cursor},
                    "text": heading_text,
                }
            })
            heading_ranges.append((cursor, cursor + len(heading_text) - 1, "HEADING_3"))
            # Check for bold within heading
            _collect_bold_ranges(heading_text, cursor, bold_ranges)
            cursor += len(heading_text)
            i += 1
            continue

        if stripped.startswith("## "):
            heading_text = stripped[3:].strip() + "\n"
            requests.append({
                "insertText": {
                    "location": {"index": cursor},
                    "text": heading_text,
                }
            })
            heading_ranges.append((cursor, cursor + len(heading_text) - 1, "HEADING_2"))
            _collect_bold_ranges(heading_text, cursor, bold_ranges)
            cursor += len(heading_text)
            i += 1
            continue

        if stripped.startswith("# "):
            heading_text = stripped[2:].strip() + "\n"
            requests.append({
                "insertText": {
                    "location": {"index": cursor},
                    "text": heading_text,
                }
            })
            heading_ranges.append((cursor, cursor + len(heading_text) - 1, "HEADING_1"))
            _collect_bold_ranges(heading_text, cursor, bold_ranges)
            cursor += len(heading_text)
            i += 1
            continue

        # ── Bullet detection ─────────────────────────────────────────────
        if re.match(r"^\s*[-*]\s+", stripped):
            bullet_text_raw = re.sub(r"^\s*[-*]\s+", "", stripped)
            # Strip bold markers from display, track bold ranges
            clean_text = bullet_text_raw + "\n"
            requests.append({
                "insertText": {
                    "location": {"index": cursor},
                    "text": clean_text,
                }
            })
            bullet_ranges.append((cursor, cursor + len(clean_text) - 1))
            _collect_bold_ranges(clean_text, cursor, bold_ranges)
            cursor += len(clean_text)
            i += 1
            continue

        # ── Plain text ───────────────────────────────────────────────────
        if stripped:
            plain_text = stripped + "\n"
            requests.append({
                "insertText": {
                    "location": {"index": cursor},
                    "text": plain_text,
                }
            })
            _collect_bold_ranges(plain_text, cursor, bold_ranges)
            cursor += len(plain_text)
        else:
            # Empty line → paragraph break
            requests.append({
                "insertText": {
                    "location": {"index": cursor},
                    "text": "\n",
                }
            })
            cursor += 1

        i += 1

    # ── Apply heading styles ─────────────────────────────────────────────
    for start, end, style in heading_ranges:
        requests.append({
            "updateParagraphStyle": {
                "range": {"startIndex": start, "endIndex": end},
                "paragraphStyle": {"namedStyleType": style},
                "fields": "namedStyleType",
            }
        })

    # ── Apply bullet styles ──────────────────────────────────────────────
    for start, end in bullet_ranges:
        requests.append({
            "createParagraphBullets": {
                "range": {"startIndex": start, "endIndex": end},
                "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
            }
        })

    # ── Apply bold styles ────────────────────────────────────────────────
    for start, end in bold_ranges:
        requests.append({
            "updateTextStyle": {
                "range": {"startIndex": start, "endIndex": end},
                "textStyle": {"bold": True},
                "fields": "bold",
            }
        })

    return requests, cursor, table_insertions


def _collect_bold_ranges(
    text: str, base_offset: int, bold_ranges: list[tuple[int, int]]
) -> None:
    """Find **bold** patterns in text and record their index ranges."""
    pattern = re.compile(r"\*\*(.+?)\*\*")
    # We need to compute positions in the text as-is (with ** markers)
    # But the text inserted into the doc still has ** markers at this point.
    # We'll handle bold by stripping ** during insert and tracking offsets.
    # Actually, for simplicity, we leave ** in text and strip+bold post-hoc.
    for match in pattern.finditer(text):
        bold_start = base_offset + match.start()
        bold_end = base_offset + match.end()
        bold_ranges.append((bold_start, bold_end))


def _build_table_requests(
    table_data: list[list[str]], insert_index: int
) -> list[dict]:
    """Build Google Docs API requests to insert a native table."""
    if not table_data:
        return []

    num_rows = len(table_data)
    num_cols = len(table_data[0]) if table_data else 0
    if num_cols == 0:
        return []

    reqs: list[dict] = []

    # Insert the table structure
    reqs.append({
        "insertTable": {
            "rows": num_rows,
            "columns": num_cols,
            "location": {"index": insert_index},
        }
    })

    return reqs


def _populate_table_after_creation(
    docs, doc_id: str, table_data: list[list[str]], table_index: int
) -> None:
    """
    After creating a table, read the doc to find the table element,
    then populate cells and bold the header row.
    """
    # Read the document to find table structure
    doc = docs.documents().get(documentId=doc_id).execute()
    body = doc.get("body", {}).get("content", [])

    # Find the table element at or after our insert index
    table_element = None
    for element in body:
        if "table" in element:
            start_idx = element.get("startIndex", 0)
            if start_idx >= table_index - 1:
                table_element = element["table"]
                break

    if not table_element:
        logger.warning("Could not find table element after insertion at index %d", table_index)
        return

    populate_requests: list[dict] = []
    bold_requests: list[dict] = []

    rows = table_element.get("tableRows", [])
    for row_idx, (row_element, row_data) in enumerate(zip(rows, table_data)):
        cells = row_element.get("tableCells", [])
        for col_idx, (cell_element, cell_text) in enumerate(zip(cells, row_data)):
            # Each cell has content with at least one paragraph
            cell_content = cell_element.get("content", [])
            if not cell_content:
                continue
            # Get the start index of the first paragraph in the cell
            first_para = cell_content[0]
            para_start = first_para.get("startIndex", 0)

            if cell_text.strip():
                populate_requests.append({
                    "insertText": {
                        "location": {"index": para_start},
                        "text": cell_text.strip(),
                    }
                })
                # Bold the header row (row 0)
                if row_idx == 0:
                    bold_requests.append({
                        "updateTextStyle": {
                            "range": {
                                "startIndex": para_start,
                                "endIndex": para_start + len(cell_text.strip()),
                            },
                            "textStyle": {"bold": True},
                            "fields": "bold",
                        }
                    })

    # Execute cell population (must be done in reverse index order to avoid offset shifts)
    all_reqs = populate_requests + bold_requests
    if all_reqs:
        # Sort by index in reverse order to prevent offset corruption
        all_reqs.sort(
            key=lambda r: (
                r.get("insertText", r.get("updateTextStyle", {}))
                .get("location", r.get("insertText", r.get("updateTextStyle", {})).get("range", {}))
                .get("index", r.get("insertText", r.get("updateTextStyle", {})).get("range", {}).get("startIndex", 0))
            ),
            reverse=True,
        )
        docs.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": all_reqs},
        ).execute()


def _strip_bold_markers(text: str) -> tuple[str, list[tuple[int, int]]]:
    """
    Remove **bold** markers from text and return clean text + bold ranges.

    Returns:
        (clean_text, [(start, end), ...]) where ranges are 0-indexed in clean_text.
    """
    result = []
    bold_ranges: list[tuple[int, int]] = []
    i = 0
    clean_pos = 0

    while i < len(text):
        if text[i:i+2] == "**":
            # Find closing **
            end = text.find("**", i + 2)
            if end != -1:
                bold_start = clean_pos
                inner = text[i+2:end]
                result.append(inner)
                clean_pos += len(inner)
                bold_ranges.append((bold_start, clean_pos))
                i = end + 2
                continue
        result.append(text[i])
        clean_pos += 1
        i += 1

    return "".join(result), bold_ranges


def _markdown_to_docs_pipeline(
    docs, doc_id: str, markdown_text: str
) -> int:
    """
    Full Markdown-to-Google-Docs conversion pipeline.

    Inserts text with native formatting into the given document.
    Returns the final cursor position.
    """
    requests: list[dict] = []
    heading_styles: list[tuple[int, int, str]] = []
    bullet_positions: list[tuple[int, int]] = []
    bold_positions: list[tuple[int, int]] = []
    tables_to_create: list[tuple[int, list[list[str]]]] = []

    cursor = 1  # Google Docs body starts at index 1
    lines = markdown_text.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # ── Table detection ──────────────────────────────────────────
        if stripped.startswith("|") and "|" in stripped[1:]:
            table_lines = []
            j = i
            while j < len(lines) and lines[j].strip().startswith("|"):
                table_lines.append(lines[j])
                j += 1
            table_data = _parse_markdown_table(table_lines)
            if table_data and len(table_data) > 0 and len(table_data[0]) > 0:
                tables_to_create.append((cursor, table_data))
                # Insert placeholder newline; table will be inserted here later
                requests.append({
                    "insertText": {
                        "location": {"index": cursor},
                        "text": "\n",
                    }
                })
                cursor += 1
            i = j
            continue

        # ── Heading detection ────────────────────────────────────────
        heading_level = 0
        if stripped.startswith("### "):
            heading_level = 3
            heading_text_raw = stripped[4:].strip()
        elif stripped.startswith("## "):
            heading_level = 2
            heading_text_raw = stripped[3:].strip()
        elif stripped.startswith("# "):
            heading_level = 1
            heading_text_raw = stripped[2:].strip()

        if heading_level:
            clean_text, bolds = _strip_bold_markers(heading_text_raw)
            insert_text = clean_text + "\n"
            requests.append({
                "insertText": {
                    "location": {"index": cursor},
                    "text": insert_text,
                }
            })
            style_name = {1: "HEADING_1", 2: "HEADING_2", 3: "HEADING_3"}[heading_level]
            heading_styles.append((cursor, cursor + len(insert_text) - 1, style_name))
            for bs, be in bolds:
                bold_positions.append((cursor + bs, cursor + be))
            cursor += len(insert_text)
            i += 1
            continue

        # ── Bullet detection ─────────────────────────────────────────
        bullet_match = re.match(r"^\s*[-*]\s+(.+)$", stripped)
        if bullet_match:
            bullet_text_raw = bullet_match.group(1)
            clean_text, bolds = _strip_bold_markers(bullet_text_raw)
            insert_text = clean_text + "\n"
            requests.append({
                "insertText": {
                    "location": {"index": cursor},
                    "text": insert_text,
                }
            })
            bullet_positions.append((cursor, cursor + len(insert_text) - 1))
            for bs, be in bolds:
                bold_positions.append((cursor + bs, cursor + be))
            cursor += len(insert_text)
            i += 1
            continue

        # ── Plain text ───────────────────────────────────────────────
        if stripped:
            clean_text, bolds = _strip_bold_markers(stripped)
            insert_text = clean_text + "\n"
            requests.append({
                "insertText": {
                    "location": {"index": cursor},
                    "text": insert_text,
                }
            })
            for bs, be in bolds:
                bold_positions.append((cursor + bs, cursor + be))
            cursor += len(insert_text)
        else:
            # Empty line → paragraph break
            requests.append({
                "insertText": {
                    "location": {"index": cursor},
                    "text": "\n",
                }
            })
            cursor += 1

        i += 1

    # ── Apply heading styles ─────────────────────────────────────────
    for start, end, style in heading_styles:
        requests.append({
            "updateParagraphStyle": {
                "range": {"startIndex": start, "endIndex": end},
                "paragraphStyle": {"namedStyleType": style},
                "fields": "namedStyleType",
            }
        })

    # ── Apply bullet styles ──────────────────────────────────────────
    for start, end in bullet_positions:
        requests.append({
            "createParagraphBullets": {
                "range": {"startIndex": start, "endIndex": end},
                "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
            }
        })

    # ── Apply bold styles ────────────────────────────────────────────
    for start, end in bold_positions:
        if start < end:
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": start, "endIndex": end},
                    "textStyle": {"bold": True},
                    "fields": "bold",
                }
            })

    # Execute the main text + formatting batch
    if requests:
        docs.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": requests},
        ).execute()

    # ── Insert native tables (reverse order to preserve indices) ─────
    for table_index, table_data in reversed(tables_to_create):
        num_rows = len(table_data)
        num_cols = max(len(row) for row in table_data) if table_data else 0
        if num_rows == 0 or num_cols == 0:
            continue

        # Insert the table
        docs.documents().batchUpdate(
            documentId=doc_id,
            body={
                "requests": [{
                    "insertTable": {
                        "rows": num_rows,
                        "columns": num_cols,
                        "location": {"index": table_index},
                    }
                }]
            },
        ).execute()

        # Populate cells and bold header
        _populate_table_after_creation(docs, doc_id, table_data, table_index)

    return cursor


# ── Reference Upload & Hyperlinking ──────────────────────────────────────────


def _create_reference_md(paper: dict, index: int) -> tuple[str, str]:
    """
    Create a lightweight reference .md string for a matched paper.

    Returns (filename, content).
    """
    title = paper.get("title", "Untitled")
    slug = re.sub(r"[^\w]+", "_", title.lower()).strip("_")[:60]
    filename = f"Reference_{index:02d}_{slug}.md"

    year = paper.get("year", "N/A")
    source = paper.get("source_url", paper.get("source", ""))
    score = paper.get("match_score", "N/A")
    abstract = paper.get("abstract", "No abstract available.")

    content = (
        f"# {title}\n\n"
        f"- **Year:** {year}\n"
        f"- **Source:** {source}\n"
        f"- **Match Score:** {score}%\n\n"
        f"## Abstract\n{abstract}\n"
    )

    return filename, content


def _upload_matched_paper_references(
    drive,
    matched_papers: list[dict],
    folder_id: str,
) -> list[dict[str, str]]:
    """
    Create and upload lightweight reference .md files for each matched paper.

    Returns list of {name, title, url} dicts.
    """
    import tempfile

    uploaded: list[dict[str, str]] = []
    total = len(matched_papers)

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        for i, paper in enumerate(matched_papers, 1):
            filename, content = _create_reference_md(paper, i)
            ref_file = tmp_path / filename
            ref_file.write_text(content, encoding="utf-8")

            print(
                f"PROGRESS: Uploading reference {i}/{total} to Google Drive...",
                flush=True,
            )

            media = MediaFileUpload(
                str(ref_file),
                mimetype="text/markdown",
                resumable=True,
            )
            body = {"name": filename, "parents": [folder_id]}
            created = (
                drive.files()
                .create(body=body, media_body=media, fields="id")
                .execute()
            )
            url = _web_link(created["id"], drive)
            uploaded.append({
                "name": filename,
                "title": paper.get("title", "Untitled"),
                "url": url,
            })

    print(
        f"PROGRESS: ✓ {len(uploaded)} references uploaded to Google Drive.",
        flush=True,
    )
    return uploaded


def _append_reference_section(
    docs,
    doc_id: str,
    references: list[dict[str, str]],
    cursor_pos: int,
) -> None:
    """
    Append 'VII. Reference Materials' section with clickable hyperlinks.
    """
    if not references:
        return

    requests: list[dict] = []

    # Section heading
    heading_text = "VII. Reference Materials\n"
    requests.append({
        "insertText": {
            "location": {"index": cursor_pos},
            "text": heading_text,
        }
    })
    heading_start = cursor_pos
    cursor_pos += len(heading_text)

    # Apply HEADING_1 style
    requests.append({
        "updateParagraphStyle": {
            "range": {
                "startIndex": heading_start,
                "endIndex": heading_start + len(heading_text) - 1,
            },
            "paragraphStyle": {"namedStyleType": "HEADING_1"},
            "fields": "namedStyleType",
        }
    })

    # Insert each reference as a hyperlinked line
    link_styles: list[tuple[int, int, str]] = []  # (start, end, url)
    for ref in references:
        title = ref.get("title", ref.get("name", "Reference"))
        url = ref.get("url", "")
        line_text = f"{title}\n"

        requests.append({
            "insertText": {
                "location": {"index": cursor_pos},
                "text": line_text,
            }
        })

        if url:
            link_styles.append((cursor_pos, cursor_pos + len(title), url))
        cursor_pos += len(line_text)

    # Apply hyperlink styles
    for start, end, url in link_styles:
        requests.append({
            "updateTextStyle": {
                "range": {"startIndex": start, "endIndex": end},
                "textStyle": {
                    "link": {"url": url},
                    "foregroundColor": {
                        "color": {
                            "rgbColor": {"red": 0.0, "green": 0.0, "blue": 0.8}
                        }
                    },
                    "underline": True,
                },
                "fields": "link,foregroundColor,underline",
            }
        })

    if requests:
        docs.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": requests},
        ).execute()


# ── Proposal Export Pipeline ─────────────────────────────────────────────────


def export_proposal_to_workspace(
    session_id: str,
    proposal_path: str,
    matched_papers_json: str | None = None,
    kb_root: str | None = None,
) -> dict[str, Any]:
    """
    Export a generated proposal to Google Workspace with native formatting.

    Creates a Google Doc with proper headings, bullets, tables and bold text.
    Optionally uploads matched paper references and hyperlinks them in
    Section VII.

    Returns a dict suitable for JSON serialization to Swift.
    """
    proposal_file = Path(proposal_path).expanduser()
    if not proposal_file.is_file():
        return {
            "status": "error",
            "message": f"Proposal file not found: {proposal_path}",
        }

    # Resolve session directory
    session_dir: Path | None = None
    if kb_root:
        runs = Path(kb_root).expanduser() / "runs"
        direct = runs / session_id.strip()
        prefixed = runs / f"session_{session_id.strip()}"
        if direct.is_dir():
            session_dir = direct
        elif prefixed.is_dir():
            session_dir = prefixed
        else:
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

    # Read proposal content
    try:
        proposal_md = proposal_file.read_text(encoding="utf-8")
    except OSError as e:
        return {"status": "error", "message": f"Cannot read proposal: {e}"}

    # Load matched papers if provided
    matched_papers: list[dict] = []
    if matched_papers_json:
        mp_path = Path(matched_papers_json).expanduser()
        if mp_path.is_file():
            try:
                matched_papers = json.loads(mp_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Could not load matched_papers.json: %s", e)

    # Authenticate
    creds = get_credentials()
    drive = _drive_service(creds)
    docs = _docs_service(creds)

    sid = session_dir.name
    topic_label = _load_topic_label(session_dir, sid)

    # Create or reuse topic folder
    folder_id, folder_url = create_topic_folder(drive, sid, topic_label)

    # Create empty proposal Google Doc
    doc_title = f"Proposal — {topic_label or sid}"
    doc_meta = {
        "name": doc_title,
        "mimeType": "application/vnd.google-apps.document",
        "parents": [folder_id],
    }
    doc_file = drive.files().create(body=doc_meta, fields="id").execute()
    doc_id = doc_file["id"]

    print(
        "PROGRESS: Rendering proposal with native Google Docs formatting...",
        flush=True,
    )

    # Insert formatted proposal using native Docs API
    final_cursor = _markdown_to_docs_pipeline(docs, doc_id, proposal_md)

    # Upload matched paper references and append Section VII
    ref_entries: list[dict[str, str]] = []
    if matched_papers:
        ref_entries = _upload_matched_paper_references(
            drive, matched_papers, folder_id
        )
        if ref_entries:
            # Re-read the doc to get the current end index
            doc = docs.documents().get(documentId=doc_id).execute()
            body_content = doc.get("body", {}).get("content", [])
            if body_content:
                last_element = body_content[-1]
                end_index = last_element.get("endIndex", final_cursor)
            else:
                end_index = final_cursor

            _append_reference_section(docs, doc_id, ref_entries, end_index - 1)

    proposal_doc_url = _web_link(
        doc_id, drive, mime_type="application/vnd.google-apps.document"
    )

    # Update Master Tracking Document
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    block = (
        f"Proposal Generated: {sid}\n"
        f"Topic: {topic_label}\n"
        f"Exported: {ts}\n"
        f"Proposal Document: {proposal_doc_url}\n"
        f"Topic Folder: {folder_url}\n"
        f"References: {len(ref_entries)} papers uploaded\n"
        f"{'—' * 40}\n\n"
    )

    master_doc_id = _find_master_document(drive)
    if not master_doc_id:
        master_doc_id = _create_master_document(drive)

    docs.documents().batchUpdate(
        documentId=master_doc_id,
        body={
            "requests": [
                {"insertText": {"location": {"index": 1}, "text": block}},
            ]
        },
    ).execute()

    master_url = _web_link(master_doc_id, drive)

    print(
        "PROGRESS: ✓ Proposal exported to Google Workspace with native formatting.",
        flush=True,
    )

    return {
        "status": "success",
        "message": "Proposal exported to Google Workspace.",
        "master_document_url": master_url,
        "proposal_document_url": proposal_doc_url,
        "topic_folder_url": folder_url,
        "session_id": sid,
        "references_uploaded": len(ref_entries),
    }

