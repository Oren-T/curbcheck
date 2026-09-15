"""Validation boundary between downloaded JSON and the rest of the ETL.

Everything past this module works on typed dataclasses. Everything before it is
untrusted text: rows are validated against explicit Pydantic schemas (never
inferred), text cells that a spreadsheet would read as a formula are
neutralized, and the rows that carry no regulation at all are labelled so the
parser's coverage number is not diluted by them (docs/DECISIONS.md D10).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

from curbcheck.etl.parse import panel_class

LOGGER = logging.getLogger(__name__)

# Excel and Sheets execute a cell that starts with one of these, so a sign
# description exported to CSV could become a formula (SPEC §7 step 2).
_FORMULA_LEAD = frozenset("=+-@")

# Length of the truncated SHA-256 used for sign_id (docs/DECISIONS.md D5).
# 64 bits over 75k rows leaves the accidental-collision probability near 1e-10.
SIGN_ID_HEX_CHARS = 16

# What `classify_panel` returns for a string that may state a regulation. The
# other values are the parser's own `panel:<kind>` labels.
REGULATION_PANEL_CLASS = "regulation"

# docs/DECISIONS.md D9: record_type is the literal 'Current' on every row
# citywide, so it filters nothing. Kept as an assertion so a change is noticed.
EXPECTED_RECORD_TYPE = "Current"


class StagingError(Exception):
    """The snapshot violated an invariant the rest of the ETL depends on."""


class RawSignRow(BaseModel):
    """Sign row exactly as Socrata sends it.

    Every field is optional because Socrata omits nulls from resource JSON; the
    required-ness is asserted in `stage_sign`, where a failure names the column.
    Unknown columns are kept so a new one is never silently dropped.
    """

    model_config = ConfigDict(extra="allow")

    borough: str | None = None
    order_number: str | None = None
    order_type: str | None = None
    order_completed_on_date: str | None = None
    record_type: str | None = None
    sign_design_voided_on_date: str | None = None
    on_street: str | None = None
    on_street_suffix: str | None = None
    from_street: str | None = None
    from_street_suffix: str | None = None
    to_street: str | None = None
    to_street_suffix: str | None = None
    side_of_street: str | None = None
    distance_from_intersection: str | None = None
    arrow_direction: str | None = None
    facing_direction: str | None = None
    sign_code: str | None = None
    sign_description: str | None = None
    sign_size: str | None = None
    sign_notes: str | None = None
    sheeting_type: str | None = None
    support: str | None = None
    sign_x_coord: str | None = None
    sign_y_coord: str | None = None


class RawCenterlineRow(BaseModel):
    """Centerline row as Socrata sends it; `the_geom` is GeoJSON in EPSG:4326."""

    model_config = ConfigDict(extra="allow")

    physicalid: str | None = None
    full_street_name: str | None = None
    stname_label: str | None = None
    rw_type: str | None = None
    status: str | None = None
    trafdir: str | None = None
    segmentlength: str | None = None
    streetwidth: str | None = None
    l_blockfaceid: str | None = None
    r_blockfaceid: str | None = None
    l_low_hn: str | None = None
    l_high_hn: str | None = None
    r_low_hn: str | None = None
    r_high_hn: str | None = None
    the_geom: dict[str, Any] | None = None


@dataclass(frozen=True)
class StagedSign:
    """One active Manhattan sign, validated, neutralized and classified."""

    sign_id: str
    order_number: str
    on_street: str
    from_street: str
    to_street: str
    side_of_street: str
    distance_from_intersection_ft: float
    arrow_direction: str | None
    facing_direction: str | None
    sign_code: str
    sign_description: str
    sign_notes: str | None
    sign_x_coord: float | None
    sign_y_coord: float | None
    panel_class: str
    is_regulation: bool

    @property
    def blockface_key(self) -> tuple[str, str, str, str]:
        """The `(on, from, to, side)` group docs/DATA.md §1.8 counts as a blockface-side."""
        return (self.on_street, self.from_street, self.to_street, self.side_of_street)


@dataclass(frozen=True)
class StageReport:
    """Counts worth carrying into `sync_meta`, so a bad snapshot is visible after the fact."""

    source_rows: int
    active_rows: int
    voided_rows: int
    duplicate_rows_dropped: int
    rejected_rows: int
    regulation_rows: int
    panel_counts: dict[str, int]
    rejections: tuple[str, ...]


def neutralize_formula(value: str) -> str:
    """Prefix a single quote when a cell would be read as a spreadsheet formula.

    Only the leading character matters to Excel and Sheets, so the quote is
    enough and keeps the text otherwise byte-identical to the source.
    """
    if value[:1] in _FORMULA_LEAD:
        return "'" + value
    return value


def classify_panel(description: str) -> str:
    """Label a sign_description with what kind of panel it is (docs/DECISIONS.md D10).

    One classifier serves both steps: staging labels the `sign` row with it and
    the parser refuses to read anything it labels. Keeping a second, coarser
    copy here meant a sign could be staged as a regulation and then dropped by
    the parser, which made the coverage numbers disagree with the row counts.
    """
    return panel_class(description) or REGULATION_PANEL_CLASS


def sign_id_for(raw_row: dict[str, Any]) -> str:
    """Stable id for a sign row: truncated SHA-256 of its canonical JSON (D5).

    Canonical means sorted keys and no whitespace, so the id does not move when
    Socrata reorders fields. Hashing the *raw* row means the id is independent
    of anything this module does to the values afterwards.
    """
    canonical = json.dumps(raw_row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:SIGN_ID_HEX_CHARS]


def is_active(row: RawSignRow) -> bool:
    """docs/DECISIONS.md D9: the only retirement signal that varies is the voided date."""
    return not (row.sign_design_voided_on_date or "").strip()


def assert_record_type(rows: Iterable[RawSignRow]) -> None:
    """Raise if `record_type` ever stops being the constant D9 measured it to be."""
    unexpected = {
        (row.record_type or "").strip()
        for row in rows
        if (row.record_type or "").strip() != EXPECTED_RECORD_TYPE
    }
    if unexpected:
        raise StagingError(
            f"record_type is no longer constant {EXPECTED_RECORD_TYPE!r}: saw {sorted(unexpected)}; "
            "the export changed, re-check docs/DECISIONS.md D9 before trusting the active filter"
        )


def stage_signs(raw_rows: Sequence[dict[str, Any]]) -> tuple[list[StagedSign], StageReport]:
    """Validate, filter, neutralize and classify a signs snapshot.

    Exactly duplicated source rows collapse into one sign: they hash to the same
    id and describe the same panel, so keeping both would double-count the same
    regulation. The count is reported rather than hidden.
    """
    staged: list[StagedSign] = []
    seen: set[str] = set()
    rejections: list[str] = []
    voided = 0
    duplicates = 0
    active_rows: list[RawSignRow] = []

    for raw_row in raw_rows:
        row = RawSignRow.model_validate(raw_row)
        if not is_active(row):
            voided += 1
            continue
        active_rows.append(row)
        sign_id = sign_id_for(raw_row)
        if sign_id in seen:
            duplicates += 1
            continue
        try:
            staged.append(_stage_sign(row, sign_id))
        except StagingError as error:
            rejections.append(str(error))
            continue
        seen.add(sign_id)

    assert_record_type(active_rows)
    panel_counts: dict[str, int] = {}
    for sign in staged:
        panel_counts[sign.panel_class] = panel_counts.get(sign.panel_class, 0) + 1

    report = StageReport(
        source_rows=len(raw_rows),
        active_rows=len(active_rows),
        voided_rows=voided,
        duplicate_rows_dropped=duplicates,
        rejected_rows=len(rejections),
        regulation_rows=sum(1 for sign in staged if sign.is_regulation),
        panel_counts=panel_counts,
        rejections=tuple(rejections[:20]),
    )
    LOGGER.info(
        "stage.signs source=%d active=%d kept=%d duplicates=%d rejected=%d regulation=%d",
        report.source_rows,
        report.active_rows,
        len(staged),
        report.duplicate_rows_dropped,
        report.rejected_rows,
        report.regulation_rows,
    )
    return staged, report


def stage_centerline(raw_rows: Sequence[dict[str, Any]]) -> list[RawCenterlineRow]:
    """Validate a centerline snapshot. Geometry is checked in `streets.build_graph`."""
    return [RawCenterlineRow.model_validate(row) for row in raw_rows]


def _stage_sign(row: RawSignRow, sign_id: str) -> StagedSign:
    description = neutralize_formula(_required(row.sign_description, "sign_description", sign_id))
    panel = classify_panel(description)
    return StagedSign(
        sign_id=sign_id,
        order_number=neutralize_formula((row.order_number or "").strip()),
        on_street=neutralize_formula(_required(row.on_street, "on_street", sign_id)),
        from_street=neutralize_formula(_required(row.from_street, "from_street", sign_id)),
        to_street=neutralize_formula(_required(row.to_street, "to_street", sign_id)),
        side_of_street=_side(row.side_of_street, sign_id),
        distance_from_intersection_ft=_distance(row.distance_from_intersection, sign_id),
        arrow_direction=_optional_text(row.arrow_direction),
        facing_direction=_optional_text(row.facing_direction),
        sign_code=neutralize_formula((row.sign_code or "").strip()),
        sign_description=description,
        sign_notes=_optional_text(row.sign_notes),
        sign_x_coord=_optional_float(row.sign_x_coord),
        sign_y_coord=_optional_float(row.sign_y_coord),
        panel_class=panel,
        is_regulation=panel == REGULATION_PANEL_CLASS,
    )


def _required(value: str | None, column: str, sign_id: str) -> str:
    text = (value or "").strip()
    if not text:
        raise StagingError(f"sign {sign_id}: {column} is empty")
    return text


def _side(value: str | None, sign_id: str) -> str:
    side = (value or "").strip().upper()
    if side not in {"N", "S", "E", "W"}:
        raise StagingError(f"sign {sign_id}: side_of_street {value!r} is not N/S/E/W")
    return side


def _distance(value: str | None, sign_id: str) -> float:
    text = (value or "").strip()
    try:
        distance = float(text)
    except ValueError as error:
        raise StagingError(
            f"sign {sign_id}: distance_from_intersection {value!r} is not a number"
        ) from error
    if distance < 0:
        raise StagingError(f"sign {sign_id}: distance_from_intersection {distance} is negative")
    return distance


def _optional_text(value: str | None) -> str | None:
    text = (value or "").strip()
    return neutralize_formula(text) if text else None


def _optional_float(value: str | None) -> float | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None
