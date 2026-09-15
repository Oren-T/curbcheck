"""Shared data contract for parsed parking regulations.

Every parser path, the gold-set labels, the database rows, and the API all
speak this schema. Change it here and nowhere else. See docs/SPEC.md §6 and §8.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Weekday = Literal[0, 1, 2, 3, 4, 5, 6]
"""Monday is 0, Sunday is 6, matching datetime.weekday()."""

ALL_DAYS: tuple[Weekday, ...] = (0, 1, 2, 3, 4, 5, 6)


class Action(StrEnum):
    """The three curb actions NYC signs regulate, least to most restrictive when prohibited."""

    PARK = "park"
    STAND = "stand"
    STOP = "stop"


class VehicleClass(StrEnum):
    """The vehicle class a sign names. ALL means the sign does not single one out."""

    ALL = "all"
    PASSENGER = "passenger"
    COMMERCIAL = "commercial"
    TRUCK = "truck"
    BUS = "bus"
    TAXI = "taxi"
    AUTHORIZED = "authorized"  # AVO, police, diplomat, government plates, etc.
    HANDICAP = "handicap"
    OTHER = "other"


class Arrow(StrEnum):
    """Which way the rule extends from the sign post along the curb.

    FORWARD means in the direction of increasing distance_from_intersection.
    NONE means the sign has no arrow; by 34 RCNY 4-08 it then governs the
    whole blockface.
    """

    FORWARD = "forward"
    BACKWARD = "backward"
    BOTH = "both"
    NONE = "none"


class ParseMethod(StrEnum):
    GRAMMAR = "grammar"
    LLM = "llm"  # reserved; not used in v1, see docs/DECISIONS.md D2
    UNPARSED = "unparsed"


class Flags(BaseModel):
    """Conditions that modify when or whether a rule is in force."""

    street_cleaning: bool = False  # broom symbol; suspended on ASP days
    school_days: bool = False  # only in force on DOE school days
    except_sunday: bool = False  # sign says EXCEPT SUNDAY
    including_sunday: bool = False  # sign says INCLUDING SUNDAY; overrides meter Sunday exemption
    snow_emergency: bool = False  # only in force during a declared snow emergency
    holiday_exempt: bool = False  # sign says EXCEPT HOLIDAYS or similar
    temporary: bool = False  # sign text marks itself temporary
    meta: bool = False  # sign modifies a sibling rule rather than standing alone (§8.4 ex. 7)


class Regulation(BaseModel):
    """One rule as read from one sign.

    A sign that combines two rules ("NO PARKING 8-9AM MON / 2 HR METERED 9AM-7PM")
    yields two Regulation objects. Times are "HH:MM" 24-hour strings; None for
    both means all day. A time_to earlier than time_from wraps past midnight.
    """

    action: Action
    permitted: bool = Field(description="Whether vehicle_class may perform action here")
    vehicle_class: VehicleClass = VehicleClass.ALL
    exclusive: bool = Field(
        default=False,
        description="True for 'X ONLY' signs: classes other than vehicle_class are prohibited",
    )
    days: list[Weekday] = Field(default_factory=lambda: list(ALL_DAYS))
    time_from: str | None = None
    time_to: str | None = None
    metered: bool = False
    max_duration_min: int | None = None
    flags: Flags = Field(default_factory=Flags)
    effective_from: str | None = Field(default=None, description="MM-DD seasonal start, inclusive")
    effective_to: str | None = Field(default=None, description="MM-DD seasonal end, inclusive")
    arrow: Arrow = Arrow.NONE

    @model_validator(mode="after")
    def _times_come_in_pairs(self) -> Regulation:
        if (self.time_from is None) != (self.time_to is None):
            raise ValueError("time_from and time_to must both be set or both be None")
        for value in (self.time_from, self.time_to):
            if value is not None and not _is_hhmm(value):
                raise ValueError(f"time must be HH:MM, got {value!r}")
        for value in (self.effective_from, self.effective_to):
            if value is not None and not _is_mmdd(value):
                raise ValueError(f"seasonal date must be MM-DD, got {value!r}")
        return self

    def applies_to_passenger(self) -> bool | None:
        """Passenger-car reading of this rule: True permitted, False prohibited, None not applicable."""
        if self.vehicle_class in (VehicleClass.ALL, VehicleClass.PASSENGER):
            return self.permitted
        if self.exclusive:
            return False
        return None


class ParsedSign(BaseModel):
    """Parser output for one sign_description string."""

    raw: str
    regulations: list[Regulation] = Field(default_factory=list)
    parse_method: ParseMethod
    confidence: float = Field(ge=0.0, le=1.0)
    notes: str = ""


def _is_hhmm(value: str) -> bool:
    if len(value) != 5 or value[2] != ":":
        return False
    hours, minutes = value[:2], value[3:]
    return hours.isdigit() and minutes.isdigit() and int(hours) < 24 and int(minutes) < 60


def _is_mmdd(value: str) -> bool:
    if len(value) != 5 or value[2] != "-":
        return False
    month, day = value[:2], value[3:]
    return month.isdigit() and day.isdigit() and 1 <= int(month) <= 12 and 1 <= int(day) <= 31
