"""Questions shown on the landing page. Ones about past data are answered every morning and
replayed; ones about right now are always answered live, since a stored answer would go stale.
None names a fixed month: a question written in August reads as stale by November."""

EXAMPLES = [
    {"q": "How is the rain and road situation in New York right now?", "live": True, "tag": "Live"},
    {"q": "Is the 7 train running normally right now?", "live": True, "tag": "Subway"},
    {"q": "Which parts of the city have the cheapest rent?", "live": False, "tag": "Rent"},
    {
        "q": "What street events are happening in Manhattan this weekend?",
        "live": True,
        "tag": "Events",
    },
    {
        "q": "Which neighborhoods had the most noise complaints last month?",
        "live": False,
        "tag": "311",
    },
    {
        "q": "What were the busiest subway stations in the most recent month on record?",
        "live": False,
        "tag": "Subway",
    },
    {
        "q": "Which zones had the most Uber and Lyft pickups in the latest month on record?",
        "live": False,
        "tag": "Rides",
    },
    {
        "q": "How many Citi Bike rides started in Williamsburg in the most recent month on record?",
        "live": False,
        "tag": "Citi Bike",
    },
    {
        "q": "How many cyclists have been injured in Brooklyn so far this year?",
        "live": False,
        "tag": "Safety",
    },
]
