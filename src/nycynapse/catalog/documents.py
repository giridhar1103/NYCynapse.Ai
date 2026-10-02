"""Turn the semantic layer into searchable documents.

Each object becomes one short text that says what it is in plain words: its label,
description, synonyms and, for columns, the documentation dbt carries. Retrieval searches
these texts by meaning (embeddings) and by words (full text).
"""

from dataclasses import dataclass, field

from ..semantic.schema import Catalog


@dataclass
class Document:
    id: str
    kind: str
    name: str
    body: str
    label: str | None = None
    workspace: str | None = None
    model: str | None = None
    payload: dict = field(default_factory=dict)


def _join(*parts) -> str:
    return " ".join(" ".join(str(p).split()) for p in parts if p)


def _syn(words: list[str]) -> str:
    return f"Also called: {', '.join(words)}." if words else ""


def build(catalog: Catalog, gold: dict) -> list[Document]:
    docs: list[Document] = []
    tables = gold["tables"]

    for w in catalog.workspaces:
        docs.append(
            Document(
                id=f"workspace:{w.id}",
                kind="workspace",
                name=w.id,
                label=w.label,
                workspace=w.id,
                body=_join(w.label + ".", w.description, " ".join(w.sample_questions)),
                payload=w.model_dump(),
            )
        )

    for m in catalog.models:
        table = tables.get(m.table.split(".")[-1], {"description": "", "columns": {}})
        cols = table["columns"]
        docs.append(
            Document(
                id=f"model:{m.name}",
                kind="model",
                name=m.name,
                label=m.label,
                workspace=m.workspace,
                model=m.name,
                body=_join(
                    m.label + ".",
                    m.description or table["description"],
                    f"Grain: {m.grain}.",
                    _syn(m.synonyms),
                ),
                payload={**m.model_dump(), "table_description": table["description"]},
            )
        )
        for d in m.dimensions:
            doc = cols.get(d.column or "", {}).get("description", "") if d.column else ""
            docs.append(
                Document(
                    id=f"dimension:{m.name}.{d.name}",
                    kind="dimension",
                    name=d.name,
                    label=d.label or d.name.replace("_", " "),
                    workspace=m.workspace,
                    model=m.name,
                    body=_join(
                        f"{d.label or d.name.replace('_', ' ')} of {m.label.lower()}.",
                        d.description or doc,
                        _syn(d.synonyms),
                    ),
                    payload={**d.model_dump(), "sql": d.sql, "column_description": doc},
                )
            )
        for t in m.time:
            docs.append(
                Document(
                    id=f"time:{m.name}.{t.name}",
                    kind="time",
                    name=t.name,
                    label=f"{t.name} time",
                    workspace=m.workspace,
                    model=m.name,
                    body=_join(
                        f"{t.name.replace('_', ' ')} time of {m.label.lower()}.",
                        cols.get(t.instant, {}).get("description", ""),
                    ),
                    payload=t.model_dump(),
                )
            )
        for ms in m.measures:
            docs.append(
                Document(
                    id=f"measure:{m.name}.{ms.name}",
                    kind="measure",
                    name=ms.name,
                    label=ms.name.replace("_", " "),
                    workspace=m.workspace,
                    model=m.name,
                    body=_join(
                        f"{ms.agg} of {ms.name.replace('_', ' ')} in {m.label.lower()}.",
                        ms.description or cols.get(ms.expr, {}).get("description", ""),
                    ),
                    payload=ms.model_dump(),
                )
            )

    models = {m.name: m for m in catalog.models}
    for metric in catalog.metrics:
        home = models[metric.sources[0].model]
        docs.append(
            Document(
                id=f"metric:{metric.id}",
                kind="metric",
                name=metric.id,
                label=metric.label,
                workspace=home.workspace,
                model=home.name,
                body=_join(
                    metric.label + ".",
                    metric.description,
                    f"Measured in {metric.unit}.",
                    _syn(metric.synonyms),
                ),
                payload=metric.model_dump(),
            )
        )

    for r in catalog.relationships:
        left = models[r.left]
        docs.append(
            Document(
                id=f"relationship:{r.id}",
                kind="relationship",
                name=r.id,
                label=f"{models[r.left].label} to {models[r.right].label}",
                workspace=left.workspace,
                model=r.left,
                body=_join(
                    r.description, f"Joins {models[r.left].label} with {models[r.right].label}."
                ),
                payload=r.model_dump(),
            )
        )

    for p in catalog.periods:
        docs.append(
            Document(
                id=f"period:{p.name}",
                kind="period",
                name=p.name,
                label=p.name.replace("_", " "),
                body=_join(p.description, _syn(p.phrases)),
                payload=p.model_dump(),
            )
        )
    for p in catalog.places:
        docs.append(
            Document(
                id=f"place:{p.name}",
                kind="place",
                name=p.name,
                label=p.name,
                body=_join(
                    f"{p.name}, a {p.kind.replace('_', ' ')} in New York City.", _syn(p.aliases)
                ),
                payload=p.model_dump(),
            )
        )
    for i in catalog.instructions:
        docs.append(
            Document(
                id=f"instruction:{i.id}",
                kind="instruction",
                name=i.id,
                label=i.id.replace("_", " "),
                body=" ".join(i.text.split()),
                payload=i.model_dump(),
            )
        )
    return docs
