"""`nycynapse-connect`: the public MCP endpoint, the access request page and the owner's admin
page, as one small service apart from the guide's site.

Routes (under the public prefix, /nycynapse by default):
  /mcp/<token>             MCP over HTTP for approved tokens
  /connect                 ask for access
  /connect/status/<key>    a requester's private page: pending, approved (token shown once), ...
  /connect/admin           the owner's page, behind a password
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import html
import logging
import os
import re
import secrets
import time
from zoneinfo import ZoneInfo

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from ..catalog import store as catalog_store
from ..config import Settings
from .server import build, mcp
from .store import Access

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")
PREFIX = os.environ.get("NYCYNAPSE_PUBLIC_PREFIX", "/nycynapse").rstrip("/")
PUBLIC = os.environ.get("NYCYNAPSE_PUBLIC_BASE", "https://api.giriworks.com").rstrip("/")
HOSTS = [
    h.strip()
    for h in os.environ.get("NYCYNAPSE_MCP_HOSTS", "api.giriworks.com,127.0.0.1:8034").split(",")
]
ADMIN_HASH = os.environ.get("NYCYNAPSE_ADMIN_HASH", "")
SESSION_KEY = os.environ.get("NYCYNAPSE_SESSION_KEY", "") or secrets.token_hex(32)
SESSION_S = 12 * 3600
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[a-z]{2,}$", re.I)

_conn = None


def _db():
    global _conn
    if _conn is None or _conn.closed:
        _conn = catalog_store.connect(Settings.from_env().app_pg_dsn)
    return _conn


access = Access(_db)
gate, mcp_inner = build(access, HOSTS)


# ---------------------------------------------------------------------------------- helpers


def _client(request: Request) -> str:
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "?")
    return hashlib.sha256(f"connect:{ip}".encode()).hexdigest()[:16]


def _when(t) -> str:
    return t.astimezone(NY).strftime("%b %-d, %-I:%M %p") if t else ""


CSS = """
:root { color-scheme: dark; --bg:#0b0b0b; --card:#151514; --ink:#f4f3ee; --muted:#a9a89f;
  --rule:#2c2c2a; --accent:#2a78d6; --good:#2fd18d; --warn:#e07a3d; --bad:#e66767; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
  font: 16px/1.55 "Helvetica Neue", Helvetica, Inter, Arial, sans-serif; }
