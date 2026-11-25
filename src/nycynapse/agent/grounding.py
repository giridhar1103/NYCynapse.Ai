"""Checks that keep an answer to what the tools said: numbers and clock times in a draft that no
tool result supports are found, so they can be rewritten or dropped."""

from __future__ import annotations

import json
import re
from datetime import datetime

from ..tools import ToolResult
from .citations import _uncite

_NUM = re.compile(
    r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(\s*(?:million|billion|thousand)\b)?", re.I
)


_SCALE = {"thousand": 1e3, "million": 1e6, "billion": 1e9}


_SMALL = {str(i) for i in range(0, 13)}


_CLOCK = re.compile(r"\d{1,2}:\d{2}|\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?")


# Numbers that are names, not figures: I-278, Route 9A, 14th St, 42 St, Pier 17, Terminal 4.
_NAMES = re.compile(
    r"\b[A-Z]{1,3}-\d+[A-Z]?\b|\b\d+(?:st|nd|rd|th)\b"
    r"|\b\d+ (?:St|Street|Av|Ave|Avenue|Pl|Rd|Dr|Blvd)\b"
    r"|\b(?:Route|Pier|Terminal|Exit|Exits|Gate|Platform|Track|Hall|PS|P\.S\.|Gates)\s+\d+\w*",
    re.I,
)


def _numbers(text: str) -> list[tuple[str, float, float]]:
    """(as written, value, how far a rounded figure may be off) for each number in a text."""
    out = []
    for m in _NUM.finditer(_CLOCK.sub(" ", text or "")):
        whole = m.group(1).replace(",", "")
        written = whole + (f".{m.group(2)}" if m.group(2) else "")
        scale = _SCALE.get((m.group(3) or "").strip().lower(), 1.0)
        value = float(written) * scale
        step = (10 ** -len(m.group(2)) if m.group(2) else 1.0) * scale
        out.append((written, value, step))
    return out


_AMPM = re.compile(r"\b(\d{1,2}):(\d{2})\s*([ap])(?:\.\s?m\.|m\b)", re.I)


_ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?")


def _minutes_of_day(h: int, m: int, ap: str) -> int:
    return (h % 12 + (12 if ap.lower() == "p" else 0)) * 60 + m


def _untraced_clocks(answer: str, results: list[ToolResult], memory: str = "") -> list[str]:
    """Clock times in the answer ("the 10:14 PM train") that no tool gave, within a minute.
    Tools write times as "10:14 PM" or as ISO timestamps, which are read in New York time
    ("2026-10-24T22:47:00Z" is 6:47 PM). Returns the times as written, so they can be dropped."""
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    texts = [memory] + [
        r.summary + " " + json.dumps(r.data, default=str)[:20000] for r in results if r.ok
    ]
    known: set[int] = set()
    for text in texts:
        for h, m, ap in _AMPM.findall(text):
            known.add(_minutes_of_day(int(h), int(m), ap))
        for stamp in _ISO.findall(text):
            try:
                t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                continue
            t = t.astimezone(ny) if t.tzinfo else t
            known.add(t.hour * 60 + t.minute)
    out = []
    for m in _AMPM.finditer(answer):
        x = _minutes_of_day(int(m.group(1)), int(m.group(2)), m.group(3))
        if not any(abs(x - k) <= 1 or abs(x - k) >= 1439 for k in known):
            out.append(m.group(0))
    return out


def _worked_out(results: list[ToolResult], texts: list[str]) -> list[float]:
    """What follows exactly from the figures: the total of each column of counts in a list of
    rows, the differences and shares of those totals, and a share of 0.7357 as 73.57 percent."""
    totals: list[float] = []
    for r in results:
        rows = r.data if r.ok and isinstance(r.data, list) else []
        if len(rows) < 2 or not all(isinstance(row, dict) for row in rows):
            continue
        for key in rows[0]:
            try:
                col = [float(row[key]) for row in rows]
            except (KeyError, TypeError, ValueError):
                continue  # a name, or a figure some rows do not have
            totals.append(sum(col))
    out = list(totals)
    for i, a in enumerate(totals):
        for b in totals[i + 1 :]:
            out.append(abs(a - b))
            out += [abs(a - b) / x * 100 for x in (a, b) if x]
    for t in texts:
        out += [v * 100 for w, v, _ in _numbers(t) if 0 < v < 1 and len(w) >= 5]
    return out


def _untraced_numbers(answer: str, results: list[ToolResult], memory: str = "") -> list[str]:
    """Numbers in the answer that cannot be traced to what the tools returned (or to earlier
    turns), allowing rounding, "4.1 million" style figures, totals, averages, differences and
    percent changes. Small counts, years and clock times are skipped."""
    values: list[float] = []
    texts = [memory]
    for r in results:
        if not r.ok:
            continue
        texts.append(r.summary + " " + json.dumps(r.data, default=str)[:20000])
        data = r.data if isinstance(r.data, dict) else {}
        rows = data.get("rows") or []
        for i in range(len(data.get("columns") or [])):
            col = [
                v
                for v in (row[i] for row in rows if i < len(row))
                if isinstance(v, int | float) and not isinstance(v, bool)
            ]
            if len(col) > 1:
                values += [float(sum(col)), float(sum(col)) / len(col)]  # totals and averages
    for t in texts:
        values += [v for _, v, _ in _numbers(t)]
    base = [v for v in values if v][:80]
    for x in base:
        for y in base:
            if x != y:
                values.append(abs(x - y))
                values.append(abs(x - y) / abs(y) * 100)
    exact = _worked_out(results, texts)
    out = []
    for written, x, step in _numbers(_NAMES.sub(" ", answer)):
        if written in _SMALL or (len(written) == 4 and written.startswith("20")):
            continue
        slack = step / 2 + 0.051
        if any(abs(x - v) <= slack + abs(v) * 0.01 for v in values):
            continue
        if any(abs(x - v) <= step / 2 + 1e-6 for v in exact):
            continue
        out.append(written)
    return out[:8]


def _drop_untraced(answer: str, figures: list[str]) -> str:
    """Remove the list lines or sentences that hold figures no tool gave, keeping the rest."""
    bad = [re.compile(rf"(?<![\w.]){re.escape(f)}(?![\w])") for f in figures]

    def holds(text: str) -> bool:
        clean = _uncite(text)
        return any(b.search(clean) for b in bad)

    out = []
    for line in answer.split("\n"):
        if line.lstrip().startswith(("- ", "* ", "• ")):
            if not holds(line):
                out.append(line)
            continue
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", line)
        kept = [x for x in parts if not holds(x)]
        if kept or not parts:
            out.append(" ".join(kept))
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip() or answer
