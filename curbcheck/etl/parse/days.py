"""Day-of-week sub-parser.

Reads one day expression off the front of a token list and returns the weekday
set it denotes plus the flags it implies. Monday is 0, matching `model.Weekday`.

Two forms of the corpus are easy to misread. `MONDAY THURSDAY` is a *list* of
two days, not a range — it is the street-cleaning pattern, and DOT writes ranges
with a hyphen or `THRU`. `EXCEPT SUNDAY` is a day set (Mon-Sat) as well as a
flag, because meters and free-parking limits are suspended on Sundays anyway.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import cast

from curbcheck.model import ALL_DAYS, Weekday

_DAY_NUMBER: dict[str, Weekday] = {
    "MONDAY": 0,
    "MON": 0,
    "TUESDAY": 1,
    "TUES": 1,
    "TUE": 1,
    "WEDNESDAY": 2,
    "WED": 2,
    "THURSDAY": 3,
    "THURS": 3,
    "THUR": 3,
    "THU": 3,
    "FRIDAY": 4,
    "FRI": 4,
    "SATURDAY": 5,
    "SAT": 5,
    "SUNDAY": 6,
    "SUN": 6,
}

# Misspellings that occur in the live data. Kept as a table rather than a fuzzy
# match so the parser never invents a day that was not written.
_DAY_TYPOS: dict[str, str] = {
    "THURSDA": "THURSDAY",
    "STAURDAY": "SATURDAY",
    "SATRUDAY": "SATURDAY",
    "TUESAY": "TUESDAY",
    "WEDNSDAY": "WEDNESDAY",
    "MONDY": "MONDAY",
}

WEEKDAYS: tuple[Weekday, ...] = (0, 1, 2, 3, 4)
MONDAY_TO_SATURDAY: tuple[Weekday, ...] = (0, 1, 2, 3, 4, 5)

_RANGE_JOINERS = frozenset({"-", "THRU", "THROUGH", "TO"})


@dataclass(frozen=True)
class DaySpec:
    """One day expression: the days it selects and the flags the wording implies."""

    days: tuple[Weekday, ...]
    length: int
    except_sunday: bool = False
    including_sunday: bool = False
    school_days: bool = False
    holidays: bool = False


def day_number(token: str) -> Weekday | None:
    """The weekday a single token names, or None if it does not name one."""
    return _DAY_NUMBER.get(_DAY_TYPOS.get(token, token))


def scan_days(tokens: list[str], start: int) -> DaySpec | None:
    """Longest day expression starting at `start`, or None if none does."""
    for scanner in (_scan_named_set, _scan_range, _scan_list):
        spec = scanner(tokens, start)
        if spec is not None:
            return _absorb_holidays(tokens, start, spec)
    return None


def _absorb_holidays(tokens: list[str], start: int, spec: DaySpec) -> DaySpec:
    """`SATURDAY SUNDAY & HOLIDAYS` names a day class we cannot expand to weekdays."""
    end = start + spec.length
    if end < len(tokens) and tokens[end].startswith("HOLIDAY"):
        return replace(spec, length=spec.length + 1, holidays=True)
    return spec


def _scan_named_set(tokens: list[str], start: int) -> DaySpec | None:
    window = tokens[start : start + 2]
    if len(window) < 2:
        return None
    first, second = window
    if first == "ALL" and second == "DAYS":
        return DaySpec(days=ALL_DAYS, length=2)
    if first == "SCHOOL" and second == "DAYS":
        return DaySpec(days=WEEKDAYS, length=2, school_days=True)
    if first == "EXCEPT" and second == "SUNDAY":
        return DaySpec(days=MONDAY_TO_SATURDAY, length=2, except_sunday=True)
    if first == "INCLUDING" and second == "SUNDAY":
        return DaySpec(days=ALL_DAYS, length=2, including_sunday=True)
    if first == "7" and second == "DAYS":
        return DaySpec(days=ALL_DAYS, length=2)
    return None


def _scan_range(tokens: list[str], start: int) -> DaySpec | None:
    """`MONDAY-FRIDAY`, `MON THRU FRI`, `MONDAY - FRIDAY`, `MONDAY -FRIDAY`."""
    token = tokens[start] if start < len(tokens) else ""
    if "-" in token:
        head, _, tail = token.partition("-")
        spec = _range_between(head, tail, length=1)
        if spec is not None:
            return spec
    window = tokens[start : start + 3]
    if len(window) == 3 and window[1] in _RANGE_JOINERS:
        return _range_between(window[0], window[2], length=3)
    # DOT also writes the joiner with a space on one side only
    # (`MONDAY -FRIDAY`), which splits the range across two tokens.
    pair = tokens[start : start + 2]
    if len(pair) == 2 and (pair[0].endswith("-") or pair[1].startswith("-")):
        head, _, tail = "".join(pair).partition("-")
        return _range_between(head, tail, length=2)
    return None


def _range_between(first: str, last: str, length: int) -> DaySpec | None:
    start_day, end_day = day_number(first), day_number(last)
    if start_day is None or end_day is None:
        return None
    span = (end_day - start_day) % 7
    days = tuple(cast(Weekday, (start_day + offset) % 7) for offset in range(span + 1))
    return DaySpec(days=days, length=length)


def _scan_list(tokens: list[str], start: int) -> DaySpec | None:
    """A run of bare day names: `MONDAY THURSDAY`, `TUESDAY THURSDAY SATURDAY`."""
    days: list[Weekday] = []
    index = start
    while index < len(tokens):
        day = day_number(tokens[index])
        if day is None:
            break
        days.append(day)
        index += 1
    if not days:
        return None
    return DaySpec(days=tuple(sorted(set(days))), length=index - start)
