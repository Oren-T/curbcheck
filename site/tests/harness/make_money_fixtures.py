"""Write `site/tests/fixtures/money_cases.json` from the reference engine.

`site/static/money.js` reproduces `cost.py`'s `Decimal` results with integers
and `BigInt`, so the check that matters is the one over the real inputs: every
distinct `hour_rates` list in the database crossed with a spread of charged
minutes, and every `(parse_confidence, snap_confidence)` pair the database
actually holds. Run once and commit the JSON; `site/tests/money.test.js`
replays it.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path

from curbcheck.config import DB_PATH
from curbcheck.db import json_string_list
from curbcheck.engine.cost import FINE_HYDRANT_CLASS, FINE_STANDARD, meter_price, risk_dollars

OUT_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "money_cases.json"

# Minutes that land either side of an hour boundary, on one, and well past the
# published rates: the partial hour is the only term `Decimal` cannot represent
# exactly, and 26 and 86 minutes are where a half-cent actually shows up.
CHARGED_MINUTES = (1, 26, 59, 60, 61, 86, 119, 120, 121, 180, 300, 1440)

_RATES_SQL = "SELECT DISTINCT hour_rates FROM meter_rate ORDER BY hour_rates"
# `regulation_segment.confidence` is the snap confidence the risk term pairs
# with the rule's own parse confidence in `search._build_result`.
_CONFIDENCE_SQL = """
    SELECT DISTINCT r.parse_confidence, rs.confidence
    FROM regulation r
    JOIN regulation_segment rs ON rs.reg_seg_id = r.reg_seg_id
    ORDER BY r.parse_confidence, rs.confidence
"""


def distinct_rate_lists(conn: sqlite3.Connection) -> list[list[str]]:
    lists: list[list[str]] = []
    for (raw,) in conn.execute(_RATES_SQL):
        items = json_string_list(raw)
        try:
            for item in items:
                Decimal(item)
        except InvalidOperation:
            continue
        lists.append(items)
    return lists


def meter_cases(conn: sqlite3.Connection) -> list[dict[str, object]]:
    cases: list[dict[str, object]] = []
    for rates in distinct_rate_lists(conn):
        decimals = [Decimal(item) for item in rates]
        for minutes in CHARGED_MINUTES:
            try:
                price: str | None = str(meter_price(decimals, minutes))
            except ValueError:
                # An empty rate list is a price we do not know, not a free spot.
                price = None
            cases.append({"hour_rates": rates, "charged_minutes": minutes, "price": price})
    return cases


def risk_cases(conn: sqlite3.Connection) -> list[dict[str, object]]:
    return [
        {
            "parse_confidence": parse_confidence,
            "snap_confidence": snap_confidence,
            "standard": str(risk_dollars(parse_confidence, snap_confidence)),
            "hydrant": str(
                risk_dollars(parse_confidence, snap_confidence, fine=FINE_HYDRANT_CLASS)
            ),
        }
        for parse_confidence, snap_confidence in conn.execute(_CONFIDENCE_SQL)
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        payload = {
            "fines": {"standard": str(FINE_STANDARD), "hydrant": str(FINE_HYDRANT_CLASS)},
            "meter": meter_cases(conn),
            "risk": risk_cases(conn),
        }
    finally:
        conn.close()

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    size_kb = OUT_PATH.stat().st_size / 1024
    print(
        f"{OUT_PATH}: {len(payload['meter'])} meter cases,"
        f" {len(payload['risk'])} risk cases, {size_kb:.0f} KB"
    )


if __name__ == "__main__":
    main()
