"""The semantic layer's object model.

Everything the query pipeline is allowed to know about the lake is declared here as data:
which tables exist and what they mean, how they join, which metrics are governed and how
they may be aggregated. The YAML in semantic/ is parsed into these classes and checked
against the gold tables dbt actually built.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Ident = str


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


GeoLevel = Literal[
    "borough",
    "neighborhood",
    "community_district",
    "zip_area",
    "council_district",
    "police_precinct",
    "taxi_zone",
    "subway_station",
    "subway_complex",
    "bike_station",
    "traffic_link",
]


class TimeDimension(Strict):
    """An event time on a model. Filters go on `instant`; grouping uses the local columns."""

    name: Ident
    instant: str
    local_date: str | None = None
    local_hour: str | None = None
    default: bool = False
    description: str | None = None


class Entity(Strict):
    """A key. Relationships join models through entities."""

    name: Ident
    column: str
    kind: Literal["primary", "foreign"] = "foreign"


class Dimension(Strict):
    name: Ident
    column: str | None = None
    expr: str | None = None
    kind: Literal["categorical", "boolean", "numeric", "identifier", "text", "geo", "list"] = (
        "categorical"
    )
    label: str | None = None
    description: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    # How values are grounded: enumerate keeps every distinct value in the catalog, index puts
    # them in a searchable value index, none means free text or numbers.
    values: Literal["enumerate", "index", "none"] = "none"
    geo_level: GeoLevel | None = None

    @property
    def sql(self) -> str:
        return self.expr or self.column or self.name


class Measure(Strict):
    name: Ident
    expr: str
    agg: Literal["sum", "count", "count_distinct", "avg", "min", "max", "median"]
    description: str | None = None


class NamedFilter(Strict):
    name: Ident
    sql: str
    description: str
    default: bool = False


class Partition(Strict):
    column: str
    granularity: Literal["month", "day", "year"]
    time_dimension: Ident


class SemanticModel(Strict):
    name: Ident
    table: str
    workspace: Ident
    label: str
    description: str | None = None
    grain: str
    synonyms: list[str] = Field(default_factory=list)
    time: list[TimeDimension] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    dimensions: list[Dimension] = Field(default_factory=list)
    measures: list[Measure] = Field(default_factory=list)
    filters: list[NamedFilter] = Field(default_factory=list)
    partition: Partition | None = None
    live: bool = False
    # Lake tables this model is built from, used to check a question's time window against
    # what the data actually covers.
    coverage: list[str] = Field(default_factory=list)
    # Status tables that keep a row per change (a bike station's status). A question about
    # "right now" reads the latest row per these keys instead of every change.
    latest_by: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_names(self):
        names = [d.name for d in self.dimensions] + [m.name for m in self.measures]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"{self.name}: duplicate names {sorted(dupes)}")
        if self.time and sum(t.default for t in self.time) != 1:
            raise ValueError(f"{self.name}: exactly one time dimension must be the default")
        return self

    def dimension(self, name: str) -> Dimension:
        for d in self.dimensions:
            if d.name == name:
                return d
        raise KeyError(f"{self.name} has no dimension {name}")

    def default_time(self) -> TimeDimension | None:
        return next((t for t in self.time if t.default), None)


class MetricSource(Strict):
    """One way to compute a metric. Listed in order of preference."""

    model: Ident
    expression: str
    filters: list[Ident] = Field(default_factory=list)
    # Dimensions this source can be grouped or filtered by. Empty means all of the model's.
    supports: list[Ident] = Field(default_factory=list)


class Metric(Strict):
    id: Ident
    label: str
    description: str
    unit: str
    format: Literal[
        "count",
        "number",
        "currency",
        "percent",
        "minutes",
        "seconds",
        "hours",
        "mph",
        "fahrenheit",
        "inches",
        "miles",
    ]
    additivity: Literal["additive", "semi_additive", "non_additive"]
    # For semi-additive metrics: dimensions that must not be summed over, and how to roll up
    # across them instead.
    non_additive_over: list[Ident] = Field(default_factory=list)
    rollup: Literal["avg", "max", "min", "last"] | None = None
    sources: list[MetricSource]
    synonyms: list[str] = Field(default_factory=list)
    bounds: tuple[float | None, float | None] = (None, None)
    higher_is: Literal["better", "worse", "neutral"] = "neutral"

    @model_validator(mode="after")
    def _semi_additive_needs_rollup(self):
        if self.additivity == "semi_additive" and not (self.non_additive_over and self.rollup):
            raise ValueError(f"{self.id}: semi_additive needs non_additive_over and rollup")
        return self


class Relationship(Strict):
    """How two models join. `condition` uses {left} and {right} for the two table aliases.

    many_to_one and one_to_one are plain joins. range relationships (an event during an
    alert) can match several rows, so the planner turns them into EXISTS filters instead of
    joins, which keeps counts from being multiplied.
    """

    id: Ident
    left: Ident
    right: Ident
    kind: Literal["many_to_one", "one_to_one", "temporal", "range"]
    condition: str
    role: Ident | None = None
    description: str
    verified: bool = True


class Workspace(Strict):
    id: Ident
    label: str
    description: str
    models: list[Ident]
    shared: list[Ident] = Field(default_factory=list)
    sample_questions: list[str] = Field(default_factory=list)


class NamedPeriod(Strict):
    name: Ident
    phrases: list[str]
    description: str
    # SQL over the local date and hour columns, written with {date} and {hour}.
    where: str


class Place(Strict):
    name: str
    aliases: list[str] = Field(default_factory=list)
    kind: Literal["landmark", "venue", "airport", "transit_hub", "park", "campus", "area"]
    latitude: float
    longitude: float
    radius_m: int = 500


class Instruction(Strict):
    id: Ident
    applies_to: list[Ident] = Field(default_factory=list)  # workspaces or models, empty = all
    text: str


class Catalog(Strict):
    version: str
    workspaces: list[Workspace]
    models: list[SemanticModel]
    metrics: list[Metric]
    relationships: list[Relationship]
    periods: list[NamedPeriod] = Field(default_factory=list)
    places: list[Place] = Field(default_factory=list)
    instructions: list[Instruction] = Field(default_factory=list)

    def model(self, name: str) -> SemanticModel:
        for m in self.models:
            if m.name == name:
                return m
        raise KeyError(f"no semantic model {name}")

    def metric(self, metric_id: str) -> Metric:
        for m in self.metrics:
            if m.id == metric_id:
                return m
        raise KeyError(f"no metric {metric_id}")
