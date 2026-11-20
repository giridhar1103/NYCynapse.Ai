"""Citations in an answer: every source the turn's tools returned gets a number, the answer's
[n] markers are renumbered 1, 2, 3 in reading order, and repeats are tidied."""

from __future__ import annotations

import re

from ..tools import ToolResult


def _source_key(s: dict) -> str:
    return (s.get("url") or s.get("title") or "").rstrip("/")


def _registry(results: list[dict]) -> list[tuple[int, str, dict]]:
    """Every source the turn's tools returned, numbered in the order they arrived."""
    out, seen = [], set()
    for r in results:
        res: ToolResult = r["result"]
        if not res.ok:
            continue
        for s in res.sources:
            key = _source_key(s)
            if key and key not in seen:
                seen.add(key)
                out.append((len(out) + 1, key, s))
    return out


_CITE = re.compile(r"\s?\[(\d{1,2}(?:\s*[,;]\s*\d{1,2})*)\]")


def _uncite(text: str) -> str:
    return _CITE.sub("", text or "")


_GROUP = re.compile(r"(?:\[\d{1,2}\])+")


def _tidy_citations(text: str) -> str:
    """Cite a run of facts from the same source once, at its end: "A [1]. B [1]." becomes
    "A. B [1].", and a list whose lines all cite the same sources cites them on its last line."""

    def within(line: str) -> str:
        groups = list(_GROUP.finditer(line))
        drop = [a for a, b in zip(groups, groups[1:], strict=False) if a.group(0) == b.group(0)]
        for g in reversed(drop):
            line = line[: g.start()] + line[g.end() :]
        return line

    lines = [within(x) for x in text.split("\n")]
    i = 0
    while i < len(lines):
        if not re.match(r"\s*[-•*]\s", lines[i]):
            i += 1
            continue
        j = i
        while j < len(lines) and re.match(r"\s*[-•*]\s", lines[j]):
            j += 1
        run = lines[i:j]
        tails = [_GROUP.findall(x) for x in run]
        if len(run) > 1 and all(t and t == tails[0] for t in tails):
            one = tails[0][-1]
            for k in range(i, j - 1):
                lines[k] = lines[k].replace(one, "")
        i = j
    return "\n".join(lines)


def _cite(answer: str, results: list[dict], declined: bool) -> tuple[str, list[dict]]:
    """Renumber the answer's citations 1, 2, 3 in reading order and keep only cited sources.

    Numbers that point at nothing are dropped. An answer that cites nothing keeps the city
    sources it used (and pages it read), unnumbered, so a reader still sees where it came from.
    """
    reg = {n: s for n, _, s in _registry(results)}
    order: list[int] = []

    def renumber(m: re.Match) -> str:
        nums = []
        for x in re.split(r"[,;]", m.group(1)):
            old = int(x)
            if old not in reg:
                continue
            if old not in order:
                order.append(old)
            nums.append(order.index(old) + 1)
        nums = sorted(set(nums))
        return "".join(f"[{n}]" for n in nums)

    text = _tidy_citations(_CITE.sub(renumber, answer))
    text = re.sub(r"[ \t]+([.,;:])", r"\1", text)
    if declined:
        return _uncite(text), []
    if order:
        return text, [{**reg[old], "n": i + 1} for i, old in enumerate(order)]
    used = [
        s
        for r in results
        if r["result"].ok
        for s in r["result"].sources
        if s.get("kind") != "web" or r["tool"] == "read_page"
    ]
    out, seen = [], set()
    for s in used:
        if _source_key(s) not in seen:
            seen.add(_source_key(s))
            out.append(s)
    return text, out
