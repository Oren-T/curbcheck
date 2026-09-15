"""Walk estimate, progressive meter pricing, and the risk term."""

from __future__ import annotations

from decimal import Decimal

import pytest

from curbcheck.config import WALK_DETOUR_FACTOR, WALK_SPEED_M_PER_S
from curbcheck.engine.cost import (
    FINE_HYDRANT_CLASS,
    FINE_STANDARD,
    Weights,
    citation_probability,
    haversine_meters,
    meter_price,
    risk_dollars,
    total_cost,
    walk_minutes,
    walk_minutes_for_meters,
    walk_radius_meters,
)

# Manhattan test points: 3rd Ave at E 85th, and a point one block north.
E85 = (-73.9550, 40.7790)
E86 = (-73.9550, 40.7880)

M1_RATES = [Decimal("5.50"), Decimal("9.00")]


def test_haversine_matches_a_known_north_south_distance() -> None:
    """One hundredth of a degree of latitude is about 1,111 m anywhere on Earth."""
    assert haversine_meters((-73.955, 40.7790), (-73.955, 40.7890)) == pytest.approx(1111, abs=2)


def test_walk_minutes_applies_the_detour_factor_and_walking_speed() -> None:
    meters = haversine_meters(E85, E86)

    expected = meters * WALK_DETOUR_FACTOR / WALK_SPEED_M_PER_S / 60

    assert walk_minutes(E85, E86) == pytest.approx(expected)
    assert walk_minutes(E85, E86) == pytest.approx(walk_minutes_for_meters(meters))


def test_walking_nowhere_takes_no_time() -> None:
    assert walk_minutes(E85, E85) == pytest.approx(0.0)


def test_walk_radius_is_the_inverse_of_walk_minutes() -> None:
    radius = walk_radius_meters(10)

    assert walk_minutes_for_meters(radius) == pytest.approx(10.0)


def test_walk_radius_rejects_negative_minutes() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        walk_radius_meters(-1)


def test_half_an_hour_costs_half_the_first_hour_rate() -> None:
    assert meter_price(M1_RATES, 30) == Decimal("2.75")


def test_a_full_first_hour_costs_the_first_hour_rate() -> None:
    assert meter_price(M1_RATES, 60) == Decimal("5.50")


def test_the_second_hour_bills_at_the_second_hour_rate_prorated() -> None:
    assert meter_price(M1_RATES, 90) == Decimal("10.00")
    assert meter_price(M1_RATES, 120) == Decimal("14.50")


def test_hours_beyond_the_published_list_bill_at_the_last_rate() -> None:
    """SPEC §13.2 publishes a first and second hour rate; a third hour repeats the last."""
    assert meter_price(M1_RATES, 180) == Decimal("23.50")


def test_a_single_rate_applies_to_every_hour() -> None:
    assert meter_price([Decimal("1.50")], 150) == Decimal("3.75")


def test_zero_charged_minutes_costs_nothing() -> None:
    assert meter_price(M1_RATES, 0) == Decimal("0.00")


def test_pricing_without_a_rate_is_an_error_not_a_free_spot() -> None:
    with pytest.raises(ValueError, match="unknown, not zero"):
        meter_price([], 60)


def test_negative_minutes_are_rejected() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        meter_price(M1_RATES, -10)


def test_prices_are_quantized_to_cents() -> None:
    price = meter_price([Decimal("5.50")], 7)

    assert price == Decimal("0.64")
    assert price.as_tuple().exponent == -2


def test_weights_default_to_the_spec_values() -> None:
    weights = Weights()

    assert (weights.walk, weights.money, weights.risk) == (1.0, 1.0, 0.5)


def test_citation_probability_is_zero_when_we_are_certain() -> None:
    assert citation_probability(1.0, 1.0) == pytest.approx(0.0)


def test_citation_probability_follows_the_weaker_confidence() -> None:
    assert citation_probability(0.5, 1.0) == citation_probability(1.0, 0.5)
    assert citation_probability(0.5, 1.0) > citation_probability(0.9, 1.0)


def test_risk_is_the_expected_fine() -> None:
    assert risk_dollars(1.0, 1.0) == Decimal("0.00")
    assert risk_dollars(0.0, 1.0) == (FINE_STANDARD / 2).quantize(Decimal("0.01"))
    assert risk_dollars(0.0, 1.0, fine=FINE_HYDRANT_CLASS) == Decimal("57.50")


def test_total_cost_is_the_weighted_sum() -> None:
    score = total_cost(6.0, Decimal("10.00"), Decimal("4.00"), Weights())

    assert score == pytest.approx(6.0 + 10.0 + 2.0)


def test_weights_can_switch_off_a_term() -> None:
    free_walking = Weights(walk=0.0, money=1.0, risk=0.0)

    assert total_cost(20.0, Decimal("3.00"), Decimal("9.00"), free_walking) == pytest.approx(3.0)


def test_total_cost_uses_the_default_weights_when_none_are_given() -> None:
    assert total_cost(1.0, Decimal("1.00"), Decimal("2.00")) == pytest.approx(3.0)
