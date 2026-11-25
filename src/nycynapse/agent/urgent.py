"""Questions from someone in danger. The number to call comes first, whatever else happens.

Every other check of an answer may shorten or rewrite it. One of them once took "Call 911" out,
because 911 was in no tool result, so the number is put back here, in code.
"""

from __future__ import annotations

import re

_WHO = (
    r"\b(?:i|im|i'm|my|me|we|our|us|he|he's|hes|she|she's|shes|they|they're|someone|somebody|friend|"
    r"guy|man|woman|person|kid|child|baby|son|daughter|mom|mother|dad|father|husband|wife|partner|"
    r"roommate|neighbou?r|there's|theres|there is)\b"
)
_LIFE = re.compile(
    rf"{_WHO}[^?!]{{0,80}}?(?:"
    r"\b(?:won'?t|wont|not|isn'?t|isnt|can'?t|cant|cannot|doesn'?t|doesnt) (?:wak(?:e|ing) up|"
    r"breath(?:e|ing)|respond(?:ing)?)\b"
    r"|\b(?:unconscious|unresponsive|passed out|collapsed)\b"
    r"|\b(?:overdos(?:ed|ing)|od'?d|od'?ing|choking|(?:having|had) an? (?:heart attack|stroke|"
    r"seizure|allergic reaction))\b"
    r"|\bchest (?:pain|hurts)\b"
    r"|\bbleeding (?:a lot|badly|heavily|out|everywhere|and it won'?t stop)\b"
    r"|\b(?:following|chasing|attacking|hitting|beating|threatening|stalking) "
    r"(?:me|us|him|her|them)\b"
    r"|\b(?:has|have|pulled|with) (?:a |got a )?(?:gun|knife)\b"
    r"|\b(?:being|getting|just got|just been|was just) (?:robbed|mugged|attacked|assaulted|stabbed|"
    r"shot|jumped)\b"
    r"|\b(?:is|caught|'s) on fire\b"
    r"|\ba fire in (?:my|our|the) (?:building|apartment|house|kitchen)\b"
    r"|\bsmell(?:s)? (?:of |like )?gas\b|\bgas leak\b"
    r")",
    re.I,
)
_SELF = re.compile(
    r"\b(?:kill(?:ing)? myself|end(?:ing)? (?:my life|it all)|hurt(?:ing)? myself|suicidal|"
    r"don'?t want to (?:live|be alive|be here) ?(?:any ?more)?|want to die)\b",
    re.I,
)
# A question about numbers or the news is not a call for help.
_ABOUT = re.compile(
    r"\bhow many\b|\bstatistics?\b|\bstats\b|\brates?\b|\bnumber of\b|\bthis year\b|\blast (?:year|"
    r"month|week)\b|\byesterday\b|\bin (?:19|20)\d\d\b|\bmovie\b|\bmuseum\b",
    re.I,
)

CALL_911 = "**Call 911 now.**"
CALL_988 = (
    "**Call or text 988 now**: it is free, at any hour, and someone will listen. If you are in "
    "danger this minute, call 911."
)


def number(question: str) -> str | None:
    """The number someone who writes this should call, or None."""
    text = (question or "").replace("’", "'")
    if _SELF.search(text):
        return "988"
    if _LIFE.search(text) and not _ABOUT.search(text):
        return "911"
    return None


def with_number(answer: str | None, call: str) -> str:
    """The answer, beginning with the number. It stands alone when there is no answer."""
    first = CALL_988 if call == "988" else CALL_911
    if not answer:
        where = " Say the street and the nearest corner, and do what the dispatcher tells you."
        return first if call == "988" else first + where
    opening = answer.strip()[:120]
    return answer if re.search(rf"\b{call}\b", opening) else f"{first}\n\n{answer.strip()}"
