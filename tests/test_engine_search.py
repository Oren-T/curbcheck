"""End-to-end radius search over a synthetic database built with `create_schema`.

The fixture places four curb spans due north of one point so distances are easy
to reason about: 0.001 degrees of latitude is about 111 m.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from decimal import Decimal

import pytest

from curbcheck import db
from curbcheck.config import NYC_TZ
from curbcheck.db import json_string_list
from curbcheck.engine.cost import Weights
from curbcheck.engine.resolve import CALENDAR_MISSING_CAVEAT, Verdict
from curbcheck.engine.search import (
    MAX_MAP_LIMIT,
    SearchResult,
    SearchResults,
    _decimal_list,
    search,
)
from curbcheck.model import Action, ParseMethod, Regulation

ORIGIN_LON = -73.9600
ORIGIN_LAT = 40.7800

SATURDAY_MORNING = (
    datetime.fromisoformat("2026-09-19T10:00").replace(tzinfo=NYC_TZ),
    datetime.fromisoformat("2026-09-19T11:30").replace(tzinfo=NYC_TZ),
)


def line_at(lat: float) -> str:
    return json.dumps(
        {"type": "LineString", "coordinates": [[ORIGIN_LON, lat], [ORIGIN_LON + 0.0005, lat]]}
    )


def add_segment(
    conn: sqlite3.Connection,
    reg_seg_id: str,
    *,
    lat: float,
    sign_ids: list[str],
    confidence: float = 1.0,
    side: str = "E",
) -> None:
    geom = line_at(lat)
    min_lon, min_lat, max_lon, max_lat = db.geojson_bbox(geom)
    conn.execute(
        "INSERT OR IGNORE INTO street_segment (segment_id, street_name, street_norm, geom,"
        " min_lon, min_lat, max_lon, max_lat) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (f"street-{reg_seg_id}", "3 AVENUE", "3 AV", geom, min_lon, min_lat, max_lon, max_lat),
    )
    conn.execute(
        "INSERT INTO regulation_segment (reg_seg_id, segment_id, side, geom, min_lon, min_lat,"
        " max_lon, max_lat, length_ft, capacity_cars, confidence, derived_from)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            reg_seg_id,
            f"street-{reg_seg_id}",
            side,
            geom,
            min_lon,
            min_lat,
            max_lon,
            max_lat,
            220.0,
            10,
            confidence,
            json.dumps(sign_ids),
        ),
    )


def add_placeholder(
    conn: sqlite3.Connection, reg_seg_id: str, *, lat: float, gap_kind: str, side: str = "E"
) -> None:
    """A coverage placeholder: a span the ETL writes with no rules, saying why it is empty."""
    add_segment(conn, reg_seg_id, lat=lat, sign_ids=[], side=side)
    conn.execute(
        "UPDATE regulation_segment SET gap_kind = ? WHERE reg_seg_id = ?", (gap_kind, reg_seg_id)
    )


def add_sign(
    conn: sqlite3.Connection, sign_id: str, description: str, code: str = "PS-127C"
) -> None:
    conn.execute(
        "INSERT INTO sign (sign_id, order_number, sign_code, sign_description) VALUES (?, ?, ?, ?)",
        (sign_id, f"order-{sign_id}", code, description),
    )


def add_regulation(
    conn: sqlite3.Connection,
    reg_id: str,
    reg_seg_id: str,
    reg: Regulation,
    *,
    raw: str,
    method: ParseMethod = ParseMethod.GRAMMAR,
    confidence: float = 0.98,
) -> None:
    conn.execute(
        db.INSERT_REGULATION_SQL,
        db.regulation_to_params(
            reg,
            reg_id=reg_id,
            reg_seg_id=reg_seg_id,
            raw_sign_description=raw,
            parse_method=method,
            parse_confidence=confidence,
        ),
    )


def add_meter_rate(
    conn: sqlite3.Connection,
    blockface_id: str,
    reg_seg_id: str,
    rates: list[str],
    *,
    label: str = "M2",
    side: str = "E",
) -> None:
    conn.execute(
        "INSERT INTO meter_rate (blockface_id, segment_id, side, rate_label, hour_rates)"
        " VALUES (?, ?, ?, ?, ?)",
        (blockface_id, f"street-{reg_seg_id}", side, label, json.dumps(rates)),
    )


METERED_SATURDAY = Regulation(
    action=Action.PARK,
    permitted=True,
    days=[5],
    time_from="08:00",
    time_to="19:00",
    metered=True,
    max_duration_min=180,
)
NO_PARKING = Regulation(action=Action.PARK, permitted=False)


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = db.connect(":memory:")
    db.create_schema(connection)

    add_sign(connection, "sign-meter", "3 HOUR METERED PARKING SATURDAY 8AM-7PM")
    add_segment(connection, "seg-meter", lat=ORIGIN_LAT + 0.0010, sign_ids=["sign-meter"])
    add_regulation(
        connection,
        "reg-meter",
        "seg-meter",
        METERED_SATURDAY,
        raw="3 HOUR METERED PARKING SATURDAY 8AM-7PM",
    )
    add_meter_rate(connection, "bf-1", "seg-meter", ["5.00", "8.25"])

    add_sign(connection, "sign-free", "NO PARKING ANYTIME")
    add_segment(connection, "seg-free", lat=ORIGIN_LAT + 0.0030, sign_ids=["sign-free"])
    add_regulation(connection, "reg-free", "seg-free", NO_PARKING, raw="NO PARKING ANYTIME")

    add_sign(connection, "sign-odd", "NO PARKING EXCEPT ODD SIDE HOLIDAYS PER SW-473")
    add_segment(connection, "seg-odd", lat=ORIGIN_LAT + 0.0020, sign_ids=["sign-odd"])
    add_regulation(
        connection,
        "reg-odd",
        "seg-odd",
        NO_PARKING,
        raw="NO PARKING EXCEPT ODD SIDE HOLIDAYS PER SW-473",
        method=ParseMethod.UNPARSED,
        confidence=0.0,
    )

    add_segment(connection, "seg-blank", lat=ORIGIN_LAT + 0.0015, sign_ids=[])

    add_sign(connection, "sign-far", "NO PARKING ANYTIME")
    add_segment(connection, "seg-far", lat=ORIGIN_LAT + 0.0200, sign_ids=["sign-far"])
    add_regulation(connection, "reg-far", "seg-far", NO_PARKING, raw="NO PARKING ANYTIME")

    connection.execute(
        "INSERT INTO asp_suspension (date, is_major_legal_holiday, meters_suspended, label)"
        " VALUES (?, ?, ?, ?)",
        ("2026-12-25", 1, 1, "Christmas Day"),
    )
    connection.commit()
    return connection


def run(conn: sqlite3.Connection, **overrides: object) -> SearchResults:
    t1, t2 = SATURDAY_MORNING
    kwargs: dict[str, object] = {
        "lon": ORIGIN_LON,
        "lat": ORIGIN_LAT,
        "t1": t1,
        "t2": t2,
        "walk_minutes_max": 10.0,
    }
    kwargs.update(overrides)
    return search(conn, **kwargs)  # type: ignore[arg-type]


def run_search(conn: sqlite3.Connection, **overrides: object) -> list[SearchResult]:
    """Every result the search returned, ranked legal first — the API's own order."""
    return run(conn, **overrides).all


