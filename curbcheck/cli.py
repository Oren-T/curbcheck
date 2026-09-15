"""Command-line entry point. `curbcheck sync` builds the database, `curbcheck serve` runs the app."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from curbcheck.api.app import create_app
from curbcheck.config import BIND_HOST, DATA_DIR, DB_PATH, PORT, RAW_DIR, REPO_ROOT
from curbcheck.etl import build, fetch
from curbcheck.etl.parse import report as parse_report

# The frontend is checked in; the basemap is a 23 MB gitignored artifact that
# `scripts/fetch_basemap.py` drops next to the database (docs/DECISIONS.md D11).
WEB_DIR = REPO_ROOT / "web"
BASEMAP_PATH = DATA_DIR / "basemap" / "manhattan.pmtiles"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="curbcheck", description=__doc__.splitlines()[0])
    subcommands = parser.add_subparsers(dest="command", required=True)
    sync = subcommands.add_parser("sync", help="fetch source data and build data/curbcheck.sqlite")
    sync.add_argument(
        "--offline",
        action="store_true",
        help="build from the snapshots already in data/raw, making no network requests",
    )
    sync.add_argument(
        "--max-age-days",
        type=float,
        default=fetch.DEFAULT_MAX_AGE_DAYS,
        help="refetch a dataset only when its snapshot is older than this",
    )
    coverage = subcommands.add_parser(
        "parse-report", help="measure grammar coverage over the description corpus"
    )
    coverage.add_argument(
        "--corpus",
        type=Path,
        default=parse_report.DEFAULT_CORPUS,
        help="TSV of count/sign_codes/sign_description, from scripts/explore_signs.py",
    )
    coverage.add_argument(
        "--residue",
        type=Path,
        default=parse_report.DEFAULT_RESIDUE,
        help="where to write the unparsed and partially parsed strings",
    )
    serve = subcommands.add_parser(
        "serve", help=f"serve the API and UI on http://{BIND_HOST}:{PORT}"
    )
    # No --host. SPEC §3.4 binds the server to 127.0.0.1 and a flag that could
    # widen that is the bug the rule exists to prevent.
    serve.add_argument("--port", type=int, default=PORT, help="TCP port on 127.0.0.1")
    serve.add_argument("--db", type=Path, default=DB_PATH, help="path to the SQLite database")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if args.command == "sync":
        return _sync(offline=args.offline, max_age_days=args.max_age_days)
    if args.command == "serve":
        return _serve(port=args.port, db_path=args.db)
    if args.command == "parse-report":
        return _parse_report(corpus=args.corpus, residue=args.residue)
    print(f"{args.command}: not implemented yet")
    return 0


def _sync(*, offline: bool, max_age_days: float) -> int:
    fetch.fetch_all(raw_dir=RAW_DIR, max_age_days=max_age_days, offline=offline)
    stats = build.build_all(RAW_DIR, DB_PATH)
    for line in _sync_summary(stats):
        print(line)
    return 0


def _sync_summary(stats: build.BuildStats) -> list[str]:
    """The four lines `curbcheck sync` prints: geometry, rules, price, calendar.

    Every count that says how much of the city we could *not* read is here on
    purpose (SPEC §11): a silent sync that halved its coverage would otherwise
    look exactly like a good one.
    """
    snap = stats.snap
    parse = stats.parse
    meters = stats.meters
    calendar = stats.calendar
    by_method = ", ".join(
        f"{method} {count}" for method, count in sorted(parse.rows_by_parse_method.items())
    )
    return [
        f"{DB_PATH}: {snap.signs} signs, {snap.matched} snapped "
        f"({100 * snap.matched_share:.1f}%), {stats.segments.segments} regulation segments, "
        f"{100 * snap.blockface_side_share:.1f}% of blockface-sides covered, "
        f"{stats.elapsed_s:.1f}s",
        f"  rules:    {parse.regulation_rows} ({by_method}); "
        f"{parse.segments_with_rules} segments with rules, "
        f"{parse.meta_segments} made ambiguous by a meta sign",
        f"  meters:   {meters.rows_written} rate rows "
        f"({meters.rate_zone_fallbacks} from a rate zone), "
        f"{meters.metered_segments_without_rate} of {meters.metered_segments} "
        f"metered segments have no rate",
        f"  calendar: {calendar.suspended_days} suspended days over "
        f"{calendar.distinct_dates} dates, {calendar.major_holidays} major legal holidays"
        + ("" if calendar.source_path else " (NO CALENDAR FILE FOUND)"),
    ]


def _parse_report(*, corpus: Path, residue: Path) -> int:
    try:
        _coverage, text = parse_report.run(corpus, residue)
    except parse_report.CorpusMissingError as error:
        print(error)
        return 1
    print(text)
    return 0


def _serve(*, port: int, db_path: Path) -> int:
    """Run the API and UI on 127.0.0.1.

    Starts even with no database rather than refusing to: the friendlier
    failure is a page that loads and says what to run next, which is what
    `/api/health` and the frontend's banner give. Refusing to start would leave
    a first-time user with a terminal message and no app.
    """
    if not db_path.is_file():
        print(f"no database at {db_path}; run `curbcheck sync`. Starting anyway.")

    app = create_app(db_path, web_dir=WEB_DIR, basemap_path=BASEMAP_PATH)
    print(f"CurbCheck on http://{BIND_HOST}:{port}")
    # access_log=False: uvicorn logs the full request line, which puts the
    # address typed into the autocomplete (`GET /api/geocode?q=...`) on the
    # terminal. `api.app.AccessLogMiddleware` logs the same request without its
    # query string instead (docs/SECURITY.md, threat T6).
    uvicorn.run(app, host=BIND_HOST, port=port, workers=1, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
