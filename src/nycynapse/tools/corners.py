"""Where two streets meet, from the city's own street map.

An address search does not know corners: it put "Atlantic Avenue and Flatbush Avenue" a hundred
metres away, on the next corner, and a count of crashes there left out the corner asked about. The
city's street centerline has the point where each pair of streets meets, so those are kept in
corners.tsv.gz: borough, the two streets' words, latitude, longitude. `nycynapse export-corners`
writes the file again.
"""

import gzip
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

FILE = Path(__file__).with_name("corners.tsv.gz")
DATASET = "inkn-q76z"
PAGE = "https://data.cityofnewyork.us/d/inkn-q76z"
BOROUGHS = {"1": "Manhattan", "2": "Bronx", "3": "Brooklyn", "4": "Queens", "5": "Staten Island"}

_TYPES = {
    "street": "st",
    "saint": "st",
    "avenue": "av",
    "ave": "av",
    "boulevard": "blvd",
    "bl": "blvd",
    "road": "rd",
    "place": "pl",
    "parkway": "pkwy",
    "py": "pkwy",
    "square": "sq",
    "drive": "dr",
    "expressway": "expy",
    "expwy": "expy",
    "lane": "ln",
    "court": "ct",
    "terrace": "ter",
    "plaza": "plz",
    "circle": "cir",
    "crescent": "cres",
    "turnpike": "tpke",
    "concourse": "conc",
    "highway": "hwy",
}
_WORDS = {
    **_TYPES,
    "west": "w",
    "east": "e",
    "north": "n",
    "south": "s",
    "beach": "bch",
    "fort": "ft",
    "mount": "mt",
    "junior": "jr",
    "lex": "lexington",
    **{
        w: str(i)
        for i, w in enumerate(
            "first second third fourth fifth sixth seventh eighth ninth tenth eleventh "
            "twelfth".split(),
            1,
        )
    },
}
_DROP = {"the", "of", "and"}
# Words a reader may leave out: the side of town, and the kind of street.
_SIDES = {"w", "e", "n", "s"}
_LOOSE = _SIDES | set(_TYPES.values()) | {"jr"}
# Manhattan streets people call by another name: the map's, theirs.
_ALSO = (
    ("AVE OF THE AMERICAS", "6 AVE"),
    ("LENOX AVE", "MALCOLM X BLVD"),
    ("ADAM CLAYTON POWELL JR BLVD", "7 AVE"),
    ("FREDERICK DOUGLASS BLVD", "8 AVE"),
    ("CENTRAL PARK W", "CPW"),
    ("CENTRAL PARK S", "W 59 ST"),
    ("CENTRAL PARK N", "W 110 ST"),
    ("CATHEDRAL PKWY", "W 110 ST"),
)
_LEAD = re.compile(r"^\s*(?:the\s+)?(?:corner|intersection|crossing|junction)\s+of\s+", re.I)
_SPLIT = re.compile(r"\s+(?:and|&|at)\s+|\s*[/@]\s*", re.I)
_CROSS = re.compile(r"\s+x\s+", re.I)  # "Bedford x N 7th", but Malcolm X Blvd is a street


def words(street: str) -> frozenset[str]:
    """The words of a street's name, spelled one way ("West 96th Street": w, 96, st)."""
    t = re.sub(r"['’.]", "", street.lower())
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\b(\d+)(?:st|nd|rd|th)\b", r"\1", t)
    return frozenset(_WORDS.get(w, w) for w in t.split()) - _DROP


def strip(text: str) -> str:
    """ "the corner of A and B" is A and B."""
    return _LEAD.sub("", text)


# Building the file ------------------------------------------------------------------------------

# Ramps and paths are not streets to meet at.
_NOT_A_STREET = re.compile(
    r"\b(?:EN|ET|ENTRANCE|EXIT|RAMP|RP|PATH|GREENWAY|OPAS|DRIVEWAY|DY|FOOTBRIDGE|ACCESS)\b|\b[NSEW]B$"
)


