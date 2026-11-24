"""The guide's tools as an MCP server over HTTP, for approved people only.

Each approved person has a token, written into the connector URL (`/mcp/<token>`), since that is
what Claude, ChatGPT and Cursor all accept. The token is checked on every request, and each tool
call counts against its hourly and daily limit before it runs. Only tools that answer from data
are offered: nothing here calls a language model or a paid search.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations

from ..tools import alerts, arrivals, citynow, live, nearby, trips
from ..tools.http import load_keys
from .store import Access

log = logging.getLogger(__name__)
load_keys()

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
mcp = MCPServer(
    name="nycynapse",
    title="NYCynapse: New York City, live",
    instructions=(
        "Live New York City data: subway and rail service changes, routes and travel times "
        "(transit with step-free checks and fares, car, bike, walk), road closures and events, "
        "weather and alerts, Citi Bike, public restrooms and fountains, place lookup and a few "
        "NYC Open Data datasets. Times are New York time. Usage limits apply during the beta."
    ),
)


@mcp.tool(annotations=READ)
def trip_plan(
    origin: str,
    destination: str,
    mode: str = "all",
    when: str | None = None,
    arrive_by: bool = False,
    step_free: bool = False,
) -> dict:
    """Routes and travel times between two places in and around NYC: transit itineraries from
    the agencies' schedules (subway, bus, LIRR, Metro-North, NJ Transit, PATH, ferries, the
    AirTrains) with fares, live times where published, step-free checks against MTA elevator
    outages when step_free is set, plus car (live traffic), bike and walking. `when` is New York
    time like 2026-10-16T17:30 or words like "tomorrow 8am"; arrive_by makes it a deadline."""
    return trips.trip_plan(origin, destination, mode, when, arrive_by, step_free).as_dict()


@mcp.tool(annotations=READ)
def subway_alerts(line: str | None = None, days: int = 0) -> dict:
    """MTA subway alerts in effect now, with the hours each applies, and planned changes in the
    next 12 hours, or the next `days` days (up to 7) for the weekend or later this week. `line`
    narrows to one line (A, 7, L...), or gives one bus route's alerts (M10, Bx12, Q58...)."""
    return live.subway_alerts(line, days).as_dict()


@mcp.tool(annotations=READ)
def transit_changes(system: str | None = None, route: str | None = None) -> dict:
    """Service changes on the subway, buses, LIRR, Metro-North, NJ Transit and PATH, ongoing or
    starting in the next 7 days. `system` is subway, bus, lirr, metro-north, nj-transit or path."""
    return live.transit_changes(system, route).as_dict()


@mcp.tool(annotations=READ)
def road_events(
    borough: str | None = None, kind: str | None = None, road: str | None = None
) -> dict:
    """Road closures, roadwork, incidents and planned events on NYC roads, active now or in the
    next 7 days. `kind` is closure, roadwork, incident or event; `road` narrows to one road,
    bridge or tunnel (GWB, BQE, I-95)."""
    return live.road_events(borough, kind, road).as_dict()


@mcp.tool(annotations=READ)
def road_speeds(borough: str | None = None) -> dict:
    """Latest road speeds from city sensors by borough, and the slowest segments."""
    return live.road_speeds(borough).as_dict()


@mcp.tool(annotations=READ)
def weather(hours: int = 6, days: int = 0, near: str | None = None, day: str | None = None) -> dict:
    """Weather now and for the next hours in each borough (and near a place), a daily forecast
    up to 7 days ahead when `days` is set, one day hour by hour with gusts when `day` is set
    (Thursday, 2026-11-26), and National Weather Service alerts."""
    return live.weather(hours, days, near, day).as_dict()


@mcp.tool(annotations=READ)
def citibike_now(near: str, n: int = 5) -> dict:
    """Bikes, e-bikes and open docks right now at the Citi Bike stations nearest a place."""
    return trips.citibike_now(near, n).as_dict()


@mcp.tool(annotations=READ)
def nearby_amenities(near: str, what: str = "restroom", n: int = 5) -> dict:
    """The nearest public restrooms (with hours, access and baby changing tables) or drinking
    fountains to a place, from city data. `what` is restroom or drinking_fountain."""
    return nearby.nearby(near, what, n).as_dict()


