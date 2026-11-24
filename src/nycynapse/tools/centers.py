"""Where a neighborhood is, for a trip: a corner in the middle of it that people there know.

The city's outlines join several neighborhoods ("Carroll Gardens-Cobble Hill-Gowanus-Red Hook") and
the middle of an outline can be a rail yard or a park, so a trip from "Cobble Hill" began in Red
Hook. Each name here is a borough and two streets that meet (tools/corners.py). Names that are not
here keep the middle of the city's outline.
"""

import re
from functools import lru_cache

from . import corners

M, B, Q, X, S = "Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island"

_CENTERS = {
    M: {
        "Meatpacking District": ("9 Ave", "W 14 St"),
        "Flatiron": ("5 Ave", "W 23 St"),
        "NoMad": ("Broadway", "W 28 St"),
        "Koreatown": ("Broadway", "W 32 St"),
        "Garment District": ("7 Ave", "W 37 St"),
        "Theater District": ("Broadway", "W 46 St"),
        "Tribeca": ("Varick St", "Franklin St"),
        "Financial District": ("Broadway", "Wall St"),
        "Civic Center": ("Centre St", "Chambers St"),
        "Battery Park City": ("South End Ave", "Liberty St"),
        "Nolita": ("Prince St", "Mott St"),
        "SoHo": ("Prince St", "Greene St"),
        "NoHo": ("Lafayette St", "Great Jones St"),
        "Hudson Square": ("Varick St", "Spring St"),
        "Little Italy": ("Grand St", "Mulberry St"),
        "Chinatown": ("Canal St", "Mott St"),
        "Two Bridges": ("Madison St", "Pike St"),
        "Lower East Side": ("Delancey St", "Essex St"),
        "Alphabet City": ("Avenue B", "E 7 St"),
        "East Village": ("2 Ave", "St Marks Pl"),
        "Greenwich Village": ("MacDougal St", "Bleecker St"),
        "West Village": ("Hudson St", "Christopher St"),
        "Chelsea": ("8 Ave", "W 23 St"),
        "Hell's Kitchen": ("9 Ave", "W 50 St"),
        "Hudson Yards": ("11 Ave", "W 34 St"),
        "Gramercy": ("3 Ave", "E 20 St"),
        "Kips Bay": ("3 Ave", "E 28 St"),
        "Murray Hill": ("Lexington Ave", "E 36 St"),
        "Midtown East": ("Lexington Ave", "E 51 St"),
        "Turtle Bay": ("2 Ave", "E 48 St"),
        "Sutton Place": ("1 Ave", "E 57 St"),
        "Upper East Side": ("Lexington Ave", "E 77 St"),
        "Lenox Hill": ("Lexington Ave", "E 70 St"),
        "Yorkville": ("2 Ave", "E 86 St"),
        "Carnegie Hill": ("Madison Ave", "E 91 St"),
        "Upper West Side": ("Broadway", "W 86 St"),
        "Lincoln Square": ("Broadway", "W 66 St"),
        "Manhattan Valley": ("Amsterdam Ave", "W 106 St"),
        "Morningside Heights": ("Broadway", "W 116 St"),
        "East Harlem": ("Lexington Ave", "E 110 St"),
        "Harlem": ("Lenox Ave", "W 125 St"),
        "Manhattanville": ("Broadway", "W 125 St"),
        "Hamilton Heights": ("Broadway", "W 145 St"),
        "Sugar Hill": ("St Nicholas Ave", "W 150 St"),
        "Washington Heights": ("Broadway", "W 175 St"),
        "Inwood": ("Broadway", "W 207 St"),
        "Marble Hill": ("Broadway", "W 225 St"),
    },
    B: {
        "Williamsburg": ("Bedford Ave", "N 7 St"),
        "East Williamsburg": ("Graham Ave", "Grand St"),
        "Greenpoint": ("Manhattan Ave", "Greenpoint Ave"),
        "Bedford-Stuyvesant": ("Tompkins Ave", "Halsey St"),
        "Bushwick": ("Knickerbocker Ave", "DeKalb Ave"),
        "Stuyvesant Heights": ("Lewis Ave", "Decatur St"),
        "Clinton Hill": ("Lafayette Ave", "Waverly Ave"),
        "Fort Greene": ("DeKalb Ave", "Cumberland St"),
        "Brooklyn Heights": ("Montague St", "Henry St"),
        "Dumbo": ("Front St", "Washington St"),
        "Vinegar Hill": ("Hudson Ave", "Water St"),
        "Boerum Hill": ("Hoyt St", "Bergen St"),
        "Cobble Hill": ("Court St", "Kane St"),
        "Carroll Gardens": ("Smith St", "Carroll St"),
        "Gowanus": ("3 Ave", "3 St"),
        "Red Hook": ("Van Brunt St", "Pioneer St"),
        "Park Slope": ("7 Ave", "5 St"),
        "South Slope": ("5 Ave", "15 St"),
        "Windsor Terrace": ("Prospect Ave", "Greenwood Ave"),
        "Prospect Heights": ("Vanderbilt Ave", "Prospect Pl"),
        "Crown Heights": ("Nostrand Ave", "Eastern Pkwy"),
        "Prospect Lefferts Gardens": ("Flatbush Ave", "Lincoln Rd"),
        "Flatbush": ("Flatbush Ave", "Church Ave"),
        "East Flatbush": ("Church Ave", "Utica Ave"),
        "Ditmas Park": ("Cortelyou Rd", "Argyle Rd"),
        "Kensington": ("Church Ave", "McDonald Ave"),
        "Borough Park": ("13 Ave", "50 St"),
        "Sunset Park": ("5 Ave", "47 St"),
        "Bay Ridge": ("4 Ave", "77 St"),
        "Dyker Heights": ("13 Ave", "77 St"),
        "Bensonhurst": ("20 Ave", "78 St"),
        "Bath Beach": ("20 Ave", "Bath Ave"),
        "Gravesend": ("Avenue U", "McDonald Ave"),
        "Midwood": ("Avenue J", "E 15 St"),
        "Sheepshead Bay": ("Sheepshead Bay Rd", "E 15 St"),
        "Brighton Beach": ("Brighton Beach Ave", "Coney Island Ave"),
        "Coney Island": ("Surf Ave", "Stillwell Ave"),
        "Canarsie": ("Rockaway Pkwy", "Flatlands Ave"),
        "Flatlands": ("Flatlands Ave", "Flatbush Ave"),
        "Brownsville": ("Pitkin Ave", "Rockaway Ave"),
        "East New York": ("Sutter Ave", "Van Siclen Ave"),
        "Cypress Hills": ("Fulton St", "Crescent St"),
    },
    Q: {
        "Astoria": ("30 Ave", "31 St"),
        "Ditmars": ("Ditmars Blvd", "31 St"),
        "Long Island City": ("Jackson Ave", "45 Ave"),
        "Hunters Point": ("Vernon Blvd", "50 Ave"),
        "Sunnyside": ("Queens Blvd", "46 St"),
        "Woodside": ("Roosevelt Ave", "61 St"),
        "Jackson Heights": ("37 Ave", "82 St"),
        "Elmhurst": ("Broadway", "Whitney Ave"),
        "East Elmhurst": ("Astoria Blvd", "94 St"),
        "Corona": ("Roosevelt Ave", "103 St"),
        "Flushing": ("Main St", "Roosevelt Ave"),
        "College Point": ("College Point Blvd", "18 Ave"),
        "Whitestone": ("14 Ave", "150 St"),
        "Bayside": ("Bell Blvd", "41 Ave"),
        "Rego Park": ("Queens Blvd", "63 Dr"),
        "Forest Hills": ("Austin St", "71 Ave"),
        "Kew Gardens": ("Austin St", "Lefferts Blvd"),
        "Kew Gardens Hills": ("Main St", "Jewel Ave"),
        "Richmond Hill": ("Jamaica Ave", "Lefferts Blvd"),
        "Woodhaven": ("Jamaica Ave", "Woodhaven Blvd"),
        "Ozone Park": ("Liberty Ave", "Cross Bay Blvd"),
        "Howard Beach": ("Cross Bay Blvd", "160 Ave"),
        "Ridgewood": ("Myrtle Ave", "Forest Ave"),
        "Maspeth": ("Grand Ave", "69 St"),
        "Middle Village": ("Metropolitan Ave", "79 St"),
        "Jamaica": ("Jamaica Ave", "Parsons Blvd"),
        "Queens Village": ("Jamaica Ave", "Springfield Blvd"),
        "St Albans": ("Linden Blvd", "Farmers Blvd"),
        "Far Rockaway": ("Mott Ave", "Beach 20 St"),
        "Rockaway Beach": ("Rockaway Beach Blvd", "Beach 90 St"),
        "Rockaway Park": ("Rockaway Beach Blvd", "Beach 116 St"),
    },
    X: {
        "Mott Haven": ("E 138 St", "Willis Ave"),
        "South Bronx": ("3 Ave", "E 149 St"),
        "Concourse": ("Grand Concourse", "E 161 St"),
        "Melrose": ("Melrose Ave", "E 156 St"),
        "Morrisania": ("Boston Rd", "E 169 St"),
        "Hunts Point": ("Hunts Point Ave", "Lafayette Ave"),
        "Fordham": ("Grand Concourse", "E Fordham Rd"),
        "Belmont": ("Arthur Ave", "E 187 St"),
        "Little Italy": ("Arthur Ave", "E 187 St"),
        "Arthur Avenue": ("Arthur Ave", "E 187 St"),
        "Bedford Park": ("Grand Concourse", "Bedford Park Blvd"),
        "Norwood": ("Bainbridge Ave", "E 204 St"),
        "Kingsbridge": ("Broadway", "W 231 St"),
        "Riverdale": ("Riverdale Ave", "W 254 St"),
        "Woodlawn": ("Katonah Ave", "E 238 St"),
        "Wakefield": ("White Plains Rd", "E 233 St"),
        "Morris Park": ("Morris Park Ave", "Hone Ave"),
        "Pelham Bay": ("Westchester Ave", "Buhre Ave"),
        "City Island": ("City Island Ave", "Fordham St"),
    },
    S: {
        "St George": ("Hyatt St", "Stuyvesant Pl"),
        "Tompkinsville": ("Bay St", "Victory Blvd"),
        "Stapleton": ("Bay St", "Water St"),
        "New Dorp": ("New Dorp Ln", "Hylan Blvd"),
        "Tottenville": ("Main St", "Amboy Rd"),
    },
}