def test_search_returns_every_segment_within_the_walk_radius(conn: sqlite3.Connection) -> None:
    results = run_search(conn)

    assert {result.reg_seg_id for result in results} == {
        "seg-meter",
        "seg-free",
        "seg-odd",
        "seg-blank",
    }


def test_segments_beyond_the_radius_are_excluded(conn: sqlite3.Connection) -> None:
    """10 walk minutes is a 618 m straight-line radius; seg-far is 2.2 km away."""
    results = run_search(conn)

    assert "seg-far" not in {result.reg_seg_id for result in results}


def test_a_tighter_radius_drops_the_farther_segments(conn: sqlite3.Connection) -> None:
    """2 walk minutes is a 124 m radius: it reaches seg-meter at 111 m but not seg-blank at 166 m."""
    results = run_search(conn, walk_minutes_max=2.0)

    assert {result.reg_seg_id for result in results} == {"seg-meter"}


def test_results_are_ordered_legal_then_ambiguous_then_illegal_then_no_data(
    conn: sqlite3.Connection,
) -> None:
    results = run_search(conn)

    assert [result.verdict for result in results] == [
        Verdict.LEGAL,
        Verdict.AMBIGUOUS,
        Verdict.ILLEGAL,
        Verdict.NO_DATA,
    ]


