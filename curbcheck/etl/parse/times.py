"""Clock times and seasonal date ranges.

Both are lexed into single tokens *before* the grammar splits on whitespace,
because DOT writes them with and without internal spaces (`8AM-7PM`,
`6AM - 5PM`, `10A M-11:30AM`, `MAY 15 - SEPT 30`). Rewriting them into one
token each is what keeps the grammar a flat scan over words.

A marked time token is `@HH:MM-HH:MM`, a marked season token is `#MM-DD:MM-DD`.
Neither prefix can occur in a sign description, so the rewrite is unambiguous.
"""

from __future__ import annotations

import re

TIME_PREFIX = "@"
SEASON_PREFIX = "#"

_MERIDIEM = r"[AP]\.?\s?M\.?"
_CLOCK = rf"\d{{1,2}}(?::\d{{2}})?\s*{_MERIDIEM}|MIDNIGHT|NOON"
# `7-10AM` leaves the meridiem off the first endpoint; it inherits the second's.
_BARE = r"\d{1,2}(?::\d{2})?"
_JOIN = r"\s*(?:-|TO)\s*"
_TIME_RANGE = re.compile(rf"\b({_CLOCK}|{_BARE}){_JOIN}({_CLOCK})(?![\w:])")

_MONTHS: dict[str, int] = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
_DAYS_IN_MONTH = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)

_MONTH_WORD = r"(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?"
_SEASON = re.compile(
    rf"\b({_MONTH_WORD})\s*(\d{{1,2}})?\s*-\s*({_MONTH_WORD})\s*(\d{{1,2}})?(?![\w])"
)


def mark_atoms(text: str) -> str:
    """Rewrite every seasonal range and clock range into one token each.

    Seasons go first: `MAY 15 - SEPT 30` contains digits around a hyphen that a
    careless time scan could mistake for a clock range.
    """
    return _TIME_RANGE.sub(_time_token, _SEASON.sub(_season_token, text))


_TIME_TOKEN = re.compile(rf"^{TIME_PREFIX}\d\d:\d\d-\d\d:\d\d$")
_SEASON_TOKEN = re.compile(rf"^{SEASON_PREFIX}\d\d-\d\d:\d\d-\d\d$")


def is_time_token(token: str) -> bool:
    return bool(_TIME_TOKEN.match(token))


def is_season_token(token: str) -> bool:
    """`#2` from `REVISION #2` shares the prefix, so the whole shape is checked."""
    return bool(_SEASON_TOKEN.match(token))


def read_time_token(token: str) -> tuple[str, str]:
    """`@HH:MM-HH:MM` back into its two `HH:MM` endpoints."""
    start, _, end = token[len(TIME_PREFIX) :].partition("-")
    return start, end


def read_season_token(token: str) -> tuple[str, str]:
    """`#MM-DD:MM-DD` back into its two `MM-DD` endpoints."""
    start, _, end = token[len(SEASON_PREFIX) :].partition(":")
    return start, end


def _time_token(match: re.Match[str]) -> str:
    end = _clock_to_minutes(match.group(2))
    if end is None:
        return match.group(0)
    start = _clock_to_minutes(match.group(1), inherit_meridiem_from=match.group(2))
    if start is None:
        return match.group(0)
    return f" {TIME_PREFIX}{_hhmm(start)}-{_hhmm(end)} "


def _clock_to_minutes(text: str, inherit_meridiem_from: str | None = None) -> int | None:
    compact = text.replace(" ", "").replace(".", "")
    if compact == "MIDNIGHT":
        return 0
    if compact == "NOON":
        return 12 * 60
    if inherit_meridiem_from is not None and not compact.endswith(("AM", "PM")):
        borrowed = inherit_meridiem_from.replace(" ", "").replace(".", "")
        if borrowed.endswith(("AM", "PM")):
            compact += borrowed[-2:]
    meridiem = compact[-2:]
    if meridiem not in ("AM", "PM"):
        return None
    hours_text, _, minutes_text = compact[:-2].partition(":")
    if not hours_text.isdigit() or (minutes_text and not minutes_text.isdigit()):
        return None
    hours, minutes = int(hours_text), int(minutes_text or 0)
    if hours > 12 or minutes > 59:
        return None
    hours %= 12
    if meridiem == "PM":
        hours += 12
    return hours * 60 + minutes


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _season_token(match: re.Match[str]) -> str:
    start_month = _month_number(match.group(1))
    end_month = _month_number(match.group(3))
    if start_month is None or end_month is None:
        return match.group(0)
    start_day = int(match.group(2)) if match.group(2) else 1
    # A month named without a day means the whole month, so an end month runs to
    # its last day. February gets 29 so a leap-year sign is never cut short.
    end_day = int(match.group(4)) if match.group(4) else _DAYS_IN_MONTH[end_month - 1]
    if not (1 <= start_day <= 31 and 1 <= end_day <= 31):
        return match.group(0)
    return f" {SEASON_PREFIX}{start_month:02d}-{start_day:02d}:{end_month:02d}-{end_day:02d} "


def _month_number(word: str) -> int | None:
    return _MONTHS.get(word[:3])
