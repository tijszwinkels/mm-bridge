"""Parse assistant output for VibeDeck-style XML directives.

Supported directives:
    <openFile path="..." [line="..."] [follow="..."] />
    <leaveChannel [reason="..."] />

Regex mirrors VibeDeck's JS parser in
``VibeDeck/src/vibedeck/templates/static/js/commands.js`` so both sides agree
on what counts as a directive (including odd cases like directives inside
code fences, which the JS parser also matches).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


@dataclass
class Directive:
    kind: Literal["open_file", "leave_channel"]
    attrs: dict[str, str]


_OPEN_FILE_RE = re.compile(r"<openFile\s+([^>]*)/>", re.IGNORECASE)
_LEAVE_CHANNEL_RE = re.compile(
    r"<leaveChannel(?:\s+([^>]*?))?\s*/>", re.IGNORECASE
)
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')

# Collapse 3+ consecutive newlines (possibly with blank whitespace) to two.
_BLANK_RUN_RE = re.compile(r"\n[ \t]*\n(?:[ \t]*\n)+")


def _parse_attrs(inner: str | None) -> dict[str, str]:
    if not inner:
        return {}
    return {m.group(1): m.group(2) for m in _ATTR_RE.finditer(inner)}


def extract(text: str) -> tuple[str, list[Directive]]:
    """Extract directives from ``text`` and return (cleaned_text, directives).

    Directives are stripped (replaced with ""). Order in the returned list
    reflects textual order. Runs of blank lines introduced by stripping are
    collapsed to a single blank line. Other whitespace around directives is
    preserved so the caller can decide how to present surrounding prose.
    """

    matches: list[tuple[int, int, Directive]] = []

    for m in _OPEN_FILE_RE.finditer(text):
        matches.append(
            (m.start(), m.end(), Directive("open_file", _parse_attrs(m.group(1))))
        )

    for m in _LEAVE_CHANNEL_RE.finditer(text):
        matches.append(
            (m.start(), m.end(), Directive("leave_channel", _parse_attrs(m.group(1))))
        )

    if not matches:
        return text, []

    matches.sort(key=lambda t: t[0])

    out: list[str] = []
    cursor = 0
    directives: list[Directive] = []
    for start, end, directive in matches:
        out.append(text[cursor:start])
        directives.append(directive)
        cursor = end
    out.append(text[cursor:])
    cleaned = "".join(out)

    # Collapse runs of blank lines that can appear when a directive occupied
    # its own line (e.g. ``"foo\n<openFile .../>\nbar"`` → ``"foo\n\nbar"``
    # stays fine, but three+ newlines get squashed back to a single blank).
    cleaned = _BLANK_RUN_RE.sub("\n\n", cleaned)

    return cleaned, directives