def test_the_metered_segment_is_priced_progressively(conn: sqlite3.Connection) -> None:
    """90 charged minutes on M2 rates: $5.00 for the first hour, half of $8.25 for the rest."""
    result = next(r for r in run_search(conn) if r.reg_seg_id == "seg-meter")

    assert result.money == "9.13"
    assert result.price_known
    assert result.rate_label == "M2"
    assert result.charged_minutes == 90


def test_a_metered_segment_without_a_published_rate_reports_an_unknown_price(
    conn: sqlite3.Connection,
) -> None:
    conn.execute("DELETE FROM meter_rate")

    result = next(r for r in run_search(conn) if r.reg_seg_id == "seg-meter")

    assert result.money is None
    assert not result.price_known
    assert "no published rate" in " ".join(result.caveats)


def test_two_meter_zones_on_one_blockface_quote_the_dearer_and_flag_it(
    conn: sqlite3.Connection,
) -> None:
    """SPEC §13.1(b): show the range and say confirm at the meter rather than guessing."""
    add_meter_rate(conn, "bf-2", "seg-meter", ["5.50", "9.00"], label="M1")

    result = next(r for r in run_search(conn) if r.reg_seg_id == "seg-meter")

    assert result.money == "10.00"
    assert not result.price_known
    assert "confirm at the meter" in " ".join(result.caveats)


def test_an_unmetered_segment_is_free(conn: sqlite3.Connection) -> None:
    result = next(r for r in run_search(conn) if r.reg_seg_id == "seg-free")

    assert result.money == "0.00"
    assert result.price_known


def test_each_result_carries_its_raw_sign_text_and_parse_metadata(conn: sqlite3.Connection) -> None:
    result = next(r for r in run_search(conn) if r.reg_seg_id == "seg-odd")

    assert [sign.sign_id for sign in result.signs] == ["sign-odd"]
    assert result.signs[0].sign_description == "NO PARKING EXCEPT ODD SIDE HOLIDAYS PER SW-473"
    assert result.signs[0].parse_method == ParseMethod.UNPARSED.value
    assert result.signs[0].sign_code == "PS-127C"
    assert result.signs[0].order_number == "order-sign-odd"


def test_a_segment_with_no_signs_is_no_data_with_no_sign_list(conn: sqlite3.Connection) -> None:
    result = next(r for r in run_search(conn) if r.reg_seg_id == "seg-blank")

    assert result.verdict is Verdict.NO_DATA
    assert result.signs == []
    assert result.capacity_cars == 10


def test_a_placeholder_span_reports_why_it_has_no_data(conn: sqlite3.Connection) -> None:
    """`gap_kind` is what lets the grey state say "no signs" or "signs we could not place"."""
    add_placeholder(conn, "seg-nosigns", lat=ORIGIN_LAT + 0.0013, gap_kind="no_signs")
    add_placeholder(conn, "seg-unmatched", lat=ORIGIN_LAT + 0.0014, gap_kind="unmatched_signs")

    by_id = {result.reg_seg_id: result for result in run_search(conn)}

    assert by_id["seg-nosigns"].verdict is Verdict.NO_DATA
    assert by_id["seg-nosigns"].gap_kind == "no_signs"
    assert by_id["seg-unmatched"].gap_kind == "unmatched_signs"
    assert by_id["seg-blank"].gap_kind is None