_ALSO = {
    "fidi": "financial district",
    "flatiron district": "flatiron",
    "meatpacking": "meatpacking district",
    "k town": "koreatown",
    "ktown": "koreatown",
    "les": "lower east side",
    "ues": "upper east side",
    "uws": "upper west side",
    "lic": "long island city",
    "bed stuy": "bedford stuyvesant",
    "bedstuy": "bedford stuyvesant",
    "plg": "prospect lefferts gardens",
    "gramercy park": "gramercy",
    "clinton": "hells kitchen",
    "spanish harlem": "east harlem",
    "el barrio": "east harlem",
    "central harlem": "harlem",
    "the village": "greenwich village",
    "village": "greenwich village",
    "downtown flushing": "flushing",
    "boro park": "borough park",
    "astoria ditmars": "ditmars",
    "ditmars steinway": "ditmars",
    "throggs neck": "throgs neck",
    "saint george": "st george",
    "saint albans": "st albans",
}


def _key(name: str) -> str:
    t = re.sub(r"['’.]", "", name.lower()).replace("-", " ")
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", t).split())


@lru_cache(maxsize=1)
def _index() -> dict[str, list[tuple[str, str, tuple[str, str]]]]:
    out: dict[str, list] = {}
    for boro, by_name in _CENTERS.items():
        for name, at in by_name.items():
            out.setdefault(_key(name), []).append((name, boro, at))
    return out


@lru_cache(maxsize=256)
def find(name: str, borough: str | None = None) -> dict | None:
    """The middle of a neighborhood by name; None when it is not here or is in another borough."""
    key = _key(name)
    named = _index().get(_ALSO.get(key, key), [])
    # Without a borough the first one is meant: Little Italy is Manhattan's.
    hit = next((h for h in named if not borough or borough.lower() == h[1].lower()), None)
    if not hit:
        return None
    title, boro, (a, b) = hit
    spot = next((c for c in corners.find(a, b) if c["borough"] == boro), None)
    if not spot:
        return None
    at = f"{a} and {b}"
    return {"name": title, "lat": spot["lat"], "lon": spot["lon"], "around": at, "borough": boro}
