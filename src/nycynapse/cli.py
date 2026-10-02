import argparse
import json
import sys

from .config import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nycynapse")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("export-gold", help="write gold tables and docs to semantic/gold_manifest.json")
    sub.add_parser("check", help="validate the semantic layer against gold_manifest.json")
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
        stats = sync.publish(conn, catalog, manifest, version=version, lake=con, git_sha=sha)
        print(f"published {version}: {stats}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
