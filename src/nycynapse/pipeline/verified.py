"""Verified question and plan pairs, used as worked examples for the planner.

Only humans add to this file. Generated plans are never promoted automatically. At query time
the closest verified pairs (by meaning) are shown to the planner, except any that are near
copies of the question itself, which keeps evaluation honest.
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from .ir import Plan
from .timeparse import TimeSpec


class VerifiedQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    question: str
    window: TimeSpec | None = None
    plan: Plan


def load(root: Path) -> list[VerifiedQuery]:
    out = []
    for path in sorted((root / "verified").glob("*.yaml")):
        for raw in (yaml.safe_load(path.read_text()) or {}).get("verified", []):
            out.append(VerifiedQuery(**raw))
    return out
