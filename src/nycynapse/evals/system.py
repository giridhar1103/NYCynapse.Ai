"""What every system under evaluation returns for a question."""

from dataclasses import dataclass, field


@dataclass
class Answer:
    classification: str  # answerable, unclear, unsupported, non_data, refuse
    sql: str | None = None
    explanation: str = ""
    workspaces: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    values: list[str] = field(default_factory=list)
    places: list[str] = field(default_factory=list)
    stages: dict = field(default_factory=dict)  # per-stage detail for traces and scoring
    model_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    error: str | None = None
