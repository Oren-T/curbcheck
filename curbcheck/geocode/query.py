"""Query text in, an index key out: normalization, then classification.

Everything the user typed passes through here before a single row is read.
`clean_query` is the sanitizer and `parse_query` the tiny grammar — a ZIP, two
streets meeting, a house number on a street, or a bare name — and the result is
what decides which rung `suggest` tries first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from curbcheck.etl.addresses import fold

# Longest query we will parse. A Manhattan address is never close to this; the
# cap keeps a pathological string out of the regexes and out of the index.
MAX_QUERY_CHARS = 120

# "&", "and", "at", "@" or a slash between two street names.
_INTERSECTION_SPLIT = re.compile(r"\s+(?:&|AND|AT)\s+|\s*[&@/]\s*")

# A leading house number, optionally hyphenated (Queens style, rare in
# Manhattan) or with a letter suffix ("123A REAR"), then the street.
_HOUSE_NUMBER = re.compile(r"^(\d{1,6})(?:-\d{1,6})?[A-Z]?\s+(.+)$")

_ZIP = re.compile(r"^\d{5}$")

# C0 and C1 controls, including the NULs and bidi-adjacent bytes the fuzzer
# splices in. Replaced with a space rather than removed, so "A\x00B" cannot
# become the word "AB".
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class AddressQuery:
    """A house number on a named street, the street already folded."""

    house_number: int
    street: str


@dataclass(frozen=True)
class IntersectionQuery:
    """Two folded street names that should meet at a node."""

    first: str
    second: str


@dataclass(frozen=True)
class StreetQuery:
    """A bare folded street name, with no house number. Also the place-name case."""

    street: str


@dataclass(frozen=True)
class ZipQuery:
    """Five digits, which in Manhattan can only be a ZIP code."""

    zipcode: str


ParsedQuery = AddressQuery | IntersectionQuery | StreetQuery | ZipQuery


def clean_query(text: str) -> str:
    """Upper-cased, control-free, single-spaced query text, or "" when there is none.

    Over `MAX_QUERY_CHARS` returns "", which every caller reads as "no query":
    a 400-character paste is not an address and is not worth a regex.
    """
    cleaned = _WHITESPACE.sub(" ", _CONTROL_CHARS.sub(" ", str(text))).strip().upper()
    return "" if len(cleaned) > MAX_QUERY_CHARS else cleaned


def parse_query(text: str) -> ParsedQuery | None:
    """Classify a query as a ZIP, an intersection, an address, or a bare street.

    Returns None when the query is empty, over `MAX_QUERY_CHARS`, or has no
    street name left after folding. The classification is the *primary* reading
    only: `suggest` falls back to the street and place passes when an address
    reading finds nothing, because "350 5th" and "1 police plaza" both parse as
    addresses and only one of them is one.
    """
    cleaned = clean_query(text)
    if not cleaned:
        return None

    if _ZIP.match(cleaned):
        return ZipQuery(zipcode=cleaned)

    parts = [part.strip() for part in _INTERSECTION_SPLIT.split(cleaned) if part.strip()]
    if len(parts) >= 2:
        first, second = fold(parts[0]), fold(parts[1])
        return IntersectionQuery(first=first, second=second) if first and second else None

    house_match = _HOUSE_NUMBER.match(cleaned)
    if house_match:
        street = fold(house_match.group(2))
        if street:
            return AddressQuery(house_number=int(house_match.group(1)), street=street)

    street = fold(cleaned)
    return StreetQuery(street=street) if street else None


def prefix_bound(prefix: str) -> str:
    """The exclusive upper end of a `LIKE prefix%` range, so it can be a range scan.

    A range scan has no pattern language in it, which is the point: there is no
    metacharacter for a user to escape or forget to escape (threat T3).
    """
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)
