"""Evaluation cases: questions with what a correct answer must look like.

A case is graded stage by stage (classification, workspaces, tables, metrics, grounded values,
places) and finally by result: the system's SQL and the gold SQL both run against the same
pinned lake snapshot and their results are compared.
"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

AS_OF = "2026-10-03T06:00:00-04:00"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Expect(Strict):
    classification: Literal["answerable", "unclear", "unsupported", "non_data", "refuse"] = (
        "answerable"
    )
    workspaces: list[str] = Field(default_factory=list)
    tables: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    values: list[str] = Field(default_factory=list)
    places: list[str] = Field(default_factory=list)


class Compare(Strict):
    order_matters: bool = False
    rows: int | None = None  # only the first n rows count, for top-n questions
    tolerance: float = 0.001  # relative, for numbers
    # all: every gold column must be matched. subset: gold's first column must be matched,
    # used when the exact extra columns are a matter of taste.
    columns: Literal["all", "subset"] = "all"


Category = Literal[
    "single_table",
    "multi_join",
    "cross_domain",
    "temporal",
    "geographic",
    "grounding",
    "live",
    "ambiguous",
    "unsupported",
    "non_data",
    "adversarial",
    "analysis",
]


class Case(Strict):
    id: str
    question: str
    category: Category
    group: str | None = None
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    expect: Expect = Field(default_factory=Expect)
    gold_sql: str | None = None
    compare: Compare = Field(default_factory=Compare)
    as_of: str = AS_OF
    notes: str | None = None

    @model_validator(mode="after")
    def _gold_matches_classification(self):
        answerable = self.expect.classification == "answerable"
        if answerable and not self.gold_sql:
            raise ValueError(f"{self.id}: answerable cases need gold_sql")
        if not answerable and self.gold_sql:
            raise ValueError(f"{self.id}: only answerable cases carry gold_sql")
        return self


def load(paths: list[Path]) -> list[Case]:
    cases: list[Case] = []
    for path in paths:
        with path.open() as f:
            for raw in (yaml.safe_load(f) or {}).get("cases", []):
                cases.append(Case(**raw))
    ids = [c.id for c in cases]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate case ids: {sorted(dupes)}")
    return cases


def load_dir(directory: Path) -> list[Case]:
    return load(sorted(directory.glob("*.yaml")))
