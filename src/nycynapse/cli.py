import argparse
import json
import subprocess
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
    er.add_argument("--model", help="provider id to run every pipeline role on")
    er.add_argument("--rep", type=int, default=1, help="repeat number, for repeated runs")
    sub.add_parser(
        "eval-report", help="build evals/results from the runs and refresh the README table"
    )
    au = sub.add_parser("audit-gold", help="have the strongest models review the answer key")
    au.add_argument("--split", default="dev,regression", help="comma separated splits")
    au.add_argument("--only", help="comma separated case ids")
    au.add_argument("--auditors", help="comma separated provider ids (default: all auditors)")
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
        from .llm.client import config as llm_config
        from .llm.client import use_model

        if args.model:
            use_model(args.model)
        llm = llm_config()
        model_id = llm["roles"]["default"]
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

        # One file per model, build, split and repeat. A stopped run resumes from its partial
        # file; a finished one is not run again.
        name = f"{args.system}-{args.split}-r{args.rep}.json"
        if args.only or args.limit:
            name = f"{args.system}-{args.split}-trial-{time.strftime('%Y%m%d-%H%M%S')}.json"
        out = Path(args.resume) if args.resume else root / "runs" / model_id / name
        if out.exists():
            print(f"already finished: {out}")
            return 0
        out.parent.mkdir(parents=True, exist_ok=True)
        git = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=root
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                capture_output=True,
                text=True,
                cwd=root,
            ).stdout.strip()
        )
        meta = {
            "model": model_id,
            "model_label": llm["providers"][model_id].get("label", model_id),
            "repeat": args.rep,
            "git": git + ("-dirty" if dirty else ""),
            "system_id": args.system,
            "system": system.name,
            "models": {
                role: f"{llm['providers'][pid].get('label', pid)} "
                f"({llm['providers'][pid].get('model')})"
                for role, pid in llm["roles"].items()
            },
            "split": args.split,
            "snapshot": snap,
            "catalog_version": catalog_version,
            "prompts": {p: prompt_version(p) for p in ("understand", "generate", "plan", "answer")},
        }
        summary = evaluate(system, cases, con, out, meta=meta)
        print(json.dumps(summary, indent=1))
        print(f"report: {out}")
        return 0

    if args.cmd == "audit-gold":
        import yaml

        from . import lake
        from .evals.audit import audit_case, save
        from .evals.cases import load_dir
        from .evals.harness import split_of
        from .llm.client import config as llm_config
        from .pipeline.cards import metric_catalog
        from .pipeline.context import Context
        from .pipeline.understand import workspace_brief
        from .semantic import gold

        root = settings.semantic_path.parent / "evals"
        snap = yaml.safe_load((root / "snapshot.yaml").read_text())["snapshot_id"]
        splits = args.split.split(",")
        cases = load_dir(root / "cases")
        if "holdout" in splits and settings.holdout_path and settings.holdout_path.exists():
            cases += load_dir(settings.holdout_path)
        cases = [c for c in cases if split_of(c) in splits]
        if args.only:
            cases = [c for c in cases if c.id in args.only.split(",")]
        con = lake.connect(settings, snapshot=snap)
        ctx = Context(settings, con)
        context = {
            "manifest": gold.load(settings.semantic_path / "gold_manifest.json"),
            "brief": workspace_brief(ctx.catalog, ctx.coverage),
            "metrics": metric_catalog(ctx.catalog, {m.name for m in ctx.catalog.models}),
            "rules": "\n".join(
                f"- {'(applies to ' + ', '.join(i.applies_to) + ') ' if i.applies_to else ''}"
                f"{' '.join(i.text.split())}"
                for i in ctx.catalog.instructions
            ),
            "conventions": (root / "CONVENTIONS.md").read_text(),
        }
        auditors = args.auditors.split(",") if args.auditors else llm_config()["auditors"]
        from concurrent.futures import ThreadPoolExecutor

        def folder_for(case):
            # Holdout opinions stay beside the holdout file, out of the repository.
            if split_of(case) == "holdout":
                return settings.holdout_path / "audit"
            return root / "audit" / split_of(case)

        todo = [c for c in cases if not (folder_for(c) / f"{c.id}.json").exists()]

        def one(case):
            cur = con.cursor()
            cur.execute("USE lake")
            result = audit_case(case, context, cur, auditors)
            save(result, folder_for(case))
            verdicts = " ".join(o.get("verdict", "?")[:5] for o in result["opinions"])
            print(f"{case.id:8} {verdicts}", flush=True)
            return bool(result["flagged_by"])

        with ThreadPoolExecutor(2) as pool:
            flagged = sum(pool.map(one, todo))
        print(f"{len(todo)} cases audited, {flagged} flagged by at least one auditor")
        return 0

    if args.cmd == "eval-report":
        from .evals.report import build, readme_table

        evals = settings.semantic_path.parent / "evals"
        summary = build(evals / "runs", evals / "results")
        readme = settings.semantic_path.parent / "README.md"
        text = readme.read_text()
        start, end = "<!-- results:start -->", "<!-- results:end -->"
        if start in text:
            head, rest = text.split(start, 1)
            tail = rest.split(end, 1)[1]
            readme.write_text(f"{head}{start}\n{readme_table(summary)}{end}{tail}")
        print(f"{len(summary['groups'])} result groups, {len(summary['paired'])} paired tests")
        return 0

    if args.cmd == "publish":
        import logging

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
