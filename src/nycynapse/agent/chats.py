"""Conversations with the guide agent: short-lived memory for follow-ups, and a private record
kept for 30 days to review how the models answer.

A conversation is a first question and at most five follow-ups. It ends after 30 minutes
without a message. Each follow-up is given a compact memory of the earlier turns (the question,
the answer and the key facts found), not the whole transcript, to keep it cheap. Nothing here is
shown to other readers.
"""

from __future__ import annotations

import re
import secrets
import time
import uuid
from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb

MAX_TURNS = 6  # the first question and five follow-ups
IDLE_MINUTES = 30
KEEP_DAYS = 30
EXAMPLE_HOURS = 30


@dataclass
class Slot:
    chat_id: str
    turn: int
    history: list[dict]


class ChatError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class Chats:
    def __init__(self, conn_factory, kind: str = "public"):
        self._connect = conn_factory
        self.kind = kind  # "eval" on the test server: kept apart from readers' chats

    def _conn(self) -> psycopg.Connection:
        return self._connect()

    def begin(self, chat_id: str | None, client: str) -> Slot:
        conn = self._conn()
        if not chat_id:
            new_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO app.chats (id, client_hash, kind) VALUES (%s, %s, %s)",
                (new_id, client, self.kind),
            )
            return Slot(new_id, 1, [])
        try:
            uuid.UUID(chat_id)
        except ValueError as e:
            raise ChatError("unknown_chat", "That conversation was not found.") from e
        row = conn.execute(
            "SELECT id, client_hash, turns, last_at > now() - make_interval(mins => %s) AS live "
            "FROM app.chats WHERE id = %s",
            (IDLE_MINUTES, chat_id),
        ).fetchone()
        if row is None or row["client_hash"] != client:
            raise ChatError("unknown_chat", "That conversation was not found. Start a new one.")
        if not row["live"]:
            raise ChatError(
                "expired",
                "This conversation timed out after 30 minutes. Start a new one to keep going.",
            )
        if row["turns"] >= MAX_TURNS:
            raise ChatError(
                "full",
                "Conversations are limited to five follow-ups during the beta. "
                "Start a new one to keep going.",
            )
        hist = conn.execute(
            "SELECT memory FROM app.chat_turns WHERE chat_id = %s AND memory IS NOT NULL "
            "AND outcome = 'answered' ORDER BY turn",
            (chat_id,),
        ).fetchall()
        return Slot(chat_id, row["turns"] + 1, [h["memory"] for h in hist])

    def record(self, slot: Slot, question: str, out: dict, outcome: str) -> None:
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO app.chat_turns (id, chat_id, turn, question, answer, outcome, model,
                fell_back, steps, sources, map, memory, model_calls, cost_usd, latency_ms, error)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (chat_id, turn) DO NOTHING
            """,
            (
                str(uuid.uuid4()),
                slot.chat_id,
                slot.turn,
                question,
                out.get("answer"),
                outcome,
                out.get("model"),
                bool(out.get("fell_back")),
                Jsonb(out.get("steps") or []),
                Jsonb(out.get("sources") or []),
                Jsonb(out.get("map") or []),
                Jsonb(out.get("memory")) if outcome == "answered" else None,
                out.get("model_calls"),
                out.get("cost_usd"),
                out.get("latency_ms"),
                out.get("error"),
            ),
        )
        if outcome == "answered":
            conn.execute(
                "UPDATE app.chats SET turns = %s, last_at = now() WHERE id = %s",
                (slot.turn, slot.chat_id),
            )
        else:
            conn.execute("UPDATE app.chats SET last_at = now() WHERE id = %s", (slot.chat_id,))

    def rate(self, chat_id: str, turn: int, client: str, rating: int, note: str | None) -> bool:
        """The reader's verdict on one of their own answers. False when it is not theirs."""
        try:
            uuid.UUID(chat_id)
        except ValueError:
            return False
        done = self._conn().execute(
            "UPDATE app.chat_turns t SET rating = %s, rating_note = %s, rated_at = now() "
            "FROM app.chats c WHERE c.id = t.chat_id AND t.chat_id = %s AND t.turn = %s "
            "AND c.client_hash = %s AND t.outcome = 'answered'",
            (rating, note, chat_id, turn, client),
        )
        return done.rowcount == 1

    def share(self, chat_id: str, turn: int, client: str) -> str | None:
        """A link id for one of the reader's own answers (the same id if shared before)."""
        try:
            uuid.UUID(chat_id)
        except ValueError:
            return None
        row = (
            self._conn()
            .execute(
                "UPDATE app.chat_turns t SET share_id = coalesce(t.share_id, %s) "
                "FROM app.chats c WHERE c.id = t.chat_id AND t.chat_id = %s AND t.turn = %s "
                "AND c.client_hash = %s AND t.outcome = 'answered' RETURNING t.share_id",
                (secrets.token_urlsafe(9), chat_id, turn, client),
            )
            .fetchone()
        )
        return row["share_id"] if row else None

    def shared(self, share_id: str) -> dict | None:
        """A shared answer, as the page shows it. Nothing about who asked."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,20}", share_id or ""):
            return None
        row = (
            self._conn()
            .execute(
                "SELECT question, answer, sources, map, model, asked_at FROM app.chat_turns "
                "WHERE share_id = %s AND outcome = 'answered'",
                (share_id,),
            )
            .fetchone()
        )
        return dict(row) if row else None

    def spent_today(self) -> float:
        row = (
            self._conn()
            .execute(
                "SELECT coalesce(sum(t.cost_usd), 0) AS c FROM app.chat_turns t "
                "JOIN app.chats c ON c.id = t.chat_id "
                "WHERE t.asked_at > date_trunc('day', now()) AND c.kind = %s",
                (self.kind,),
            )
            .fetchone()
        )
        return float(row["c"])

    def asked_recently(self, client: str, minutes: int = 60) -> int:
        row = (
            self._conn()
            .execute(
                "SELECT count(*) AS n FROM app.chat_turns t JOIN app.chats c ON c.id = t.chat_id "
                "WHERE c.client_hash = %s AND t.asked_at > now() - make_interval(mins => %s) "
                # a reader turned away at full capacity has not used an answer
                "AND c.kind = %s AND t.outcome IS DISTINCT FROM 'busy'",
                (client, minutes, self.kind),
            )
            .fetchone()
        )
        return int(row["n"])

    def example(self, question: str) -> dict | None:
        """A stored first-turn answer to a homepage example, answered this morning."""
        row = (
            self._conn()
            .execute(
                "SELECT t.answer, t.sources, t.map, t.steps, t.model, t.latency_ms, t.memory FROM "
                "app.chat_turns t JOIN app.chats c ON c.id = t.chat_id WHERE c.client_hash = "
                "'examples' AND t.turn = 1 AND t.outcome = 'answered' AND lower(t.question) = "
                "lower(%s) AND t.asked_at > now() - make_interval(hours => %s) "
                "ORDER BY t.asked_at DESC LIMIT 1",
                (question.strip(), EXAMPLE_HOURS),
            )
            .fetchone()
        )
        return dict(row) if row else None

    def purge(self, days: int = KEEP_DAYS) -> dict:
        """Delete conversations and question traces older than the retention period."""
        conn = self._conn()
        chats = conn.execute(
            "DELETE FROM app.chats WHERE started_at < now() - make_interval(days => %s)",
            (days,),
        ).rowcount
        traces = conn.execute(
            "DELETE FROM app.traces WHERE asked_at < now() - make_interval(days => %s)",
            (days,),
        ).rowcount
        return {"chats": chats, "traces": traces, "at": time.strftime("%Y-%m-%d %H:%M")}
