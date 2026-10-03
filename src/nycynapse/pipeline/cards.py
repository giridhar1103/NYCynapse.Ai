"""Compact descriptions of the semantic models in scope, written for the model to read."""

from ..semantic.schema import Catalog, SemanticModel


def model_card(m: SemanticModel, manifest: dict, values: dict[tuple[str, str], list[str]]) -> str:
    table = manifest["tables"].get(m.table.split(".")[-1], {"description": "", "columns": {}})
    lines = [f"TABLE {m.table}  ({m.label}, {m.grain})"]
    desc = " ".join((m.description or table["description"]).split())
    if desc:
        lines.append(f"  {desc}")
    for t in m.time:
        local = ", ".join(x for x in (t.local_date, t.local_hour) if x)
        lines.append(
            f"  time {t.name}: filter on {t.instant}"
            + (f"; New York local: {local}" if local else "")
            + ("  (default)" if t.default else "")
        )
    if m.partition:
        lines.append(
            f"  split by {m.partition.column} ({m.partition.granularity}); add it when "
            f"the window covers whole {m.partition.granularity}s"
        )
    for f in m.filters:
        lines.append(
            f"  {'default ' if f.default else ''}filter {f.name}: {f.sql}  ({f.description})"
        )
    dim_by_col = {d.column: d for d in m.dimensions if d.column}
    for col, info in table["columns"].items():
        d = dim_by_col.get(col)
        extra = ""
        if d and (m.name, d.name) in values:
            vs = values[(m.name, d.name)]
            extra = f"  values: {', '.join(vs[:15])}{' ...' if len(vs) > 15 else ''}"
        doc = info["description"]
        lines.append(f"    {col} {info['type']}" + (f"  -- {doc}" if doc else "") + extra)
    return "\n".join(lines)


def metric_lines(catalog: Catalog, model_names: set[str]) -> str:
    out = []
    for metric in catalog.metrics:
        sources = [s for s in metric.sources if s.model in model_names]
        if not sources:
            continue
        ways = "; ".join(
            f"{catalog.model(s.model).table}: {s.expression}"
            + (f" with filters {', '.join(s.filters)}" if s.filters else "")
            for s in sources
        )
        note = ""
        if metric.additivity == "semi_additive":
            note = (
                f" Do not add across {', '.join(metric.non_additive_over)}: compute per "
                f"{metric.non_additive_over[0]} then take the {metric.rollup}."
            )
        out.append(
            f"- {metric.id} ({metric.label}, {metric.unit}): {metric.description.strip()} "
            f"Compute as {ways}.{note}"
        )
    return "\n".join(out)


def relationship_lines(catalog: Catalog, model_names: set[str]) -> str:
    out = []
    for r in catalog.relationships:
        if r.left in model_names and r.right in model_names:
            left, right = catalog.model(r.left).table, catalog.model(r.right).table
            cond = r.condition.replace("{left}", left).replace("{right}", right)
            how = " (use as EXISTS, it can match several rows)" if r.kind == "range" else ""
            out.append(f"- {r.description} {cond}{how}")
    return "\n".join(out)


def instruction_lines(catalog: Catalog, workspaces: list[str], model_names: set[str]) -> str:
    keep = [
        i
        for i in catalog.instructions
        if not i.applies_to or set(i.applies_to) & (set(workspaces) | model_names)
    ]
    return "\n".join(f"- {' '.join(i.text.split())}" for i in keep)


def semantic_card(
    m: SemanticModel, manifest: dict, values: dict[tuple[str, str], list[str]], catalog: Catalog
) -> str:
    """The model in semantic terms, for the planner: dimensions, keys, times, filters."""
    table = manifest["tables"].get(m.table.split(".")[-1], {"columns": {}})
    cols = table["columns"]
    lines = [f"MODEL {m.name}: {m.label}, {m.grain}"]
    for t in m.time:
        lines.append(f"  time {t.name}{' (default)' if t.default else ''}")
    for e in m.entities:
        lines.append(f"  key {e.column}")
    for d in m.dimensions:
        doc = d.description or cols.get(d.column or "", {}).get("description", "")
        vs = values.get((m.name, d.name))
        extra = f" values: {', '.join(vs[:12])}{' ...' if len(vs) > 12 else ''}" if vs else ""
        syn = f" (also: {', '.join(d.synonyms)})" if d.synonyms else ""
        lines.append(f"  dimension {d.name} [{d.kind}]{syn}: {doc[:160]}{extra}")
    for f in m.filters:
        lines.append(f"  filter {f.name}{' (always on)' if f.default else ''}: {f.description}")
    return "\n".join(lines)


def metric_catalog(catalog: Catalog, model_names: set[str]) -> str:
    out = []
    for metric in catalog.metrics:
        srcs = [s.model for s in metric.sources if s.model in model_names]
        if srcs:
            note = (
                f" Must not be summed across {', '.join(metric.non_additive_over)}."
                if metric.non_additive_over
                else ""
            )
            out.append(
                f"- {metric.id}: {metric.label}, {metric.unit}. "
                f"{' '.join(metric.description.split())}{note} Sources: {', '.join(srcs)}"
            )
    return "\n".join(out)


def relationship_catalog(catalog: Catalog, model_names: set[str]) -> str:
    out = []
    for r in catalog.relationships:
        if r.left in model_names and r.right in model_names:
            role = f" as {r.role}" if r.role else ""
            kind = " (filter only)" if r.kind == "range" else ""
            out.append(f"- {r.left} -> {r.right}{role}{kind}: {r.description}")
    return "\n".join(out)
