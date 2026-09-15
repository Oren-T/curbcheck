"""Run the engine against the real database and print what it says, for eyeballing.

Not a unit test: it reads the live `data/curbcheck.sqlite` that `curbcheck sync`
produced, so it cannot run in CI and it makes no claim about code that has not
been built. It exists so that after a sync a human can read three windows' worth
of verdicts at one corner, plus a handful of checks whose right answer is known
from the posted signs and the published rate table, and catch a plausible-looking
but wrong reading of the rules.

The corner is 3 Ave & E 85 St, the block docs/DATA.md §2.3 profiles by hand.

    python scripts/smoke_search.py [--db data/curbcheck.sqlite] [--limit 10]
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from curbcheck import db
from curbcheck.config import DB_PATH, NYC_TZ
from curbcheck.engine.search import SearchResult, search

# 3 Ave & E 85 St.
LON = -73.9557
LAT = 40.7784
WALK_MINUTES_MAX = 8.0

# `search`'s default limit truncates the ranked list, and since legal sorts
# first that would make the verdict tally read "everything here is legal".
ALL_RESULTS = 1_000_000

# Fixed so two runs are comparable. 2026-09-14 is a Monday.
REFERENCE_MONDAY = datetime(2026, 9, 14, tzinfo=NYC_TZ)


def window(day_offset: int, start_hour: int, end_hour: int) -> tuple[datetime, datetime]:
    day = REFERENCE_MONDAY + timedelta(days=day_offset)
    return (day.replace(hour=start_hour), day.replace(hour=end_hour))


WINDOWS: tuple[tuple[str, tuple[datetime, datetime]], ...] = (
    ("Wednesday 10:00-12:00 (meters running)", window(2, 10, 12)),
    ("Sunday 14:00-16:00 (meters not in effect, SPEC §9.3b)", window(6, 14, 16)),
    ("Monday 00:00-03:00 (overnight, before any daytime rule starts)", window(7, 0, 3)),
)

# Segments picked out of a previous run by their sign text. Each one is a rule
# whose answer can be checked against the posted sign and SPEC §13.2's rate
# table without running any of this code; see the report in git history.
CLEANING_SEGMENT = "43471d73663b2394"  # NO PARKING (BROOM) TUESDAY FRIDAY 9AM-10:30AM
THURSDAY_SEGMENT = "26a0b4a2af4fe862"  # NO PARKING (BROOM) MONDAY THURSDAY 9AM-10:30AM
METERED_SEGMENT = "10ea3b40d46180ec"  # 2 HMP 8AM-7PM EXCEPT SUNDAY, ParkNYC Zone M2

# (label, segment, t1, t2, expected verdict, expected money)
HandCheck = tuple[str, str, datetime, datetime, str, str | None]


def hand_checks() -> tuple[HandCheck, ...]:
    def at(month: int, day: int, start: int, end: int) -> tuple[datetime, datetime]:
        return (
            datetime(2026, month, day, start, tzinfo=NYC_TZ),
            datetime(2026, month, day, end, tzinfo=NYC_TZ),
        )

    return (
        ("street cleaning, inside its window (Tue 9-10:30)", CLEANING_SEGMENT, *at(9, 15, 10, 12), "illegal", "0.00"),
        ("street cleaning, after its window", CLEANING_SEGMENT, *at(9, 15, 11, 12), "legal", "0.00"),
        ("street cleaning, on a day it does not run", CLEANING_SEGMENT, *at(9, 16, 10, 12), "legal", "0.00"),
        ("street cleaning, on a normal Thursday", THURSDAY_SEGMENT, *at(11, 19, 10, 11), "illegal", "0.00"),
        # Thanksgiving is in the ASP calendar, so the broom rule lifts (SPEC §9.3c).
        ("street cleaning, on an ASP suspension date", THURSDAY_SEGMENT, *at(11, 26, 10, 11), "legal", "0.00"),
        # Zone M2 is $5.00 for the first hour and $8.25 for the second (SPEC
        # §13.2), and ParkNYC publishes $13.25 as the two-hour max session.
        ("2 HMP metered, 2 hours in Zone M2", METERED_SEGMENT, *at(9, 16, 10, 12), "legal", "13.25"),
        # A 2-hour posted limit cannot cover a 3-hour window (SPEC §9.2).
        ("2 HMP metered, 3-hour window", METERED_SEGMENT, *at(9, 16, 10, 13), "illegal", "21.50"),
        # Major Legal Holiday: meters are not in effect (SPEC §5.6).
        ("2 HMP metered, on Thanksgiving", METERED_SEGMENT, *at(11, 26, 10, 12), "legal", "0.00"),
        # Sunday: same (SPEC §9.3b), and the sign says EXCEPT SUNDAY anyway.
        ("2 HMP metered, on a Sunday", METERED_SEGMENT, *at(9, 20, 14, 16), "legal", "0.00"),
    )


def rank(conn: sqlite3.Connection, t1: datetime, t2: datetime) -> list[SearchResult]:
    return search(
        conn, lon=LON, lat=LAT, t1=t1, t2=t2, walk_minutes_max=WALK_MINUTES_MAX, limit=ALL_RESULTS
    )


def describe(result: SearchResult, index: int) -> str:
    money = "n/a" if result.money is None else f"${result.money}"
    if not result.price_known:
        money += " (unconfirmed)"
    lines = [
        f"{index:2d}. {result.verdict.value.upper():<9} {result.walk_min:4.1f} min walk"
        f"  {money:>18}  conf {result.confidence:.2f}  {result.reg_seg_id}",
        f"      {result.reason}",
    ]
    if result.rate_label:
        lines.append(f"      rate: {result.rate_label}, {result.charged_minutes} charged minutes")
    lines += [f"      caveat: {caveat}" for caveat in result.caveats]
    lines += [
        f"      sign [{sign.sign_code}] {sign.parse_method}/{sign.parse_confidence}: "
        f"{sign.sign_description}"
        for sign in result.signs
    ]
    return "\n".join(lines)


def print_window(conn: sqlite3.Connection, title: str, t1: datetime, t2: datetime, limit: int) -> None:
    results = rank(conn, t1, t2)
    counts: dict[str, int] = {}
    for result in results:
        counts[result.verdict.value] = counts.get(result.verdict.value, 0) + 1
    print(f"\n=== {title}")
    print(f"    {t1:%Y-%m-%d %H:%M} to {t2:%H:%M} {t1:%Z}, {len(results)} segments in range")
    print(f"    verdicts: {counts}")
    for index, result in enumerate(results[:limit], start=1):
        print(describe(result, index))
    priced = [r for r in results if r.metered and r.money not in (None, "0.00")]
    refused = [r for r in results if r.verdict.value == "illegal"]
    ambiguous = [r for r in results if r.verdict.value == "ambiguous"]
    for label, subset in (
        ("nearest priced meters", priced),
        ("nearest refusals", refused),
        ("nearest ambiguous", ambiguous),
    ):
        if not subset:
            continue
        print(f"    -- {label} ({len(subset)} of {len(results)})")
        for index, result in enumerate(subset[:3], start=1):
            print(describe(result, index))


def print_hand_checks(conn: sqlite3.Connection) -> int:
    print("\n=== checks whose answer is known from the sign and the rate table")
    failures = 0
    for label, reg_seg_id, t1, t2, verdict, money in hand_checks():
        found = next((r for r in rank(conn, t1, t2) if r.reg_seg_id == reg_seg_id), None)
        if found is None:
            print(f"    SKIP  {label}: segment {reg_seg_id} is not in this database")
            continue
        ok = found.verdict.value == verdict and found.money == money
        failures += not ok
        print(
            f"    {'ok  ' if ok else 'FAIL'}  {label}\n"
            f"          {t1:%a %Y-%m-%d %H:%M}-{t2:%H:%M}  expected {verdict}/{money}, "
            f"got {found.verdict.value}/{found.money}  ({found.reason})"
        )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--limit", type=int, default=10, help="ranked rows to print per window")
    args = parser.parse_args()
    if not args.db.is_file():
        print(f"no database at {args.db}; run `curbcheck sync` first")
        return 1
    conn = db.connect(args.db, readonly=True)
    try:
        for title, (t1, t2) in WINDOWS:
            print_window(conn, title, t1, t2, args.limit)
        failures = print_hand_checks(conn)
    finally:
        conn.close()
    if failures:
        print(f"\n{failures} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
