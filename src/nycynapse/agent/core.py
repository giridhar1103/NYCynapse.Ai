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
from datetime import datetime
import re
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
from . import places as place_index
from .host import MCPHost
from .lake import Lake, now_as_of
from .lake import tools as lake_tools

UNFINISHED = "the draft leaves part of the question unanswered"
MAX_ROUNDS = 5
MAX_CALLS = 9
MAX_PER_TOOL = {"web_search": 3, "read_page": 3, "ask_lake": 3}
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
        "places": {
            "type": "array",
            "maxItems": 6,
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "note": {"type": "string"}},
                "required": ["name", "note"],
                "additionalProperties": False,
            },
        },
        "declined": {"type": "boolean"},
        "remember": {
            "type": "string",
            "description": "with an answer: what the reader has told you that later turns must "
            "keep (start, destination, day and time, deadline, mobility, luggage, budget, who "
            "is with them), in one short line; empty if nothing",
        },
    },
    "required": ["status", "calls", "answer", "follow_ups", "places", "declined", "remember"],
    "additionalProperties": False,
}

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "problems": {
            "type": "array",
            "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {"quote": {"type": "string"}, "why": {"type": "string"}},
                "required": ["quote", "why"],
                "additionalProperties": False,
            },
        },
        "unfinished": {
            "type": "array",
            "maxItems": 2,
            "items": {"type": "string"},
        },
    },
    "required": ["problems", "unfinished"],
    "additionalProperties": False,
}

_CLAIMS_FAILURE = re.compile(
    r"no such tool|tools? (?:is |are )?(?:not available|unavailable)|(?:tool|request|call) "
    r"(?:failed|error)|could(?:n't| not) (?:run|reach|call|use)|didn't run",
    re.I,
)

# Drafts that read like notes instead of an answer: thinking out loud, talking about the data
# rather than the city, or announcing the time. Each is sent back once to be rewritten.
_DRAFT_SMELLS = [
    (
        re.compile(
            # "Correction: ..." about an earlier answer is allowed; a mid-answer change of mind is not
            r"\.\.\.\s*(?:correct|actually|wait)\b|\b(?:scratch that|wait,|I mean,)",
            re.I,
        ),
        "it corrects itself mid-answer; write only the final, correct version",
    ),
    (
        re.compile(
            r"\b(?:is|are|was|were|isn't|aren't|not|nothing|none|no \w+) (?:listed|reported)\b"
            r"|\bfeed(?:'s)?\b|\breadings?\b|\bdataset\b|\bthe data (?:shows|says)\b"
            r"|\baccording to\b|\bpublished (?:text|wording)\b|\bI (?:found|have no|can't tell)\b"
            r"|\b(?:trip )?planner\b|\bavailable to me\b|\bmy (?:tools?|data|numbers|search)\b"
            r"|\bI (?:searched|pulled|ran|queried)\b|\b(?:tool|search) (?:returned|results?)\b"
            r"|\bwouldn't load\b",
            re.I,
        ),
        "it talks about the data or what was checked; describe the city itself "
        '("no closures are posted tonight", not "no closure listed")',
    ),
    (
        re.compile(
            r"\b(?:it's|it is) (?:about |now |already )?\d{1,2}(?::\d\d)? ?[AP]M\b"
            r"|\b\d{1,2}(?::\d\d)? ?[AP]M now\b",
            re.I,
        ),
        "it announces the current time; leave the clock out",
    ),
]


MAX_WORDS = 150


def _embedded_calls(answer: str) -> list[dict] | None:
    """Tool calls written as JSON into the answer text ('{"status": ..., "calls": [...]}',
    maybe in a code fence). None when the answer is ordinary prose."""
    text = llm.strip_thought((answer or "").strip())
    # Step fields written out as text ("declined: false\ncalls: [...]"): never an answer.
    if re.match(r"\s*(?:status|calls|declined|answer)\s*:", text) and re.search(
        r"^\s*(?:calls|declined)\s*:", text, re.M
    ):
        return []
    if not text.startswith(("{", "```")):
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    try:
        obj = json.loads(text)
    except ValueError:
        # Cut off mid-way: still JSON, never an answer.
        return [] if '"calls"' in text or '"status"' in text else None
    if not isinstance(obj, dict) or "calls" not in obj:
        return [] if isinstance(obj, dict) and "status" in obj else None
    out = []
    for c in obj.get("calls") or []:
        if isinstance(c, dict) and c.get("tool"):
            args = c.get("args") or {}
            out.append(
                {"tool": c["tool"], "args": args if isinstance(args, str) else json.dumps(args)}
            )
    return out


