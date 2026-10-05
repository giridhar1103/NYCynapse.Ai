"""Cross-checks between the semantic layer and the gold tables.

Every SQL fragment in the YAML (dimension expressions, measures, filters, metric sources,
relationship conditions, day parts) is parsed with sqlglot and every column it names is
looked up in the table it belongs to. A typo or a column dropped from gold fails here, long
before a user question would hit it.
"""

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from .schema import Catalog, SemanticModel

DIALECT = "duckdb"


@dataclass(frozen=True)
class Problem:
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.where}: {self.message}"


def _parse(sql: str):
    return sqlglot.parse_one(sql, dialect=DIALECT)


def _columns(sql: str) -> list[exp.Column]:
    if sql.strip() == "*":
        return []
    return list(_parse(sql).find_all(exp.Column))


def _table_aliases(tree) -> dict[str, str]:
    out = {}
    for t in tree.find_all(exp.Table):
        out[t.alias_or_name] = t.name
    return out


class Validator:
    def __init__(self, catalog: Catalog, gold: dict):
        self.catalog = catalog
        self.tables = gold["tables"]
        self.problems: list[Problem] = []

    def problem(self, where: str, message: str) -> None:
        self.problems.append(Problem(where, message))

    def gold_columns(self, model: SemanticModel) -> set[str] | None:
        name = model.table.split(".")[-1]
        table = self.tables.get(name)
        return None if table is None else set(table["columns"])

    def check_sql(self, where: str, sql: str, allowed: set[str]) -> None:
        try:
            cols = _columns(sql)
        except sqlglot.errors.ParseError as e:
            self.problem(where, f"does not parse: {e}")
            return
        for c in cols:
            if c.table:
                continue
            if c.name not in allowed:
                self.problem(where, f"unknown column {c.name}")

    def run(self) -> list[Problem]:
        self._models()
        self._metrics()
        self._relationships()
        self._workspaces()
        self._periods()
        self._aliases()
        return self.problems

    def _aliases(self) -> None:
        models = {m.name: m for m in self.catalog.models}
        for a in self.catalog.aliases:
            m = models.get(a.model)
            if m is None:
                self.problem(f"alias {a.names[0]}", f"unknown model {a.model}")
            elif a.dimension not in {d.name for d in m.dimensions}:
                self.problem(f"alias {a.names[0]}", f"{a.model} has no dimension {a.dimension}")

    def _models(self) -> None:
        names = [m.name for m in self.catalog.models]
        for dup in {n for n in names if names.count(n) > 1}:
            self.problem(dup, "model defined twice")
        for m in self.catalog.models:
            cols = self.gold_columns(m)
            if cols is None:
                self.problem(m.name, f"table {m.table} is not in gold")
                continue
            for t in m.time:
                for part in (t.instant, t.local_date, t.local_hour):
                    if part:
                        self.check_sql(f"{m.name}.time.{t.name}", part, cols)
            for e in m.entities:
                self.check_sql(f"{m.name}.entity.{e.name}", e.column, cols)
            for d in m.dimensions:
                self.check_sql(f"{m.name}.{d.name}", d.sql, cols)
            for ms in m.measures:
                self.check_sql(f"{m.name}.{ms.name}", ms.expr, cols)
            for f in m.filters:
                self.check_sql(f"{m.name}.filter.{f.name}", f.sql, cols)
            if m.partition:
                self.check_sql(f"{m.name}.partition", m.partition.column, cols)
                if m.partition.time_dimension not in {t.name for t in m.time}:
                    self.problem(m.name, "partition names an unknown time dimension")

    def _metrics(self) -> None:
        ids = [m.id for m in self.catalog.metrics]
        for dup in {i for i in ids if ids.count(i) > 1}:
            self.problem(dup, "metric defined twice")
        models = {m.name: m for m in self.catalog.models}
        for metric in self.catalog.metrics:
            for i, src in enumerate(metric.sources):
                where = f"metric {metric.id} source {i}"
                model = models.get(src.model)
                if model is None:
                    self.problem(where, f"unknown model {src.model}")
                    continue
                cols = self.gold_columns(model) or set()
                self.check_sql(where, src.expression, cols)
                try:
                    tree = _parse(src.expression)
                    if not any(isinstance(n, exp.AggFunc) for n in tree.walk()):
                        self.problem(where, "expression has no aggregate")
                except sqlglot.errors.ParseError:
                    pass
                known_filters = {f.name for f in model.filters}
                for f in src.filters:
                    if f not in known_filters:
                        self.problem(where, f"unknown filter {f}")
                dims = {d.name for d in model.dimensions}
                for d in metric.non_additive_over:
                    if d not in dims:
                        self.problem(where, f"non_additive_over names unknown dimension {d}")
                for d in src.supports:
                    if d not in dims:
                        self.problem(where, f"supports unknown dimension {d}")

    def _relationships(self) -> None:
        models = {m.name: m for m in self.catalog.models}
        ids = [r.id for r in self.catalog.relationships]
        for dup in {i for i in ids if ids.count(i) > 1}:
            self.problem(dup, "relationship defined twice")
        for r in self.catalog.relationships:
            left, right = models.get(r.left), models.get(r.right)
            if left is None or right is None:
                self.problem(r.id, f"unknown model in {r.left} -> {r.right}")
                continue
            sql = r.condition.replace("{left}", "l").replace("{right}", "r")
            try:
                tree = _parse(sql)
            except sqlglot.errors.ParseError as e:
                self.problem(r.id, f"does not parse: {e}")
                continue
            aliases = _table_aliases(tree)
            for c in tree.find_all(exp.Column):
                if c.table == "l":
                    allowed = self.gold_columns(left) or set()
                elif c.table == "r":
                    allowed = self.gold_columns(right) or set()
                elif c.table in aliases:
                    t = self.tables.get(aliases[c.table])
                    allowed = set(t["columns"]) if t else set()
                else:
                    self.problem(r.id, f"column {c.sql()} has no table alias")
                    continue
                if c.name not in allowed:
                    self.problem(r.id, f"unknown column {c.sql()}")

    def _workspaces(self) -> None:
        names = {m.name for m in self.catalog.models}
        homes: dict[str, list[str]] = {}
        for w in self.catalog.workspaces:
            for m in w.models + w.shared:
                if m not in names:
                    self.problem(w.id, f"unknown model {m}")
            for m in w.models:
                homes.setdefault(m, []).append(w.id)
        for m in self.catalog.models:
            if m.workspace not in {w.id for w in self.catalog.workspaces}:
                self.problem(m.name, f"unknown workspace {m.workspace}")
            if homes.get(m.name) != [m.workspace]:
                self.problem(
                    m.name, f"listed in workspaces {homes.get(m.name)}, says {m.workspace}"
                )

    def _periods(self) -> None:
        for p in self.catalog.periods:
            try:
                _parse(p.where.replace("{date}", "d").replace("{hour}", "h"))
            except sqlglot.errors.ParseError as e:
                self.problem(f"period {p.name}", f"does not parse: {e}")


def validate(catalog: Catalog, gold: dict) -> list[Problem]:
    return Validator(catalog, gold).run()
