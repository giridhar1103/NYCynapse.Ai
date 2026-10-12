"""The guide agent: a small, budgeted tool-using loop that works with any model provider.

Each step the model returns JSON: a status line for the reader and either up to three tool
calls (run in parallel) or the final answer. Tool results, errors included, go back to the
model on the next step. Limits keep a turn cheap and bounded: a few steps, a few calls, at most
two web searches and two page reads, and a time limit after which the agent must answer with
what it has.

Models: the "agent" role answers; when it is out of allowance or failing, the "agent_fallback"
role takes over for that turn (and the main one rests for a while). The lake pipeline inside a
turn uses the same model as the agent.
"""

from __future__ import annotations

import contextvars
import json
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ..llm import client as llm
from ..llm.client import LLMError, ProviderLimit
from ..prompts import prompt as load_prompt
from ..tools import Tool, ToolResult, failed
from .host import MCPHost
from .lake import Lake, now_as_of
from .lake import tools as lake_tools

MAX_ROUNDS = 4
MAX_CALLS = 7
MAX_PER_TOOL = {"web_search": 2, "read_page": 2, "ask_lake": 3}
TURN_SECONDS = 150
REST_S = 20 * 60  # how long a main model that hit its limit is left alone

STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "calls": {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string"},
                    "args": {"type": "string", "description": "the arguments as a JSON object"},
                },
                "required": ["tool", "args"],
                "additionalProperties": False,
            },
        },
        "answer": {"type": ["string", "null"]},
        "follow_ups": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "declined": {"type": "boolean"},
    },
    "required": ["status", "calls", "answer", "follow_ups", "declined"],
    "additionalProperties": False,
}

BUSY = (
    "Our models are at full capacity right now. Please wait a minute and try again; "
    "nothing you asked was lost."
)


@dataclass
class Turn:
    question: str
    history: list[dict]
    emit: Callable[[str, dict], None]
    started: float = field(default_factory=time.monotonic)
    results: list[dict] = field(default_factory=list)  # {id, tool, args, result}
    steps: list[dict] = field(default_factory=list)
    cost_usd: float = 0.0
    model_calls: int = 0
    model: str | None = None
    fell_back: bool = False