def build(rows: list[dict]) -> list[str]:
    """Lines of the file. Streets meet where pieces with different names end at the same point on
    the same level, so a street under a highway does not meet it."""
    nodes: dict[tuple, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    streets = {r.get("full_street_name") for r in rows if r.get("rw_type") == "1"}
    for r in rows:
        name, boro = r.get("full_street_name") or "", r.get("boroughcode")
        # Type 3 is a bridge, 6 a walk: the plazas on Broadway are still Broadway.
        if r.get("rw_type") not in ("1", "3", "6") or name not in streets:
            continue
        if boro not in BOROUGHS or not r.get("the_geom") or _NOT_A_STREET.search(name):
            continue
        key = " ".join(sorted(words(name)))
        parts = r["the_geom"]["coordinates"]
        for (lon, lat), level in (
            (parts[0][0], r.get("from_level_code")),
            (parts[-1][-1], r.get("to_level_code")),
        ):
            nodes[(round(lon, 6), round(lat, 6), level)][boro].add(key)
    meet: dict[tuple, list] = defaultdict(list)
    for (lon, lat, _), by_boro in nodes.items():
        for boro, names in by_boro.items():
            names = sorted(names)
            for i, a in enumerate(names):
                for b in names[i + 1 :]:
                    meet[(boro, a, b)].append((lat, lon))
    for key, points in _round_circles(nodes).items():
        meet.setdefault(key, points)
    out = []
    for (boro, a, b), points in meet.items():
        for lat, lon in _together(points):
            out.append(f"{boro}\t{a}\t{b}\t{lat:.5f}\t{lon:.5f}")
    return sorted(out)


def _round_circles(nodes: dict) -> dict[tuple, list]:
    """Streets that end at the same traffic circle (Central Park West and 110th Street at
    Frederick Douglass Circle) meet there, though none of their pieces touch."""
    circles: dict[tuple, list] = defaultdict(list)
    for (lon, lat, _), by_boro in nodes.items():
        for boro, names in by_boro.items():
            for name in names:
                if "cir" in name.split():
                    circles[(boro, name)].append((lat, lon, names - {name}))
    out = {}
    for (boro, _), points in circles.items():
        lat, lon = (sum(p[i] for p in points) / len(points) for i in (0, 1))
        if any(
            ((y - lat) * 111_200) ** 2 + ((x - lon) * 84_300) ** 2 > 120**2 for y, x, _ in points
        ):
            continue  # a street called circle, not a junction
        names = sorted(set().union(*(n for _, _, n in points)))
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                out[(boro, a, b)] = [(lat, lon)]
    return out


def _together(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """A street with two roadways meets another at two points: one corner. Points further apart
    than about 120 metres are different corners of the same two streets."""
    groups: list[list] = []
    for lat, lon in sorted(points):
        near = [
            g
            for g in groups
            if any(((lat - y) * 111_200) ** 2 + ((lon - x) * 84_300) ** 2 < 120**2 for y, x in g)
        ]
        merged = [(lat, lon)] + [p for g in near for p in g]
        groups = [g for g in groups if g not in near] + [merged]
    return [(sum(y for y, _ in g) / len(g), sum(x for _, x in g) / len(g)) for g in sorted(groups)]


def export(fetch) -> dict:
    """Write the file. fetch(offset, limit) gives rows of the centerline dataset."""
    rows, size = [], 20_000
    while True:
        page = fetch(len(rows), size)
        rows += page
        if len(page) < size:
            break
    lines = build(rows)
    if len(rows) < 100_000 or len(lines) < 40_000:
        raise RuntimeError(f"only {len(rows)} street pieces and {len(lines)} corners came back")
    # The same streets give the same bytes.
    FILE.write_bytes(gzip.compress(("\n".join(lines) + "\n").encode(), 9, mtime=0))
    return {"pieces": len(rows), "corners": len(lines), "kb": FILE.stat().st_size // 1024}


# Finding a corner -------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _index() -> tuple[list[tuple], dict[frozenset, list[int]]]:
    corners, streets = [], defaultdict(list)
    for line in gzip.decompress(FILE.read_bytes()).decode().splitlines():
        boro, a, b, lat, lon = line.split("\t")
        for key in (a, b):
            streets[frozenset(key.split())].append(len(corners))
        corners.append((boro, float(lat), float(lon)))
    for ours, theirs in _ALSO:
        same = streets.get(words(ours), [])
        streets[words(theirs)] += [i for i in same if corners[i][0] == "1"]
    return corners, dict(streets)


def _streets(said: frozenset[str]) -> dict[int, list[tuple[int, frozenset]]]:
    """Corners on streets the words can mean: how many words the reader left out, which street."""
    _, streets = _index()
    out: dict[int, list] = defaultdict(list)
    if not said or said <= _SIDES:
        return out
    for name, corners in streets.items():
        left_out = name - said
        if said <= name and left_out <= _LOOSE:
            for i in corners:
                out[i].append((len(left_out), name))
    return out


def find(a: str, b: str) -> list[dict]:
    """Every place in the city where the two streets meet, best match of the names first. A side
    of town or a kind of street that was left out only counts when nothing fits better in that
    borough ("4th St" in Brooklyn is not East 4th Street)."""
    if not FILE.exists():
        return []
    corners, _ = _index()
    first, second = _streets(words(a)), _streets(words(b))
    hits = []
    for i in first.keys() & second.keys():
        # "1st and 1st" is a street and an avenue.
        pairs = [x + y for x, n in first[i] for y, m in second[i] if n != m]
        if pairs:
            hits.append((min(pairs), i))
    best: dict[str, int] = {}
    for missing, i in hits:
        boro = corners[i][0]
        best[boro] = min(best.get(boro, 9), missing)
    points = defaultdict(list)
    for missing, i in sorted(hits):
        boro, lat, lon = corners[i]
        if missing == best[boro]:
            points[boro].append((lat, lon))
    # East and West 42nd Street both start at 5th Avenue: one corner.
    return [
        {"borough": BOROUGHS[boro], "lat": round(lat, 5), "lon": round(lon, 5)}
        for boro, found in points.items()
        for lat, lon in _together(list(dict.fromkeys(found)))
    ]


def parse(text: str) -> tuple[str, str] | None:
    """The two streets of "A and B" or "A & B", without an area."""
    text = strip(text).strip()
    parts = _SPLIT.split(text)
    parts = parts if len(parts) > 1 else _CROSS.split(text)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 and all(parts) else None
