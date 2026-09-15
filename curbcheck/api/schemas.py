"""Pydantic models for everything that arrives over HTTP.

STYLE_GUIDE §2: every external input passes through a Pydantic model before
the rest of the code touches it. `extra="forbid"` everywhere, so a typo in a
field name is a 422 rather than a silently ignored setting.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from curbcheck.config import NYC_TZ
from curbcheck.engine.cost import Weights

# A Manhattan-ish bounding box, generous at every edge: Battery Park to Inwood,
# the Hudson to the East River. An input sanity check, not a service area --
# the database only holds Manhattan, so a point in the corner of the box simply
# returns nothing.
MIN_LAT, MAX_LAT = 40.68, 40.90
MIN_LON, MAX_LON = -74.05, -73.88

# A window shorter than this is not a parking decision, and one longer than a
# day would make the per-day expansion in engine/window.py unbounded work.
MIN_WINDOW = timedelta(minutes=5)
MAX_WINDOW = timedelta(hours=24)

MAX_QUERY_CHARS = 200

# Ids the ETL mints look like "3681:W:0". Anything outside this charset cannot
# be a real id, so it is rejected before a query is built rather than after.
REG_SEG_ID_PATTERN = re.compile(r"^[A-Za-z0-9:_.-]{1,80}$")


class WeightsRequest(BaseModel):
    """The three ranking sliders (SPEC §9.4). Upper bound keeps one from dwarfing the score."""

    model_config = ConfigDict(extra="forbid")

    walk: float = Field(default=1.0, ge=0.0, le=10.0, allow_inf_nan=False)
    money: float = Field(default=1.0, ge=0.0, le=10.0, allow_inf_nan=False)
    risk: float = Field(default=0.5, ge=0.0, le=10.0, allow_inf_nan=False)

    def to_engine(self) -> Weights:
        return Weights(walk=self.walk, money=self.money, risk=self.risk)


class SearchRequest(BaseModel):
    """A destination, a window, and how far the user will walk."""

    model_config = ConfigDict(extra="forbid")

    lat: float | None = Field(default=None, ge=MIN_LAT, le=MAX_LAT, allow_inf_nan=False)
    lon: float | None = Field(default=None, ge=MIN_LON, le=MAX_LON, allow_inf_nan=False)
    address: str | None = Field(default=None, min_length=1, max_length=MAX_QUERY_CHARS)
    t1: datetime
    t2: datetime
    walk_minutes: float = Field(default=10.0, ge=1.0, le=30.0, allow_inf_nan=False)
    weights: WeightsRequest = Field(default_factory=WeightsRequest)
    limit: int = Field(default=100, ge=1, le=500)

    @field_validator("t1", "t2")
    @classmethod
    def _in_new_york(cls, value: datetime) -> datetime:
        """Read a naive time as local and convert an aware one.

        Every parking sign states local time, so a bare "2026-09-15T09:00" from
        the UI means nine in the morning in New York, not UTC. The engine
        requires an aware datetime and would otherwise reject it.
        """
        if value.tzinfo is None:
            return value.replace(tzinfo=NYC_TZ)
        return value.astimezone(NYC_TZ)

    @model_validator(mode="after")
    def _check_destination_and_window(self) -> Self:
        if (self.lat is None) != (self.lon is None):
            raise ValueError("lat and lon must be given together")
        has_point = self.lat is not None
        if has_point and self.address is not None:
            raise ValueError("give either lat and lon or address, not both")
        if not has_point and self.address is None:
            raise ValueError("give a destination: either lat and lon, or address")

        span = self.t2 - self.t1
        if span < MIN_WINDOW:
            raise ValueError(
                "the parking window must be at least 5 minutes and end after it starts"
            )
        if span > MAX_WINDOW:
            raise ValueError("the parking window must be 24 hours or less")
        return self
