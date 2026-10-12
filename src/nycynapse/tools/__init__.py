"""Tools the guide agent can use besides the lake pipeline.

Each tool returns a ToolResult: a short text summary for the model, structured data for the
page, the sources it used, and map directives (areas to shade, points, lines, where to look).
The same functions are served over MCP by `nycynapse.mcp_tools`, so any MCP client can use them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

SUMMARY_LIMIT = 4000


@dataclass
class ToolResult:
    summary: str
    data: Any = None
    sources: list[dict] = field(default_factory=list)
    map: list[dict] = field(default_factory=list)
    ok: bool = True
    error: str | None = None
    cost_usd: float = 0.0

    def as_dict(self) -> dict:
        d = asdict(self)
        d["summary"] = d["summary"][:SUMMARY_LIMIT]
        return d


def failed(message: str) -> ToolResult:
    return ToolResult(summary=f"Tool failed: {message}", ok=False, error=message)


@dataclass
class Tool:
    name: str
    kind: str  # lake, live or web: the order the agent should reach for them
    description: str
    params: dict  # JSON schema of the arguments
    fn: Callable[..., ToolResult]
    enabled: Callable[[], bool] = lambda: True

    def spec(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "description": self.description,
            "params": self.params,
        }

    def __call__(self, **kwargs) -> ToolResult:
        try:
            return self.fn(**kwargs)
        except TypeError as e:
            return failed(f"bad arguments: {e}")
        except Exception as e:  # noqa: BLE001 - a tool error is reported to the agent
            return failed(f"{type(e).__name__}: {str(e)[:300]}")
