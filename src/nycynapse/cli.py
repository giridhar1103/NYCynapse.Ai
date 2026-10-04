import argparse
import json
import sys
import time
from pathlib import Path

from .config import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nycynapse")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("export-gold", help="write gold tables and docs to semantic/gold_manifest.json")
    sub.add_parser("check", help="validate the semantic layer against gold_manifest.json")
    ev = sub.add_parser("verify-gold", help="run every gold query and report problems")
    ev.add_argument("--only", help="comma separated case ids")
    pin = sub.add_parser("eval-pin", help="pin the current lake snapshot for evaluation")
    pin.add_argument("--reason", default="evaluation set")
    er = sub.add_parser("eval", help="run a system over the evaluation cases")
    er.add_argument("--system", choices=["e0", "e1", "e2", "e3", "e4"], default="e0")
    er.add_argument("--split", choices=["dev", "regression", "holdout", "all"], default="dev")
    er.add_argument("--only", help="comma separated case ids")
    er.add_argument("--limit", type=int)
    er.add_argument("--resume", help="report path of a stopped run to continue")
    ep = sub.add_parser("eval-publish", help="copy a run's summary to evals/results for the site")
    ep.add_argument("report", nargs="+")
    pub = sub.add_parser("publish", help="validate, then publish the catalog to Postgres")
    pub.add_argument("--no-lake", action="store_true", help="skip the value index and places")
    args = parser.parse_args(argv)
    settings = Settings.from_env()

    if args.cmd == "export-gold":
        from . import lake
        from .semantic import gold

        con = lake.connect(settings)
        data = gold.export(con, settings.dbt_manifest)
        out = settings.semantic_path / "gold_manifest.json"
        out.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
        print(f"{len(data['tables'])} gold tables written to {out}")
        return 0

    if args.cmd == "check":
        from .semantic import gold
        from .semantic.load import load_catalog
        from .semantic.validate import validate

        catalog = load_catalog(settings.semantic_path)
        problems = validate(catalog, gold.load(settings.semantic_path / "gold_manifest.json"))
        for p in problems:
            print(p)
        print(
            f"{len(catalog.models)} models, {len(catalog.metrics)} metrics, "
            f"{len(catalog.relationships)} relationships, {len(problems)} problems"
        )
        return 1 if problems else 0
    if args.cmd == "verify-gold":
        import yaml

        from . import lake
        from .evals.cases import load_dir
        from .evals.execute import run

        root = settings.semantic_path.parent / "evals"
        every = load_dir(root / "cases")
        if settings.holdout_path and settings.holdout_path.exists():
            every += load_dir(settings.holdout_path)
        cases = [c for c in every if c.gold_sql and (not args.only or c.id in args.only.split(","))]
        pinned = root / "snapshot.yaml"
        snap = yaml.safe_load(pinned.read_text())["snapshot_id"] if pinned.exists() else None
        con = lake.connect(settings, snapshot=snap)
        bad = 0
        for c in cases:
            r = run(con, c.gold_sql, timeout_s=300)
            flag = "ERROR" if not r.ok else ("EMPTY" if not r.rows or r.rows == [(None,)] else "ok")
            bad += flag != "ok"
            preview = r.error if not r.ok else str(r.rows[:3])[:110]
            print(f"{flag:5} {c.id:8} {r.ms:>7}ms  {preview}")
        print(f"{len(cases)} gold queries, {bad} problems")
        return 1 if bad else 0

    if args.cmd == "eval-pin":
        import psycopg
        import yaml

        from . import lake

        con = lake.connect(settings)
        snap = con.execute("SELECT id FROM ducklake_current_snapshot('lake')").fetchone()[0]
        with psycopg.connect(settings.lake_pg_dsn, autocommit=True) as pg:
            pg.execute(
                "INSERT INTO ops.pinned_snapshots (snapshot_id, reason) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (snap, args.reason),
            )
        path = settings.semantic_path.parent / "evals" / "snapshot.yaml"
        path.write_text(yaml.safe_dump({"snapshot_id": snap, "reason": args.reason}))
        print(f"pinned snapshot {snap}")
        return 0

    if args.cmd == "eval":
        import yaml

        from . import lake
        from .evals.cases import load_dir
        from .evals.harness import evaluate, split_of
        from .semantic import gold

        root = settings.semantic_path.parent / "evals"
        snap = yaml.safe_load((root / "snapshot.yaml").read_text())["snapshot_id"]
        cases = load_dir(root / "cases")
        holdout = settings.holdout_path
        if holdout and holdout.exists():
            cases += load_dir(holdout)
        if args.only:
            cases = [c for c in cases if c.id in args.only.split(",")]
        elif args.split != "all":
            cases = [c for c in cases if split_of(c) == args.split]
        if args.limit:
            cases = cases[: args.limit]
        manifest = gold.load(settings.semantic_path / "gold_manifest.json")
        if args.system == "e0":
            from .baselines.full_schema import FullSchemaBaseline

            system = FullSchemaBaseline(manifest)
        con = lake.connect(settings, snapshot=snap)
        catalog_version = None
        if args.system in ("e1", "e2", "e3", "e4"):
            from .pipeline.context import Context
            from .pipeline.graph import PipelineSystem

            names = {
                "e1": "E1 routed and pruned",
                "e2": "E2 plus grounding",
                "e3": "E3 plan and compile",
                "e4": "E4 plus verified examples",
            }
            ctx = Context(settings, con)
            catalog_version = ctx.version
            system = PipelineSystem(
                ctx,
                grounding=args.system != "e1",
                planning=args.system in ("e3", "e4"),
                examples=args.system == "e4",
                name=names[args.system],
            )
        from .prompts import prompt_version

        stamp = time.strftime("%Y%m%d-%H%M%S")
        out = (
            Path(args.resume)
            if args.resume
            else (root / "runs" / f"{stamp}-{args.system}-{args.split}.json")
        )
        meta = {
            "system": system.name,
            "split": args.split,
            "snapshot": snap,
            "catalog_version": catalog_version,
            "prompts": {p: prompt_version(p) for p in ("understand", "generate", "plan")},
        }
        summary = evaluate(system, cases, con, out, meta=meta)
        print(json.dumps(summary, indent=1))
        print(f"report: {out}")
        return 0

    if args.cmd == "eval-publish":
        results = settings.semantic_path.parent / "evals" / "results"
        results.mkdir(exist_ok=True)
        for path in args.report:
            run = json.loads(Path(path).read_text())
            meta = run["meta"]
            if meta.get("split") == "all":
                print(f"skipped {path}: publish one split at a time")
                continue
            key = meta["system"].split()[0].lower()
            # Summaries only. Per-case results would reveal the holdout questions.
            out = results / f"{meta['split']}-{key}.json"
            out.write_text(
                json.dumps({"id": key, **meta, "summary": run["summary"]}, indent=1) + "\n"
            )
            print(f"wrote {out}")
        return 0

    if args.cmd == "publish":
        import logging
        import subprocess

        from . import lake
        from .catalog import store, sync
        from .semantic import gold
        from .semantic.load import load_catalog
        from .semantic.validate import validate

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        catalog = load_catalog(settings.semantic_path)
        manifest = gold.load(settings.semantic_path / "gold_manifest.json")
        problems = validate(catalog, manifest)
        if problems:
            for p in problems:
                print(p)
            print("not publishing: fix the problems above first")
            return 1
        conn = store.connect(settings.app_pg_dsn)
        store.migrate(conn)
        version = f"{catalog.version}+{sync.content_hash(settings.semantic_path)}"
        sha = (
            subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                cwd=settings.semantic_path,
            ).stdout.strip()
            or None
        )
        con = None if args.no_lake else lake.connect(settings)
        from .pipeline.verified import load as load_verified

        stats = sync.publish(
            conn,
            catalog,
            manifest,
            version=version,
            lake=con,
            git_sha=sha,
            verified=load_verified(settings.semantic_path),
        )
        print(f"published {version}: {stats}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
