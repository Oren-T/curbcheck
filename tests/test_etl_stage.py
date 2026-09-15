"""The validation boundary: active filter, formula neutralization, panel class, sign ids."""

from __future__ import annotations

from typing import Any

import pytest

from curbcheck.etl.stage import (
    StagingError,
    classify_panel,
    neutralize_formula,
    sign_id_for,
    stage_signs,
)

BASE_ROW: dict[str, Any] = {
    "borough": "Manhattan",
    "record_type": "Current",
    "order_number": "P-01667780",
    "on_street": "3 AVENUE",
    "from_street": "EAST   85 STREET",
    "to_street": "EAST   86 STREET",
    "side_of_street": "W",
    "distance_from_intersection": "44",
    "sign_code": "PS-127C",
    "sign_description": "2 HMP SATURDAY 8AM-7PM <->",
    "sign_x_coord": "996877",
    "sign_y_coord": "222815",
}


def row(**overrides: Any) -> dict[str, Any]:
    return {**BASE_ROW, **overrides}


def test_only_rows_without_a_voided_date_are_active():
    rows = [
        row(),
        row(order_number="P-2", sign_design_voided_on_date="2019-04-01T00:00:00.000"),
    ]

    staged, report = stage_signs(rows)

    assert [sign.order_number for sign in staged] == ["P-01667780"]
    assert report.active_rows == 1
    assert report.voided_rows == 1


def test_a_record_type_that_is_no_longer_constant_aborts_staging():
    # docs/DECISIONS.md D9 keeps this as an assertion: the day it starts
    # removing rows, the export changed and the active filter needs re-checking.
    with pytest.raises(StagingError, match="record_type"):
        stage_signs([row(record_type="Historical")])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("=1+1", "'=1+1"),
        ("+SUM(A1)", "'+SUM(A1)"),
        ("-2 HOUR PARKING", "'-2 HOUR PARKING"),
        ("@import", "'@import"),
        ("NO PARKING ANYTIME", "NO PARKING ANYTIME"),
        ("", ""),
    ],
)
def test_formula_leading_text_is_neutralized(raw, expected):
    assert neutralize_formula(raw) == expected


def test_neutralization_reaches_the_staged_text_columns():
    staged, _ = stage_signs([row(sign_notes="@BEWARE", sign_description="=NO PARKING")])

    assert staged[0].sign_notes == "'@BEWARE"
    assert staged[0].sign_description == "'=NO PARKING"


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("NO PARKING ANYTIME <->", "regulation"),
        ("2 HMP SATURDAY 8AM-7PM <->", "regulation"),
        ("BUS STOP SIGN", "regulation"),
        ("LOCAL MTA BUS ROUTE PANEL (TEXT TO BE MODIFIED AS REQUESTED)", "panel:mta_route"),
        ("EXPRESS MTA BUS DESTINATION PANEL (TEXT TO BE MODIFIED)", "panel:mta_route"),
        ("PAY-BY-CELL LOCATOR NUMBER", "panel:pay_by_cell"),
        ("6TH AVE PAY-BY-APP INFORMATION SIGN", "panel:pay_by_cell"),
        ("14 STREET & UNION SQ (BOTTOM LOCATION PANEL)", "panel:location"),
        ("   ", "panel:blank"),
    ],
)
def test_panel_classification_separates_regulations_from_panels(description, expected):
    assert classify_panel(description) == expected


def test_only_regulation_panels_are_marked_as_regulations():
    staged, report = stage_signs(
        [
            row(),
            row(order_number="P-2", sign_description="PAY-BY-CELL LOCATOR NUMBER"),
        ]
    )

    assert [sign.is_regulation for sign in staged] == [True, False]
    assert report.regulation_rows == 1
    assert report.panel_counts == {"regulation": 1, "panel:pay_by_cell": 1}


def test_sign_id_is_a_stable_16_character_hash_of_the_raw_row():
    first = sign_id_for(row())
    reordered = dict(reversed(list(row().items())))

    assert len(first) == 16
    assert int(first, 16) >= 0
    # Key order is not information: Socrata is free to reorder fields.
    assert sign_id_for(reordered) == first
    assert sign_id_for(row(distance_from_intersection="45")) != first


def test_identical_source_rows_collapse_into_one_sign_and_are_counted():
    staged, report = stage_signs([row(), row()])

    assert len(staged) == 1
    assert report.duplicate_rows_dropped == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"side_of_street": "X"},
        {"distance_from_intersection": "not a number"},
        {"distance_from_intersection": "-5"},
        {"on_street": "  "},
    ],
)
def test_rows_that_cannot_be_positioned_are_rejected_with_a_reason(bad):
    staged, report = stage_signs([row(**bad)])

    assert staged == []
    assert report.rejected_rows == 1
    assert report.rejections


def test_missing_optional_columns_are_read_as_null():
    # Socrata omits null fields entirely, so absence must not be an error.
    staged, _ = stage_signs([row()])

    assert staged[0].arrow_direction is None
    assert staged[0].sign_notes is None
    assert staged[0].sign_x_coord == 996877.0