def test_a_database_without_gap_kind_still_searches(conn: sqlite3.Connection) -> None:
    """Older snapshots predate the column; they lose the reason, not the search."""
    conn.execute("ALTER TABLE regulation_segment DROP COLUMN gap_kind")

    results = run_search(conn)

    assert len(results) == 4
    assert all(result.gap_kind is None for result in results)


def test_each_result_carries_a_human_street_label(conn: sqlite3.Connection) -> None:
    """SPEC §10 wants a human label; the cards used to show the opaque id instead."""
    conn.execute(
        "INSERT INTO street_node (node_id, lon, lat, street_names) VALUES (?,?,?,?)",
        ("n85", ORIGIN_LON, ORIGIN_LAT, json.dumps(["3 AVENUE", "E 85 ST"])),
    )
    conn.execute(
        "INSERT INTO street_node (node_id, lon, lat, street_names) VALUES (?,?,?,?)",
        ("n86", ORIGIN_LON, ORIGIN_LAT, json.dumps(["E 86 ST", "3 AVENUE"])),
    )
    conn.execute(
        "UPDATE street_segment SET from_node = ?, to_node = ? WHERE segment_id = ?",
        ("n85", "n86", "street-seg-meter"),
    )

    result = next(r for r in run_search(conn) if r.reg_seg_id == "seg-meter")

    assert result.street_name == "3 AVENUE, east side, E 85 ST → E 86 ST"


def test_a_corner_is_never_labelled_as_its_own_cross_street(conn: sqlite3.Connection) -> None:
    """CSCL writes `E 85 ST` on the node and `E  85 ST` on the segment; both are one street."""
    conn.execute(
        "INSERT INTO street_node (node_id, lon, lat, street_names) VALUES (?,?,?,?)",
        ("n-dup", ORIGIN_LON, ORIGIN_LAT, json.dumps(["E  85 ST", "e 85 st", "2 AVENUE"])),
    )
    conn.execute(
        "UPDATE street_segment SET street_name = ?, from_node = ? WHERE segment_id = ?",
        ("E  85 ST", "n-dup", "street-seg-meter"),
    )

    result = next(r for r in run_search(conn) if r.reg_seg_id == "seg-meter")

    assert result.street_name == "E  85 ST, east side, at 2 AVENUE"


def test_a_span_whose_chain_ends_name_no_cross_street_is_still_labelled(
    conn: sqlite3.Connection,
) -> None:
    result = next(r for r in run_search(conn) if r.reg_seg_id == "seg-free")

    assert result.street_name == "3 AVENUE, east side"


def test_geometry_comes_back_as_geojson(conn: sqlite3.Connection) -> None:
    result = next(r for r in run_search(conn) if r.reg_seg_id == "seg-meter")

    assert result.geometry["type"] == "LineString"
    assert result.geometry["coordinates"][0] == [ORIGIN_LON, ORIGIN_LAT + 0.0010]


def test_walk_minutes_grow_with_distance(conn: sqlite3.Connection) -> None:
    results = {r.reg_seg_id: r.walk_min for r in run_search(conn)}

    assert results["seg-meter"] < results["seg-blank"] < results["seg-free"]
    assert results["seg-meter"] == pytest.approx(1.8, abs=0.3)


def test_the_limit_caps_the_ranked_legal_list_only(conn: sqlite3.Connection) -> None:
    """docs/VALIDATION.md U1: `limit` must never cost the map its illegal curb."""
    found = run(conn, limit=1)

    assert [result.verdict for result in found.legal] == [Verdict.LEGAL]
    assert {result.verdict for result in found.others} == {
        Verdict.AMBIGUOUS,
        Verdict.ILLEGAL,
        Verdict.NO_DATA,
    }


def test_counts_are_measured_before_either_cap(conn: sqlite3.Connection) -> None:
    found = run(conn, limit=1, map_limit=1)

    assert len(found.legal) == 1
    assert len(found.others) == 1
    assert found.counts.legal == 1
    assert found.counts.ambiguous == 1
    assert found.counts.illegal == 1
    assert found.counts.no_data == 1
    assert found.counts.total == 4


