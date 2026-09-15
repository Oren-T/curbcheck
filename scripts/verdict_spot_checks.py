"""Five verdicts whose right answer is known without running the engine.

Each case is a rule from SPEC §9.2/§9.3 or §13.2 that an eyeball check of the
posted sign and the published calendar settles, on a real segment of the live
`data/curbcheck.sqlite`. They complement `scripts/smoke_search.py`, which walks
one corner; these pin the five calendar and duration rules that a wrong reading
would turn into a false "legal". Not a unit test: it needs a synced database.

    python scripts/verdict_spot_checks.py [--db data/curbcheck.sqlite]

Exit status is the number of checks that disagreed with the expected answer.
"""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from curbcheck import db
from curbcheck.config import DB_PATH, NYC_TZ
from curbcheck.engine.search import search

WALK_MINUTES = 3.0
ALL_RESULTS = 1_000_000


@dataclass(frozen=True)
class Case:
    label: str
    reg_seg_id: str
    lat: float
    lon: float
    t1: datetime
    t2: datetime
    verdict: str
    money: str | None
    reason_holds: str
    """A substring the reason or a caveat must contain for the check to pass."""


def at(year: int, month: int, day: int, start: int, end: int) -> tuple[datetime, datetime]:
    return (
        datetime(year, month, day, start, tzinfo=NYC_TZ),
        datetime(year, month, day, end, tzinfo=NYC_TZ),
    )


CASES = (
    # SPEC §5.6 / §13.2: meters are not in effect on the six Major Legal Holidays,
    # and `asp_suspension` marks 2026-11-26 `meters_suspended`. The sign is a
    # plain two-hour meter, so the window is legal and the meter charges nothing.
    Case(
        "metered block on Thanksgiving: meters off, still legal",
        "3dc27dd9e77b6179",
        40.727174,
        -73.984167,
        *at(2026, 11, 26, 10, 12),
        "legal",
        "0.00",
        "",
    ),
    # "MOON & STARS (SYMBOLS) NO STANDING 11PM-7AM ALL DAYS": a window that starts
    # at midnight is inside the overnight ban on every day of the week, and the
    # ban's own calendar says ALL DAYS, so no holiday lifts it.
    Case(
        "MOON & STARS overnight ban, Monday 00:00-03:00",
        "267900ccaef7f809",
        40.732779,
        -73.998088,
        *at(2026, 9, 21, 0, 3),
        "illegal",
        "0.00",
        "no standing",
    ),
    # SPEC §9.3c: a street-cleaning rule lifts on an ASP suspension date. Holy
    # Thursday 2026-04-02 is in the calendar and is a Thursday, which is one of
    # this sign's two broom days, so the rule that would otherwise bite does not.
    Case(
        "broom block on an ASP suspension Thursday (Holy Thursday)",
        "64a79e8c0273121e",
        40.712408,
        -73.978553,
        *at(2026, 4, 2, 11, 12),
        "legal",
        "0.00",
        "suspended",
    ),
    # SPEC §9.2: legal only if legal for the ENTIRE window, so a posted limit
    # shorter than the window refuses the spot. The spec's own example is a
    # one-hour meter, but every one of Manhattan's 360 one-hour metered rules is
    # commercial-only, which would refuse a passenger car for the wrong reason;
    # the two-hour passenger meter below exercises the same rule and the reason
    # string has to name the limit, not the vehicle class.
    Case(
        "3-hour window on a 2-hour passenger meter",
        "3dc27dd9e77b6179",
        40.727174,
        -73.984167,
        *at(2026, 9, 16, 10, 13),
        "illegal",
        None,
        "posted limit of 120 min is shorter",
    ),
    # The same rule on a one-hour meter, which in Manhattan is always commercial:
    # two independent reasons to refuse, and the verdict must still be illegal.
    Case(
        "3-hour window on a 1-hour (commercial) meter",
        "cc02e52e071c8824",
        40.753260,
        -73.999962,
        *at(2026, 9, 16, 10, 13),
        "illegal",
        None,
        "",
    ),
    # A holiday suspends alternate-side and the meters, never a standing ban.
    Case(
        "NO STANDING ANYTIME on Christmas Day",
        "46ff88541bc0a44d",
        40.748170,
        -73.988841,
        *at(2026, 12, 25, 10, 12),
        "illegal",
        "0.00",
        "no standing",
    ),
)


def run(conn: sqlite3.Connection, case: Case) -> tuple[bool, str]:
    results = search(
        conn,
        lon=case.lon,
        lat=case.lat,
        t1=case.t1,
        t2=case.t2,
        walk_minutes_max=WALK_MINUTES,
        limit=ALL_RESULTS,
        map_limit=ALL_RESULTS,
    ).all
    found = next((r for r in results if r.reg_seg_id == case.reg_seg_id), None)
    if found is None:
        return False, f"segment {case.reg_seg_id} is not in this database"

    verdict_ok = found.verdict.value == case.verdict
    money_ok = case.money is None or found.money == case.money
    haystack = " | ".join([found.reason, *found.caveats]).lower()
    reason_ok = not case.reason_holds or case.reason_holds.lower() in haystack
    detail = (
        f"{found.verdict.value}/{found.money}  {found.reason}\n"
        + "\n".join(f"            caveat: {caveat}" for caveat in found.caveats)
        + "\n"
        + "\n".join(f"            sign: {sign.sign_description}" for sign in found.signs)
    )
    return verdict_ok and money_ok and reason_ok, detail


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DB_PATH)
    args = parser.parse_args()
    if not args.db.is_file():
        print(f"no database at {args.db}; run `curbcheck sync` first")
        return 1

    conn = db.connect(args.db, readonly=True)
    failures = 0
    try:
        for case in CASES:
            ok, detail = run(conn, case)
            failures += not ok
            money = "any" if case.money is None else f"${case.money}"
            print(
                f"{'ok  ' if ok else 'FAIL'}  {case.label}\n"
                f"        {case.t1:%a %Y-%m-%d %H:%M}-{case.t2:%H:%M}"
                f"  expected {case.verdict}/{money}"
                + (f" mentioning {case.reason_holds!r}" if case.reason_holds else "")
                + f"\n        got {detail}"
            )
    finally:
        conn.close()
    if failures:
        print(f"\n{failures} check(s) failed")
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
