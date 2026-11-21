"""Push notifications for new alerts near a watched spot.

A browser that opts in ("Get alerts near here") sends its push subscription and a spot; the
alerts poller calls `notify` with the alerts it saw for the first time, and each watch within
reach gets at most one notification every 15 minutes and six a day. Only fresh alerts count, so
a backlog of long-running closures never floods anyone.
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import UTC, datetime, timedelta

log = logging.getLogger(__name__)
GAP = timedelta(minutes=15)
PER_DAY = 6
FRESH = timedelta(hours=2)
KINDS = {"incident", "closure", "subway", "weather"}


def _db():
    from ..tools.alerts import _db as alerts_db

    return alerts_db()


def watch(sub: dict, lat: float, lon: float, radius_m: int) -> bool:
    keys = sub.get("keys") or {}
    endpoint = str(sub.get("endpoint") or "")
    if not endpoint.startswith("https://") or len(endpoint) > 800:
        return False
    if not keys.get("p256dh") or not keys.get("auth"):
        return False
    if not (40.4 <= lat <= 41.1 and -74.4 <= lon <= -73.6):
        return False
    _db().execute(
        """
        INSERT INTO app.push_watch (endpoint, p256dh, auth, lat, lon, radius_m)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (endpoint) DO UPDATE SET p256dh = EXCLUDED.p256dh, auth = EXCLUDED.auth,
            lat = EXCLUDED.lat, lon = EXCLUDED.lon, radius_m = EXCLUDED.radius_m,
            renewed_at = now()
        """,
        (
            endpoint,
            keys["p256dh"][:200],
            keys["auth"][:100],
            round(lat, 3),
            round(lon, 3),
            max(300, min(int(radius_m), 3000)),
        ),
    )
    return True


def unwatch(endpoint: str) -> None:
    _db().execute("DELETE FROM app.push_watch WHERE endpoint = %s", (endpoint,))


def _send(w: dict, payload: dict) -> bool:
    """Deliver one notification; False when the subscription is gone (and it is removed)."""
    from pywebpush import WebPushException, webpush

    try:
        webpush(
            subscription_info={
                "endpoint": w["endpoint"],
                "keys": {"p256dh": w["p256dh"], "auth": w["auth"]},
            },
            data=json.dumps(payload),
            vapid_private_key=os.environ["NYCYNAPSE_VAPID_PRIVATE"],
            vapid_claims={
                "sub": os.environ.get("NYCYNAPSE_VAPID_SUBJECT", "https://giriworks.com/nycynapse")
            },
            ttl=1800,
            timeout=10,
        )
        return True
    except WebPushException as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code in (404, 410):
            unwatch(w["endpoint"])
        else:
            log.warning("push failed (%s): %s", code, str(e)[:160])
        return False


def notify(new_ids: list[str]) -> int:
    """Tell the watches near each new alert. Returns how many notifications went out."""
    if not new_ids or not os.environ.get("NYCYNAPSE_VAPID_PRIVATE"):
        return 0
    c = _db()
    c.execute("DELETE FROM app.push_watch WHERE renewed_at < now() - interval '30 days'")
    rows = c.execute(
        "SELECT * FROM app.city_alerts WHERE id = ANY(%s) AND severity >= 2", (new_ids,)
    ).fetchall()
    now = datetime.now(UTC)
    sent = 0
    for a in rows:
        if a["kind"] not in KINDS:
            continue
        if a["starts"] and a["starts"] < now - FRESH:
            continue  # first seen now, but not news
        if a["lat"] is None:
            near = c.execute("SELECT * FROM app.push_watch").fetchall()
        else:
            d = 3000 / 111_320
            near = c.execute(
                "SELECT * FROM app.push_watch "
                "WHERE lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s",
                (a["lat"] - d, a["lat"] + d, a["lon"] - d * 1.4, a["lon"] + d * 1.4),
            ).fetchall()
        for w in near:
            if a["lat"] is not None:
                dy = (w["lat"] - a["lat"]) * 111_320
                dx = (w["lon"] - a["lon"]) * 111_320 * math.cos(math.radians(w["lat"]))
                if math.hypot(dx, dy) > w["radius_m"]:
                    continue
            today = now.date()
            count = w["sent_today"] if w["sent_day"] == today else 0
            if count >= PER_DAY or (w["sent_at"] and now - w["sent_at"] < GAP):
                continue
            body = a["title"] if a["lat"] is None else f"{a['title']} (near your watched spot)"
            if _send(
                w,
                {"title": "NYCynapse alert", "body": body[:180], "tag": a["id"], "url": "./#guide"},
            ):
                c.execute(
                    "UPDATE app.push_watch SET sent_at = now(), sent_today = %s, sent_day = %s "
                    "WHERE endpoint = %s",
                    (count + 1, today, w["endpoint"]),
                )
                sent += 1
    if sent:
        log.info("sent %s push notifications", sent)
    return sent