.wrap { max-width: 880px; margin: 0 auto; padding: 40px 16px 64px; }
a { color: #8ab4f8; }
.brand { display:flex; align-items:center; gap:10px; font-weight:800; font-size:20px; margin-bottom:28px; }
a.brand { color:inherit; text-decoration:none; }
.card label:first-of-type { margin-top:0; }
.brand i { display:grid; place-items:center; width:30px; height:30px; border-radius:50%;
  background:var(--accent); font-style:normal; font-size:15px; }
h1 { font-size: clamp(28px, 4vw, 40px); line-height:1.1; margin: 0 0 12px; letter-spacing:-.02em; }
h2 { font-size: 20px; margin: 32px 0 10px; }
p.lede { color:#cfcec6; font-size:18px; margin:0 0 24px; }
.card { background:var(--card); border:1px solid var(--rule); border-radius:16px; padding:20px; margin: 16px 0; }
label { display:block; font-size:13px; color:var(--muted); margin: 14px 0 6px; font-weight:700;
  letter-spacing:.04em; text-transform:uppercase; }
input, textarea { width:100%; background:#0e0e0d; color:var(--ink); border:1px solid #3d3d39;
  border-radius:10px; padding:11px 12px; font:inherit; }
textarea { min-height: 90px; resize: vertical; }
input:focus, textarea:focus { outline:none; border-color:var(--accent); box-shadow:0 0 0 3px #183a63; }
button, .btn { display:inline-block; background:#fff; color:#0b0b0b; border:0; border-radius:10px;
  padding:10px 18px; font-weight:700; font-size:15px; cursor:pointer; text-decoration:none; }
button.ghost { background:transparent; color:var(--ink); border:1px solid #3d3d39; }
button.bad { background:var(--bad); color:#fff; }
.muted { color:var(--muted); font-size:14px; }
code, pre { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 14px; }
pre { background:#0e0e0d; border:1px solid var(--rule); border-radius:10px; padding:14px; overflow-x:auto; white-space:pre-wrap; word-break:break-all; }
.pill { display:inline-block; border-radius:999px; padding:2px 10px; font-size:12px; font-weight:700; }
.pending { background:#3a2e12; color:#eda100; } .approved { background:#123a28; color:var(--good); }
.denied, .revoked { background:#3a1616; color:var(--bad); }
table { width:100%; border-collapse: collapse; font-size:14px; }
td, th { text-align:left; padding:10px 8px; border-bottom:1px solid var(--rule); vertical-align:top; }
th { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.05em; }
.row-actions { display:flex; gap:6px; flex-wrap:wrap; }
.row-actions input { width: 72px; padding:6px 8px; }
.stat { display:inline-block; margin-right:22px; } .stat b { font-size:24px; display:block; }
.hp { position:absolute; left:-9999px; }
@media (max-width: 640px) { table, thead, tbody, tr, td { display:block; } th { display:none; } td { border:0; padding:4px 0; } tr { border-bottom:1px solid var(--rule); padding:10px 0; display:block; } }
"""


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = (
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<meta name='robots' content='noindex'><title>{html.escape(title)}</title>"
        f"<style>{CSS}</style></head><body><div class='wrap'>"
        f"<a class='brand' href='https://giriworks.com/nycynapse/'><i>N</i>NYCynapse</a>{body}</div></body></html>"
    )
    r = HTMLResponse(doc, status_code=status)
    r.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    )
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["Referrer-Policy"] = "no-referrer"
    r.headers["Cache-Control"] = "no-store"
    return r


e = html.escape


def connector(token: str) -> str:
    return f"{PUBLIC}{PREFIX}/mcp/{token}"


TOOLS_TEXT = (
    "trip planning (transit with fares and step-free checks, car, bike, walk), the next trains at "
    "a station, subway and rail service changes, incidents and street closures near a place, air "
    "quality, road closures and events, live road speeds, weather and alerts, Citi Bike "
    "right now, public restrooms and fountains, place lookup, and NYC Open Data"
)


# ------------------------------------------------------------------------------- request page


async def ask_page(request: Request):
    if request.method == "GET":
        return page("Connect to NYCynapse", _ask_form())
    form = await request.form()
    if form.get("website"):  # a field people never see: bots fill it
        return page("Thanks", "<h1>Thanks.</h1>")
    name = " ".join(str(form.get("name", "")).split())[:80]
    email = str(form.get("email", "")).strip()[:200]
    purpose = " ".join(str(form.get("purpose", "")).split())[:600]
    problems = []
    if len(name) < 2:
        problems.append("your name")
    if not EMAIL.match(email):
        problems.append("a valid email")
    if len(purpose) < 10:
        problems.append("a sentence on what you will use it for")
    if problems:
        return page(
            "Connect to NYCynapse",
            _ask_form("Please add " + ", ".join(problems) + ".", name, email, purpose),
            400,
        )
    client = _client(request)
    if await asyncio.to_thread(access.asked_today, client) >= 3:
        return page(
            "Connect to NYCynapse",
            _ask_form("That's several requests today already. Please try again tomorrow."),
            429,
        )
    _, key = await asyncio.to_thread(access.ask, name, email, purpose, client)
    link = f"{PUBLIC}{PREFIX}/connect/status/{key}"
    return page(
        "Request received",
        f"<h1>Request received.</h1><p class='lede'>Thanks, {e(name)}. Requests are reviewed by hand.</p>"
        f"<div class='card'><b>Save this private link.</b> It's the only way to see your request's "
        f"status and, once approved, your connector address. It is not emailed.<pre>{e(link)}</pre>"
        f"<a class='btn' href='{e(link)}'>Open my status page</a></div>",
    )


def _ask_form(error: str = "", name: str = "", email: str = "", purpose: str = "") -> str:
    err = f"<p style='color:var(--warn)'>{e(error)}</p>" if error else ""
    return (
        "<h1>Use NYCynapse in your own AI app.</h1>"
        "<p class='lede'>Connect Claude, ChatGPT, Cursor or any MCP client to live New York City "
        "data. Your app's own model does the thinking; NYCynapse supplies the facts.</p>"
        f"<p class='muted'>Tools: {TOOLS_TEXT}. "
        f"<a href='{PREFIX}/connect/docs'>Read the connector guide</a>.</p>"
        "<div class='card'><form method='post'>"
        f"{err}<label for='n'>Name</label><input id='n' name='name' maxlength='80' value='{e(name)}' required>"
        f"<label for='m'>Email</label><input id='m' name='email' type='email' maxlength='200' value='{e(email)}' required>"
        f"<label for='p'>What will you use it for?</label><textarea id='p' name='purpose' maxlength='600' required>{e(purpose)}</textarea>"
        "<input class='hp' name='website' tabindex='-1' autocomplete='off' aria-hidden='true'>"
        "<p class='muted'>Access is reviewed by hand. Usage limits apply during the beta. Your "
        "email is used only to identify your request and is never shared.</p>"
        "<button type='submit'>Request access</button></form></div>"
    )


async def status_page(request: Request):
    key = request.path_params["key"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,40}", key):
        return page("Not found", "<h1>Not found.</h1>", 404)
    row = await asyncio.to_thread(access.status, key)
    if not row:
        return page("Not found", "<h1>That status link was not found.</h1>", 404)
    st = row["status"]
    head = f"<h1>Hi {e(row['name'])}.</h1><p>Status: <span class='pill {st}'>{st}</span></p>"
    if st == "pending":
        body = "<p class='lede'>Your request is waiting for review. Check back on this page.</p>"
    elif st == "approved" and row["token_once"]:
        url = connector(row["token_once"])
        body = (
            "<p class='lede'>You're approved. Here is your personal connector address. "
            "<b>It is shown only this once</b>; copy it now and keep it private.</p>"
            f"<div class='card'><pre>{e(url)}</pre></div>"
            + _howto(url)
            + f"<p class='muted'>Limits during the beta: {row['per_hour']} tool calls an hour, "
            f"{row['per_day']} a day.</p>"
        )
    elif st == "approved":
        body = (
            "<p class='lede'>You're approved. Your connector address was shown the first time "
            f"you opened this page after approval (it ends in <code>…{e(row['token_hint'] or '')}</code>). "
            "If you lost it, send a new request and mention it.</p>"
            f"<p class='muted'>Limits during the beta: {row['per_hour']} tool calls an hour, "
            f"{row['per_day']} a day.</p>" + _howto(connector("<your token>"))
        )
    else:
        body = "<p class='lede'>This request isn't active.</p>"
    return page("Your NYCynapse access", head + body)


def _howto(url: str) -> str:
    cfg = '{\n  "mcpServers": {\n    "nycynapse": { "url": "' + url + '" }\n  }\n}'
    return (
        "<h2>Connect it</h2>"
        "<p><b>Claude (web or desktop):</b> Settings, Connectors, Add custom connector, paste the "
        "address.</p><p><b>ChatGPT:</b> Settings, Connectors (developer mode), Create, paste the "
        "address.</p><p><b>Cursor, Claude Code or any MCP client</b> that reads a config file:</p>"
        f"<pre>{e(cfg)}</pre><p class='muted'>Then ask things like \"Is the L running normally "
        'right now?" or "Step-free way from Penn Station to the Met tomorrow at 10?"</p>'
    )


# ---------------------------------------------------------------------------------- docs page


async def docs_page(request: Request):
    """How to connect, and every tool as the server itself describes it."""
    rows = []
    for t in sorted(await mcp.list_tools(), key=lambda t: t.name):
        schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}
        need = set(schema.get("required") or [])
        args = ", ".join(
            f"<code>{e(k)}</code>" + ("" if k in need else " (optional)")
            for k in schema.get("properties") or {}
        )
        text = " ".join((t.description or "").split())
        rows.append(
            f"<div class='card'><b><code>{e(t.name)}</code></b><p>{e(text)}</p>"
            f"<p class='muted'>Takes: {args or 'nothing'}</p></div>"
        )
    body = (
        "<h1>Connector guide</h1><p class='lede'>Live New York City data as MCP tools for your own "
        "app. Your app's model does the thinking; every tool is read-only.</p>"
        "<h2>Get access</h2><p>Ask on the <a href='"
        f"{PREFIX}/connect'>request page</a>. Requests are "
        "reviewed by hand. Once approved you get a personal address, shown one time. Keep it "
        "private: anyone who has it uses your limits.</p>"
        + _howto(connector("YOUR-TOKEN"))
        + "<h2>Limits</h2><p>Usage limits apply during the beta, per hour and per day, and are "
        "shown on your status page. A call past the limit returns an error that says so.</p>"
        "<h2>What to know</h2><p>Times are New York time. Results carry a short summary, the data "
        "and the sources with the time of the reading. Feeds can lag and plans change, so show "
        "the time of the reading to your readers.</p>"
        f"<h2>Tools</h2>{''.join(rows)}"
    )
    return page("NYCynapse connector guide", body)


# ---------------------------------------------------------------------------------- admin page

_tries: dict[str, list[float]] = {}


def _check_password(pw: str) -> bool:
    try:
        algo, salt, want = ADMIN_HASH.split("$")
        assert algo == "scrypt"
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1).hex()
        return hmac.compare_digest(got, want)
    except Exception:  # noqa: BLE001 - no or bad hash configured: nobody gets in
        return False


def _session(request: Request) -> bool:
    raw = request.cookies.get("nyc_admin", "")
    try:
        exp, sig = raw.split(".")
        good = hmac.compare_digest(
            sig, hmac.new(SESSION_KEY.encode(), exp.encode(), "sha256").hexdigest()
        )
        return good and int(exp) > time.time()
    except ValueError:
        return False


def _cookie(resp: Response, value: str, age: int) -> None:
    resp.set_cookie(
        "nyc_admin",
        value,
        max_age=age,
        path=f"{PREFIX}/connect/admin",
        secure=True,
        httponly=True,
        samesite="strict",
    )


async def admin(request: Request):
    if request.method == "POST" and request.url.path.endswith("/login"):
        client = _client(request)
        recent = [t for t in _tries.get(client, []) if time.time() - t < 900]
        if len(recent) >= 5:
            return page("Admin", _login("Too many attempts. Wait 15 minutes."), 429)
        form = await request.form()
        if not _check_password(str(form.get("password", ""))):
            _tries[client] = recent + [time.time()]
            await asyncio.sleep(1)
            return page("Admin", _login("Wrong password."), 401)
        _tries.pop(client, None)
        exp = str(int(time.time()) + SESSION_S)
        sig = hmac.new(SESSION_KEY.encode(), exp.encode(), "sha256").hexdigest()
        resp = RedirectResponse(f"{PREFIX}/connect/admin", status_code=303)
        _cookie(resp, f"{exp}.{sig}", SESSION_S)
        return resp
    if not _session(request):
        return page("Admin", _login())
    if request.method == "POST":
        form = await request.form()
        act, rid = str(form.get("act", "")), str(form.get("id", ""))
        hour = max(1, min(int(form.get("per_hour") or 30), 1000))
        day = max(1, min(int(form.get("per_day") or 200), 20000))
        if act == "approve":
            await asyncio.to_thread(access.approve, rid, hour, day)
        elif act in ("deny", "revoke"):
            await asyncio.to_thread(
                access.set_status, rid, "denied" if act == "deny" else "revoked"
            )
        elif act == "limits":
            await asyncio.to_thread(
                access.set_limits, rid, hour, day, str(form.get("note", ""))[:300] or None
            )
        elif act == "delete":
            await asyncio.to_thread(access.delete, rid)
        elif act == "logout":
            resp = RedirectResponse(f"{PREFIX}/connect/admin", status_code=303)
            _cookie(resp, "", 0)
            return resp
        return RedirectResponse(f"{PREFIX}/connect/admin", status_code=303)
    if request.query_params.get("view") == "ratings":
        return page(
            "NYCynapse admin", _tabs("ratings") + _ratings(await asyncio.to_thread(access.ratings))
        )
    rows = await asyncio.to_thread(access.all)
    return page("NYCynapse admin", _tabs("access") + _dashboard(rows))


def _tabs(on: str) -> str:
    def tab(key: str, label: str, href: str) -> str:
        style = (
            "background:#fff;color:#0b0b0b"
            if key == on
            else "background:transparent;color:var(--ink);border:1px solid #3d3d39"
        )
        return f"<a class='btn' style='{style};margin-right:8px;padding:6px 14px' href='{href}'>{label}</a>"

    base = f"{PREFIX}/connect/admin"
    return (
        "<div style='margin-bottom:18px'>"
        + tab("access", "Access requests", base)
        + tab("ratings", "Answer ratings", base + "?view=ratings")
        + "</div>"
    )


def _ratings(rows: list[dict]) -> str:
    down = sum(1 for r in rows if r["rating"] < 0)
    up = len(rows) - down
    head = (
        "<h1>Answer ratings</h1><p class='muted'>What readers said about the guide's answers, "
        '"wrong" first. Kept with the conversation for 30 days.</p>'
        f"<div class='card'><span class='stat'><b>{down}</b>marked wrong</span>"
        f"<span class='stat'><b>{up}</b>marked right</span></div>"
    )
    items = []
    for r in rows:
        tools = ", ".join(
            s["tool"] for s in (r["steps"] or []) if isinstance(s, dict) and s.get("tool")
        )
        verdict = (
            "<span class='pill denied'>wrong</span>"
            if r["rating"] < 0
            else "<span class='pill approved'>right</span>"
        )
        note = f"<p><b>Reader's note:</b> {e(r['rating_note'])}</p>" if r.get("rating_note") else ""
        secs = f"{round((r['latency_ms'] or 0) / 1000)} s" if r.get("latency_ms") else ""
        items.append(
            f"<div class='card'>{verdict} <span class='muted'>{_when(r['rated_at'])} · {e(r['kind'])} · "
            f"{e(r['model'] or '')} · {secs}</span><p><b>Q:</b> {e(r['question'])}</p>{note}"
            f"<details><summary class='muted'>Answer and tools</summary><p style='white-space:pre-wrap'>"
            f"{e(r['answer'] or '')}</p><p class='muted'>Tools: {e(tools) or 'none'}</p></details></div>"
        )
    return head + ("".join(items) or "<div class='card muted'>No ratings yet.</div>")


def _login(error: str = "") -> str:
    err = f"<p style='color:var(--warn)'>{e(error)}</p>" if error else ""
    return (
        "<h1>Admin</h1><div class='card'>"
        f"<form method='post' action='{PREFIX}/connect/admin/login'>{err}"
        "<label for='pw'>Password</label><input id='pw' name='password' type='password' "
        "autocomplete='current-password' required autofocus>"
        "<p><button type='submit'>Sign in</button></p></form></div>"
    )


def _dashboard(rows: list[dict]) -> str:
    n = {
        s: sum(1 for r in rows if r["status"] == s)
        for s in ("pending", "approved", "denied", "revoked")
    }
    today = sum(int(r["calls_day"]) for r in rows)
    stats = (
        f"<div class='card'><span class='stat'><b>{n['pending']}</b>waiting</span>"
        f"<span class='stat'><b>{n['approved']}</b>approved</span>"
        f"<span class='stat'><b>{today}</b>tool calls in 24 h</span>"
        f"<span class='stat'><b>{n['denied'] + n['revoked']}</b>denied or revoked</span></div>"
    )
    body = []
    for r in rows:
        st = r["status"]
        hidden = f"<input type='hidden' name='id' value='{e(str(r['id']))}'>"
        limits = (
            f"<input name='per_hour' value='{r['per_hour']}' title='calls per hour' aria-label='calls per hour'>"
            f"<input name='per_day' value='{r['per_day']}' title='calls per day' aria-label='calls per day'>"
        )
        if st == "pending" or st in ("denied", "revoked"):
            acts = (
                f"<form method='post' class='row-actions'>{hidden}{limits}"
                "<button name='act' value='approve'>Approve</button>"
                + (
                    "<button class='ghost' name='act' value='deny'>Deny</button>"
                    if st == "pending"
                    else ""
                )
                + "<button class='ghost' name='act' value='delete'>Delete</button></form>"
            )
        else:
            acts = (
                f"<form method='post' class='row-actions'>{hidden}{limits}"
                "<button class='ghost' name='act' value='limits'>Save limits</button>"
                "<button class='bad' name='act' value='revoke'>Revoke</button></form>"
            )
        used = f"{r['calls_day']} today, {r['calls_all']} total" if st == "approved" else ""
        body.append(
            f"<tr><td><b>{e(r['name'])}</b><br><span class='muted'>{e(r['email'])}</span></td>"
            f"<td>{e(r['purpose'])}</td>"
            f"<td><span class='pill {st}'>{st}</span><br><span class='muted'>{_when(r['created_at'])}"
            f"{'<br>token …' + e(r['token_hint']) if r['token_hint'] and st == 'approved' else ''}"
            f"<br>{used}</span></td><td>{acts}</td></tr>"
        )
    table = (
        "<table><thead><tr><th>Who</th><th>Use</th><th>Status</th><th>Actions (per hour, per day)</th>"
        "</tr></thead><tbody>"
        + ("".join(body) or "<tr><td colspan='4' class='muted'>No requests yet.</td></tr>")
        + "</tbody></table>"
    )
    out = (
        "<form method='post' style='float:right'><button class='ghost' name='act' value='logout'>Sign out</button></form>"
        "<h1>Access requests</h1>"
        f"<p class='muted'>Sign-up page: <a href='{PREFIX}/connect'>{e(PUBLIC + PREFIX)}/connect</a></p>"
    )
    return out + stats + f"<div class='card'>{table}</div>"


# ------------------------------------------------------------------------- alerts near a spot

WATCH_ORIGINS = {"https://giriworks.com", "https://www.giriworks.com"}
_watch_hits: dict[str, list[float]] = {}


async def watch_api(request: Request):
    """The guide's page subscribes a browser to push alerts near a spot (or stops them)."""
    origin = request.headers.get("origin", "")
    cors = (
        {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "content-type",
            "Access-Control-Max-Age": "600",
            "Vary": "Origin",
        }
        if origin in WATCH_ORIGINS
        else {}
    )
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=cors)
    if request.method == "GET":
        return JSONResponse({"key": os.environ.get("NYCYNAPSE_VAPID_PUBLIC", "")}, headers=cors)
    if not cors:
        return JSONResponse({"ok": False}, status_code=403)
    client = _client(request)
    recent = [t for t in _watch_hits.get(client, []) if time.time() - t < 3600]
    if len(recent) >= 20:
        return JSONResponse(
            {"ok": False, "error": "too many changes"}, status_code=429, headers=cors
        )
    _watch_hits[client] = recent + [time.time()]
    try:
        data = await request.json()
    except ValueError:
        return JSONResponse({"ok": False}, status_code=400, headers=cors)
    from . import push

    sub = data.get("subscription") or {}
    if data.get("action") == "stop":
        await asyncio.to_thread(push.unwatch, str(sub.get("endpoint", "")))
        return JSONResponse({"ok": True}, headers=cors)
    try:
        lat, lon = float(data.get("lat")), float(data.get("lon"))
        radius = int(data.get("radius_m") or 1000)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False}, status_code=400, headers=cors)
    ok = await asyncio.to_thread(push.watch, sub, lat, lon, radius)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 400, headers=cors)


# ------------------------------------------------------------------------------------- the app


@contextlib.asynccontextmanager
async def lifespan(app):
    try:
        catalog_store.migrate(_db())
    except Exception:  # noqa: BLE001
        log.exception("could not apply migrations")
    async with mcp_inner.router.lifespan_context(mcp_inner):
        yield


pages = Starlette(
    routes=[
        Route("/connect", ask_page, methods=["GET", "POST"]),
        Route("/connect/docs", docs_page),
        Route("/connect/status/{key}", status_page),
        Route("/connect/admin", admin, methods=["GET", "POST"]),
        Route("/connect/admin/login", admin, methods=["POST"]),
        Route("/connect/watch", watch_api, methods=["GET", "POST", "OPTIONS"]),
    ],
    lifespan=lifespan,
)


async def app(scope, receive, send):
    if scope["type"] == "http" and scope["path"].startswith("/mcp"):
        return await gate(scope, receive, send)
    return await pages(scope, receive, send)


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ.get("NYCYNAPSE_CONNECT_PORT", "8034")),
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        access_log=False,
    )


if __name__ == "__main__":
    main()
