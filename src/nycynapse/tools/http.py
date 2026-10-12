"""HTTP for the tools: one shared client, a small time-limited cache, and a fetch that cannot
reach private networks.

Pages named by a model or found by a search are untrusted. `safe_get` only allows http and
https on ports 80 and 443, resolves the host itself, refuses any address that is not public
(loopback, private ranges, link-local, cloud metadata, multicast), connects to the address it
checked so a second DNS answer cannot point somewhere else, checks every redirect the same way,
and stops reading after a size limit.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import threading
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

UA = "NYCynapse/1.0 (+https://giriworks.com/nycynapse; NYC guide)"
TIMEOUT = httpx.Timeout(12.0, connect=6.0)
MAX_BYTES = 1_500_000
MAX_REDIRECTS = 3
TEXT_TYPES = (
    "text/html",
    "text/plain",
    "application/xhtml+xml",
    "application/json",
    "application/xml",
    "text/xml",
)

_client = httpx.Client(timeout=TIMEOUT, headers={"User-Agent": UA}, follow_redirects=False)
_cache: dict[tuple, tuple[float, object]] = {}
_cache_lock = threading.Lock()


class BlockedURL(Exception):  # noqa: N818
    pass


def env(name: str) -> str:
    """Tool keys live in their own file, read once into the environment at startup."""
    return os.environ.get(name, "").strip()


def load_keys(path: str = "/root/.secrets/nycynapse-tools.env") -> None:
    if not os.path.isfile(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def cached(key: tuple, ttl_s: float, make):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl_s:
            return hit[1]
    value = make()
    with _cache_lock:
        if len(_cache) > 500:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
                _cache.pop(k, None)
        _cache[key] = (now, value)
    return value


def get_json(
    url: str, params: dict | None = None, headers: dict | None = None, ttl_s: float = 60
) -> object:
    """GET a known API (not user-supplied URLs) and parse JSON, with caching."""

    def make():
        r = _client.get(url, params=params, headers=headers)
        r.raise_for_status()
        return r.json()

    key = ("GET", url, tuple(sorted((params or {}).items())))
    return cached(key, ttl_s, make)


def post_json(url: str, body: dict, headers: dict | None = None, ttl_s: float = 300) -> object:
    def make():
        r = _client.post(url, json=body, headers=headers)
        r.raise_for_status()
        return r.json()

    key = ("POST", url, repr(sorted(body.items())))
    return cached(key, ttl_s, make)


def public_address(host: str, port: int) -> str:
    """Resolve a host and return one public address for it, or raise BlockedURL."""
    if (
        not host
        or host.lower() in ("localhost",)
        or host.lower().endswith((".local", ".internal", ".localhost"))
    ):
        raise BlockedURL(f"host not allowed: {host}")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise BlockedURL(f"cannot resolve {host}") from e
    addrs = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            raise BlockedURL(f"{host} resolves to a non-public address")
        addrs.append(str(ip))
    if not addrs:
        raise BlockedURL(f"cannot resolve {host}")
    return addrs[0]


def check_url(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedURL("only http and https links can be read")
    if parts.username or parts.password:
        raise BlockedURL("links with credentials are not allowed")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in (80, 443):
        raise BlockedURL("only ports 80 and 443 are allowed")
    host = parts.hostname or ""
    return host, public_address(host, port), port


def safe_get(url: str) -> tuple[str, str, str]:
    """Fetch an untrusted URL. Returns (final url, content type, text)."""
    for _ in range(MAX_REDIRECTS + 1):
        host, ip, port = check_url(url)
        parts = urlsplit(url)
        netloc = f"[{ip}]" if ":" in ip else ip
        if parts.port:
            netloc += f":{port}"
        direct = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
        headers = {
            "Host": host if not parts.port else f"{host}:{port}",
            "User-Agent": UA,
            "Accept": "text/html,text/plain;q=0.9,*/*;q=0.5",
        }
        with _client.stream("GET", direct, headers=headers, extensions={"sni_hostname": host}) as r:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = urljoin(url, r.headers["location"])
                continue
            if r.status_code >= 400:
                raise BlockedURL(f"the page returned HTTP {r.status_code}")
            ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype and not ctype.startswith(TEXT_TYPES):
                raise BlockedURL(f"not a text page ({ctype})")
            body = bytearray()
            for chunk in r.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    break
            text = bytes(body[:MAX_BYTES]).decode(r.encoding or "utf-8", errors="replace")
            return url, ctype, text
    raise BlockedURL("too many redirects")
