"""Compile a semantic query plan into SQL, deterministically.

The model chooses what to compute. This code decides how: which governed expression a metric
uses, which joins connect the tables (only declared relationships, found by walking the
relationship graph), how the time window becomes a filter the engine can skip files with,
and how a metric that must not be summed across a dimension is rolled up instead.
"""

from collections import deque
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from ..semantic.schema import Catalog, Metric, MetricSource, Relationship, SemanticModel
from .ir import Plan
from .timeparse import Window

TZ = "America/New_York"


class CompileError(Exception):
    pass


@dataclass
class Edge:
    rel: Relationship
    to: str


GRAINS = {
    "day": "{date}",
    "week": "CAST(date_trunc('week', {date}) AS DATE)",
    "month": "CAST(date_trunc('month', {date}) AS DATE)",
    "year": "year({date})",
    "hour": "date_trunc('hour', {instant} AT TIME ZONE '" + TZ + "')",
    "hour_of_day": "{hour}",
    "day_of_week": "dayname({date})",
}


def _lit(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


class Compiler:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self.models = {m.name: m for m in catalog.models}
        self.graph: dict[str, list[Edge]] = {}
        for r in catalog.relationships:
            self.graph.setdefault(r.left, []).append(Edge(r, r.right))

    # paths

    def path(self, start: str, target: str, role: str | None) -> list[Relationship]:
        """Shortest chain of declared relationships from start to target."""
        queue = deque([(start, [])])
        seen = {start}
        while queue:
            node, chain = queue.popleft()
            for e in self.graph.get(node, []):
                if e.to in seen and e.to != target:
                    continue
                if e.to == target and (role is None or e.rel.role in (role, None)):
                    if role is None and e.rel.role not in (None, "pickup", "start"):
                        continue
                    return [*chain, e.rel]
                if e.to not in seen:
                    seen.add(e.to)
                    queue.append((e.to, [*chain, e.rel]))
        raise CompileError(
            f"no declared relationship from {start} to {target}" + (f" as {role}" if role else "")
        )

    # compile

    def compile(
        self,
        plan: Plan,
        window: Window | None,
        places: dict[str, dict[str, list[tuple[str, str]]]] | None = None,
    ) -> str:
        base = self._model(plan.model)
        join_sql: list[str] = []
        exists: list[str] = []
        aliases = {(base.name, None): "t0"}

        def alias_for(model: str, role: str | None) -> str:
            if model == base.name and role is None:
                return "t0"
            key = (model, role)
            if key in aliases:
                return aliases[key]
            prev_alias = "t0"
            for rel in self.path(base.name, model, role):
                k = (rel.right, rel.role if rel.right == model else None)
                if k in aliases:
                    prev_alias = aliases[k]
                    continue
                if rel.kind == "range":
                    raise CompileError(f"{rel.id} can only filter, not group or select")
                a = f"t{len(aliases)}"
                aliases[k] = a
                cond = rel.condition.replace("{left}", prev_alias).replace("{right}", a)
                # Inner joins throughout: a crash with no weather hour to match cannot say
                # whether it rained, and a LEFT JOIN would show it as its own NULL group.
                join_sql.append(f"JOIN {self.models[rel.right].table} {a} ON {cond}")
                prev_alias = a
            return aliases[key] if key in aliases else prev_alias

        def field_sql(ref: str) -> str:
            if ref.startswith("time:"):
                return self._grain(base, plan, ref[5:])
            model, role, dim = _split(ref)
            m = self._model(model)
            if model != base.name or role:
                rel = self._range_rel(base.name, model)
                if rel is not None:
                    raise CompileError(f"{ref}: {rel.id} is a range relationship, filter only")
            a = alias_for(model, role)
            return _qualify(self._dim_sql(m, dim), a)

        def is_list(ref: str) -> bool:
            if ref.startswith("time:"):
                return False
            model, _, dim = _split(ref)
            return any(d.name == dim and d.kind == "list" for d in self._model(model).dimensions)

        table = self._table(base, plan, window)
        where: list[str] = []
        # time
        if plan.use_time_window and window is not None and not window.is_now:
            where += self._time_filter(base, plan, window)
        if plan.day_part:
            p = next((p for p in self.catalog.periods if p.name == plan.day_part), None)
            if p is None:
                raise CompileError(f"unknown day part {plan.day_part}")
            t = self._time(base, plan)
            if not (t.local_date and t.local_hour):
                raise CompileError(f"{base.name} has no local date and hour for a day part")
            part = p.where.replace("{date}", f"t0.{t.local_date}")
            where.append("(" + part.replace("{hour}", f"t0.{t.local_hour}") + ")")
        # filters
        for f in plan.filters:
            model, role, dim = _split(f.field)
            rel = self._range_rel(base.name, model) if model != base.name else None
            if rel is not None:
                other = self._model(model)
                cond = rel.condition.replace("{left}", "t0").replace("{right}", "x")
                pred = _predicate(_qualify(self._dim_sql(other, dim), "x"), f.op, f.values)
                exists.append(f"EXISTS (SELECT 1 FROM {other.table} x WHERE {cond} AND {pred})")
            elif is_list(f.field) and f.op in ("=", "in", "not in"):
                # A list column holds several values per row (an alert naming three lines).
                values = ", ".join(_lit(v) for v in f.values)
                neg = "NOT " if f.op == "not in" else ""
                where.append(f"{neg}list_has_any({field_sql(f.field)}, [{values}])")
            else:
                where.append(_predicate(field_sql(f.field), f.op, f.values))
        # Columns the question filters on itself; a default filter on the same column gives
        # way (ridership leaves out the Staten Island Railway unless it is asked for).
        explicit = {
            c
            for f in plan.filters
            if _split(f.field)[0] == base.name
            for c in _columns(self._dim_sql(base, _split(f.field)[2]))
        }
        for p in plan.places:
            codes = [c for c, _ in (places or {}).get(p.place, {}).get(p.level, [])]
            if not codes:
                raise CompileError(f"{p.place} covers no {p.level}")
            values = [int(c) if c.isdigit() and p.level == "taxi_zone" else c for c in codes]
            where.append(_predicate(field_sql(p.field), "in", values))

        if plan.kind == "list":
            if plan.share_of:
                raise CompileError("share_of needs an aggregate plan")
            missing = [g for g in plan.per_group if g not in plan.select]
            if missing:
                raise CompileError(f"per_group fields must be selected: {', '.join(missing)}")
            cols = [f"{field_sql(s)} AS {_name(s)}" for s in plan.select] or ["t0.*"]
            sql = f"SELECT {', '.join(cols)}\nFROM {table} t0"
            filters = self._default_filters(base, None, explicit)
            return self._finish(sql, join_sql, where + filters + exists, [], plan, {})

        if not plan.metrics:
            raise CompileError("an aggregate plan needs at least one metric")
        metrics = [self._metric(m, base.name) for m in plan.metrics]
        sources = [self._source(m, base.name) for m in metrics]
        filters = []
        for src in sources:
            for f in self._default_filters(base, src, explicit):
                if f not in filters:
                    filters.append(f)
        groups = []
        for g in plan.group_by:
            if is_list(g):
                # Grouping by a list counts each value once per row that names it.
                u = f"u{len(groups)}"
                join_sql.append(f"CROSS JOIN unnest({field_sql(g)}) AS {u}(value)")
                groups.append((f"{u}.value", _name(g)))
            else:
                groups.append((field_sql(g), _name(g)))
        missing = [g for g in plan.per_group if g not in plan.group_by]
        if missing:
            raise CompileError(f"per_group fields must be grouped: {', '.join(missing)}")
        semi = [m for m in metrics if m.additivity == "semi_additive"]
        if semi:
            if plan.share_of or plan.per_group:
                raise CompileError("share_of and per_group are not supported with " + semi[0].id)
            return self._semi_additive(
                plan, base, table, metrics, sources, groups, join_sql, where + filters + exists
            )
        selects = [f"{expr} AS {name}" for expr, name in groups]
        names = {m.id: m.id for m in metrics}
        for m, src in zip(metrics, sources, strict=True):
            total = _qualify_expr(src.expression, "t0")
            selects.append(f"{total} AS {m.id}")
            if plan.share_of:
                if m.additivity != "additive":
                    raise CompileError(f"a share of {m.id} makes no sense; it does not add up")
                cond = " AND ".join(
                    _predicate(field_sql(f.field), f.op, f.values) for f in plan.share_of
                )
                part = _with_condition(total, cond)
                selects.append(f"{part} AS {m.id}_matching")
                selects.append(f"{part} * 1.0 / NULLIF({total}, 0) AS {m.id}_share")
                names[f"{m.id}_matching"] = f"{m.id}_matching"
                names[f"{m.id}_share"] = f"{m.id}_share"
        sql = f"SELECT {', '.join(selects)}\nFROM {table} t0"
        return self._finish(sql, join_sql, where + filters + exists, groups, plan, names)

    def _finish(self, sql, join_sql, where, groups, plan: Plan, metric_names) -> str:
        parts = [sql, *join_sql]
        if where:
            parts.append("WHERE " + "\n  AND ".join(where))
        if groups:
            parts.append("GROUP BY " + ", ".join(str(i + 1) for i in range(len(groups))))
        if plan.having_min:
            parts.append(
                "HAVING "
                + " AND ".join(
                    f"{metric_names.get(k, k)} >= {v}" for k, v in plan.having_min.items()
                )
            )
        if plan.per_group:
            per = ", ".join(_name(g) for g in plan.per_group)
            order = ", ".join(
                f"{metric_names.get(o.by, _name(o.by))} {'DESC' if o.desc else 'ASC'}"
                for o in plan.order
            )
            if not order:
                raise CompileError("per_group needs an order to rank by")
            return "\n".join(
                [
                    "SELECT * FROM (",
                    *parts,
                    ") ranked",
                    f"QUALIFY row_number() OVER (PARTITION BY {per} ORDER BY {order}) "
                    f"<= {plan.limit or 1}",
                    f"ORDER BY {per}, {order}",
                    "LIMIT 1000",
                ]
            )
        if plan.order:
            parts.append(
                "ORDER BY "
                + ", ".join(
                    f"{metric_names.get(o.by, _name(o.by))} {'DESC' if o.desc else 'ASC'}"
                    for o in plan.order
                )
            )
        elif groups:
            # Grouped by time with no order asked for: read it in time order.
            times = [str(i + 1) for i, g in enumerate(plan.group_by) if g.startswith("time:")]
            if times:
                parts.append("ORDER BY " + ", ".join(times))
        parts.append(f"LIMIT {min(plan.limit or 1000, 1000)}")
        return "\n".join(parts)

    def _semi_additive(self, plan, base, table, metrics, sources, groups, join_sql, where) -> str:
        # Aggregate per (groups + the dimensions the metric must not be summed over), then roll
        # those up with the metric's rollup.
        extra = []
        for m in metrics:
            for dim in m.non_additive_over:
                expr = _qualify(self._dim_sql(base, dim), "t0")
                if expr not in [g[0] for g in groups] and expr not in [e[0] for e in extra]:
                    extra.append((expr, f"_{dim}"))
        inner_groups = groups + extra
        inner = [f"{e} AS {n}" for e, n in inner_groups]
        inner += [
            f"{_qualify_expr(s.expression, 't0')} AS {m.id}"
            for m, s in zip(metrics, sources, strict=True)
        ]
        sql = f"SELECT {', '.join(inner)}\nFROM {table} t0"
        parts = [sql, *join_sql]
        if where:
            parts.append("WHERE " + "\n  AND ".join(where))
        parts.append("GROUP BY " + ", ".join(str(i + 1) for i in range(len(inner_groups))))
        outer = [n for _, n in groups]
        outer += [
            f"{(m.rollup or 'sum')}({m.id}) AS {m.id}"
            if m.additivity == "semi_additive"
            else f"sum({m.id}) AS {m.id}"
            for m in metrics
        ]
        out = [f"SELECT {', '.join(outer)}\nFROM (\n" + "\n".join(parts) + "\n) per_part"]
        if groups:
            out.append("GROUP BY " + ", ".join(n for _, n in groups))
        if plan.order:
            out.append(
                "ORDER BY "
                + ", ".join(
                    f"{o.by if o.by in {m.id for m in metrics} else _name(o.by)} "
                    f"{'DESC' if o.desc else 'ASC'}"
                    for o in plan.order
                )
            )
        out.append(f"LIMIT {min(plan.limit or 1000, 1000)}")
        return "\n".join(out)

    # helpers

    def _model(self, name: str) -> SemanticModel:
        if name not in self.models:
            raise CompileError(f"unknown model {name}")
        return self.models[name]

    def _metric(self, metric_id: str, model: str | None = None) -> Metric:
        if metric_id == "row_count" and model:
            return Metric(
                id="row_count",
                label="Rows",
                description="Rows of the model.",
                unit="rows",
                format="count",
                additivity="additive",
                sources=[MetricSource(model=model, expression="count(*)")],
            )
        try:
            return self.catalog.metric(metric_id)
        except KeyError as e:
            raise CompileError(f"unknown metric {metric_id}") from e

    def _table(self, base: SemanticModel, plan: Plan, window: Window | None) -> str:
        """The base table, or for "right now" on a status table its latest row per key."""
        if window is None or not window.is_now or not base.latest_by:
            return base.table
        t = self._time(base, plan)
        bound = ""
        if window.end is not None:
            bound = (
                f"WHERE {t.instant} <= timezone('America/New_York', "
                f"TIMESTAMP '{window.end:%Y-%m-%d %H:%M:%S}') "
            )
        keys = ", ".join(base.latest_by)
        return (
            f"(SELECT * FROM {base.table} {bound}"
            f"QUALIFY row_number() OVER (PARTITION BY {keys} ORDER BY {t.instant} DESC) = 1)"
        )

    def _source(self, metric: Metric, model: str) -> MetricSource:
        for s in metric.sources:
            if s.model == model:
                return s
        raise CompileError(
            f"metric {metric.id} cannot be computed from {model}; it comes from "
            + ", ".join(s.model for s in metric.sources)
        )

    def _default_filters(
        self, m: SemanticModel, src: MetricSource | None, explicit: set[str] = frozenset()
    ) -> list[str]:
        named = {f.name: f for f in m.filters}
        # A default filter steps aside when the question filters the same column itself. A
        # metric's own filters always apply: they are part of its definition.
        wanted = [f.name for f in m.filters if f.default and not (_columns(f.sql) & explicit)]
        if src:
            # Listing a default filter in a metric does not make it part of the metric's
            # meaning: it still steps aside for an explicit filter on the same column.
            wanted += [
                f
                for f in src.filters
                if f not in wanted
                and not (f in named and named[f].default and _columns(named[f].sql) & explicit)
            ]
        return [_qualify(named[f].sql, "t0") for f in wanted if f in named]

    def _dim_sql(self, m: SemanticModel, dim: str) -> str:
        for d in m.dimensions:
            if d.name == dim:
                return d.sql
        for e in m.entities:
            if e.name == dim or e.column == dim:
                return e.column
        for t in m.time:
            if dim in (t.local_date, t.local_hour, t.instant):
                return dim
        raise CompileError(f"{m.name} has no dimension {dim}")

    def _time(self, m: SemanticModel, plan: Plan):
        if plan.time_dimension:
            for t in m.time:
                if t.name == plan.time_dimension:
                    return t
            raise CompileError(f"{m.name} has no time dimension {plan.time_dimension}")
        t = m.default_time()
        if t is None:
            raise CompileError(f"{m.name} has no time dimension")
        return t

    def _grain(self, m: SemanticModel, plan: Plan, grain: str) -> str:
        t = self._time(m, plan)
        if grain not in GRAINS:
            raise CompileError(f"unknown time grain {grain}")
        if grain in ("hour",) or not t.local_date:
            if grain != "hour" and not t.local_date:
                raise CompileError(f"{m.name} has no local date column")
            return GRAINS[grain].format(instant=_qualify(t.instant, "t0"), date="", hour="")
        return GRAINS[grain].format(
            date=f"t0.{t.local_date}", hour=f"t0.{t.local_hour}", instant=_qualify(t.instant, "t0")
        )

    def _time_filter(self, m: SemanticModel, plan: Plan, w: Window) -> list[str]:
        t = self._time(m, plan)
        s, e = w.iso()
        out = []
        simple_instant = t.instant.isidentifier()
        if simple_instant:
            col = f"t0.{t.instant}"
            if s:
                out.append(f"{col} >= timezone('{TZ}', TIMESTAMP '{s}')")
            if e:
                out.append(f"{col} < timezone('{TZ}', TIMESTAMP '{e}')")
        elif t.local_date:
            col = f"t0.{t.local_date}"
            if s:
                out.append(f"{col} >= DATE '{s[:10]}'")
            if e:
                out.append(
                    f"{col} < DATE '{e[:10]}'"
                    if e[11:] == "00:00:00"
                    else f"{col} <= DATE '{e[:10]}'"
                )
        else:
            raise CompileError(f"cannot filter {m.name} by time")
        return out

    def _range_rel(self, left: str, right: str) -> Relationship | None:
        for e in self.graph.get(left, []):
            if e.to == right and e.rel.kind == "range":
                return e.rel
        return None


def _split(ref: str) -> tuple[str, str | None, str]:
    if "." not in ref:
        raise CompileError(f"field {ref} must be written model.dimension")
    model, dim = ref.split(".", 1)
    role = None
    if "@" in model:
        model, role = model.split("@", 1)
    return model, role, dim


def _qualify(sql: str, alias: str) -> str:
    if sql.isidentifier():
        return f"{alias}.{sql}"
    return _qualify_expr(sql, alias)


def _qualify_expr(sql: str, alias: str) -> str:
    """Prefix bare column names in an expression with the table alias."""
    if sql.strip() in ("*", "count(*)"):
        return sql
    tree = sqlglot.parse_one(sql, dialect="duckdb")
    for col in tree.find_all(exp.Column):
        if not col.table:
            col.set("table", exp.to_identifier(alias))
    return tree.sql(dialect="duckdb")


def _columns(sql: str) -> set[str]:
    try:
        return {c.name for c in sqlglot.parse_one(sql, dialect="duckdb").find_all(exp.Column)}
    except sqlglot.errors.ParseError:
        return set()


def _with_condition(expr_sql: str, cond_sql: str) -> str:
    """The same aggregate restricted to rows matching a condition, kept inside each aggregate
    so a metric's own definition (FILTER clauses included) is untouched."""
    tree = sqlglot.parse_one(expr_sql, dialect="duckdb")
    cond = sqlglot.parse_one(cond_sql, dialect="duckdb")
    for agg in list(tree.find_all(exp.AggFunc)):
        if isinstance(agg.parent, exp.Filter):
            where = agg.parent.expression
            where.set("this", exp.and_(where.this, cond.copy()))
            continue
        wrapped = exp.Filter(this=agg.copy(), expression=exp.Where(this=cond.copy()))
        if agg is tree:  # the whole expression is one aggregate, such as count(*)
            tree = wrapped
        else:
            agg.replace(wrapped)
    return tree.sql(dialect="duckdb")


def _predicate(col: str, op: str, values: list) -> str:
    if op in ("is true", "is false", "is null", "is not null"):
        return f"{col} {op.upper()}"
    if op in ("in", "not in"):
        if not values:
            raise CompileError(f"{op} needs values")
        return f"{col} {op.upper()} ({', '.join(_lit(v) for v in values)})"
    if op == "like":
        return f"{col} ILIKE {_lit(values[0])}"
    if not values:
        raise CompileError(f"{op} needs a value")
    return f"{col} {op} {_lit(values[0])}"


def _name(ref: str) -> str:
    return ref.replace("time:", "").replace("@", "_").split(".")[-1]
