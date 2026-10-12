"""Questions shown on the landing page. Ones about past data are answered every morning and
replayed; ones about right now are always answered live, since a stored answer would go stale."""

EXAMPLES = [
    {"q": "How is the rain and road situation in New York right now?", "live": True, "tag": "Live"},
    {"q": "Is the 7 train running normally right now?", "live": True, "tag": "Subway"},
    {
        "q": "What street events are happening in Manhattan this weekend?",
        "live": True,
        "tag": "Events",
    },
    {
        "q": "Which neighborhoods had the most noise complaints in August 2026?",
        "live": False,
        "tag": "311",
    },
    {
        "q": "What were the busiest subway stations by ridership in August 2026?",
        "live": False,
        "tag": "Subway",
    },
    {
        "q": "Which pickup zones had the most Uber and Lyft trips in August 2026?",
        "live": False,
        "tag": "Rides",
    },
    {
        "q": "How many Citi Bike rides started in Williamsburg in August 2026?",
        "live": False,
        "tag": "Citi Bike",
    },
    {
        "q": "How many cyclists were injured in Brooklyn from January to May 2026?",
        "live": False,
        "tag": "Safety",
    },
]
