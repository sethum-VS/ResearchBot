"""
PdfExtractor.py — In-memory open-access PDF download and text extraction.

Partial mode: first/last three pages (Phase 2 snippet path, legacy).
Full mode: every page with char/byte safety caps (Phase 2.2 triage path).

Thread-safe: each call uses local buffers and its own fitz document handle.
"""

from __future__ import annotations

import io
import logging
import os

import fitz
import requests
from requests import exceptions as requests_exceptions

# Suppress verbose C-level console output from the underlying MuPDF engine
try:
    fitz.TOOLS.mupdf_display_errors(False)
    fitz.TOOLS.mupdf_display_warnings(False)
except AttributeError:
    pass

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT: tuple[float, float] = (5, 15)

_DEFAULT_PARTIAL_MAX_BYTES = 25 * 1024 * 1024
_DEFAULT_PARTIAL_MAX_CHARS = 12_000
_DEFAULT_FULLTEXT_MAX_BYTES = 50 * 1024 * 1024
_DEFAULT_FULLTEXT_MAX_CHARS = 100_000
_PAGES_HEAD = 3
_PAGES_TAIL = 3


def _partial_max_chars() -> int:
    raw = os.getenv("ACADEMIC_PDF_MAX_CHARS", str(_DEFAULT_PARTIAL_MAX_CHARS))
    try:
        return max(1000, int(raw))
    except ValueError:
        return _DEFAULT_PARTIAL_MAX_CHARS


def _partial_max_bytes() -> int:
    raw = os.getenv("ACADEMIC_PDF_MAX_BYTES", str(_DEFAULT_PARTIAL_MAX_BYTES))
    try:
        return max(1_000_000, int(raw))
    except ValueError:
        return _DEFAULT_PARTIAL_MAX_BYTES


def _fulltext_max_chars() -> int:
    raw = os.getenv("ACADEMIC_FULLTEXT_MAX_CHARS", str(_DEFAULT_FULLTEXT_MAX_CHARS))
    try:
        return max(5000, int(raw))
    except ValueError:
        return _DEFAULT_FULLTEXT_MAX_CHARS


def _fulltext_max_bytes() -> int:
    raw = os.getenv("ACADEMIC_FULLTEXT_MAX_BYTES", str(_DEFAULT_FULLTEXT_MAX_BYTES))
    try:
        return max(5_000_000, int(raw))
    except ValueError:
        return _DEFAULT_FULLTEXT_MAX_BYTES


def _page_indices(page_count: int) -> list[int]:
    if page_count <= 0:
        return []
    if page_count <= _PAGES_HEAD + _PAGES_TAIL:
        return list(range(page_count))
    head = list(range(_PAGES_HEAD))
    tail = list(range(page_count - _PAGES_TAIL, page_count))
    return sorted(set(head + tail))


def _truncate(text: str, max_chars: int) -> str:
    cleaned = text.strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 20].rstrip() + "\n\n… [truncated]"


def _extract_partial_pages_text(doc: fitz.Document) -> str:
    indices = _page_indices(doc.page_count)
    parts: list[str] = []
    for idx in indices:
        try:
            page_text = doc.load_page(idx).get_text("text")
        except Exception as exc:
            logger.debug("PdfExtractor: page %s skipped: %s", idx, exc)
            continue
        if page_text and page_text.strip():
            parts.append(page_text.strip())
    return "\n\n".join(parts)


def _extract_all_pages_text(doc: fitz.Document) -> str:
    parts: list[str] = []
    for idx in range(doc.page_count):
        try:
            page_text = doc.load_page(idx).get_text("text")
        except Exception as exc:
            logger.debug("PdfExtractor: page %s skipped: %s", idx, exc)
            continue
        if page_text and page_text.strip():
            parts.append(page_text.strip())
    return "\n\n".join(parts)


def _download_pdf_bytes(url: str, max_bytes: int) -> bytes:
    response = requests.get(
        url,
        timeout=_REQUEST_TIMEOUT,
        stream=True,
        headers={"User-Agent": "ResearchBot/1.0 (PdfExtractor)"},
    )
    response.raise_for_status()

    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                logger.warning(
                    "PdfExtractor: %s exceeds max size (%s bytes)",
                    url,
                    content_length,
                )
                return b""
        except ValueError:
            pass

    buffer = io.BytesIO()
    downloaded = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        downloaded += len(chunk)
        if downloaded > max_bytes:
            logger.warning(
                "PdfExtractor: %s exceeded %s bytes while downloading",
                url,
                max_bytes,
            )
            return b""
        buffer.write(chunk)
    return buffer.getvalue()


def _parse_pdf_bytes(
    pdf_bytes: bytes,
    url: str,
    max_chars: int,
    *,
    full_document: bool,
) -> str:
    if not pdf_bytes:
        return ""
    try:
        try:
            with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
                if full_document:
                    text = _extract_all_pages_text(doc)
                else:
                    text = _extract_partial_pages_text(doc)
        except fitz.FileDataError as exc:
            logger.warning("PdfExtractor: invalid PDF data for %s: %s", url, exc)
            return ""
        except Exception as exc:
            logger.warning("PdfExtractor: parse failed for %s: %s", url, exc)
            return ""
    finally:
        # Periodic cleanup of MuPDF internal error/warning queue to prevent memory growth
        try:
            fitz.TOOLS.reset_mupdf_warnings()
        except AttributeError:
            pass

    if not text:
        return ""
    return _truncate(text, max_chars)


def _extract_from_url(pdf_url: str, *, full_document: bool) -> str:
    if not pdf_url or not pdf_url.strip():
        return ""

    url = pdf_url.strip()
    max_chars = _fulltext_max_chars() if full_document else _partial_max_chars()
    max_bytes = _fulltext_max_bytes() if full_document else _partial_max_bytes()

    try:
        pdf_bytes = _download_pdf_bytes(url, max_bytes)
        return _parse_pdf_bytes(pdf_bytes, url, max_chars, full_document=full_document)
    except requests_exceptions.RequestException as exc:
        logger.warning("PdfExtractor: download failed for %s: %s", url, exc)
        return ""
    except fitz.FileDataError as exc:
        logger.warning("PdfExtractor: file data error for %s: %s", url, exc)
        return ""
    except Exception as exc:
        logger.warning("PdfExtractor: unexpected error for %s: %s", url, exc)
        return ""


def extract_text_from_url(pdf_url: str) -> str:
    """First/last three pages only (partial extraction)."""
    return _extract_from_url(pdf_url, full_document=False)


def extract_full_text_from_url(pdf_url: str) -> str:
    """Every page, capped by ACADEMIC_FULLTEXT_MAX_CHARS / ACADEMIC_FULLTEXT_MAX_BYTES."""
    return _extract_from_url(pdf_url, full_document=True)
