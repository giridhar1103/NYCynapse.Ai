"""Markdown tables for the README, built from the published summaries in evals/results."""

import json
from pathlib import Path

SPLITS = [
    ("holdout", "Holdout", "never seen while building"),
    ("dev", "Development", "used while building"),
    ("regression", "Regression", "earlier mistakes"),
]
ROWS = [
    ("Right answer", "overall_correct", "pct"),
    ("Rows match the reference", "execution_accuracy", "pct"),
    ("Declined when it should", "abstention_recall", "pct"),
    ("Declined when it should not", "false_abstention_rate", "pct"),
    ("Same answer to rephrasings", "paraphrase_consistency", "pct"),
    ("Median time", "latency_ms_p50", "s"),
    ("Model cost per question", "cost_usd", "cost"),
]


def _fmt(kind: str, v, cases: int) -> str:
    if v is None:
        return "n/a"
    if kind == "pct":
        return f"{v * 100:.1f}".removesuffix(".0") + "%"
    if kind == "s":
        return f"{v / 1000:.1f} s"
    return f"${v / max(1, cases):.3f}"


def load(results: Path) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    for path in sorted(results.glob("*.json")):
        run = json.loads(path.read_text())
        out.setdefault(run["split"], {})[run["id"]] = run
    return out


def readme_table(results: Path) -> str:
    runs = load(results)
    parts = []
    for split, title, note in SPLITS:
        found = runs.get(split)
        if not found:
            continue
        ids = sorted(found)
        cases = found[ids[0]]["summary"]["cases"]
        lines = [
            f"**{title}**, {cases} questions, {note}.",
            "",
            "| | " + " | ".join(i.upper() for i in ids) + " |",
            "|---|" + "---:|" * len(ids),
        ]
        for label, key, kind in ROWS:
            cells = [_fmt(kind, found[i]["summary"].get(key), cases) for i in ids]
            lines.append(f"| {label} | " + " | ".join(cells) + " |")
        parts.append("\n".join(lines))
    snapshots = {r["snapshot"] for split in runs.values() for r in split.values()}
    if snapshots:
        parts.append(f"All runs read lake snapshot {', '.join(map(str, sorted(snapshots)))}.")
    return "\n\n".join(parts) + "\n" if parts else "Results are being produced.\n"
