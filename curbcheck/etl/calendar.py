"""The DOT Alternate Side Parking suspension calendar, read from its ICS.

No NYC Open Data dataset publishes this calendar; nyc.gov's ICS is the only
machine-readable source and it answers 403 without a browser User-Agent
(docs/DECISIONS.md D11, docs/DATA.md §4). The file is small and its shape is
fixed, so it is parsed here with the standard library rather than by adding an
`icalendar` dependency: unfold RFC 5545 continuation lines, read
`DTSTART;VALUE=DATE` through the exclusive `DTEND`, and take the reason and the
meter status out of `DESCRIPTION`.

Sundays are not inserted. The engine already knows meters do not run on a
Sunday (`window.CalendarContext.meters_in_effect`), and a row here means "the
city suspended alternate-side on this date", which is a different claim.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

LOGGER = logging.getLogger(__name__)

CALENDAR_DIRNAME = "calendar"

# SPEC §5.6 quotes DOT: metered parking is not in effect on Sundays nor the six
# Major Legal Holidays. The ICS states the meter status per date, so the list is
# a cross-check on that reading, not the source of it (docs/DECISIONS.md D11).
MAJOR_LEGAL_HOLIDAYS: tuple[str, ...] = (
    "New Year's Day",
    "Memorial Day",
    "Independence Day",
    "Labor Day",
    "Thanksgiving Day",
    "Christmas Day",
)

# DOT writes the reason into DESCRIPTION; SUMMARY is the constant string
# "Alternate Side Parking Suspended" on every event (docs/DATA.md §4).
_REASON = re.compile(r"suspended for (.+?)\s*\.", re.IGNORECASE)
_METERS_IN_EFFECT = re.compile(r"meters will be in effect", re.IGNORECASE)
_METERS_NOT_IN_EFFECT = re.compile(r"meters will not be in effect", re.IGNORECASE)
_VEVENT = re.compile(r"BEGIN:VEVENT(.*?)END:VEVENT", re.DOTALL)
_DTSTART = re.compile(r"^DTSTART[^:\n]*:(\d{8})", re.MULTILINE)
_DTEND = re.compile(r"^DTEND[^:\n]*:(\d{8})", re.MULTILINE)
_DESCRIPTION = re.compile(r"^DESCRIPTION:(.*)$", re.MULTILINE)
_FOLDED = re.compile(r"\r?\n[ \t]")

# ICS escapes commas, semicolons and newlines inside a text value (RFC 5545 §3.3.11).
_ESCAPES = (("\\n", " "), ("\\N", " "), ("\\,", ","), ("\\;", ";"), ("\\\\", "\\"))

# A hostile or corrupt file should not be able to expand into an unbounded
# number of rows; no real DOT event covers more than three days.
MAX_EVENT_DAYS = 14


@dataclass(frozen=True)
class AspSuspension:
    """One `asp_suspension` row: a date the city suspended alternate-side parking."""

    date: date
    label: str
    is_major_legal_holiday: bool
    meters_suspended: bool


@dataclass(frozen=True)
class CalendarReport:
    """What the ICS yielded, for `sync_meta`. A `None` path means no file was found.

    `suspended_days` counts every day every event covers and `distinct_dates`
    counts the rows written, so two events naming the same date show up as a
    difference between them (docs/DATA.md §4 measures 47 over 42 for 2026).
    """

    source_path: str | None
    events: int
    suspended_days: int
    distinct_dates: int
    meters_suspended_dates: int
    major_holidays: int
    unnamed_major_holidays: tuple[str, ...]


def unfold(text: str) -> str:
    """Undo RFC 5545 line folding: a continuation line starts with a space or tab."""
    return _FOLDED.sub("", text)


def unescape(value: str) -> str:
    text = value.strip()
    for token, replacement in _ESCAPES:
        text = text.replace(token, replacement)
    return text


def is_major_legal_holiday(label: str, meters_suspended: bool) -> bool:
    """Whether this suspension is one of the six days meters also stop (SPEC §5.6).

    The ICS's own "Parking meters will not be in effect" sentence is the
    evidence; the name list is the cross-check. When the two disagree we take
    the meter sentence, because that is the statement the rule is about, and the
    disagreement is reported so the name list can be corrected.
    """
    return meters_suspended or _names_a_major_holiday(label)


def expanded_day_count(text: str) -> int:
    """Days every VEVENT covers, before overlapping events collapse onto one date."""
    total = 0
    for block in _VEVENT.findall(unfold(text)):
        start = _DTSTART.search(block)
        if start is None:
            continue
        end = _DTEND.search(block)
        first = _as_date(start.group(1))
        total += sum(1 for _ in _expand(first, _as_date(end.group(1)) if end else None))
    return total


def parse_ics(text: str) -> list[AspSuspension]:
    """Every suspended day in an ICS, one row per date.

    `DTEND` is exclusive, so a two-day holiday reads as `DTSTART` 12/25 and
    `DTEND` 12/26 and must expand to one day; eight of the 2026 events span two
    days, which is why counting VEVENTs undercounts (docs/DATA.md §4).
    """
    days: dict[date, AspSuspension] = {}
    for block in _VEVENT.findall(unfold(text)):
        start = _DTSTART.search(block)
        if start is None:
            continue
        description = _DESCRIPTION.search(block)
        detail = unescape(description.group(1)) if description else ""
        reason = _REASON.search(detail)
        label = reason.group(1).strip() if reason else "Alternate Side Parking suspended"
        meters_suspended = _meters_suspended(detail)
        first = _as_date(start.group(1))
        end = _DTEND.search(block)
        last = _as_date(end.group(1)) if end else None
        for day in _expand(first, last):
            days[day] = AspSuspension(
                date=day,
                label=label,
                is_major_legal_holiday=is_major_legal_holiday(label, meters_suspended),
                meters_suspended=meters_suspended,
            )
    return sorted(days.values(), key=lambda row: row.date)


def find_ics(raw_dir: Path, year: int | None = None) -> Path | None:
    """The newest `*.ics` in `raw_dir/calendar`, or the one for `year` if named."""
    directory = raw_dir / CALENDAR_DIRNAME
    if not directory.is_dir():
        return None
    candidates = sorted(directory.glob("*.ics"))
    if year is not None:
        for path in candidates:
            if str(year) in path.name:
                return path
    return candidates[-1] if candidates else None


def load_asp_suspensions(
    raw_dir: Path, year: int | None = None
) -> tuple[list[AspSuspension], CalendarReport]:
    """Read the calendar snapshot. A missing file is an empty table, not an error.

    SPEC §11 puts the burden on surfacing the gap rather than on refusing to
    build: `CalendarReport.source_path` is None and `build` writes
    `sync_meta.calendar_missing`.
    """
    path = find_ics(raw_dir, year)
    if path is None:
        LOGGER.warning("calendar.missing dir=%s", raw_dir / CALENDAR_DIRNAME)
        return ([], CalendarReport(None, 0, 0, 0, 0, 0, ()))

    text = path.read_text(encoding="utf-8", errors="replace")
    suspensions = parse_ics(text)
    holidays = [row for row in suspensions if row.is_major_legal_holiday]
    report = CalendarReport(
        source_path=str(path),
        events=text.count("BEGIN:VEVENT"),
        suspended_days=expanded_day_count(text),
        distinct_dates=len({row.date for row in suspensions}),
        meters_suspended_dates=sum(1 for row in suspensions if row.meters_suspended),
        major_holidays=len(holidays),
        unnamed_major_holidays=tuple(
            sorted({row.label for row in holidays if not _names_a_major_holiday(row.label)})
        ),
    )
    return (suspensions, report)


def _meters_suspended(detail: str) -> bool:
    if _METERS_NOT_IN_EFFECT.search(detail):
        return True
    if _METERS_IN_EFFECT.search(detail):
        return False
    # Silence is not permission: an event that does not say either way keeps
    # meters running, which is the reading that cannot produce a false free spot.
    return False


def _names_a_major_holiday(label: str) -> bool:
    folded = label.casefold()
    return any(_fold(name) in _fold(folded) for name in MAJOR_LEGAL_HOLIDAYS)


def _fold(value: str) -> str:
    """Compare holiday names without their apostrophes: DOT writes both forms."""
    return value.casefold().replace("'", "").replace("\u2019", "")


def _as_date(yyyymmdd: str) -> date:
    return datetime.strptime(yyyymmdd, "%Y%m%d").date()


def _expand(first: date, last: date | None) -> Iterable[date]:
    """Dates from `first` up to but not including `last`; at least `first` itself."""
    end = first + timedelta(days=1) if last is None or last <= first else last
    end = min(end, first + timedelta(days=MAX_EVENT_DAYS))
    day = first
    while day < end:
        yield day
        day += timedelta(days=1)
