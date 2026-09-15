"""Folding AddressPoint and CommonPlace into the suggester's vocabulary index."""

from __future__ import annotations

import json

import pytest
from test_etl_fixtures import address_point_row, address_point_rows, common_place_rows, grid_rows

from curbcheck import db
from curbcheck.etl.addresses import (
    NICKNAMES,
    PLACE_ALIASES,
    build_address_index,
    fold,
    name_words,
    place_spellings,
    stage_address_points,
    stage_places,
    street_variants,
)
from curbcheck.etl.stage import stage_centerline
from curbcheck.etl.streets import build_graph, normalize_street_name


@pytest.fixture
def indexed():
    """The fixture grid's centerline plus its address points, as a built index."""
    conn = db.connect(":memory:")
    db.create_schema(conn)
    graph = build_graph(stage_centerline(grid_rows()))
    for segment in graph.segments.values():
        conn.execute(
            "INSERT INTO street_segment (segment_id, street_name, street_norm, geom,"
            " min_lon, min_lat, max_lon, max_lat) VALUES (?, ?, ?, ?, 0, 0, 0, 0)",
            (
                segment.segment_id,
                segment.street_name,
                segment.street_norm,
                json.dumps({"type": "LineString", "coordinates": list(segment.line_deg.coords)}),
            ),
        )
    for node in graph.nodes.values():
        conn.execute(
            "INSERT INTO street_node (node_id, lon, lat, street_names) VALUES (?, ?, ?, ?)",
            (node.node_id, node.lon, node.lat, json.dumps(sorted(node.street_norms))),
        )
    report = build_address_index(
        conn, address_rows=address_point_rows(), place_rows=common_place_rows()
    )
    yield conn, report
    conn.close()


def test_fold_agrees_with_the_etl_normalizer_and_adds_the_digit_ordinals():
    # Anything the ETL already folds has to fold the same way here, or the
    # index keys and the query keys stop meeting.
    for name in ("EAST 86 STREET", "W  48 ST", "FIFTH AVENUE", "FDR DRIVE"):
        assert fold(name) == normalize_street_name(name)
    assert fold("e 86th st.") == "E 86 ST"
    assert fold("3rd ave") == "3 AVE"
    assert fold("third avenue") == "3 AVE"
    assert fold("") == ""


def test_the_king_boulevard_alias_reaches_the_centerline_name():
    # 43 AddressPoint doors are filed under this name and CSCL has no segment
    # for it (docs/ux/AUTOCOMPLETE_RESEARCH.md §1.3).
    assert fold("DR M L KING JR BLVD") == "W 125 ST"


def test_street_variants_cover_the_spellings_a_person_types():
    assert street_variants("E 86 ST") == {"E 86 ST", "E 86", "86 ST", "86"}
    assert street_variants("3 AVE") == {"3 AVE", "3"}
    assert "HOUSTON" in street_variants("E HOUSTON ST")
    assert street_variants("BROADWAY") == {"BROADWAY"}


def test_name_words_drop_stopwords_and_fold_the_spoken_cardinal():
    assert name_words("One World Trade Center") == ["1", "WORLD", "TRADE", "CENTER"]
    assert name_words("The Museum of Modern Art") == ["MUSEUM", "MODERN", "ART"]


def test_a_name_keeps_its_word_order_so_the_start_of_it_can_be_recognized():
    """`geocode.names` scores a name by where the typed words landed in it."""
    assert name_words("Bryant Park") == ["BRYANT", "PARK"]
    assert name_words("Five Bryant Park") == ["5", "BRYANT", "PARK"]


def test_a_school_is_indexed_under_both_spellings_of_its_kind():
    spellings = place_spellings("High School of Fashion Industries")

    assert ("HIGH", "SCHOOL", "FASHION", "INDUSTRIES") in spellings
    assert ("HS", "FASHION", "INDUSTRIES") in spellings


def test_a_landmark_is_indexed_under_the_name_new_yorkers_say():
    assert ("MOMA",) in place_spellings("Museum of Modern Art (MoMA)")
    assert ("PORT", "AUTHORITY") in place_spellings("Port Authority Bus Terminal")


