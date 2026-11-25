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

from ..agent import urgent
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
RUNNING_USD = 0.30  # a typical answer's cost, for answers still running
KEEPALIVE_S = 8
PAUSED = os.environ.get("NYCYNAPSE_PAUSED", "") == "1"
# The test server returns what each tool said with the answer, for the evaluation judge.
TRACE = os.environ.get("NYCYNAPSE_EVAL_TRACE", "") == "1"

gate = Gate(ACTIVE, WAITING)

# The same first question asked again soon gets the answer just given, with no model call:
# for a few minutes when it used live feeds or the web, for hours when only city records.
LIVE_TTL_S = 10 * 60
LAKE_TTL_S = 6 * 3600
_recent: dict[str, tuple[float, float, dict]] = {}
_recent_lock = threading.Lock()


def _key(question: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", question.lower()).strip()


def _keep_answer(question: str, out: dict) -> None:
    kinds = {s.get("kind") for s in out.get("steps") or [] if "tool" in s}
    if not kinds or out.get("declined") or out.get("error") or not out.get("answer"):
        return
    ttl = LAKE_TTL_S if kinds == {"lake"} else LIVE_TTL_S
    with _recent_lock:
        if len(_recent) > 300:
            for k in sorted(_recent, key=lambda k: _recent[k][0])[:100]:
                _recent.pop(k, None)
        _recent[_key(question)] = (time.time(), ttl, out)


def _answered_recently(question: str) -> dict | None:
    with _recent_lock:
        hit = _recent.get(_key(question))
    if not hit or time.time() - hit[0] > hit[1]:
        return None
    out = hit[2]
    return {
        "answer": out["answer"],
        "sources": out.get("sources") or [],
        "map": out.get("map") or [],
        "steps": out.get("steps") or [],
        "model": out.get("model"),
        "memory": out.get("memory") or {},
        "asked_at": hit[0],
    }


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
    "budget": "Today's question allowance is used up during the beta. Please come back tomorrow.",
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


def verify_turnstile(token: str, ip: str | None, strict: bool = False) -> bool:
    """Cloudflare's bot check. If the check itself cannot run, the question goes through, unless
    `strict` (the day's budget is already half spent): then it is refused."""
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
        log.warning(
            "bot check failed to run; %s",
            "refusing (budget half spent)" if strict else "letting the question through",
        )
        return not strict


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
        "connect_base": os.environ.get(
            "NYCYNAPSE_CONNECT_BASE", "https://api.giriworks.com/nycynapse"
        ).rstrip("/"),
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


@router.get("/api/chat/share")
def share(request: Request, chat: str, turn: int = Query(ge=1, le=MAX_TURNS)):
    """A link to one of the reader's own answers, made only when they ask for it."""
    try:
        sid = chats().share(chat.strip(), turn, _client(request))
    except psycopg.Error:
        log.exception("could not share an answer")
        sid = None
    return {"ok": bool(sid), "id": sid}


@router.get("/api/shared/{share_id}")
def shared(share_id: str):
    from fastapi import HTTPException

    try:
        row = chats().shared(share_id)
    except psycopg.Error:
        row = None
    if not row:
        raise HTTPException(404, "That shared answer was not found. Links last 30 days.")
    layers = row["map"] or []
    places = next((x.get("items") or [] for x in layers if x.get("type") == "highlight"), [])
    return {
        "question": row["question"],
        "text": row["answer"],
        "sources": row["sources"] or [],
        "map": layers,
        "places": places,
        "model": row["model"],
        "asked_at": row["asked_at"].isoformat() if row["asked_at"] else None,
    }


@router.get("/api/chat")
async def chat(
    request: Request,
    q: str = Query("", max_length=2000),
    chat: str = "",
    token: str = "",
):
    client = _client(request)
    ip = _address(request)
    question = _clean(q)
    # The reader's location, and the home and work saved on their device, come in headers: an
    # address ends up in logs along the way. What is stored of the turn has them taken out.
    spots = {k: _where(request.headers.get(f"x-nyc-{k}", "")) for k in ("at", "home", "work")}
    where = spots.pop("at")
    places = {k: v for k, v in spots.items() if v}
    return StreamingResponse(
        _turn(question, chat.strip() or None, token, client, ip, where, places),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _where(at: str) -> tuple[float, float] | None:
    """A location the reader chose to share ("40.7128,-74.0060"), rounded to about 100 m, and
    only inside the region the guide covers."""
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
    places: dict[str, tuple[float, float]] | None = None,
):
    loop = asyncio.get_running_loop()
    personal = bool(where or places)  # never cached or replayed to anyone else

    def err(code: str, message: str | None = None) -> bytes:
        return _sse("error", {"code": code, "message": message or MESSAGES[code]})

    # Someone in danger sees the number at once: before any limit, line or model, and whatever
    # becomes of the answer.
    call = urgent.number(question)
    if call:
        yield _sse("urgent", {"call": call, "text": urgent.with_number(None, call)})
    if not 3 <= len(question) <= 400:
        yield err("length")
        return
    if PAUSED:
        yield err("paused")
        return
    store_ = chats()
    try:
        recent, today, spent = await asyncio.to_thread(store_.limits, client)
    except psycopg.Error:
        recent, today, spent = 0, 0, 0.0
    if recent >= PER_HOUR or today >= PER_DAY:
        yield err("rate")
        return
    # Answers already running will add to today's spend: count them at a typical cost, so several
    # at once cannot carry the day past its budget.
    if spent + gate.stats()["active"] * RUNNING_USD >= DAILY_USD:
        yield err("budget")
        return
    strict = spent >= DAILY_USD / 2
    if chat_id is None and not await asyncio.to_thread(verify_turnstile, token, ip, strict):
        yield err("verify")
        return
    try:
        slot: Slot = await asyncio.to_thread(chats().begin, chat_id, client)
    except ChatError as e:
        yield err(e.code, e.message)
        return
    yield _sse("chat", {"id": slot.chat_id, "turn": slot.turn, "max_turns": MAX_TURNS})

    if slot.turn == 1:
        cached = None if personal else _answered_recently(question)
        cached = cached or await asyncio.to_thread(chats().example, question)
        if cached:
            for chunk in _replay(cached):
                yield chunk
                await asyncio.sleep(0.12)
            out = {
                "follow_ups": (cached.get("memory") or {}).get("follow_ups") or [],
                "answer": cached["answer"],
                "sources": cached["sources"],
                "map": cached["map"],
                # The answer's own places keep their numbers and pins, as in a fresh answer.
                "places": cached.get("places")
                or next(
                    (
                        x.get("items") or []
                        for x in (cached.get("map") or [])
                        if x.get("type") == "highlight"
                    ),
                    [],
                ),
                "steps": cached["steps"],
                "model": cached["model"],
                "cost_usd": 0,
                "latency_ms": 0,
                "memory": {
                    "q": question,
                    "answer": cached["answer"][:900],
                    "facts": (cached.get("memory") or {}).get("facts", ""),
                },
            }
            await asyncio.to_thread(chats().record, slot, question, out, "answered")
            yield _sse(
                "answer",
                {**_answer_payload(slot, out, cached=True), "asked_at": cached.get("asked_at")},
            )
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
                a.run(question, slot.history, emit, where, places or None)
                if personal
                else a.run(question, slot.history, emit)
            )
            outcome = "answered" if out.get("answer") and not out.get("error") else "failed"
            if out.get("declined"):
                outcome = "declined"
            if slot.turn == 1 and not personal:
                _keep_answer(question, out)
            out["steps"] = (out.get("steps") or []) + [{"queued_ms": queued_ms}]
            _log_turn(slot, out, outcome, queued_ms)
            try:
                from ..agent.core import without_spots

                kept = without_spots(out, where, places) if personal else out
                chats().record(slot, question, kept, outcome)
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
        "latency_ms": out.get("latency_ms"),
        "cached": cached,
        "untraced": out.get("untraced") or [],
        **({"trace": out.get("trace") or [], "cost_usd": out.get("cost_usd")} if TRACE else {}),
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
