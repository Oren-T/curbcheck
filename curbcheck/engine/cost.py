"""What a parking spot costs: minutes of walking, dollars at the meter, and risk.

SPEC §9.4 defines the score as a weighted sum of the three so the user sees the
trade rather than a black-box number. Money is `Decimal` throughout; walk time
is float minutes.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, Field

from curbcheck.config import WALK_DETOUR_FACTOR, WALK_SPEED_M_PER_S

CENTS = Decimal("0.01")

# NYC parking fines in Manhattan below 96th St (NYC Finance parking violation
# schedule): code 40 "fire hydrant" is $115; ASP/street-cleaning and expired
# meter are $65. We use the higher figure only for hydrant-class prohibitions.
FINE_STANDARD = Decimal("65")
FINE_HYDRANT_CLASS = Decimal("115")

# Probability that parking somewhere we misread as legal actually draws a
# ticket during the window. A placeholder: we have no citation-outcome data to
# fit against, and the term only needs to rank low-confidence segments below
# confident ones. Change this one number to retune the risk term.
P_CITE_IF_WRONG = 0.5

EARTH_RADIUS_M = 6_371_008.8  # IUGG mean radius


class Weights(BaseModel):
    """User-tunable weights for the ranking score (SPEC §9.4, exposed as sliders)."""

    walk: float = Field(default=1.0, ge=0.0)
    money: float = Field(default=1.0, ge=0.0)
    risk: float = Field(default=0.5, ge=0.0)


def haversine_meters(from_lonlat: tuple[float, float], to_lonlat: tuple[float, float]) -> float:
    """Great-circle distance between two (lon, lat) pairs in degrees."""
    lon1, lat1 = from_lonlat
    lon2, lat2 = to_lonlat
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def walk_minutes(from_lonlat: tuple[float, float], to_lonlat: tuple[float, float]) -> float:
    """Straight-line walk estimate: great-circle distance, detour factor, walking speed.

    No pedestrian router in v1 (SPEC §9.4); this function is the single seam
    where one would be swapped in.
    """
    return walk_minutes_for_meters(haversine_meters(from_lonlat, to_lonlat))


def walk_minutes_for_meters(straight_line_meters: float) -> float:
    """Walk minutes for an already-computed straight-line distance."""
    return straight_line_meters * WALK_DETOUR_FACTOR / WALK_SPEED_M_PER_S / 60


def walk_radius_meters(walk_minutes_max: float) -> float:
    """Straight-line radius reachable within `walk_minutes_max`, the inverse of `walk_minutes`."""
    if walk_minutes_max < 0:
        raise ValueError("walk_minutes_max must not be negative")
    return walk_minutes_max * 60 * WALK_SPEED_M_PER_S / WALK_DETOUR_FACTOR


def meter_price(hour_rates: Sequence[Decimal], charged_minutes: int) -> Decimal:
    """Progressive meter price for `charged_minutes`, prorated by the minute.

    The first charged hour bills at `hour_rates[0]`, the second at
    `hour_rates[1]`, and anything beyond the listed hours at the last rate --
    NYC publishes a first- and second-hour rate and charges the later rate for
    longer stays (SPEC §13.2). Only minutes when the meter is actually running
    should be passed in; Sundays and holidays are free.
    """
    if charged_minutes < 0:
        raise ValueError("charged_minutes must not be negative")
    if charged_minutes == 0:
        return Decimal("0.00")
    if not hour_rates:
        raise ValueError("no meter rates for this segment; price is unknown, not zero")

    total = Decimal(0)
    remaining = charged_minutes
    hour_index = 0
    while remaining > 0:
        rate = hour_rates[min(hour_index, len(hour_rates) - 1)]
        minutes = min(remaining, 60)
        total += rate * Decimal(minutes) / Decimal(60)
        remaining -= minutes
        hour_index += 1
    return total.quantize(CENTS, rounding=ROUND_HALF_UP)


def citation_probability(parse_confidence: float, snap_confidence: float) -> float:
    """Chance of a ticket if we park here, as a function of how sure we are of the rules.

    The weaker of the two confidences drives it: a perfectly parsed sign matched
    to the wrong blockface is as dangerous as an unreadable sign on the right
    one. Linear in doubt, capped by `P_CITE_IF_WRONG`.
    """
    doubt = 1.0 - min(parse_confidence, snap_confidence)
    return min(1.0, max(0.0, doubt)) * P_CITE_IF_WRONG


def risk_dollars(
    parse_confidence: float, snap_confidence: float, *, fine: Decimal = FINE_STANDARD
) -> Decimal:
    """Expected fine: citation probability times the fine for this class of violation."""
    probability = Decimal(str(citation_probability(parse_confidence, snap_confidence)))
    return (probability * fine).quantize(CENTS, rounding=ROUND_HALF_UP)


def total_cost(
    walk_min: float, money: Decimal, risk: Decimal, weights: Weights | None = None
) -> float:
    """Weighted sum used for ranking. Minutes and dollars are added as raw numbers.

    They are not commensurable units, which is the point of the weights: the
    default 1.0/1.0/0.5 reads as "one minute of walking is worth one dollar".
    """
    resolved = weights or Weights()
    return resolved.walk * walk_min + resolved.money * float(money) + resolved.risk * float(risk)
