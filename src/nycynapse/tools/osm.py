"""OpenStreetMap place search, polite and with a spare.

Nominatim asks for one request a second at most. When it refuses, has an outage or its turn is too
far off, the same data is searched through Photon.
"""

from __future__ import annotations

import re
import threading
import time

import httpx

from . import trips as T
from .http import cached, get_json

NOMINATIM = "https://nominatim.openstreetmap.org/search"
PHOTON = "https://photon.komoot.io/api/"
SPACING_S = 1.1
REST_S = 15 * 60
DAY = 86400

_lock = threading.Lock()
_state = {"next": 0.0, "rest_until": 0.0}


def _turn() -> float | None:
    """Seconds until Nominatim may be asked again, with that turn taken, or None: do not ask."""
    with _lock:
        now = time.monotonic()
        wait = max(0.0, _state["next"] - now)
        if now < _state["rest_until"] or wait > 2.5:
            return None
        _state["next"] = now + wait + SPACING_S
        return wait


def _nominatim(q: str) -> list[dict]:
    """Raises when it does not answer properly."""
    return get_json(
        NOMINATIM,
        {
            "q": q,
            "format": "jsonv2",
            "limit": 1,
            "viewbox": ",".join(str(x) for x in T.BOX),
            "bounded": 1,
        },
        ttl_s=0,
    )


def _photon(q: str) -> list[dict]:
    d = get_json(
        PHOTON,
        {"q": q, "limit": 5, "lang": "en", "bbox": ",".join(str(x) for x in T.BOX)},
        ttl_s=0,
    )
    want = set(re.findall(r"[a-z0-9]+", q.partition(",")[0].lower()))
    out = []
    for f in d.get("features") or []:
        p, (lon, lat) = f["properties"], f["geometry"]["coordinates"]
        if p.get("name"):
            out.append(
                {"name": p["name"], "lat": lat, "lon": lon, "addresstype": p.get("osm_value")}
            )
    # It lists near misses too: the name with the fewest other words first.
    out.sort(key=lambda h: len(set(re.findall(r"[a-z0-9]+", h["name"].lower())) ^ want))
    return out[:1]


def search(q: str) -> list[dict]:
    """The best match in the region ({name, lat, lon, addresstype}), or an empty list."""
    try:
        return cached(("OSM", q), DAY, lambda: _ask(q))
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return []


def _ask(q: str) -> list[dict]:
    wait = _turn()
    if wait is not None:
        time.sleep(wait)
        try:
            return _nominatim(q)
        except (httpx.HTTPError, ValueError):
            with _lock:
                _state["rest_until"] = time.monotonic() + REST_S
    return _photon(q)


def _tagged(q: str) -> list[dict]:
    wait = _turn()
    if wait is None:
        raise ValueError("Nominatim is resting or busy")
    time.sleep(wait)
    return get_json(
        NOMINATIM,
        {
            "q": q,
            "format": "jsonv2",
            "limit": 1,
            "extratags": 1,
            "viewbox": ",".join(str(x) for x in T.BOX),
            "bounded": 1,
        },
        ttl_s=0,
    )


def tagged(q: str) -> dict | None:
    """The best match in the region with its OpenStreetMap tags (opening_hours, website), or
    None. Only Nominatim gives the tags, so a busy turn gives None, not the spare."""
    try:
        hits = cached(("OSM tags", q), DAY, lambda: _tagged(q))
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None
    return hits[0] if hits else None
