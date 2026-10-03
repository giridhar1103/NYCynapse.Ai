import os
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    lake_pg_dsn: str | None
    app_pg_dsn: str | None
    lake_data_path: Path
    semantic_path: Path = REPO / "semantic"
    dbt_manifest: Path = Path("/root/NYCynapse_Lake/dbt/target/manifest.json")
    memory_limit: str = "1GB"
    holdout_path: Path | None = Path("/srv/nycynapse/evals-holdout")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            lake_pg_dsn=os.environ.get("NYC_LAKE_PG_DSN"),
            app_pg_dsn=os.environ.get("NYC_APP_PG_DSN"),
            lake_data_path=Path(os.environ.get("NYC_LAKE_DATA_PATH", "/srv/nycynapse/lake")),
            semantic_path=Path(os.environ.get("NYCYNAPSE_SEMANTIC", REPO / "semantic")),
            dbt_manifest=Path(
                os.environ.get(
                    "NYC_LAKE_DBT_MANIFEST", "/root/NYCynapse_Lake/dbt/target/manifest.json"
                )
            ),
            memory_limit=os.environ.get("NYCYNAPSE_MEMORY_LIMIT", "1GB"),
        )
