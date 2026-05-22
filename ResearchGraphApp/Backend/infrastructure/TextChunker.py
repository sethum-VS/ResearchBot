"""
TextChunker.py — semantic compression for large academic Markdown corpora.

Preserves introduction/abstract and conclusion/discussion bookends when section
headers are detectable; otherwise falls back to fixed head/tail windows.
"""

from __future__ import annotations

import re

_OMITTED_SEP = "\n\n[... Middle sections omitted for brevity ...]\n\n"

_FALLBACK_HALF_CHARS = 30_000

_INTRO_HEADER = re.compile(
    r"(?im)^(?:#+\s*)?(?:abstract|introduction)\s*:?\s*$"
)
_OUTRO_HEADER = re.compile(
    r"(?im)^(?:#+\s*)?(?:conclusion|discussion|future\s+work|limitations)\s*:?\s*$"
)
_NEXT_SECTION = re.compile(r"(?m)^#{1,6}\s+\S")


def _line_end(text: str, pos: int) -> int:
    nl = text.find("\n", pos)
    return len(text) if nl == -1 else nl + 1


def _cap_to_max_chars(result: str, max_chars: int) -> str:
    if len(result) <= max_chars:
        return result
    if _OMITTED_SEP in result:
        head, tail = result.split(_OMITTED_SEP, 1)
        budget = max_chars - len(_OMITTED_SEP)
        if budget < 1:
            return result[:max_chars]
        half = budget // 2
        return head[:half] + _OMITTED_SEP + tail[-(budget - half) :]
    return result[:max_chars]


def _fallback_head_tail(text: str, max_chars: int) -> str:
    if len(text) <= _FALLBACK_HALF_CHARS:
        return _cap_to_max_chars(text, max_chars)
    head = text[:_FALLBACK_HALF_CHARS]
    tail = text[-_FALLBACK_HALF_CHARS:]
    result = head + _OMITTED_SEP + tail
    return _cap_to_max_chars(result, max_chars)


def extract_academic_bookends(text: str, max_chars: int = 60000) -> str:
    """
    Compress long academic text by keeping semantically important bookends.

    When standard section headers are found, keeps content after
    Introduction/Abstract and after Conclusion/Discussion/Future Work/Limitations.
    Otherwise keeps the first and last 30k characters.
    The returned string never exceeds ``max_chars``.
    """
    if not text or len(text) <= max_chars:
        return text

    intro_match = _INTRO_HEADER.search(text)
    outro_match = _OUTRO_HEADER.search(text)

    if intro_match and outro_match and outro_match.start() > intro_match.start():
        intro_body_start = _line_end(text, intro_match.end())
        outro_body_start = _line_end(text, outro_match.end())
        intro_region = text[intro_body_start : outro_match.start()]
        next_sec = _NEXT_SECTION.search(intro_region)
        opening = (
            intro_region[: next_sec.start()].strip()
            if next_sec
            else intro_region.strip()
        )
        closing = text[outro_body_start:].strip()
        if opening or closing:
            parts: list[str] = []
            if opening:
                parts.append(opening)
            if closing:
                parts.append(closing)
            result = _OMITTED_SEP.join(parts)
            return _cap_to_max_chars(result, max_chars)

    return _fallback_head_tail(text, max_chars)
