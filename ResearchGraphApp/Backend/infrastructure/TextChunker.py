"""
TextChunker.py — semantic compression for large academic Markdown corpora.

Aggressively targets only the high-signal sections of a peer-reviewed paper:
  1. Abstract
  2. Introduction
  3. Conclusion / Discussion / Future Work / Limitations

The Methodology, Related Work, and Results bodies are discarded entirely.
The three extracted sections are concatenated with a clear markdown divider.
Falls back to a fixed head/tail window when section headers are undetectable.
"""

from __future__ import annotations

import re

# Divider injected between the three extracted high-signal sections.
_SECTION_DIVIDER = "\n\n--- [Middle Sections Omitted] ---\n\n"

# Legacy separator kept for backward-compat with the cap helper (same string shape).
_OMITTED_SEP = _SECTION_DIVIDER

_FALLBACK_HALF_CHARS = 30_000

# ── Section header patterns ────────────────────────────────────────────────────
# Match lines that ARE a section heading (optional markdown #'s, then the keyword).

_ABSTRACT_HEADER = re.compile(
    r"(?im)^(?:#{1,6}\s*)?abstract\s*:?\s*$"
)
_INTRODUCTION_HEADER = re.compile(
    r"(?im)^(?:#{1,6}\s*)?(?:\d+[\.\s]*)?\s*introduction\s*:?\s*$"
)
# Targets conclusion, discussion, future work, limitations — the closing sections.
_CONCLUSION_HEADER = re.compile(
    r"(?im)^(?:#{1,6}\s*)?(?:\d+[\.\s]*)?\s*"
    r"(?:conclusion|discussion|future\s+work|limitations?)\s*:?\s*$"
)
# Any markdown section heading — used to find the END of a section body.
_NEXT_SECTION = re.compile(r"(?m)^#{1,6}\s+\S")
# Any numbered or markdown heading — broader fallback for non-# delimited papers.
_ANY_HEADING = re.compile(r"(?m)^(?:#{1,6}\s+\S|\d+\.\s+[A-Z])")


def _line_end(text: str, pos: int) -> int:
    """Return the index of the first character after the newline at/after pos."""
    nl = text.find("\n", pos)
    return len(text) if nl == -1 else nl + 1


def _extract_section_body(text: str, header_end: int, stop_pattern: re.Pattern) -> str:
    """
    Extract the body of a section starting right after *header_end*.

    Scans forward for the next section header matching *stop_pattern* and
    returns everything between header_end and that match (stripped).
    If no next section is found, returns everything to the end of the text.
    """
    body_start = _line_end(text, header_end)
    next_sec = stop_pattern.search(text, body_start)
    body_end = next_sec.start() if next_sec else len(text)
    return text[body_start:body_end].strip()


def _cap_to_max_chars(result: str, max_chars: int) -> str:
    """Truncate *result* to *max_chars*, preserving the divider structure if present."""
    if len(result) <= max_chars:
        return result
    if _SECTION_DIVIDER in result:
        parts = result.split(_SECTION_DIVIDER)
        budget = max_chars - len(_SECTION_DIVIDER) * (len(parts) - 1)
        if budget < 1:
            return result[:max_chars]
        per_part = budget // len(parts)
        truncated = _SECTION_DIVIDER.join(p[:per_part] for p in parts)
        return truncated
    return result[:max_chars]


def _fallback_head_tail(text: str, max_chars: int) -> str:
    """Fixed head/tail window used when no standard section headers are found."""
    if len(text) <= _FALLBACK_HALF_CHARS:
        return _cap_to_max_chars(text, max_chars)
    head = text[:_FALLBACK_HALF_CHARS]
    tail = text[-_FALLBACK_HALF_CHARS:]
    result = head + _SECTION_DIVIDER + tail
    return _cap_to_max_chars(result, max_chars)


def extract_academic_bookends(text: str, max_chars: int = 60_000) -> str:
    """
    Compress long academic text by keeping ONLY the high-signal bookend sections:
      • Abstract
      • Introduction
      • Conclusion / Discussion / Future Work / Limitations

    All other body content (Methodology, Related Work, Results, etc.) is discarded.
    The three extracted sections are joined with ``_SECTION_DIVIDER``.

    When standard section headers cannot be detected, falls back to a fixed
    head/tail window (first + last 30k characters).

    The returned string never exceeds ``max_chars``.

    Parameters
    ----------
    text:
        Raw text of the academic document (Markdown or plain text).
    max_chars:
        Hard upper limit on the length of the returned string.

    Returns
    -------
    str
        Compressed, high-signal representation of the paper.
    """
    if not text:
        return text

    abstract_match = _ABSTRACT_HEADER.search(text)
    intro_match = _INTRODUCTION_HEADER.search(text)
    conclusion_match = _CONCLUSION_HEADER.search(text)

    sections: list[str] = []

    # ── 1. Abstract ──────────────────────────────────────────────────────────
    if abstract_match:
        abstract_body = _extract_section_body(
            text, abstract_match.end(), _ANY_HEADING
        )
        if abstract_body:
            sections.append(abstract_body)

    # ── 2. Introduction ──────────────────────────────────────────────────────
    if intro_match:
        # Introduction ends at the next any-heading after it.
        intro_body = _extract_section_body(
            text, intro_match.end(), _ANY_HEADING
        )
        if intro_body:
            sections.append(intro_body)

    # ── 3. Conclusion / Discussion ───────────────────────────────────────────
    if conclusion_match:
        # Conclusion body runs to the next heading (References, Appendix, etc.)
        # or to the end of the document.
        conclusion_body = _extract_section_body(
            text, conclusion_match.end(), _ANY_HEADING
        )
        if conclusion_body:
            sections.append(conclusion_body)

    if sections:
        result = _SECTION_DIVIDER.join(sections)
        return _cap_to_max_chars(result, max_chars)

    # ── Fallback: no recognisable headers found ──────────────────────────────
    return _fallback_head_tail(text, max_chars)
