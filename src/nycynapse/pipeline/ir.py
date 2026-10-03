"""The semantic query plan: what to compute, in the semantic layer's own terms.

Fields are written model.dimension, optionally with a role when a model is reached in more
than one way: taxi_zones@dropoff.borough. Time grains are written time:day, time:week,
time:month, time:year, time:hour, time:hour_of_day, time:day_of_week.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Filter(Strict):
    field: str
    op: Literal[
        "=",
        "!=",
        "in",
        "not in",
        "like",
        ">",
        ">=",
        "<",
        "<=",
        "is true",
        "is false",
        "is null",
        "is not null",
    ]
    values: list[str | float | int | bool] = Field(default_factory=list)


class PlaceFilter(Strict):
    place: str
    level: Literal["taxi_zone", "neighborhood", "subway_station", "bike_station", "borough"]
    # Field the place's codes apply to, for example trips_by_zone_hour.pickup_zone_id
    field: str


class Order(Strict):
    by: str  # a metric id, a field or a time grain
    desc: bool = True


class Plan(Strict):
    kind: Literal["aggregate", "list"] = "aggregate"
    model: str
    metrics: list[str] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)
    places: list[PlaceFilter] = Field(default_factory=list)
    time_dimension: str | None = None
    use_time_window: bool = True
    day_part: str | None = None
    select: list[str] = Field(default_factory=list)  # list queries only
    order: list[Order] = Field(default_factory=list)
    limit: int | None = None
    having_min: dict[str, float] = Field(default_factory=dict)  # metric id -> minimum value