def test_the_map_cap_keeps_the_nearest_of_the_other_verdicts(conn: sqlite3.Connection) -> None:
    """seg-blank at 166 m is nearer than seg-odd at 222 m and seg-free at 333 m."""
    found = run(conn, map_limit=1)

    assert [result.reg_seg_id for result in found.others] == ["seg-blank"]


def test_a_dense_band_of_legal_curb_cannot_push_the_illegal_off_the_map(
    conn: sqlite3.Connection,
) -> None:
    """The U1 measurement, in miniature: 150 legal and 50 illegal spans, limit 100."""
    for index in range(150):
        add_sign(conn, f"sign-ok-{index}", "PARKING PERMITTED")
        add_segment(
            conn,
            f"seg-ok-{index}",
            lat=ORIGIN_LAT + 0.0001 + index * 0.000001,
            sign_ids=[f"sign-ok-{index}"],
        )
        add_regulation(
            conn,
            f"reg-ok-{index}",
            f"seg-ok-{index}",
            Regulation(action=Action.PARK, permitted=True),
            raw="PARKING PERMITTED",
        )
    for index in range(50):
        add_sign(conn, f"sign-no-{index}", "NO PARKING ANYTIME")
        add_segment(
            conn,
            f"seg-no-{index}",
            lat=ORIGIN_LAT + 0.0002 + index * 0.000001,
            sign_ids=[f"sign-no-{index}"],
        )
        add_regulation(
            conn, f"reg-no-{index}", f"seg-no-{index}", NO_PARKING, raw="NO PARKING ANYTIME"
        )

    found = run(conn, limit=100)

    assert len(found.legal) == 100
    assert all(result.verdict is Verdict.LEGAL for result in found.legal)
    assert sum(1 for result in found.others if result.verdict is Verdict.ILLEGAL) == 51
    assert found.counts.legal == 151
    assert found.counts.illegal == 51
    assert found.counts.total == 204


def test_the_map_limit_is_clamped_to_its_hard_maximum(conn: sqlite3.Connection) -> None:
    """A request cannot ask the map for more features than MAX_MAP_LIMIT."""
    assert run(conn, map_limit=MAX_MAP_LIMIT * 10).others == run(conn).others


def test_a_limit_below_one_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        run_search(conn, limit=0)
    with pytest.raises(ValueError, match="at least 1"):
        run_search(conn, map_limit=0)


def test_weights_change_the_ranking(conn: sqlite3.Connection) -> None:
    """With money weighted heavily the priced metered segment falls behind a free one."""
    add_sign(conn, "sign-free-near", "NO PARKING ANYTIME")
    add_segment(conn, "seg-free-near", lat=ORIGIN_LAT + 0.0012, sign_ids=["sign-free-near"])
    add_regulation(
        conn,
        "reg-free-near",
        "seg-free-near",
        Regulation(action=Action.PARK, permitted=True, max_duration_min=None),
        raw="PARKING PERMITTED",
    )

    money_matters = run_search(conn, weights=Weights(walk=1.0, money=5.0, risk=0.0))
    walk_matters = run_search(conn, weights=Weights(walk=5.0, money=0.0, risk=0.0))

    assert money_matters[0].reg_seg_id == "seg-free-near"
    assert walk_matters[0].reg_seg_id == "seg-meter"


def test_the_rule_stack_is_loaded_in_a_fixed_number_of_queries(conn: sqlite3.Connection) -> None:
    """Guards against an N+1: one query per table, whatever the candidate count."""
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    run_search(conn)
    baseline = len(statements)
    statements.clear()

    for index in range(20):
        add_sign(conn, f"sign-bulk-{index}", "NO PARKING ANYTIME")
        add_segment(
            conn,
            f"seg-bulk-{index}",
            lat=ORIGIN_LAT + 0.0005 + index * 0.00001,
            sign_ids=[f"sign-bulk-{index}"],
        )
        add_regulation(
            conn, f"reg-bulk-{index}", f"seg-bulk-{index}", NO_PARKING, raw="NO PARKING ANYTIME"
        )
    statements.clear()

    run_search(conn)

    assert len(statements) == baseline
    conn.set_trace_callback(None)


