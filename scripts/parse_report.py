"""Print the sign-grammar coverage report and write the residue TSV.

Thin wrapper; the logic lives in `curbcheck.etl.parse.report` so that
`curbcheck parse-report` and this script cannot drift apart.

    python scripts/parse_report.py
"""

from __future__ import annotations

from curbcheck.etl.parse import report


def main() -> int:
    try:
        _coverage, text = report.run()
    except report.CorpusMissingError as error:
        print(error)
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