def _draft_issues(answer: str) -> list[str]:
    text = _uncite(answer)
    issues = [why for pattern, why in _DRAFT_SMELLS if pattern.search(text)]
    if len(text.split()) > MAX_WORDS:
        issues.append(
            f"it runs past {MAX_WORDS} words; keep only what decides the reader's plan, "
            "best option first, and drop side notes"
        )
    return issues


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
    retried: bool = False
    unchecked: bool = False
    look: list[str] = field(default_factory=list)  # checks the verifier says are still missing
    verified: int = 0  # fact checks run on drafts
    redrafts: int = 0  # rewrites asked for
    had_facts: bool = False  # the last rewrite was for factual problems
    switched: bool = False  # moved to the other model after a failure (only once per turn)
    embedded_ok: bool = False
    redraft: str | None = None


class Agent:
    verify_answers = True

    def __init__(self, lake: Lake, host: MCPHost):
        self.lake = lake
        self.host = host
        self._rest_until: dict[str, float] = {}
        self._rest_lock = threading.RLock()
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
        """At least one of the two models is not resting after hitting its limit."""
        now = time.time()
        order = self.models()
        with self._rest_lock:
            return any(self._rest_until.get(m, 0) <= now for m in order)

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
        last: tuple[str, dict] | None = None  # the latest draft, in case the rounds run out
        for round_no in range(MAX_ROUNDS + 1):
            elapsed = time.monotonic() - t.started
            if round_no == MAX_ROUNDS or elapsed > TURN_SECONDS - 30 or self._calls(t) >= MAX_CALLS:
                force = True
            step = self._step(t, system, static, force)
            if step is None:
                if last:  # a rewrite failed or timed out: the draft in hand beats an error
                    answer, step = last
                    t.steps.append({"note": "rewrite failed; answered with the last draft"})
                    return self._finish(
                        t, answer, step.get("follow_ups") or [], declined=False,
                        places=step.get("places") or [], remember=step.get("remember"),
                    )
                return self._finish(t, None, [], declined=False, error=BUSY)
            status = (step.get("status") or "").strip()[:120]
            if status:
                emit("status", {"text": status})
            answer = (step.get("answer") or "").strip()
            embedded = _embedded_calls(answer)
            if embedded is not None:
                # The model wrote its tool calls into the answer instead of the calls list. Run
                # them as calls (once, even when told to answer) and never show that text.
                answer = ""
                if not step.get("calls"):
                    step["calls"] = embedded
                    if force and not t.embedded_ok and self._calls(t) < MAX_CALLS:
                        force = False
                        t.embedded_ok = True
            calls = [] if force else self._valid_calls(t, step.get("calls") or [])
            if (
                answer
                and not calls
                and not t.results
                and not step.get("declined")
                and _CLAIMS_FAILURE.search(answer)
                and not t.retried
            ):
                # The model tried a native tool call, which this setup refuses, and is
                # reporting that as a failure. Ask again, once, with a reminder.
                t.retried = True
                t.steps.append({"note": "retried a step that claimed a tool failure"})
                continue
            if (
                answer
                and not calls
                and not t.results
                and not step.get("declined")
                and not t.unchecked
                and not force
                and len(t.question) > 20
            ):
                # An answer about the city written without checking anything is a guess, and
                # the fallback model does this most. Ask once for the checks it needs.
                t.unchecked = True
                t.steps.append({"note": "asked to check before answering"})
                continue
            style = [] if step.get("declined") else _draft_issues(answer)
            loose: list[str] = []
            factual: list[str] = []
            found: list[str] = []
            if answer and not calls and not step.get("declined"):
                loose = self._untraced(t, answer)
                if loose:
                    factual.append(
                        "these figures are not in any tool result: "
                        + ", ".join(loose)
                        + ". Use the figures the tools gave, or leave them out; never estimate "
                        "times, distances, prices or counts yourself"
                    )
            # Every finished draft gets the fact check, whatever its wording; a rewrite made
            # for factual problems is checked once more.
            if (
                answer
                and not calls
                and t.results
                and self.verify_answers
                and not step.get("declined")
                and (t.verified == 0 or (t.verified == 1 and t.redrafts == 1 and t.had_facts))
                and round_no < MAX_ROUNDS
                and time.monotonic() - t.started < TURN_SECONDS - 60
            ):
                t.verified += 1
                found = self._verify(t, answer)
                if found == [UNFINISHED] and (
                    self._calls(t) >= MAX_CALLS or round_no >= MAX_ROUNDS - 1
                ):
                    found = []  # no room left to look: a rewrite alone would not finish it
                factual += found
            issues = style + factual
            if answer and not calls:
                last = (answer, step)
            # A second rewrite only for what the fact check still finds wrong; figures no tool
            # gave are dropped after the first instead.
            again = t.redrafts == 1 and found and found != [UNFINISHED]
            if answer and not calls and issues and (t.redrafts == 0 or again) and round_no < MAX_ROUNDS:
                t.had_facts = bool(found)
                t.redrafts += 1
                # Made-up figures may need a tool (trip_plan for a travel time), so that rewrite
                # may still call one; a rewrite for wording alone answers straight away.
                can_look = (
                    bool(loose or t.look)
                    and self._calls(t) < MAX_CALLS
                    and round_no < MAX_ROUNDS - 1
                    and time.monotonic() - t.started < TURN_SECONDS - 60
                )
                t.redraft = (
                    f"Your draft answer was:\n{answer}\n\nRewrite it as the final answer, "
                    "keeping what is right with its citations and places, and correcting or "
                    "leaving out what the problems name, because "
                    + "; and ".join(issues)
                    + "."
                    + (
                        " If a tool can supply what is missing (trip_plan for routes and travel "
                        "times), call it first."
                        if can_look
                        else ""
                    )
                    + (
                        " Still unanswered: " + "; ".join(t.look) + ". Make those checks now "
                        "(read_page on an official page you already found counts) instead of "
                        "telling the reader to check."
                        if can_look and t.look
                        else ""
                    )
                )
                t.steps.append({"note": "redrafted: " + "; ".join(issues)})
                force = not can_look
                continue
            if answer and not calls:
                return self._finish(
                    t,
                    answer,
                    step.get("follow_ups") or [],
                    declined=bool(step.get("declined")),
                    places=step.get("places") or [],
                    remember=step.get("remember"),
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
                        places=step.get("places") or [],
                    )
                force = True
                continue
            t.steps.append({"status": status, "calls": [c["tool"] for c in calls]})
            self._run_calls(t, calls)
        if last:  # out of rounds with a draft in hand: that draft, not an error
            answer, step = last
            return self._finish(
                t, answer, step.get("follow_ups") or [], declined=False,
                places=step.get("places") or [], remember=step.get("remember"),
            )
        return self._finish(t, None, [], declined=False, error=BUSY)

    def _calls(self, t: Turn) -> int:
        return len(t.results)

    def _verify(self, t: Turn, answer: str) -> list[str]:
        """A second look at a finished draft: claims the tool results do not support, times
        that do not fit together, places or details stated without a source. Problems found
        go into the one rewrite; a failed check never blocks the answer."""
        numbers = {key: n for n, key, _ in _registry(t.results)}
        prompt = (
            f"Now: {now_as_of()} (New York).\n\nReader's question: {t.question}\n\n"
            + (
                "Earlier in the conversation:\n"
                + "\n".join(
                    f"Q: {h['q']}\nA: {_uncite(h['answer'])}"
                    + (f"\nFacts found then: {h['facts']}" if h.get("facts") else "")
                    for h in t.history
                )
                + "\n\n"
                if t.history
                else ""
            )
            + (
                f"What the reader has told you: {_reader(t.history)}\n\n"
                if _reader(t.history)
                else ""
            )
            + "Tool results:\n\n"
            + "\n\n".join(_result_text(r, numbers) for r in t.results)
            + f"\n\nDraft answer:\n{answer}"
        )
        try:
            r = llm.complete_with(
                t.model or self.models()[0],
                load_prompt("verify"),
                prompt,
                schema=VERIFY_SCHEMA,
                retries=0,
                timeout=60,
            )
            found = r.json().get("problems") or []
            t.look = [str(x)[:200] for x in (r.json().get("unfinished") or [])[:2]]
            t.cost_usd += r.cost_usd or 0
            t.model_calls += 1
        except Exception:  # noqa: BLE001 - the check is a safety net, not a gate
            return []
        out = [f'"{p["quote"][:120]}": {p["why"][:200]}' for p in found[:4] if p.get("quote")]
        if out:
            t.steps.append({"note": "verify: " + " | ".join(out)[:600]})
            return ["a check against the tool results found problems: " + "; ".join(out)]
        if t.look:
            t.steps.append({"note": "verify, unfinished: " + " | ".join(t.look)})
            return [UNFINISHED]
        return []

    def _untraced(self, t: Turn, answer: str) -> list[str]:
        # Evidence is what tools returned, now or in earlier turns, and what the reader wrote;
        # never the guide's own earlier answers, or a made-up figure would vouch for itself.
        memory = (
            " ".join(f"{h.get('q', '')} {h.get('facts', '')}" for h in t.history)
            + " "
            + t.question
        )
        results = [r["result"] for r in t.results]
        return (_untraced_numbers(_uncite(answer), results, memory)
                + _untraced_clocks(_uncite(answer), results, memory))[:8]

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
                    f"Q{i + 1}: {h['q']}\nA{i + 1}: {_uncite(h['answer'])}"
                    + (f"\nFacts found: {h['facts']}" if h.get("facts") else "")
                    for i, h in enumerate(t.history)
                )
            )
        if _reader(t.history):
            parts.append(
                "What the reader has told you about their plan (keep to it unless they change "
                f"it now): {_reader(t.history)}"
            )
        parts.append(f"Reader's question: {t.question}")
        if t.results:
            numbers = {key: n for n, key, _ in _registry(t.results)}
            parts.append(
                "Tool results so far. Cite each fact with the number of its source in square "
                "brackets, like [2].\n\n" + "\n\n".join(_result_text(r, numbers) for r in t.results)
            )
        left = MAX_CALLS - len(t.results)
        if t.retried and not t.results:
            parts.append(
                "Reminder: you cannot call tools natively. Put the sources you need "
                "in the `calls` array of your JSON reply and the system will run them."
            )
        if t.unchecked and not t.results:
            parts.append(
                "You answered without checking anything. Facts about routes, stations, times, "
                "hours, conditions and places must come from tools: call the ones this question "
                "needs now (trip_plan for getting anywhere). Answer straight away only if the "
                "earlier turns already hold every fact you need."
            )
        if t.redraft:
            parts.append(t.redraft)
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
                # Each call gets what is left of the turn (and a little more to finish), so
                # one slow call cannot hold the reader for five minutes.
                left = TURN_SECONDS + 45 - (time.monotonic() - t.started)
                r = llm.complete_with(
                    pid, system, text, schema=STEP_SCHEMA, retries=1, timeout=max(30, left)
                )
                step = r.json()
            except ProviderLimit:
                self._rest(pid)
                continue
            except (LLMError, ValueError) as e:
                t.steps.append({"error": f"{pid}: {str(e)[:200]}"})
                if t.model:  # already committed to a model this turn: try the other one, once
                    others = [m for m in self.models() if m != pid]
                    if others and not t.switched:
                        t.model, t.fell_back, t.switched = others[0], True, True
                        return self._step(t, system, static, force)
                    return None
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
        places: list[dict] | None = None,
        remember: str | None = None,
    ) -> dict:
        if answer and _embedded_calls(answer) is not None:
            answer = (
                "I couldn't finish checking that in time. Please ask again, or ask about one "
                "part at a time."
            )
        answer, sources = _cite(answer or "", t.results, declined) if answer else (answer, [])
        layers = [layer for r in t.results if r["result"].ok for layer in r["result"].map]
        trips = [x for x in layers if x.get("type") == "trip"]
        if trips:
            # A trip answer shows one trip, the last one planned (earlier attempts were
            # superseded), and nothing else that would compete with its route on the map.
            layers = [x for x in layers if x.get("type") not in ("trip", "routes", "points")]
            layers.append(trips[-1])
        # Highlights a tool drew while working (the top neighborhoods by rent, say) are not
        # the answer's places: the final map keeps only what the answer names.
        layers = [x for x in layers if x.get("type") != "highlight"]
        highlight = None
        has_trip = any(
            x.get("type") == "trip" for r in t.results if r["result"].ok for x in r["result"].map
        )
        if places and not declined and not has_trip:  # a trip draws its own start and end
            hints = [
                i
                for r in t.results
                if r["result"].ok
                for x in r["result"].map
                if x.get("type") == "highlight"
                for i in x.get("items", [])
            ]
            try:
                items = place_index.resolve(places, hints)
            except Exception:  # noqa: BLE001 - the answer stands without its map highlight
                items = []
            if items:
                highlight = {"type": "highlight", "label": "In this answer", "items": items}
                layers = [x for x in layers if x.get("type") != "highlight"] + [highlight]
                t.emit("map", {"id": "answer", "layers": [highlight]})
        kinds = {x.name: x.kind for x in self.tools()}
        # What the next turn remembers: facts from city data only. Web text is untrusted and
        # could carry instructions into later turns, so only page titles are kept from it.
        # Trip plans keep more, so a follow-up still has the departures it was given.
        facts = "; ".join(
            _short(r["result"].summary, 1200 if r["tool"] == "trip_plan" else 400)
            if kinds.get(r["tool"]) != "web"
            else "web: " + ", ".join(s.get("title", "") for s in r["result"].sources[:3])
            for r in t.results
            if r["result"].ok
        )[:3000]
        untraced = self._untraced(t, answer or "") if answer and not declined else []
        if untraced and t.redraft is not None:
            # Rewritten once and still carrying figures nothing supports: drop the sentences
            # or list lines that hold them rather than publish a made-up number.
            answer = _drop_untraced(answer, untraced)
            untraced = self._untraced(t, answer)
        return {
            "answer": answer,
            "follow_ups": [f.strip() for f in follow_ups if f.strip()][:3],
            "declined": declined,
            "error": error,
            "sources": sources,
            "map": layers,
            "places": highlight["items"] if highlight else [],
            "steps": t.steps
            + [
                {
                    "tool": r["tool"],
                    "kind": kinds.get(r["tool"]),
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
            "memory": {
                "q": t.question,
                "answer": _uncite(answer or "")[:900],
                "facts": facts,
                "follow_ups": [f.strip() for f in follow_ups if f.strip()][:3],
                # What the reader said about their plan, kept apart from the guide's own words.
                "reader": (remember or "").strip()[:400] or _reader(t.history),
            },
            "untraced": untraced,
            "trace": [
                {
                    "tool": r["tool"],
                    "args": r["args"],
                    "ok": r["result"].ok,
                    "summary": (r["result"].summary or "")[:6000],
                }
                for r in t.results
            ]
            + [{"note": s["note"]} for s in t.steps if "note" in s],
        }


_NUM = re.compile(
    r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(\s*(?:million|billion|thousand)\b)?", re.I
)
_SCALE = {"thousand": 1e3, "million": 1e6, "billion": 1e9}
_SMALL = {str(i) for i in range(0, 13)}
_CLOCK = re.compile(r"\d{1,2}:\d{2}|\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?")


# Numbers that are names, not figures: I-278, Route 9A, 14th St, 42 St, Pier 17, Terminal 4.
_NAMES = re.compile(
    r"\b[A-Z]{1,3}-\d+[A-Z]?\b|\b\d+(?:st|nd|rd|th)\b"
    r"|\b\d+ (?:St|Street|Av|Ave|Avenue|Pl|Rd|Dr|Blvd)\b"
    r"|\b(?:Route|Pier|Terminal|Exit|Exits|Gate|Platform|Track|Hall|PS|P\.S\.|Gates)\s+\d+\w*",
    re.I,
)


def _numbers(text: str) -> list[tuple[str, float, float]]:
    """(as written, value, how far a rounded figure may be off) for each number in a text."""
    out = []
    for m in _NUM.finditer(_CLOCK.sub(" ", text or "")):
        whole = m.group(1).replace(",", "")
        written = whole + (f".{m.group(2)}" if m.group(2) else "")
        scale = _SCALE.get((m.group(3) or "").strip().lower(), 1.0)
        value = float(written) * scale
        step = (10 ** -len(m.group(2)) if m.group(2) else 1.0) * scale
        out.append((written, value, step))
    return out


_AMPM = re.compile(r"\b(\d{1,2}):(\d{2})\s*([ap])(?:\.\s?m\.|m\b)", re.I)
_ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?")


def _minutes_of_day(h: int, m: int, ap: str) -> int:
    return (h % 12 + (12 if ap.lower() == "p" else 0)) * 60 + m


def _untraced_clocks(answer: str, results: list[ToolResult], memory: str = "") -> list[str]:
    """Clock times in the answer ("the 10:14 PM train") that no tool gave, within a minute.
    Tools write times as "10:14 PM" or as ISO timestamps, which are read in New York time
    ("2026-10-24T22:47:00Z" is 6:47 PM). Returns the times as written, so they can be dropped."""
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    texts = [memory] + [
        r.summary + " " + json.dumps(r.data, default=str)[:20000] for r in results if r.ok
    ]
    known: set[int] = set()
    for text in texts:
        for h, m, ap in _AMPM.findall(text):
            known.add(_minutes_of_day(int(h), int(m), ap))
        for stamp in _ISO.findall(text):
            try:
                t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                continue
            t = t.astimezone(ny) if t.tzinfo else t
            known.add(t.hour * 60 + t.minute)
    out = []
    for m in _AMPM.finditer(answer):
        x = _minutes_of_day(int(m.group(1)), int(m.group(2)), m.group(3))
        if not any(abs(x - k) <= 1 or abs(x - k) >= 1439 for k in known):
            out.append(m.group(0))
    return out


def _untraced_numbers(answer: str, results: list[ToolResult], memory: str = "") -> list[str]:
    """Numbers in the answer that cannot be traced to what the tools returned (or to earlier
    turns), allowing rounding, "4.1 million" style figures, totals, averages, differences and
    percent changes. Small counts, years and clock times are skipped."""
    values: list[float] = []
    texts = [memory]
    for r in results:
        if not r.ok:
            continue
        texts.append(r.summary + " " + json.dumps(r.data, default=str)[:20000])
        data = r.data if isinstance(r.data, dict) else {}
        rows = data.get("rows") or []
        for i in range(len(data.get("columns") or [])):
            col = [
                v
                for v in (row[i] for row in rows if i < len(row))
                if isinstance(v, int | float) and not isinstance(v, bool)
            ]
            if len(col) > 1:
                values += [float(sum(col)), float(sum(col)) / len(col)]  # totals and averages
    for t in texts:
        values += [v for _, v, _ in _numbers(t)]
    base = [v for v in values if v][:80]
    for x in base:
        for y in base:
            if x != y:
                values.append(abs(x - y))
                values.append(abs(x - y) / abs(y) * 100)
    out = []
    for written, x, step in _numbers(_NAMES.sub(" ", answer)):
        if written in _SMALL or (len(written) == 4 and written.startswith("20")):
            continue
        slack = step / 2 + 0.051
        if any(abs(x - v) <= slack + abs(v) * 0.01 for v in values):
            continue
        out.append(written)
    return out[:8]


def _source_key(s: dict) -> str:
    return (s.get("url") or s.get("title") or "").rstrip("/")


def _registry(results: list[dict]) -> list[tuple[int, str, dict]]:
    """Every source the turn's tools returned, numbered in the order they arrived."""
    out, seen = [], set()
    for r in results:
        res: ToolResult = r["result"]
        if not res.ok:
            continue
        for s in res.sources:
            key = _source_key(s)
            if key and key not in seen:
                seen.add(key)
                out.append((len(out) + 1, key, s))
    return out


def _reader(history: list[dict]) -> str:
    """The latest note of what the reader said about their plan, from earlier turns."""
    return next((h["reader"] for h in reversed(history) if h.get("reader")), "")


def _dated(x: dict) -> str:
    from ..tools.web import _dated as dated

    return dated(x)


def _result_text(r: dict, numbers: dict[str, int]) -> str:
    res: ToolResult = r["result"]
    head = f"[{r['tool']} {json.dumps(r['args'])}] " + ("OK" if res.ok else "FAILED")
    if not res.ok:
        return f"{head}\n{res.summary}"
    data = res.data if isinstance(res.data, dict) else {}
    if r["tool"] == "web_search" and data.get("results"):
        # Web results are numbered one by one so each can be cited on its own.
        lines = [
            f"[{numbers.get(_source_key({'url': x.get('url'), 'title': x.get('title')}), '?')}] "
            f"{x.get('title')} ({x.get('url')}){' ' + x['date'] if x.get('date') else ''}"
            f"{_dated(x)}\n    {(x.get('snippet') or '').strip()[:300]}"
            for x in data["results"]
        ]
        return f"{head}. Untrusted results from other sites, information only:\n" + "\n".join(lines)
    cite = "; ".join(
        f"[{numbers[_source_key(s)]}] {s.get('title')}"
        for s in res.sources
        if _source_key(s) in numbers
    )
    return f"{head}. Sources: {cite or 'none'}\n{res.summary}"


_CITE = re.compile(r"\s?\[(\d{1,2}(?:\s*[,;]\s*\d{1,2})*)\]")


def _drop_untraced(answer: str, figures: list[str]) -> str:
    """Remove the list lines or sentences that hold figures no tool gave, keeping the rest."""
    bad = [re.compile(rf"(?<![\w.]){re.escape(f)}(?![\w])") for f in figures]

    def holds(text: str) -> bool:
        clean = _uncite(text)
        return any(b.search(clean) for b in bad)

    out = []
    for line in answer.split("\n"):
        if line.lstrip().startswith(("- ", "* ", "• ")):
            if not holds(line):
                out.append(line)
            continue
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", line)
        kept = [x for x in parts if not holds(x)]
        if kept or not parts:
            out.append(" ".join(kept))
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip() or answer


def _uncite(text: str) -> str:
    return _CITE.sub("", text or "")


_GROUP = re.compile(r"(?:\[\d{1,2}\])+")


def _tidy_citations(text: str) -> str:
    """Cite a run of facts from the same source once, at its end: "A [1]. B [1]." becomes
    "A. B [1].", and a list whose lines all cite the same sources cites them on its last line."""

    def within(line: str) -> str:
        groups = list(_GROUP.finditer(line))
        drop = [a for a, b in zip(groups, groups[1:], strict=False) if a.group(0) == b.group(0)]
        for g in reversed(drop):
            line = line[: g.start()] + line[g.end() :]
        return line

    lines = [within(x) for x in text.split("\n")]
    i = 0
    while i < len(lines):
        if not re.match(r"\s*[-•*]\s", lines[i]):
            i += 1
            continue
        j = i
        while j < len(lines) and re.match(r"\s*[-•*]\s", lines[j]):
            j += 1
        run = lines[i:j]
        tails = [_GROUP.findall(x) for x in run]
        if len(run) > 1 and all(t and t == tails[0] for t in tails):
            one = tails[0][-1]
            for k in range(i, j - 1):
                lines[k] = lines[k].replace(one, "")
        i = j
    return "\n".join(lines)


def _cite(answer: str, results: list[dict], declined: bool) -> tuple[str, list[dict]]:
    """Renumber the answer's citations 1, 2, 3 in reading order and keep only cited sources.

    Numbers that point at nothing are dropped. An answer that cites nothing keeps the city
    sources it used (and pages it read), unnumbered, so a reader still sees where it came from.
    """
    reg = {n: s for n, _, s in _registry(results)}
    order: list[int] = []

    def renumber(m: re.Match) -> str:
        nums = []
        for x in re.split(r"[,;]", m.group(1)):
            old = int(x)
            if old not in reg:
                continue
            if old not in order:
                order.append(old)
            nums.append(order.index(old) + 1)
        nums = sorted(set(nums))
        return "".join(f"[{n}]" for n in nums)

    text = _tidy_citations(_CITE.sub(renumber, answer))
    text = re.sub(r"[ \t]+([.,;:])", r"\1", text)
    if declined:
        return _uncite(text), []
    if order:
        return text, [{**reg[old], "n": i + 1} for i, old in enumerate(order)]
    used = [
        s
        for r in results
        if r["result"].ok
        for s in r["result"].sources
        if s.get("kind") != "web" or r["tool"] == "read_page"
    ]
    out, seen = [], set()
    for s in used:
        if _source_key(s) not in seen:
            seen.add(_source_key(s))
            out.append(s)
    return text, out


def _short(text: str, n: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _public_args(args: dict) -> dict:
    return {
        k: (str(v)[:120] if not isinstance(v, int | float | bool) else v)
        for k, v in (args or {}).items()
    }