class Agent:
    def __init__(self, lake: Lake, host: MCPHost):
        self.lake = lake
        self.host = host
        self._rest_until: dict[str, float] = {}
        self._rest_lock = threading.Lock()
        self._emit_local = threading.local()
        self.lake_tools = {
            t.name: t
            for t in lake_tools(lake, now_as_of, lambda: getattr(self._emit_local, "fn", None))
        }

    # models

    def models(self) -> list[str]:
        cfg = llm.config()
        roles = cfg["roles"]
        main = roles.get("agent") or roles["default"]
        backup = roles.get("agent_fallback")
        order = [main] + ([backup] if backup and backup != main else [])
        now = time.time()
        with self._rest_lock:
            resting = [m for m in order if self._rest_until.get(m, 0) > now]
        return [m for m in order if m not in resting] + resting

    def _rest(self, pid: str) -> None:
        with self._rest_lock:
            self._rest_until[pid] = time.time() + REST_S

    def available(self) -> bool:
        now = time.time()
        with self._rest_lock:
            return any(self._rest_until.get(m, 0) <= now for m in self.models())

    # tools

    def tools(self) -> list[Tool]:
        return list(self.lake_tools.values()) + self.host.specs()

    def _tool_text(self) -> str:
        out = []
        for t in self.tools():
            props = t.params.get("properties", {})
            args = ", ".join(
                f"{k}: {v.get('type', 'any')}" + (f" one of {v['enum']}" if "enum" in v else "")
                for k, v in props.items()
            )
            out.append(f"- {t.name} [{t.kind}] ({args}): {t.description}")
        return "\n".join(out)

    def _coverage_text(self) -> str:
        try:
            ctx = self.lake.fork()
            cov = ctx.coverage
            ctx.lake.close()
        except Exception:  # noqa: BLE001
            return "(lake coverage unavailable)"
        return "\n".join(f"- {k}: {v}" for k, v in cov.items())

    # the loop

    def run(self, question: str, history: list[dict], emit: Callable[[str, dict], None]) -> dict:
        t = Turn(question=question, history=history, emit=emit)
        system = load_prompt("agent")
        static = (
            f"Now: {now_as_of()} (New York).\n\nTools:\n{self._tool_text()}\n\n"
            f"What the lake covers (first to latest data):\n{self._coverage_text()}"
        )
        force = False
        for round_no in range(MAX_ROUNDS + 1):
            elapsed = time.monotonic() - t.started
            if round_no == MAX_ROUNDS or elapsed > TURN_SECONDS - 30 or self._calls(t) >= MAX_CALLS:
                force = True
            step = self._step(t, system, static, force)
            if step is None:
                return self._finish(t, None, [], declined=False, error=BUSY)
            status = (step.get("status") or "").strip()[:120]
            if status:
                emit("status", {"text": status})
            calls = [] if force else self._valid_calls(t, step.get("calls") or [])
            answer = (step.get("answer") or "").strip()
            if answer and not calls:
                return self._finish(
                    t, answer, step.get("follow_ups") or [], declined=bool(step.get("declined"))
                )
            if not calls:
                if force:
                    return self._finish(
                        t,
                        answer
                        or "I could not put together an answer to that. Try asking "
                        "about one thing at a time.",
                        [],
                        declined=False,
                    )
                force = True
                continue
            t.steps.append({"status": status, "calls": [c["tool"] for c in calls]})
            self._run_calls(t, calls)
        return self._finish(t, None, [], declined=False, error=BUSY)

    def _calls(self, t: Turn) -> int:
        return len(t.results)

    def _valid_calls(self, t: Turn, calls: list[dict]) -> list[dict]:
        names = {x.name for x in self.tools()}
        used: dict[str, int] = {}
        for r in t.results:
            used[r["tool"]] = used.get(r["tool"], 0) + 1
        seen_before = {(r["tool"], json.dumps(r["args"], sort_keys=True)) for r in t.results}
        out = []
        for c in calls[:3]:
            name = str(c.get("tool", "")).strip()
            try:
                args = json.loads(c.get("args") or "{}")
                if not isinstance(args, dict):
                    raise ValueError
            except (ValueError, TypeError):
                args = {}
            if name not in names:
                t.results.append(
                    {
                        "id": uuid.uuid4().hex[:8],
                        "tool": name,
                        "args": args,
                        "result": failed(f"there is no tool called {name}"),
                    }
                )
                continue
            if used.get(name, 0) >= MAX_PER_TOOL.get(name, 4):
                t.results.append(
                    {
                        "id": uuid.uuid4().hex[:8],
                        "tool": name,
                        "args": args,
                        "result": failed(
                            f"{name} was already used as often as allowed in one answer"
                        ),
                    }
                )
                continue
            if (name, json.dumps(args, sort_keys=True)) in seen_before:
                continue
            if len(t.results) + len(out) >= MAX_CALLS:
                break
            used[name] = used.get(name, 0) + 1
            out.append({"id": uuid.uuid4().hex[:8], "tool": name, "args": args})
        return out

    def _run_calls(self, t: Turn, calls: list[dict]) -> None:
        kinds = {x.name: x.kind for x in self.tools()}
        model = t.model

        def one(c: dict) -> dict:
            token = llm.override(model)
            started = time.monotonic()
            t.emit(
                "tool",
                {
                    "id": c["id"],
                    "name": c["tool"],
                    "kind": kinds.get(c["tool"]),
                    "args": _public_args(c["args"]),
                    "state": "start",
                },
            )
            if c["tool"] in self.lake_tools:
                self._emit_local.fn = lambda s, cid=c["id"]: t.emit("lake", {"id": cid, **s})
                try:
                    res = self.lake_tools[c["tool"]](**c["args"])
                finally:
                    self._emit_local.fn = None
                via = "lake"
            else:
                res, via = self.host.call(c["tool"], c["args"])
            llm._override.reset(token)
            ms = int((time.monotonic() - started) * 1000)
            t.emit(
                "tool",
                {
                    "id": c["id"],
                    "name": c["tool"],
                    "kind": kinds.get(c["tool"]),
                    "state": "done" if res.ok else "error",
                    "ms": ms,
                    "via": via,
                    "error": res.error,
                    "sources": res.sources[:6],
                    "note": _short(res.summary) if res.ok else None,
                },
            )
            if res.ok and res.map:
                t.emit("map", {"id": c["id"], "layers": res.map})
            return {**c, "result": res, "ms": ms}

        with ThreadPoolExecutor(max_workers=len(calls)) as pool:
            futures = [pool.submit(contextvars.copy_context().run, one, c) for c in calls]
            for f in futures:
                r = f.result()
                t.cost_usd += r["result"].cost_usd or 0
                calls_made = (
                    (r["result"].data or {}).get("model_calls")
                    if isinstance(r["result"].data, dict)
                    else None
                )
                t.model_calls += calls_made or 0
                t.results.append(r)

    def _prompt(self, t: Turn, static: str, force: bool) -> str:
        parts = [static]
        if t.history:
            parts.append(
                "Earlier in this conversation (oldest first):\n"
                + "\n\n".join(
                    f"Q{i + 1}: {h['q']}\nA{i + 1}: {h['answer']}"
                    + (f"\nFacts found: {h['facts']}" if h.get("facts") else "")
                    for i, h in enumerate(t.history)
                )
            )
        parts.append(f"Reader's question: {t.question}")
        if t.results:
            parts.append(
                "Tool results so far:\n"
                + "\n\n".join(
                    f"[{r['tool']} {json.dumps(r['args'])}] "
                    + ("OK" if r["result"].ok else "FAILED")
                    + f"\n{r['result'].summary}"
                    for r in t.results
                )
            )
        left = MAX_CALLS - len(t.results)
        if force:
            parts.append("Answer now with what you have. Do not call any more tools.")
        else:
            parts.append(
                f"Budget left: {left} tool calls, "
                f"{max(0, int(TURN_SECONDS - (time.monotonic() - t.started)))} seconds."
            )
        return "\n\n".join(parts)

    def _step(self, t: Turn, system: str, static: str, force: bool) -> dict | None:
        text = self._prompt(t, static, force)
        for pid in [t.model] if t.model else self.models():
            try:
                r = llm.complete_with(pid, system, text, schema=STEP_SCHEMA, retries=1)
                step = r.json()
            except ProviderLimit:
                self._rest(pid)
                continue
            except (LLMError, ValueError) as e:
                t.steps.append({"error": f"{pid}: {str(e)[:200]}"})
                if t.model:  # already committed to a model this turn: try the other one
                    others = [m for m in self.models() if m != pid]
                    if others:
                        t.model, t.fell_back = others[0], True
                        return self._step(t, system, static, force)
                continue
            if t.model is None:
                roles = llm.config()["roles"]
                t.model = pid
                t.fell_back = pid != (roles.get("agent") or roles["default"])
            t.cost_usd += r.cost_usd or 0
            t.model_calls += 1
            return step
        return None

    def _finish(
        self,
        t: Turn,
        answer: str | None,
        follow_ups: list[str],
        *,
        declined: bool,
        error: str | None = None,
    ) -> dict:
        sources, seen = [], set()
        for r in t.results:
            res: ToolResult = r["result"]
            if not res.ok:
                continue
            for s in res.sources:
                key = s.get("url") or s.get("title")
                if key and key not in seen:
                    seen.add(key)
                    sources.append(s)
        facts = "; ".join(_short(r["result"].summary, 300) for r in t.results if r["result"].ok)[
            :1800
        ]
        return {
            "answer": answer,
            "follow_ups": [f.strip() for f in follow_ups if f.strip()][:3],
            "declined": declined,
            "error": error,
            "sources": sources,
            "map": [layer for r in t.results if r["result"].ok for layer in r["result"].map],
            "steps": t.steps
            + [
                {
                    "tool": r["tool"],
                    "args": _public_args(r["args"]),
                    "ok": r["result"].ok,
                    "error": r["result"].error,
                    "ms": r.get("ms"),
                }
                for r in t.results
            ],
            "model": t.model,
            "fell_back": t.fell_back,
            "cost_usd": round(t.cost_usd, 4),
            "model_calls": t.model_calls,
            "latency_ms": int((time.monotonic() - t.started) * 1000),
            "memory": {"q": t.question, "answer": (answer or "")[:900], "facts": facts},
        }


def _short(text: str, n: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _public_args(args: dict) -> dict:
    return {
        k: (str(v)[:120] if not isinstance(v, int | float | bool) else v)
        for k, v in (args or {}).items()
    }
