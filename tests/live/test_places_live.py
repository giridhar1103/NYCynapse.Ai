"""Frozen checks on the live place resolver and trip planner: no model, real city services.
They catch the failures readers notice most (the wrong place, no route at all) before a
release. Run on purpose, since they need the network:

    NYC_LIVE=1 pytest tests/live -q
"""

import math
import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("NYC_LIVE") != "1", reason="set NYC_LIVE=1")

# (what a reader writes, expected latitude, longitude, how far off is still right in metres)
PLACES = [
    ("Times Square", 40.7580, -73.9855, 300),
    ("Grand Central Terminal", 40.7527, -73.9772, 300),
    ("JFK Terminal 4", 40.6441, -73.7823, 500),
    ("LaGuardia Terminal B", 40.7741, -73.8720, 600),
    ("Newark Airport", 40.6895, -74.1745, 2500),
    ("the Met", 40.7794, -73.9632, 400),
    ("Barclays Center", 40.6826, -73.9754, 300),
    ("Yankee Stadium", 40.8296, -73.9262, 400),
    ("Coney Island", 40.5755, -73.9707, 1500),
    ("Flushing-Main St subway station, Queens", 40.7596, -73.8300, 250),
    ("Church Av station on the F", 40.6441, -73.9796, 300),
    ("5th Ave and 42nd St", 40.7531, -73.9810, 250),
    ("5th Ave and 59th St, Brooklyn", 40.6397, -74.0159, 400),
    ("Ludlow Street and Stanton Street, Manhattan", 40.7213, -73.9879, 200),
    ("Main St and 41st Ave Flushing NY", 40.7578, -73.8294, 250),
    ("233 5th Ave", 40.7440, -73.9874, 200),
    ("233 5th Ave, Brooklyn", 40.6754, -73.9809, 200),
    ("Red Hook Ball Fields", 40.6722, -74.0040, 400),
    ("Ronkonkoma LIRR station", 40.8081, -73.1059, 500),
    ("Hoboken Terminal", 40.7351, -74.0279, 500),
    ("Snug Harbor Cultural Center, Staten Island", 40.6430, -74.1030, 500),
    ("Wall Street, Pier 11", 40.7033, -74.0071, 350),
    ("40.7128,-74.0060", 40.7128, -74.0060, 10),
]


def _metres(a, b):
    dy = (a[0] - b[0]) * 111_320
    dx = (a[1] - b[1]) * 111_320 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


@pytest.fixture(scope="module", autouse=True)
def keys():
    from nycynapse.tools.http import load_keys

    load_keys()


@pytest.mark.parametrize("text,lat,lon,slack", PLACES)
def test_place_lands_where_meant(text, lat, lon, slack):
    from nycynapse.tools.trips import locate

    p = locate(text)
    assert p, f"{text!r} was not placed"
    off = _metres((p["lat"], p["lon"]), (lat, lon))
    assert off <= slack, f"{text!r} -> {p['name']} is {off:.0f} m off"


# (origin, destination, mode, a word every good answer's routes contain)
TRIPS = [
    ("Astoria-Ditmars Blvd station", "Coney Island-Stillwell Av station", "transit", "N train"),
    ("Times Square", "Grand Central Terminal", "transit", "door to door"),
    ("JFK Terminal 4", "Grand Central Terminal", "transit", "AirTrain"),
    ("Penn Station", "LaGuardia Terminal B", "transit", "door to door"),
    ("World Trade Center", "Hoboken Terminal", "transit", "PATH"),
    ("Brooklyn Museum", "Prospect Park", "walk", "Walk"),
]


@pytest.mark.parametrize("a,b,mode,must", TRIPS)
def test_trip_has_a_sensible_route(a, b, mode, must):
    from nycynapse.tools.trips import trip_plan

    r = trip_plan(a, b, mode, "tomorrow 10 AM")
    assert r.ok, r.error
    assert "No transit itinerary was found" not in r.summary, r.summary[:300]
    assert must.lower() in r.summary.lower(), r.summary[:500]


def test_nearby_restrooms_near_a_busy_place():
    from nycynapse.tools.nearby import nearby

    r = nearby("Grand Central Terminal", "restroom", 3)
    assert r.ok and r.summary.count("\n- ") >= 2, r.summary[:300]
