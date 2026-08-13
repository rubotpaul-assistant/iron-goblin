"""Pure domain rule tests."""

from decimal import Decimal

from gym_tracker.domain import EquipmentRange, Orientation, Performance, progression_weight


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


def test_progression_does_not_increase_on_missed_reps_or_unsafe_form() -> None:
    equipment = EquipmentRange(Decimal(10), Decimal(200), Decimal(10))
    assert progression_weight(Decimal(90), Performance((5, 5, 4), 0), equipment)[0] == 90
    assert (
        progression_weight(Decimal(90), Performance((5, 5, 5), 2, unsafe=True), equipment)[0] == 90
    )


def test_equipment_range_rejects_unsupported_weights_and_caps_increase() -> None:
    equipment = EquipmentRange(Decimal(10), Decimal(100), Decimal(10))
    assert equipment.contains(90)
    assert not equipment.contains(95)
    assert equipment.next_weight(100) == 100