@mcp.tool(annotations=READ)
def alerts_near(near: str, radius_m: int = 1000, since_minutes: int | None = None) -> dict:
    """Road incidents and closures, subway station alerts and weather warnings in effect right
    now near a place, closest first. since_minutes limits to alerts posted in the last that many
    minutes, for "anything new near me?"."""
    return alerts.alerts_near(near, radius_m, since_minutes).as_dict()


@mcp.tool(annotations=READ)
def street_closures(near: str, radius_m: int = 500, days_ahead: int = 7) -> dict:
    """Streets closed for construction near a place, in effect now or starting within days_ahead
    days, from NYC DOT permits."""
    return citynow.street_closures(near, radius_m, days_ahead).as_dict()


@mcp.tool(annotations=READ)
def air_quality(near: str | None = None) -> dict:
    """Air quality now and for the next hours, citywide or near a place: the US AQI with what it
    means for being outdoors, fine particles (PM2.5) and ozone."""
    return citynow.air_quality(near).as_dict()


@mcp.tool(annotations=READ)
def next_trains(station: str, line: str | None = None, n: int = 3) -> dict:
    """The next trains at a subway station right now, by line and direction (where each train is
    headed), with minutes away and clock times, live from the MTA's real-time feeds."""
    return arrivals.next_trains(station, line, n).as_dict()


@mcp.tool(annotations=READ)
def find_place(text: str) -> dict:
    """Find an NYC address, landmark, station or intersection: coordinates, borough and
    neighborhood."""
    return live.find_place(text).as_dict()


@mcp.tool(annotations=READ)
def open_data(
    dataset: str,
    select: str = "",
    where: str = "",
    group: str = "",
    order: str = "",
    limit: int = 20,
    area: str = "",
) -> dict:
    """Query one NYC Open Data dataset with SoQL clauses: 311, collisions, events, film_permits,
    restaurant_inspections, crime, farmers_markets. `area` limits 311, collisions, crime and
    restaurant_inspections to a neighborhood or ZIP code by the city's outlines."""
    return live.open_data(dataset, select, where, group, order, limit, area).as_dict()


def build(access: Access, hosts: list[str]):
    inner = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=[]
        ),
    )
    return TokenGate(inner, access), inner


class TokenGate:
    """In front of the MCP app: `/mcp/<token>` must carry an approved token, and every tool call
    in a request is counted against that token's limits before the request goes in."""

    def __init__(self, inner, access: Access):
        self.inner = inner
        self.access = access
        self._seen: dict[str, tuple[float, object]] = {}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        parts = scope["path"].split("/")
        if len(parts) < 3 or parts[1] != "mcp" or not parts[2]:
            return await _json(send, 404, {"error": "use /mcp/<your token>"})
        token = parts[2]
        grant = await self._grant(token)
        if grant is None:
            return await _json(send, 401, {"error": "unknown or revoked token"})
        body = b""
        more = True
        while more:
            msg = await receive()
            body += msg.get("body", b"")
            more = msg.get("more_body", False)
            if len(body) > 1_000_000:
                return await _json(send, 413, {"error": "request too large"})
        refused = await self._count(grant, body) if scope["method"] == "POST" else None
        if refused:
            return await _json(send, 200, refused)
        sent = False

        async def replay():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        inner_scope = {**scope, "path": "/mcp", "raw_path": b"/mcp"}
        await self.inner(inner_scope, replay, send)

    async def _grant(self, token: str):
        hit = self._seen.get(token)
        if hit and time.monotonic() - hit[0] < 60:
            return hit[1]
        g = await asyncio.to_thread(self.access.grant, token)
        self._seen[token] = (time.monotonic(), g)
        if len(self._seen) > 2000:
            self._seen.clear()
        return g

    async def _count(self, grant, body: bytes):
        try:
            msg = json.loads(body or b"null")
        except ValueError:
            return None
        calls = [m for m in (msg if isinstance(msg, list) else [msg]) if isinstance(m, dict)]
        for m in calls:
            if m.get("method") != "tools/call":
                continue
            why = await asyncio.to_thread(self.access.spend, grant)
            if why:
                log.info("refused a call for token %s: %s", grant.request_id[:8], why)
                text = f"Usage limits apply during the beta: {why}."
                return {
                    "jsonrpc": "2.0",
                    "id": m.get("id"),
                    "result": {"content": [{"type": "text", "text": text}], "isError": True},
                }
        return None


async def _json(send, status: int, payload: dict):
    data = json.dumps(payload).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(data)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": data})
