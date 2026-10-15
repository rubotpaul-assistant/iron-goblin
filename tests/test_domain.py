"""Pure domain rule tests."""

from datetime import date
from decimal import Decimal

from gym_tracker.domain import (
    EquipmentRange,
    Orientation,
    Performance,
    age_on_date,
    met_calories,
    mifflin_st_jeor_bmr,
    parse_distance_meters,
    parse_duration_seconds,
    parse_height_cm,
    parse_weight_kg,
    progression_weight,
)


def test_orientation_opposite_round_trip() -> None:
    assert Orientation.VERTICAL.opposite() is Orientation.HORIZONTAL
    assert Orientation.HORIZONTAL.opposite() is Orientation.VERTICAL


def test_progression_repeats_at_zero_rir() -> None:
    equipment = EquipmentRange(Decimal(10), Decimal(200), Decimal(10))
    assert progression_weight(Decimal(90), Performance((5, 5, 5), 0), equipment) == (
        Decimal(90),
        "progression_repeat",
    )


def test_progression_increases_one_valid_increment_with_reserve() -> None:
    equipment = EquipmentRange(Decimal(10), Decimal(200), Decimal(10))
    assert progression_weight(Decimal(90), Performance((5, 5, 5), 1), equipment) == (
        Decimal(100),
        "progression_increase",
    )


def test_progression_skips_two_supported_loads_when_three_reps_remain() -> None:
    equipment = EquipmentRange(
        Decimal(20),
        Decimal(295),
        Decimal(5),
        supported_weights=(Decimal(115), Decimal(130), Decimal(145), Decimal(160)),
    )
    assert progression_weight(Decimal(115), Performance((5, 5, 5), 3), equipment) == (
        Decimal(145),
        "progression_aggressive_increase",
    )


def test_irregular_equipment_uses_explicit_supported_loads() -> None:
    equipment = EquipmentRange(
        Decimal(30),
        Decimal(220),
        Decimal(5),
        supported_weights=(Decimal(30), Decimal(45), Decimal(55), Decimal(70)),
    )
    assert equipment.contains(Decimal(55))
    assert not equipment.contains(Decimal(60))
    assert equipment.next_weight(Decimal(55)) == 70


def test_assistance_equipment_progresses_toward_less_assistance() -> None:
    equipment = EquipmentRange(
        Decimal(30),
        Decimal(220),
        Decimal(5),
        supported_weights=(Decimal(45), Decimal(55), Decimal(70), Decimal(80)),
        progression_direction="decrease",
    )
    assert equipment.next_weight(Decimal(70)) == 55
    assert equipment.next_weight(Decimal(70), 2) == 45
    assert equipment.next_weight(Decimal(45)) == 45


def test_uniform_assistance_range_decreases_and_stops_at_minimum() -> None:
    equipment = EquipmentRange(
        Decimal(30), Decimal(100), Decimal(10), progression_direction="decrease"
    )
    assert equipment.next_weight(Decimal(60), 2) == 40
    assert equipment.next_weight(Decimal(30)) == 30


def test_progression_does_not_increase_on_missed_reps_or_unsafe_form() -> None:
    equipment = EquipmentRange(Decimal(10), Decimal(200), Decimal(10))
    assert progression_weight(Decimal(90), Performance((5, 5, 4), 0), equipment)[0] == 90
    assert (
        progression_weight(Decimal(90), Performance((5, 5, 5), 2, unsafe=True), equipment)[0] == 90
    )


def test_equipment_range_rejects_unsupported_weights_and_caps_increase() -> None:
    equipment = EquipmentRange(Decimal(10), Decimal(100), Decimal(10))
    assert equipment.contains(Decimal(90))
    assert not equipment.contains(Decimal(95))
    assert equipment.next_weight(Decimal(100)) == 100


def test_metabolic_math_and_age_are_deterministic() -> None:
    assert age_on_date(date(1987, 9, 1), date(2026, 8, 18)) == 38
    assert mifflin_st_jeor_bmr(Decimal(68), Decimal(165), 38, "female") == Decimal("1360.25")
    gross, net = met_calories(Decimal("7.3"), Decimal(68), 2460)
    assert gross.quantize(Decimal("0.01")) == Decimal("356.17")
    assert net.quantize(Decimal("0.01")) == Decimal("307.38")
    assert net < gross


def test_us_units_normalize_to_metric() -> None:
    assert parse_height_cm("5ft 6in").quantize(Decimal("0.01")) == Decimal("167.64")
    assert parse_weight_kg("150 lb").quantize(Decimal("0.01")) == Decimal("68.04")
    assert parse_distance_meters("1.41 mi").quantize(Decimal("0.01")) == Decimal("2269.18")
    assert parse_duration_seconds("41m") == 2460
    assert parse_height_cm("1.7m") == Decimal("170.0")
    assert parse_distance_meters("2 km") == Decimal(2000)
    assert parse_distance_meters("240 ft") == Decimal("73.1520")


def test_domain_parsers_reject_missing_values() -> None:
    import pytest

    with pytest.raises(ValueError, match="female or male"):
        mifflin_st_jeor_bmr(Decimal(68), Decimal(165), 38, "unknown")
    with pytest.raises(ValueError, match="numeric"):
        parse_weight_kg("many")
    with pytest.raises(ValueError, match="duration"):
        parse_duration_seconds("41")
