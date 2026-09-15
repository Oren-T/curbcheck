"""Command-line entry point. `curbcheck sync` builds the database, `curbcheck serve` runs the app.

Both subcommands are stubs until the ETL (docs/BUILD_STATUS Phase B) and the
API (Phase C) land.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from curbcheck.config import BIND_HOST, PORT


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="curbcheck", description=__doc__.splitlines()[0])
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("sync", help="fetch source data and build data/curbcheck.sqlite")
    subcommands.add_parser("serve", help=f"serve the API and UI on http://{BIND_HOST}:{PORT}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(f"{args.command}: not implemented yet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
