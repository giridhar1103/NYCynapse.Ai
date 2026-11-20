"""The guide agent over HTTP: GET /api/chat streams one turn of a conversation as server-sent
events.

Events, in order: `chat` (conversation id and turn), `queue` (place in line, while waiting),
`status` (what the agent is doing), `tool` (each tool call starting and finishing, with errors),
`lake` (the lake pipeline's stages inside a lake call), `map` (layers to draw), then `answer`
or `error`. Comment lines go out while nothing else does, so proxies keep the stream open.

Before a model is called: a bot check on the first message, a per-reader hourly allowance, a
site-wide daily budget, and a fixed number of answers at once with a short line behind them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time

import httpx
import psycopg
from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from ..agent.chats import IDLE_MINUTES, MAX_TURNS, ChatError, Chats, Slot
from ..agent.examples import EXAMPLES
from ..catalog import store
from .capacity import Full, Gate

log = logging.getLogger(__name__)
router = APIRouter()

ACTIVE = int(os.environ.get("NYCYNAPSE_CHAT_ACTIVE", "3"))
WAITING = int(os.environ.get("NYCYNAPSE_CHAT_WAITING", "10"))
PER_HOUR = int(os.environ.get("NYCYNAPSE_CHAT_PER_HOUR", "16"))
PER_DAY = int(os.environ.get("NYCYNAPSE_CHAT_PER_DAY", "48"))
DAILY_USD = float(os.environ.get("NYCYNAPSE_CHAT_DAILY_USD", "10"))
WAIT_S = 150
KEEPALIVE_S = 8
PAUSED = os.environ.get("NYCYNAPSE_PAUSED", "") == "1"
# The test server returns what each tool said with the answer, for the evaluation judge.
TRACE = os.environ.get("NYCYNAPSE_EVAL_TRACE", "") == "1"

gate = Gate(ACTIVE, WAITING)
_local = threading.local()
_state: dict = {}
_setup = threading.Lock()

MESSAGES = {
    "full": "We're at full capacity right now: every seat is answering someone. Please wait "
    "a minute for other questions to finish, then send yours again.",
    "waited": "It's busier than usual and your question waited too long. Please try again "
    "in a minute.",
    "models": "Our models are at full capacity right now. Please wait a few minutes and try again.",
    "rate": "That's a lot of questions in an hour. Take a short break and come back soon.",
    "budget": "Today's question allowance for the demo is used up. Please come back tomorrow.",
    "paused": "The guide is paused for maintenance. Please check back shortly.",
    "verify": "We couldn't confirm you're not a bot. Reload the page and try again.",
    "length": "Questions need between 3 and 400 characters.",
}


def _conn() -> psycopg.Connection:
    c = getattr(_local, "conn", None)
    if c is None or c.closed:
        c = _local.conn = store.connect(_state["dsn"])
    return c


def chats() -> Chats:
    return Chats(_conn, os.environ.get("NYCYNAPSE_CHAT_KIND", "public"))


def setup(settings, base_ctx_fn) -> None:
    from ..tools.http import load_keys

    load_keys()
    _state["dsn"] = settings.app_pg_dsn
    _state["ctx_fn"] = base_ctx_fn


def agent():
    with _setup:
        if "agent" not in _state:
            from ..agent.core import Agent
            from ..agent.host import MCPHost
            from ..agent.lake import Lake
            from ..tools.http import load_keys

            load_keys()
            _state["agent"] = Agent(Lake(_state["ctx_fn"]()), MCPHost())
        return _state["agent"]


def _sse(event: str, data) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n".encode()


def _clean(q: str) -> str:
    q = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", q)
    return " ".join(q.split())


def _address(request: Request) -> str | None:
    """The reader's address. nginx works it out (see deploy/nginx-nycynapse-zones.conf) and
    passes it in these headers; a caller cannot set them."""
    return (
        request.headers.get("x-client-ip")
        or request.headers.get("x-real-ip")
        or (request.client.host if request.client else None)
    )


def _client(request: Request) -> str:
    from .service import client_hash

    return client_hash(_address(request) or "?")


def verify_turnstile(token: str, ip: str | None) -> bool:
    secret = os.environ.get("TURNSTILE_SECRET_KEY", "").strip()
    if not secret or os.environ.get("NYCYNAPSE_TURNSTILE", "") == "off":
        return True
    if not token or len(token) > 2048:
        return False
    try:
        r = httpx.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={"secret": secret, "response": token, **({"remoteip": ip} if ip else {})},
            timeout=8,
        )
        result = r.json()
        if not result.get("success"):
            log.warning("bot check refused: %s", result.get("error-codes"))
        return bool(result.get("success"))
    except (httpx.HTTPError, ValueError):
        log.warning("turnstile check failed to run; letting the question through")
        return True


@router.get("/api/config")
def config():
    return {
        "turnstile_site_key": (os.environ.get("TURNSTILE_SITE_KEY", "").strip() or None)
        if os.environ.get("NYCYNAPSE_TURNSTILE", "") != "off"
        else None,
        "max_turns": MAX_TURNS,
        "examples": [{"q": e["q"], "tag": e["tag"]} for e in EXAMPLES],
        "idle_minutes": IDLE_MINUTES,
        "capacity": gate.stats(),
    }


@router.get("/api/chat/rate")
def rate(
    request: Request,
    chat: str,
    turn: int = Query(ge=1, le=MAX_TURNS),
    v: str = Query(pattern="^(up|down)$"),
    note: str = Query("", max_length=300),
):
    """A reader marks one of their own answers right or wrong (a GET, like everything here:
    the site's proxy passes nothing else)."""
    try:
        ok = chats().rate(
            chat.strip(), turn, _client(request), 1 if v == "up" else -1, _clean(note) or None
        )
    except psycopg.Error:
        log.exception("could not store a rating")
        ok = False
    if ok:
        log.info("rating %s", json.dumps({"chat": chat[:8], "turn": turn, "v": v}))
    return {"ok": ok}


@router.get("/api/chat")
async def chat(
    request: Request,
    q: str = Query("", max_length=2000),
    chat: str = "",
    token: str = "",
    at: str = Query("", max_length=40),
):
    client = _client(request)
    ip = _address(request)
    question = _clean(q)
    return StreamingResponse(
        _turn(question, chat.strip() or None, token, client, ip, _where(at)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _where(at: str) -> tuple[float, float] | None:
    """A location the reader chose to share ("40.7128,-74.0060"), rounded to about 100 m, and
    only inside the region the guide covers. Never stored."""
    m = re.fullmatch(r"\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*", at or "")
    if not m:
        return None
    lat, lon = round(float(m.group(1)), 3), round(float(m.group(2)), 3)
    return (lat, lon) if 40.0 <= lat <= 41.8 and -75.0 <= lon <= -71.8 else None


async def _turn(
    question: str,
    chat_id: str | None,
    token: str,
    client: str,
    ip: str | None,
    where: tuple[float, float] | None = None,
):
    loop = asyncio.get_running_loop()

    def err(code: str, message: str | None = None) -> bytes:
        return _sse("error", {"code": code, "message": message or MESSAGES[code]})

    if not 3 <= len(question) <= 400:
        yield err("length")
        return
    if PAUSED:
        yield err("paused")
        return
    store_ = chats()
    try:
        recent = await asyncio.to_thread(store_.asked_recently, client)
        today = await asyncio.to_thread(store_.asked_recently, client, 24 * 60)
        spent = await asyncio.to_thread(store_.spent_today)
    except psycopg.Error:
        recent, today, spent = 0, 0, 0.0
    if recent >= PER_HOUR or today >= PER_DAY:
        yield err("rate")
        return
    if spent >= DAILY_USD:
        yield err("budget")
        return
    if chat_id is None and not await asyncio.to_thread(verify_turnstile, token, ip):
        yield err("verify")
        return
    try:
        slot: Slot = await asyncio.to_thread(chats().begin, chat_id, client)
    except ChatError as e:
        yield err(e.code, e.message)
        return
    yield _sse("chat", {"id": slot.chat_id, "turn": slot.turn, "max_turns": MAX_TURNS})

    if slot.turn == 1:
        cached = await asyncio.to_thread(chats().example, question)
        if cached:
            for chunk in _replay(cached):
                yield chunk
                await asyncio.sleep(0.12)
            out = {
                "follow_ups": (cached.get("memory") or {}).get("follow_ups") or [],
                "answer": cached["answer"],
                "sources": cached["sources"],
                "map": cached["map"],
                "steps": cached["steps"],
                "model": cached["model"],
                "cost_usd": 0,
                "latency_ms": 0,
                "memory": {"q": question, "answer": cached["answer"][:900], "facts": ""},
            }
            await asyncio.to_thread(chats().record, slot, question, out, "answered")
            yield _sse("answer", _answer_payload(slot, out, cached=True))
            return

    a = await asyncio.to_thread(agent)
    if not a.available():
        await asyncio.to_thread(chats().record, slot, question, {"error": "models"}, "busy")
        yield err("models")
        return
    try:
        ticket = gate.join()
    except Full:
        await asyncio.to_thread(chats().record, slot, question, {"error": "full"}, "busy")
        yield err("full")
        return
    try:
        started, last = time.monotonic(), -1
        while not await asyncio.to_thread(gate.wait, ticket, 2.0):
            pos = gate.position(ticket)
            if pos != last:
                yield _sse("queue", {"position": pos, "active": gate.stats()["active"]})
                last = pos
            if time.monotonic() - started > WAIT_S:
                gate.leave_line(ticket)
                await asyncio.to_thread(chats().record, slot, question, {"error": "waited"}, "busy")
                yield err("waited")
                return
    except BaseException:
        gate.leave_line(ticket)  # the reader left while waiting
        raise
    queued_ms = int((time.monotonic() - started) * 1000)

    events: asyncio.Queue = asyncio.Queue()

    def emit(event: str, data: dict) -> None:
        loop.call_soon_threadsafe(events.put_nowait, _sse(event, data))

    def work() -> None:
        # Owns the seat: gives it back when the answer is done, even if the reader has left.
        try:
            out = (
                a.run(question, slot.history, emit, where)
                if where
                else a.run(question, slot.history, emit)
            )
            outcome = "answered" if out.get("answer") and not out.get("error") else "failed"
            if out.get("declined"):
                outcome = "declined"
            out["steps"] = (out.get("steps") or []) + [{"queued_ms": queued_ms}]
            _log_turn(slot, out, outcome, queued_ms)
            try:
                chats().record(slot, question, out, outcome)
            except psycopg.Error:
                log.exception("could not store a chat turn")
            if out.get("answer"):
                emit("answer", _answer_payload(slot, out))
            else:
                emit("error", {"code": "models", "message": out.get("error") or MESSAGES["models"]})
        except Exception:  # noqa: BLE001 - the reader gets a message, the log the details
            log.exception("chat turn failed")
            emit(
                "error",
                {
                    "code": "failed",
                    "message": "Something went wrong answering that. Please try again.",
                },
            )
        finally:
            gate.done()
            loop.call_soon_threadsafe(events.put_nowait, None)
            # This thread ends here; so does its database connection.
            conn = getattr(_local, "conn", None)
            if conn is not None and not conn.closed:
                conn.close()
            _local.conn = None

    threading.Thread(target=work, daemon=True, name="chat-turn").start()
    yield _sse("status", {"text": "Reading your question"})
    while True:
        try:
            chunk = await asyncio.wait_for(events.get(), KEEPALIVE_S)
        except TimeoutError:
            yield b": still working\n\n"
            continue
        if chunk is None:
            return
        yield chunk


def _log_turn(slot: Slot, out: dict, outcome: str, queued_ms: int) -> None:
    """One line per turn for the service log: how it went and where the time was spent.
    Never the question or who asked."""
    timeline = next((s["timeline"] for s in out.get("steps") or [] if "timeline" in s), [])
    spent = {"model": 0, "tool": 0}
    for x in timeline:
        spent[x["kind"]] = spent.get(x["kind"], 0) + x["ms"]
    notes = [s["note"] for s in out.get("steps") or [] if "note" in s]
    log.info(
        "turn %s",
        json.dumps(
            {
                "chat": slot.chat_id[:8],
                "turn": slot.turn,
                "outcome": outcome,
                "ms": out.get("latency_ms"),
                "queued_ms": queued_ms,
                "model": out.get("model"),
                "fell_back": bool(out.get("fell_back")),
                "model_calls": out.get("model_calls"),
                "model_ms": spent["model"],
                "tools": sum(1 for x in timeline if x["kind"] == "tool"),
                "tool_ms": spent["tool"],
                "tools_failed": [
                    x["name"] for x in timeline if x["kind"] == "tool" and not x["ok"]
                ],
                "rewrites": sum(1 for n in notes if n.startswith("redrafted")),
                "fixed_in_check": sum(1 for n in notes if n.startswith("fixed in the check")),
                "usd": out.get("cost_usd"),
            }
        ),
    )


def _answer_payload(slot: Slot, out: dict, cached: bool = False) -> dict:
    declined = bool(out.get("declined"))
    turns_left = MAX_TURNS - slot.turn
    return {
        "chat": slot.chat_id,
        "turn": slot.turn,
        "follow_ups_left": max(0, turns_left),
        "can_rate": not declined and bool(out.get("answer")),
        "text": out.get("answer"),
        "follow_ups": out.get("follow_ups") or [],
        "declined": declined,
        "sources": out.get("sources") or [],
        "model": out.get("model"),
        "fell_back": bool(out.get("fell_back")),
        "cost_usd": out.get("cost_usd"),
        "latency_ms": out.get("latency_ms"),
        "cached": cached,
        "untraced": out.get("untraced") or [],
        **({"trace": out.get("trace") or []} if TRACE else {}),
        "places": out.get("places") or [],
    }


def _replay(cached: dict):
    """A stored example answer, played back as the same events, quickly."""
    yield _sse("status", {"text": "Answered earlier today from the same sources"})
    for i, s in enumerate(cached.get("steps") or []):
        if "tool" not in s:
            continue
        cid = f"r{i}"
        yield _sse(
            "tool",
            {
                "id": cid,
                "name": s["tool"],
                "args": s.get("args"),
                "state": "start",
                "kind": s.get("kind"),
            },
        )
        yield _sse(
            "tool",
            {
                "id": cid,
                "name": s["tool"],
                "kind": s.get("kind"),
                "state": "done" if s.get("ok") else "error",
                "ms": s.get("ms"),
                "error": s.get("error"),
            },
        )
    if cached.get("map"):
        yield _sse("map", {"id": "cached", "layers": cached["map"]})