def test_a_calendar_override_changes_the_verdict(conn: sqlite3.Connection) -> None:
    """A Christmas Day window: the meter is free but the segment is still parkable."""
    christmas = (
        datetime.fromisoformat("2026-12-25T10:00").replace(tzinfo=NYC_TZ),
        datetime.fromisoformat("2026-12-25T11:00").replace(tzinfo=NYC_TZ),
    )
    conn.execute(
        db.INSERT_REGULATION_SQL,
        db.regulation_to_params(
            Regulation(action=Action.PARK, permitted=True, metered=True, max_duration_min=180),
            reg_id="reg-meter-daily",
            reg_seg_id="seg-meter",
            raw_sign_description="3 HOUR METERED PARKING 9AM-7PM",
            parse_method=ParseMethod.GRAMMAR,
            parse_confidence=0.98,
        ),
    )

    results = run_search(conn, t1=christmas[0], t2=christmas[1])
    result = next(r for r in results if r.reg_seg_id == "seg-meter")

    assert result.verdict is Verdict.LEGAL
    assert result.charged_minutes == 0
    assert result.money == "0.00"


def test_a_missing_calendar_puts_the_caveat_on_every_result(conn: sqlite3.Connection) -> None:
    """Without the calendar a holiday reads as an ordinary day, so every verdict is a guess."""
    conn.execute("DELETE FROM asp_suspension")

    results = run_search(conn)

    assert results
    for result in results:
        assert CALENDAR_MISSING_CAVEAT in result.caveats


def test_the_etl_can_declare_the_calendar_missing_even_with_rows(
    conn: sqlite3.Connection,
) -> None:
    """`sync_meta.calendar_missing` is the ETL saying it found no calendar file."""
    conn.execute("INSERT INTO sync_meta (key, value) VALUES ('calendar_missing', '1')")

    result = run_search(conn)[0]

    assert CALENDAR_MISSING_CAVEAT in result.caveats


def test_a_database_with_a_calendar_carries_no_calendar_caveat(conn: sqlite3.Connection) -> None:
    for result in run_search(conn):
        assert CALENDAR_MISSING_CAVEAT not in result.caveats


