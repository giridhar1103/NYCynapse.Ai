"""How much of a bike ride runs on protected paths, painted lanes or plain streets.

The bike planner picks its own route, and a reader who "doesn't love riding with cars" wants to
know what that route is like. NYC DOT's bike route map (mzxg-pwib) has every current segment with
its class: I is a protected lane or greenway, II a painted lane, III a shared street with signs,
L a link. bikelanes.tsv.gz keeps class, street and points every 20 m along each segment;
`nycynapse export-bikelanes` writes it again.
"""

import gzip
import math
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

FILE = Path(__file__).with_name("bikelanes.tsv.gz")
DATASET = "mzxg-pwib"
PAGE = "https://data.cityofnewyork.us/d/mzxg-pwib"
STEP_M = 20  # points along a lane, and along a ride
NEAR_M = 15  # a ride point this close to a lane point is on that lane
CELL = 0.0005  # grid cell in degrees, about 55 m north to south
NAMES = {"I": "protected", "II": "painted", "III": "shared", "L": "protected"}


def _metres(a: tuple, b: tuple) -> float:
    dy = (a[0] - b[0]) * 111_200
    dx = (a[1] - b[1]) * 84_300
    return math.hypot(dx, dy)


def _dense(points: list[tuple], step: float = STEP_M) -> list[tuple]:
    """Points every `step` metres along a line of (lat, lon)."""
    out = points[:1]
    for a, b in zip(points, points[1:], strict=False):
        d = _metres(a, b)
        n = int(d // step)
        out += [
            (a[0] + (b[0] - a[0]) * k / (n + 1), a[1] + (b[1] - a[1]) * k / (n + 1))
            for k in range(1, n + 1)
        ]
        out.append(b)
    return out


def _street(name: str) -> str:
    words = (name or "").title().split()
    fix = {"Av": "Av", "St": "St", "Br": "Bridge", "Gwy": "Greenway", "Pk": "Park"}
    return " ".join(fix.get(w, w) for w in words)


def export(fetch) -> str:
    """Write bikelanes.tsv.gz from the city's current bike routes. `fetch(offset, limit)` returns
    rows with the_geom, street and facilitycl."""
    rows, offset = [], 0
    while True:
        page = fetch(offset, 5000)
        if not page:
            break
        for r in page:
            if r.get("status", "Current") != "Current" or r.get("facilitycl") not in NAMES:
                continue
            geom = r.get("the_geom") or {}
            for line in geom.get("coordinates") or []:
                pts = _dense([(lat, lon) for lon, lat in line])
                text = ";".join(f"{lat:.5f},{lon:.5f}" for lat, lon in pts)
                rows.append(f"{r['facilitycl']}\t{_street(r.get('street', ''))}\t{text}")
        offset += len(page)
    with gzip.open(FILE, "wt") as f:
        f.write("\n".join(rows) + "\n")
    return f"{len(rows)} bike route lines written to {FILE}"


@lru_cache(maxsize=1)
def _grid() -> dict[tuple[int, int], list[tuple[float, float, str, str]]]:
    grid: dict[tuple[int, int], list] = defaultdict(list)
    if not FILE.exists():
        return grid
    with gzip.open(FILE, "rt") as f:
        for line in f:
            cls, street, text = line.rstrip("\n").split("\t")
            for pair in text.split(";"):
                lat, lon = map(float, pair.split(","))
                grid[(int(lat / CELL), int(lon / CELL))].append((lat, lon, cls, street))
    return grid


def _lane_at(p: tuple) -> tuple[str, str] | None:
    """The best lane within NEAR_M of a point: a protected one over a painted one."""
    grid = _grid()
    ci, cj = int(p[0] / CELL), int(p[1] / CELL)
    best = None
    for i in (ci - 1, ci, ci + 1):
        for j in (cj - 1, cj, cj + 1):
            for lat, lon, cls, street in grid.get((i, j), ()):
                if _metres(p, (lat, lon)) <= NEAR_M:
                    rank = {"I": 0, "L": 0, "II": 1, "III": 2}[cls]
                    if best is None or rank < best[0]:
                        best = (rank, cls, street)
    return (best[1], best[2]) if best else None


def along(coords: list[list[float]]) -> dict | None:
    """Shares of a ride ([lon, lat] points) on each kind of bike route, and the protected streets
    it uses most. None without the file or a ride."""
    if not coords or len(coords) < 2 or not _grid():
        return None
    pts = _dense([(lat, lon) for lon, lat in coords])
    kinds: dict[str, int] = defaultdict(int)
    streets: dict[str, int] = defaultdict(int)
    for p in pts:
        hit = _lane_at(p)
        kind = NAMES[hit[0]] if hit else "none"
        kinds[kind] += 1
        if hit and kind == "protected" and hit[1]:
            streets[hit[1]] += 1
    total = sum(kinds.values())
    share = {
        k: round(100 * kinds.get(k, 0) / total) for k in ("protected", "painted", "shared", "none")
    }
    main = [s for s, n in sorted(streets.items(), key=lambda x: -x[1]) if n * STEP_M >= 300][:4]
    return {**share, "protected_streets": main}


def describe(found: dict) -> str:
    text = f"Bike routes along it (city bike map): {found['protected']}% protected lane or greenway"
    if found["protected_streets"]:
        text += f" ({', '.join(found['protected_streets'])})"
    return (
        text + f", {found['painted']}% painted lane, {found['shared']}% shared street with signs, "
        f"{found['none']}% no bike route."
    )


def calmer_vias(coords: list[list[float]], used: list[str], n: int = 2) -> list[tuple[str, tuple]]:
    """Protected lanes or greenways close to the stretches of a ride ([lon, lat] points) that are
    not protected, most such lane first, each with a point on it to ride through."""
    grid = _grid()
    if not grid or not coords or len(coords) < 2:
        return []
    pts = _dense([(lat, lon) for lon, lat in coords])
    plain = [(k, p) for k, p in enumerate(pts) if (_lane_at(p) or ("III",))[0] not in ("I", "L")]
    samples = plain[::5]
    if len(samples) < 3:
        return []
    reach = 600
    di, dj = int(reach / 111_200 / CELL) + 1, int(reach / 84_300 / CELL) + 1
    best: dict[tuple, tuple] = {}
    for k, p in samples:
        ci, cj = int(p[0] / CELL), int(p[1] / CELL)
        for i in range(ci - di, ci + di + 1):
            for j in range(cj - dj, cj + dj + 1):
                for lat, lon, cls, street in grid.get((i, j), ()):
                    if NAMES[cls] != "protected" or not street or street in used:
                        continue
                    d = _metres(p, (lat, lon))
                    if d <= reach and ((lat, lon) not in best or d < best[(lat, lon)][0]):
                        best[(lat, lon)] = (d, k, street)
    streets: dict[str, list] = defaultdict(list)
    for q, (_, k, street) in best.items():
        streets[street].append((k, q))
    long = [(s, v) for s, v in streets.items() if len(v) * STEP_M >= 400]
    # A lane beside much of the plain part of the ride, not just near one end of it.
    long.sort(key=lambda x: (-len({k for k, _ in x[1]}), -len(x[1])))
    out = []
    for s, v in long[:n]:
        v.sort()
        out.append((s, v[len(v) // 2][1]))
    return out
