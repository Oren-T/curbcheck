"""Human labels for a stretch of curb: the street, the side, the cross streets.

Shared by the result cards (`engine.search`) and the geocoder's candidate list
(`curbcheck.geocode`), so a block is named the same way wherever the user meets
it. CSCL writes street names in capitals ("3 AVENUE") and they are shown as
stored, because a title-cased "3 Avenue" would be our text rather than DOT's.
"""

from __future__ import annotations

from typing import Any

from curbcheck.db import json_string_list

SIDE_WORDS = {"N": "north", "S": "south", "E": "east", "W": "west"}


def single_spaced(name: str) -> str:
    """CSCL writes the same street as `E 85 ST` on one row and `E  85 ST` on another.

    The label is the one place a street name reaches the user's eye, so the
    runs of spaces the source carries are collapsed there rather than in the
    ETL, which keeps the stored name byte-identical to DOT's.
    """
    return " ".join(name.split())


def between_phrase(street_name: str, from_names: Any, to_names: Any) -> str | None:
    """ "E 85 ST → E 86 ST", "at E 85 ST", or None when neither node names a cross street."""
    start = cross_street(from_names, street_name)
    end = cross_street(to_names, street_name)
    if start and end:
        return f"{start} → {end}"
    if start or end:
        return f"at {start or end}"
    return None


def cross_street(names: Any, street_name: str) -> str | None:
    """The first name on the node that is not the street the span runs along.

    Compared on collapsed whitespace: CSCL writes the same street as `E 85 ST`
    on the node and `E  85 ST` on the segment often enough that an exact match
    would label a corner as its own cross street.
    """
    own = _collapse(street_name)
    for name in json_string_list(names):
        if name and _collapse(name) != own:
            return single_spaced(name)
    return None


def span_label(street_name: str, side: str | None, phrase: str | None) -> str:
    """ "3 AVENUE, west side, E 85 ST → E 86 ST", with each part dropped when unknown."""
    parts = [single_spaced(street_name)]
    side_word = SIDE_WORDS.get(side or "")
    if side_word:
        parts.append(f"{side_word} side")
    if phrase:
        parts.append(phrase)
    return ", ".join(parts)


def _collapse(name: str) -> str:
    return single_spaced(name).casefold()
