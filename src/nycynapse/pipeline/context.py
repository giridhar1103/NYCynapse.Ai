"""Everything the pipeline consults that is not the model: the semantic layer, the catalog
in Postgres (search and value index), and the read-only lake connection."""

import re
from dataclasses import dataclass, field
from functools import cached_property

import duckdb
import psycopg

from ..catalog import store
from ..catalog.sync import EMBEDDING_MODEL, normalize
from ..config import Settings
from ..semantic import gold
from ..semantic.load import load_catalog
from ..semantic.schema import Catalog, SemanticModel

# Words that say what kind of thing is meant rather than which one. They match far too much.
GENERIC = {
    "complaint",
    "complaints",
    "request",
    "requests",
    "report",
    "reports",
    "call",
    "calls",
    "about",
    "issue",
    "issues",
    "problem",
    "problems",
    "the",
    "a",
    "an",
    "of",
    "in",
    "at",
    "near",
    "on",
    "for",
    "trips",
    "trip",
    "rides",
    "ride",
    "area",
    "neighborhood",
}


@dataclass
class Hit:
    id: str
    kind: str
    name: str
    label: str | None
    workspace: str | None
    model: str | None
    score: float
    payload: dict = field(default_factory=dict)


@dataclass
class Grounding:
    mention: str
    kind: str  # value or place
    model: str | None
    dimension: str | None
    value: str
    score: float
    rows: int = 0


