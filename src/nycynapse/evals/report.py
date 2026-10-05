"""Build the published results from the raw runs.

Raw runs live in evals/runs/<model>/<build>-<split>-r<repeat>.json and are not committed, since
holdout runs contain the holdout questions. This turns them into evals/results/summary.json:
every metric with a 95% interval, repeat statistics, and paired tests between builds and between
models. Per-case results are published for the development and regression splits only.
"""

import json
import time
from collections import defaultdict
from pathlib import Path

from . import stats

SPLITS = ["holdout", "dev", "regression"]
SYSTEMS = ["e0", "e1", "e2", "e3", "e4"]
CURRENT = "current"


def load_runs(runs: Path) -> list[dict]:
    out = []
    for path in sorted(runs.rglob("*.json")):
        if "trial" in path.name or "2026-10-03-trials" in path.parts:
            continue
        run = json.loads(path.read_text())
        meta = run["meta"]
        if not meta.get("model") or meta.get("split") not in SPLITS:
            continue
        out.append(
            {
                "series": meta.get("series", CURRENT),
                "model": meta["model"],
                "model_label": meta.get("model_label", meta["model"]),
                "system": meta.get("system_id") or meta["system"].split()[0].lower(),
                "split": meta["split"],
                "repeat": meta.get("repeat", 1),
                "meta": meta,
                "results": run["results"],
            }
        )
    return out


def _pct(xs: list[float], p: int):
    xs = sorted(x for x in xs if x is not None)
    return xs[min(len(xs) - 1, int(len(xs) * p / 100))] if xs else None


def describe(rows: list[dict]) -> dict:
    answerable = [r for r in rows if r["expected"] == "answerable"]

    def strict(sample):
        return stats.rate(sample, lambda r: bool(r.get("strict_match")) and r["classification_ok"])

    def table_recall(sample):
        xs = [r["table_recall"] for r in sample if r.get("table_recall") is not None]
        return sum(xs) / len(xs) if xs else None

    by_cat = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)
    return {
        "cases": len(rows),
        "accuracy": stats.bootstrap(rows, stats.accuracy).as_dict(),
        "strict_accuracy": stats.bootstrap(answerable, strict).as_dict(),
        "answered_should_decline": stats.bootstrap(rows, stats.answered_unanswerable).as_dict(),
        "wrong_when_answered": stats.bootstrap(rows, stats.wrong_when_answered).as_dict(),
        "declined_answerable": stats.bootstrap(rows, stats.declined_answerable).as_dict(),
        "query_ran": stats.rate(answerable, lambda r: bool(r["executed"])),
        "query_returned_rows": stats.rate(
            answerable, lambda r: bool(r["executed"]) and bool(r.get("rows"))
        ),
        "table_recall": table_recall(rows),
        "by_category": {
            k: {"n": len(v), "accuracy": stats.accuracy(v)} for k, v in sorted(by_cat.items())
        },
        "latency_ms_p50": _pct([r["latency_ms"] for r in rows], 50),
        "latency_ms_p95": _pct([r["latency_ms"] for r in rows], 95),
        "cost_usd_per_question": sum(r["cost_usd"] or 0 for r in rows) / max(1, len(rows)),
        "tokens_in_per_question": sum(r["tokens_in"] or 0 for r in rows) / max(1, len(rows)),
    }


def build(runs_dir: Path, out_dir: Path) -> dict:
    runs = load_runs(runs_dir)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in runs:
        groups[(r["series"], r["model"], r["system"], r["split"])].append(r)
    entries, first = [], {}
    for key in sorted(groups):
        reps = sorted(groups[key], key=lambda r: r["repeat"])
        series, model, system, split = key
        first[key] = reps[0]["results"]
        meta = reps[0]["meta"]
        entries.append(
            {
                "series": series,
                "model": model,
                "model_label": reps[0]["model_label"],
                "system": system,
                "system_name": meta.get("system"),
                "split": split,
                "snapshot": meta.get("snapshot"),
                "git": meta.get("git"),
                "catalog_version": meta.get("catalog_version"),
                "finished_at": meta.get("finished_at"),
                **describe(reps[0]["results"]),
                "repeats": stats.repeats([r["results"] for r in reps]) if len(reps) > 1 else None,
            }
        )
    paired = []
    for (series, model, system, split), rows in first.items():
        # Each build against the bare model and against the build before it.
        i = SYSTEMS.index(system) if system in SYSTEMS else -1
        for other in {"e0", SYSTEMS[i - 1] if i > 0 else None} - {None, system}:
            base = first.get((series, model, other, split))
            if base:
                paired.append(
                    {
                        "series": series,
                        "split": split,
                        "kind": "build",
                        "a": f"{model}/{other}",
                        "b": f"{model}/{system}",
                        **stats.paired(base, rows),
                    }
                )
    for (series, model, system, split), rows in first.items():
        # Models against each other on the same build.
        for (s2, m2, sys2, sp2), rows2 in first.items():
            if (s2, sys2, sp2) == (series, system, split) and m2 > model:
                paired.append(
                    {
                        "series": series,
                        "split": split,
                        "kind": "model",
                        "a": f"{model}/{system}",
                        "b": f"{m2}/{system}",
                        **stats.paired(rows, rows2),
                    }
                )
    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "method": {
            "interval": f"95% cluster bootstrap, {stats.B} resamples, paraphrases kept together",
            "paired_test": "exact McNemar on cases where exactly one side is right",
        },
        "groups": entries,
        "paired": paired,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1, default=str) + "\n")
    _per_case(first, out_dir / "cases")
    return summary


def _per_case(first: dict, folder: Path) -> None:
    keep = ["id", "category", "question", "expected", "predicted", "result_match"]
    keep += ["strict_match", "match_reason", "sql", "explanation", "latency_ms"]
    for (series, model, system, split), rows in first.items():
        if split == "holdout":
            continue  # holdout questions stay private
        path = folder / series.split(" ")[0] / model / f"{system}-{split}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps({**{k: r.get(k) for k in keep}, "correct": stats.is_correct(r)})
            for r in rows
        ]
        path.write_text("\n".join(lines) + "\n")


def _fmt(est: dict | None) -> str:
    if not est or est.get("value") is None:
        return "n/a"
    v = f"{est['value'] * 100:.1f}".removesuffix(".0") + "%"
    if est.get("low") is None:
        return v
    return f"{v} ({est['low'] * 100:.0f} to {est['high'] * 100:.0f})"


def readme_table(summary: dict, series: str = CURRENT) -> str:
    """Accuracy by model and build, one table per split, with 95% intervals."""
    parts = []
    for split in SPLITS:
        rows = [g for g in summary["groups"] if g["series"] == series and g["split"] == split]
        if not rows:
            continue
        systems = sorted({g["system"] for g in rows}, key=SYSTEMS.index)
        models = sorted({(g["model_label"], g["model"]) for g in rows})
        cell = {(g["model"], g["system"]): g for g in rows}
        lines = [
            f"**{split.capitalize()}**, {rows[0]['cases']} questions. "
            "Right answers, with 95% intervals.",
            "",
            "| Model | " + " | ".join(s.upper() for s in systems) + " |",
            "|---|" + "---:|" * len(systems),
        ]
        for label, model in models:
            cells = [
                _fmt(cell[(model, s)]["accuracy"]) if (model, s) in cell else "" for s in systems
            ]
            lines.append(f"| {label} | " + " | ".join(cells) + " |")
        parts.append("\n".join(lines))
    return "\n\n".join(parts) + "\n" if parts else "Results are being produced.\n"
