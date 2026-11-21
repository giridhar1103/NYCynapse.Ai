"""Access requests, tokens and usage, in Postgres. Tokens and status links are random and kept
only as SHA-256 hashes; a new token is held in plain text just until its owner first sees it."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass

import psycopg

STATUSES = ("pending", "approved", "denied", "revoked")
# All approved tokens together, per day: the beta's ceiling whatever the single limits say.
SITE_PER_DAY = 3000


def digest(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


@dataclass
class Grant:
    token_hash: str
    request_id: str
    per_hour: int
    per_day: int


class Access:
    def __init__(self, connect):
        self._connect = connect

    def _c(self) -> psycopg.Connection:
        return self._connect()

    # requests

    def ask(self, name: str, email: str, purpose: str, client: str) -> tuple[str, str]:
        """A new request; returns its id and the requester's private status key."""
        key = secrets.token_urlsafe(18)
        rid = str(uuid.uuid4())
        self._c().execute(
            "INSERT INTO app.access_requests (id, name, email, purpose, status_key_hash, "
            "client_hash) VALUES (%s, %s, %s, %s, %s, %s)",
            (rid, name, email, purpose, digest(key), client),
        )
        return rid, key

    def asked_today(self, client: str) -> int:
        row = (
            self._c()
            .execute(
                "SELECT count(*) AS n FROM app.access_requests WHERE client_hash = %s "
                "AND created_at > now() - interval '1 day'",
                (client,),
            )
            .fetchone()
        )
        return int(row["n"])

    def status(self, key: str) -> dict | None:
        """The request behind a status key. A token approved since the last look is returned
        once and then forgotten."""
        c = self._c()
        row = c.execute(
            "SELECT id, name, status, token_once, token_hint, per_hour, per_day, created_at, "
            "decided_at FROM app.access_requests WHERE status_key_hash = %s",
            (digest(key),),
        ).fetchone()
        if not row:
            return None
        row = dict(row)
        if row["token_once"]:
            c.execute(
                "UPDATE app.access_requests SET token_once = NULL WHERE id = %s", (row["id"],)
            )
        return row

    def all(self) -> list[dict]:
        rows = (
            self._c()
            .execute(
                "SELECT r.id, r.name, r.email, r.purpose, r.status, r.token_hint, r.per_hour, "
                "r.per_day, r.note, r.created_at, r.decided_at, r.last_used_at, "
                "coalesce((SELECT sum(calls) FROM app.access_usage u "
                "WHERE u.token_hash = r.token_hash AND u.hour > now() - interval '1 day'), 0) "
                "AS calls_day, coalesce((SELECT sum(calls) FROM app.access_usage u "
                "WHERE u.token_hash = r.token_hash), 0) AS calls_all "
                "FROM app.access_requests r ORDER BY (r.status = 'pending') DESC, "
                "r.created_at DESC LIMIT 500"
            )
            .fetchall()
        )
        return [dict(r) for r in rows]

    def approve(self, rid: str, per_hour: int, per_day: int) -> bool:
        token = "nyc_" + secrets.token_urlsafe(24)
        done = self._c().execute(
            "UPDATE app.access_requests SET status = 'approved', token_hash = %s, token_once = %s, "
            "token_hint = %s, per_hour = %s, per_day = %s, decided_at = now() "
            "WHERE id = %s AND status IN ('pending', 'denied', 'revoked')",
            (digest(token), token, token[-4:], per_hour, per_day, rid),
        )
        return done.rowcount == 1

    def set_status(self, rid: str, status: str) -> bool:
        if status not in ("denied", "revoked"):
            return False
        done = self._c().execute(
            "UPDATE app.access_requests SET status = %s, token_once = NULL, decided_at = now() "
            "WHERE id = %s",
            (status, rid),
        )
        return done.rowcount == 1

    def set_limits(self, rid: str, per_hour: int, per_day: int, note: str | None) -> bool:
        done = self._c().execute(
            "UPDATE app.access_requests SET per_hour = %s, per_day = %s, note = %s WHERE id = %s",
            (per_hour, per_day, note, rid),
        )
        return done.rowcount == 1

    def delete(self, rid: str) -> bool:
        return (
            self._c().execute("DELETE FROM app.access_requests WHERE id = %s", (rid,)).rowcount == 1
        )

    # tokens

    def grant(self, token: str) -> Grant | None:
        row = (
            self._c()
            .execute(
                "SELECT id, token_hash, per_hour, per_day FROM app.access_requests "
                "WHERE token_hash = %s AND status = 'approved'",
                (digest(token),),
            )
            .fetchone()
        )
        return (
            Grant(row["token_hash"], str(row["id"]), row["per_hour"], row["per_day"])
            if row
            else None
        )

    def spend(self, g: Grant) -> str | None:
        """Count one tool call against a token; returns why it is refused, or None."""
        c = self._c()
        row = c.execute(
            "SELECT coalesce(sum(calls) FILTER (WHERE hour > now() - interval '1 hour'), 0) AS h, "
            "coalesce(sum(calls), 0) AS d FROM app.access_usage WHERE token_hash = %s "
            "AND hour > now() - interval '1 day'",
            (g.token_hash,),
        ).fetchone()
        if row["h"] >= g.per_hour:
            return f"this token's limit of {g.per_hour} calls an hour is reached"
        if row["d"] >= g.per_day:
            return f"this token's limit of {g.per_day} calls a day is reached"
        site = c.execute(
            "SELECT coalesce(sum(calls), 0) AS n FROM app.access_usage "
            "WHERE hour > now() - interval '1 day'"
        ).fetchone()
        if site["n"] >= SITE_PER_DAY:
            return "the service's daily limit is reached; try again tomorrow"
        c.execute(
            "INSERT INTO app.access_usage (token_hash, hour, calls) "
            "VALUES (%s, date_trunc('hour', now()), 1) ON CONFLICT (token_hash, hour) "
            "DO UPDATE SET calls = app.access_usage.calls + 1",
            (g.token_hash,),
        )
        c.execute(
            "UPDATE app.access_requests SET last_used_at = now() WHERE token_hash = %s",
            (g.token_hash,),
        )
        return None