class Context:
    def __init__(
        self,
        settings: Settings,
        lake: duckdb.DuckDBPyConnection,
        conn: psycopg.Connection | None = None,
    ):
        self.settings = settings
        self.lake = lake
        self.conn = conn or store.connect(settings.app_pg_dsn)
        self.catalog: Catalog = load_catalog(settings.semantic_path)
        self.manifest = gold.load(settings.semantic_path / "gold_manifest.json")
        self.version = store.current_version(self.conn)
        self.models = {m.name: m for m in self.catalog.models}

    @cached_property
    def embedder(self):
        from fastembed import TextEmbedding

        return TextEmbedding(EMBEDDING_MODEL)

    def embed(self, text: str) -> list[float]:
        return next(iter(self.embedder.embed([text]))).tolist()

    # coverage

    @cached_property
    def coverage(self) -> dict[str, str]:
        """What each workspace's tables cover in time, from the lake's own freshness table."""
        rows = self.lake.execute(
            "SELECT domain, table_name, CAST(coverage_start AS DATE), CAST(coverage_end AS DATE) "
            "FROM gold.ops_source_freshness WHERE coverage_start IS NOT NULL ORDER BY 1, 2"
        ).fetchall()
        out: dict[str, list[str]] = {}
        for domain, table, start, end in rows:
            out.setdefault(domain, []).append(f"{table} {start} to {end}")
        return {k: "; ".join(v) for k, v in out.items()}

    @cached_property
    def table_coverage(self) -> dict[str, tuple]:
        """Lake table -> (first event, last event) in New York local time."""
        rows = self.lake.execute(
            "SELECT table_name, coverage_start AT TIME ZONE 'America/New_York', "
            "coverage_end AT TIME ZONE 'America/New_York' FROM gold.ops_source_freshness"
        ).fetchall()
        return {t: (start, end) for t, start, end in rows}

    def models_for_tables(self, tables: list[str]) -> list[SemanticModel]:
        return [m for m in self.catalog.models if m.table in tables]

    # workspaces and models

    def workspace_models(self, workspaces: list[str]) -> list[SemanticModel]:
        names: list[str] = []
        for w in self.catalog.workspaces:
            if w.id in workspaces:
                for m in w.models + w.shared:
                    if m not in names:
                        names.append(m)
        return [self.models[n] for n in names]

    # retrieval

    def search(self, question: str, workspaces: list[str], *, k: int = 25) -> list[Hit]:
        """Meaning and word search over the catalog, merged by reciprocal rank fusion."""
        scope = [m.name for m in self.workspace_models(workspaces)]
        vec = str(self.embed(question))
        where = (
            "version = %(v)s AND (model = ANY(%(models)s) OR workspace = ANY(%(ws)s) "
            "OR kind IN ('period', 'place', 'instruction'))"
        )
        args = {"v": self.version, "models": scope, "ws": workspaces, "vec": vec, "q": question}
        by_vec = self.conn.execute(
            f"SELECT id FROM catalog.objects WHERE {where} "
            "ORDER BY embedding <=> %(vec)s::vector LIMIT 60",
            args,
        ).fetchall()
        by_text = self.conn.execute(
            f"SELECT id FROM catalog.objects WHERE {where} "
            "AND tsv @@ websearch_to_tsquery('english', %(q)s) "
            "ORDER BY ts_rank(tsv, websearch_to_tsquery('english', %(q)s)) DESC LIMIT 60",
            args,
        ).fetchall()
        fused: dict[str, float] = {}
        for ranking in (by_vec, by_text):
            for rank, row in enumerate(ranking):
                fused[row["id"]] = fused.get(row["id"], 0) + 1 / (60 + rank)
        top = sorted(fused, key=fused.get, reverse=True)[:k]
        if not top:
            return []
        rows = self.conn.execute(
            "SELECT id, kind, name, label, workspace, model, payload FROM catalog.objects "
            "WHERE version = %s AND id = ANY(%s)",
            (self.version, top),
        ).fetchall()
        by_id = {r["id"]: r for r in rows}
        return [
            Hit(
                i,
                by_id[i]["kind"],
                by_id[i]["name"],
                by_id[i]["label"],
                by_id[i]["workspace"],
                by_id[i]["model"],
                round(fused[i], 5),
                by_id[i]["payload"],
            )
            for i in top
            if i in by_id
        ]

    def verified_examples(
        self,
        question: str,
        workspaces: list[str],
        *,
        k: int = 3,
        min_similarity: float = 0.55,
        near_copy: float = 0.95,
    ) -> list[tuple[float, dict]]:
        """The closest verified question and plan pairs, leaving out near copies."""
        vec = str(self.embed(question))
        rows = self.conn.execute(
            "SELECT payload, 1 - (embedding <=> %s::vector) AS sim FROM catalog.objects "
            "WHERE version = %s AND kind = 'verified' AND workspace = ANY(%s) "
            "ORDER BY embedding <=> %s::vector LIMIT %s",
            (vec, self.version, workspaces, vec, k + 3),
        ).fetchall()
        out = []
        for r in rows:
            sim = float(r["sim"])
            if sim >= near_copy or normalize(r["payload"]["question"]) == normalize(question):
                continue
            if sim >= min_similarity:
                out.append((round(sim, 3), r["payload"]))
        return out[:k]

    # grounding

    def ground(
        self,
        mentions: list[str],
        workspaces: list[str],
        *,
        per_dimension: int = 2,
        per_mention: int = 16,
        containing: int = 8,
    ) -> list[Grounding]:
        """Candidate values for each mention, the best few per dimension.

        Keeping a few per dimension matters: a neighborhood name also matches station and taxi
        zone names, and the right reading depends on the rest of the question. Values that
        contain the whole mention are all kept, up to a limit: "Williamsburg" is three
        neighborhoods, and a common name means all of them.
        """
        scope = [m.name for m in self.workspace_models(workspaces)]
        out: list[Grounding] = []
        for mention in mentions:
            norm = " ".join(w for w in normalize(mention).split() if w not in GENERIC)
            if not norm:
                continue
            found = self._route(mention, scope) + self._aliases(norm, scope)
            rows = self.conn.execute(
                """
                SELECT model, dimension, value, rows, normalized,
                       greatest(similarity(normalized, %(m)s), word_similarity(%(m)s, normalized),
                                CASE WHEN normalized = %(m)s THEN 1.0 ELSE 0 END) AS s
                FROM catalog.dimension_values
                WHERE version = %(v)s AND model = ANY(%(models)s)
                  AND (normalized %% %(m)s OR %(m)s <%% normalized OR normalized = %(m)s)
                ORDER BY s DESC, rows DESC LIMIT 80
                """,
                {"v": self.version, "models": scope, "m": norm},
            ).fetchall()
            seen: dict[tuple, int] = {}
            whole = re.compile(rf"\b{re.escape(norm)}\b")
            for r in rows:
                key = (r["model"], r["dimension"])
                limit = containing if whole.search(r["normalized"]) else per_dimension
                if seen.get(key, 0) >= limit:
                    continue
                seen[key] = seen.get(key, 0) + 1
                found.append(
                    Grounding(
                        mention,
                        "value",
                        r["model"],
                        r["dimension"],
                        r["value"],
                        round(float(r["s"]), 3),
                        r["rows"],
                    )
                )
            for place in self.catalog.places:
                best = max(_name_score(norm, normalize(n)) for n in [place.name, *place.aliases])
                if best >= 0.6:
                    found.append(Grounding(mention, "place", None, None, place.name, best))
            found.sort(key=lambda g: (-g.score, -g.rows))
            out += found[:per_mention]
        return out

    def _aliases(self, norm: str, scope: list[str]) -> list[Grounding]:
        return [
            Grounding(norm, "value", a.model, a.dimension, value, 1.0)
            for a in self.catalog.aliases
            if a.model in scope and any(normalize(n) == norm for n in a.names)
            for value in a.values
        ]

    def _route(self, mention: str, scope: list[str]) -> list[Grounding]:
        """Subway routes are one or two characters, too short for trigram matching."""
        m = re.search(
            r"\b(?:the\s+)?([A-Za-z0-9]{1,2}X?)\s*(?:train|line|express)s?\b", mention, re.I
        ) or re.fullmatch(r"\s*(?:the\s+)?([A-Za-z0-9]{1,2})\s*", mention)
        if not m:
            return []
        code = m.group(1).upper()
        routes = {
            r["value"]
            for r in self.conn.execute(
                "SELECT value FROM catalog.dimension_values WHERE version = %s "
                "AND model = 'subway_routes' AND dimension = 'route'",
                (self.version,),
            ).fetchall()
        }
        if code not in routes:
            return []
        return [
            Grounding(mention, "value", model, "route", code, 1.0)
            for model in ("subway_arrivals", "subway_routes", "subway_trips")
            if model in scope
        ]

    def place_coverage(self, place: str) -> dict[str, list[tuple[str, str]]]:
        rows = self.conn.execute(
            "SELECT level, code, name FROM catalog.place_coverage "
            "WHERE version = %s AND place = %s",
            (self.version, place),
        ).fetchall()
        out: dict[str, list[tuple[str, str]]] = {}
        for r in rows:
            out.setdefault(r["level"], []).append((r["code"], r["name"]))
        return out


def _name_score(mention: str, name: str) -> float:
    if mention == name:
        return 1.0
    if re.search(rf"\b{re.escape(name)}\b", mention) or re.search(
        rf"\b{re.escape(mention)}\b", name
    ):
        return 0.8
    a, b = set(mention.split()), set(name.split())
    return len(a & b) / max(len(a | b), 1)