def test_an_empty_database_returns_nothing(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM regulation")
    conn.execute("DELETE FROM regulation_segment")

    found = run(conn)

    assert found.all == []
    assert found.counts.total == 0


def test_an_unreadable_geometry_skips_its_row_and_logs_one_warning(
    conn: sqlite3.Connection, caplog
) -> None:
    """A truncated snapshot used to answer every search with a 500 (SECURITY.md residual 9)."""
    conn.execute("UPDATE regulation_segment SET geom = ? WHERE reg_seg_id = ?", ("{", "seg-free"))
    conn.execute(
        "UPDATE regulation_segment SET geom = ? WHERE reg_seg_id = ?",
        ('{"type": "LineString"}', "seg-odd"),
    )

    with caplog.at_level(logging.WARNING, logger="curbcheck.engine.search"):
        results = run_search(conn)

    assert {result.reg_seg_id for result in results} == {"seg-meter", "seg-blank"}
    warnings = [record.getMessage() for record in caplog.records]
    assert warnings == ["skipped 2 regulation_segment row(s) with unreadable geometry"]


def test_a_readable_database_logs_no_geometry_warning(conn: sqlite3.Connection, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="curbcheck.engine.search"):
        run_search(conn)

    assert caplog.records == []


def test_an_unreadable_json_column_reads_as_empty_rather_than_raising():
    """A half-built snapshot must degrade, not 500 (CLAUDE.md: data/ is untrusted)."""
    for broken in ("not json", "", "{", '{"a": 1}', "null", "12"):
        assert json_string_list(broken) == []
        assert _decimal_list(broken) == []

    assert json_string_list('["sign-1", "sign-2"]') == ["sign-1", "sign-2"]
    assert _decimal_list('["4.50"]') == [Decimal("4.50")]


def add_overlapping_pair(conn: sqlite3.Connection) -> None:
    """Two spans on one centerline side whose curb lines overlap, as D20 produces them.

    A `NO STANDING ANYTIME <->` post and a `2 HMP <->` post 100 ft apart each
    extend to the other, so both claim the curb between them. Each is its own
    row with its own rule stack, so nothing in `resolve` can see the conflict.
    """
    lat = ORIGIN_LAT + 0.0012
    for reg_seg_id, from_lon, to_lon in (
        ("seg-ban", ORIGIN_LON, ORIGIN_LON + 0.0006),
        ("seg-hmp", ORIGIN_LON + 0.0002, ORIGIN_LON + 0.0012),
    ):
        geom = json.dumps({"type": "LineString", "coordinates": [[from_lon, lat], [to_lon, lat]]})
        min_lon, min_lat, max_lon, max_lat = db.geojson_bbox(geom)
        conn.execute(
            "INSERT INTO regulation_segment (reg_seg_id, segment_id, side, geom, min_lon,"
            " min_lat, max_lon, max_lat, length_ft, capacity_cars, confidence, derived_from)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                reg_seg_id,
                "street-seg-meter",
                "E",
                geom,
                min_lon,
                min_lat,
                max_lon,
                max_lat,
                150.0,
                6,
                1.0,
                json.dumps([f"sign-{reg_seg_id}"]),
            ),
        )
    add_sign(conn, "sign-seg-ban", "NO STANDING ANYTIME <->", code="PS-2G")
    add_regulation(
        conn,
        "reg-ban",
        "seg-ban",
        Regulation(action=Action.STAND, permitted=False),
        raw="NO STANDING ANYTIME <->",
    )
    add_sign(conn, "sign-seg-hmp", "2 HMP 8AM-7PM <->")
    add_regulation(
        conn,
        "reg-hmp",
        "seg-hmp",
        METERED_SATURDAY,
        raw="2 HMP 8AM-7PM <->",
    )
    conn.commit()


def test_a_legal_span_a_prohibition_overlaps_is_ambiguous_not_legal(
    conn: sqlite3.Connection,
) -> None:
    """SPEC §8.6: a permissive span reaching over a ban must never read green."""
    add_overlapping_pair(conn)

    by_id = {result.reg_seg_id: result for result in run_search(conn)}

    assert by_id["seg-ban"].verdict is Verdict.ILLEGAL
    assert by_id["seg-hmp"].verdict is Verdict.AMBIGUOUS
    assert "conflict" in by_id["seg-hmp"].reason
    # The span that is not contested keeps its verdict.
    assert by_id["seg-meter"].verdict is Verdict.LEGAL


def test_spans_that_only_touch_at_a_shared_end_do_not_contest_each_other(
    conn: sqlite3.Connection,
) -> None:
    """Abutting spans tile the curb; treating a shared endpoint as a conflict
    would make every blockface with two regimes ambiguous."""
    add_overlapping_pair(conn)
    conn.execute(
        "UPDATE regulation_segment SET geom = ?, min_lon = ? WHERE reg_seg_id = ?",
        (
            json.dumps(
                {
                    "type": "LineString",
                    "coordinates": [
                        [ORIGIN_LON + 0.0006, ORIGIN_LAT + 0.0012],
                        [ORIGIN_LON + 0.0012, ORIGIN_LAT + 0.0012],
                    ],
                }
            ),
            ORIGIN_LON + 0.0006,
            "seg-hmp",
        ),
    )
    conn.commit()

    by_id = {result.reg_seg_id: result for result in run_search(conn)}

    assert by_id["seg-hmp"].verdict is Verdict.LEGAL


def test_a_prohibition_on_the_other_side_of_the_street_does_not_contest(
    conn: sqlite3.Connection,
) -> None:
    add_overlapping_pair(conn)
    conn.execute("UPDATE regulation_segment SET side = 'W' WHERE reg_seg_id = 'seg-ban'")
    conn.commit()

    by_id = {result.reg_seg_id: result for result in run_search(conn)}

    assert by_id["seg-hmp"].verdict is Verdict.LEGAL