def test_every_place_alias_names_one_place_and_not_a_kind_of_place():
    for alias, target in PLACE_ALIASES.items():
        assert name_words(alias), alias
        assert name_words(target), target


def test_a_place_that_only_repeats_a_spelling_of_a_street_is_dropped():
    """MADISON is how a user reaches MADISON AVE, so the place point may not own it."""
    rows = [
        {"feature_name": name, "the_geom": {"type": "Point", "coordinates": [-73.98, 40.75]}}
        for name in ("MADISON", "MADISON SQUARE GARDEN")
    ]

    staged, dropped = stage_places(rows, frozenset({"MADISON", "MADISON AVE"}))

    assert dropped == 1
    assert [place.display for place in staged] == ["MADISON SQUARE GARDEN"]


def test_every_nickname_points_at_a_normalized_street_name():
    for nickname, target in NICKNAMES.items():
        assert normalize_street_name(target) == target, nickname


def test_rows_with_no_geometry_or_no_house_number_are_dropped():
    staged, dropped = stage_address_points(address_point_rows(), {})

    assert dropped == 2
    assert all(row.lon and row.lat for row in staged)
    assert {row.house_number for row in staged} == {100, 101, 104, 106, 7, 10}


def test_a_suffixed_house_number_keeps_its_letter_in_the_label_and_not_in_the_key():
    staged, _ = stage_address_points(
        [address_point_row("7", "E  2 ST", -73.9878, 40.75, suffix="A")], {}
    )

    assert staged[0].house_number == 7
    assert staged[0].display == "7A E  2 ST"


def test_a_hyphenated_house_number_keys_on_the_digits_before_the_hyphen():
    staged, _ = stage_address_points(
        [address_point_row("159-48", "HARLEM RIVER DR", -73.937, 40.833)], {}
    )

    assert staged[0].house_number == 159
    assert staged[0].display == "159-48 HARLEM RIVER DR"


def test_a_place_name_that_would_be_read_as_a_formula_is_neutralized():
    conn = db.connect(":memory:")
    db.create_schema(conn)
    hostile = {
        "feature_name": "=cmd|'/c calc'!A1",
        "the_geom": {"type": "Point", "coordinates": [-73.98, 40.75]},
    }

    build_address_index(conn, address_rows=[], place_rows=[hostile])

    assert conn.execute("SELECT display FROM place").fetchone()[0].startswith("'=")
    conn.close()


def test_the_index_holds_every_table_the_suggester_reads(indexed):
    conn, report = indexed

    assert report.address_points == 6
    assert report.dropped_address_rows == 2
    # MAIN ST is in CommonPlace too and is dropped: the centerline entry has
    # real geometry behind it and the place point does not.
    assert report.places == 2
    assert report.places_dropped_as_streets == 1
    assert conn.execute("SELECT count(*) FROM street").fetchone()[0] == report.streets
    assert conn.execute("SELECT count(*) FROM zip_centroid").fetchone()[0] == 2


def test_a_door_is_keyed_by_the_folded_street_and_labelled_with_the_centerline_spelling(indexed):
    conn, _ = indexed

    row = conn.execute(
        "SELECT display, zipcode, lon FROM address_point WHERE street_norm = ? AND house_number = ?",
        ("BROAD AVE", 104),
    ).fetchone()

    assert row["display"] == "104 BROAD AVE"
    assert row["zipcode"] == "10002"


def test_a_corner_is_stored_both_ways_round(indexed):
    conn, _ = indexed

    forward = conn.execute(
        "SELECT display FROM intersection WHERE a_norm = ? AND b_norm = ?", ("BROAD AVE", "E 2 ST")
    ).fetchone()
    backward = conn.execute(
        "SELECT display FROM intersection WHERE a_norm = ? AND b_norm = ?", ("E 2 ST", "BROAD AVE")
    ).fetchone()

    assert forward["display"] == backward["display"]
    assert "&" in forward["display"]


def test_a_numbered_cross_street_is_reachable_without_its_directional(indexed):
    conn, _ = indexed

    norms = {
        row[0]
        for row in conn.execute("SELECT street_norm FROM street_variant WHERE variant = ?", ("2",))
    }

    assert "E 2 ST" in norms
