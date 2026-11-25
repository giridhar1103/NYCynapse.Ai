"""Sunrise and sunset in New York City, worked out from the date (NOAA's solar equations).

A day plan around a sunset needed a web search and a page for one clock time. Within a minute or
so of the published tables.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from math import acos, asin, cos, degrees, radians, sin, tan
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
LAT, LON = 40.7128, -74.0060
HORIZON = 90.833  # the sun's upper edge, with the air bending its light


def _at(day: date, rising: bool) -> datetime:
    """The moment, by two passes: the sun's place is taken at noon, then at the time found."""
    minutes = 720.0
    for _ in range(3):
        days = (day - date(2000, 1, 1)).days - 0.5 + minutes / 1440
        c = days / 36525
        mean_long = radians((280.46646 + c * (36000.76983 + c * 0.0003032)) % 360)
        anomaly = radians(357.52911 + c * (35999.05029 - 0.0001537 * c))
        e = 0.016708634 - c * (0.000042037 + 0.0000001267 * c)
        centre = (
            sin(anomaly) * (1.914602 - c * (0.004817 + 0.000014 * c))
            + sin(2 * anomaly) * (0.019993 - 0.000101 * c)
            + sin(3 * anomaly) * 0.000289
        )
        node = radians(125.04 - 1934.136 * c)
        true_long = radians(degrees(mean_long) + centre - 0.00569 - 0.00478 * sin(node))
        tilt = radians(23.439291 - 0.0130042 * c + 0.00256 * cos(node))
        declination = asin(sin(tilt) * sin(true_long))
        y = tan(tilt / 2) ** 2
        equation = 4 * degrees(
            y * sin(2 * mean_long)
            - 2 * e * sin(anomaly)
            + 4 * e * y * sin(anomaly) * cos(2 * mean_long)
            - 0.5 * y * y * sin(4 * mean_long)
            - 1.25 * e * e * sin(2 * anomaly)
        )
        lat = radians(LAT)
        angle = degrees(
            acos(
                cos(radians(HORIZON)) / (cos(lat) * cos(declination)) - tan(lat) * tan(declination)
            )
        )
        minutes = 720 - 4 * (LON + (angle if rising else -angle)) - equation
    midnight = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return (midnight + timedelta(minutes=minutes)).astimezone(NY)


def sunrise(day: date) -> datetime:
    return _at(day, True)


def sunset(day: date) -> datetime:
    return _at(day, False)


def line(day: date) -> str:
    return f"{day:%a %b %-d}: sunrise {sunrise(day):%-I:%M %p}, sunset {sunset(day):%-I:%M %p}"
