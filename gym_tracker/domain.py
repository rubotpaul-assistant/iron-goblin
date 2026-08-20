"""Pure domain rules for workouts and progression."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum


class Category(StrEnum):
    """Required workout movement categories."""

    PUSH = "push"
    PULL = "pull"
    LEGS = "legs"


class Orientation(StrEnum):
    """Workout movement planes."""

    HORIZONTAL = "horizontal"
    VERTICAL = "vertical"

    def opposite(self) -> Orientation:
        """Return the other orientation."""
        return self.HORIZONTAL if self is self.VERTICAL else self.VERTICAL


@dataclass(frozen=True)
class EquipmentRange:
    """Weights supported by a piece of equipment."""

    minimum: Decimal
    maximum: Decimal
    increment: Decimal

    def next_weight(self, current: Decimal) -> Decimal:
        """Advance one supported increment without exceeding the maximum."""
        return min(self.maximum, current + self.increment)

    def contains(self, weight: Decimal) -> bool:
        """Check whether a weight is aligned to this equipment's stack."""
        if weight < self.minimum or weight > self.maximum or self.increment <= 0:
            return False
        return (weight - self.minimum) % self.increment == 0


@dataclass(frozen=True)
class Performance:
    """A completed three-set performance."""

    reps: tuple[int, int, int]
    final_rir: int | None
    technical_failure: bool = False
    unsafe: bool = False

    @property
    def total_reps(self) -> int:
        """Return total completed repetitions."""
        return sum(self.reps)


def progression_weight(
    current: Decimal, performance: Performance, equipment: EquipmentRange
) -> tuple[Decimal, str]:
    """Calculate the next supported working weight and reason code."""
    if performance.unsafe or performance.technical_failure or performance.total_reps < 15:
        return current, "progression_repeat"
    if performance.reps == (5, 5, 5) and (performance.final_rir or 0) >= 1:
        return equipment.next_weight(current), "progression_increase"
    return current, "progression_repeat"


def age_on_date(birth_date: date, requested_date: date) -> int:
    """Return completed years at a requested date."""
    return (
        requested_date.year
        - birth_date.year
        - ((requested_date.month, requested_date.day) < (birth_date.month, birth_date.day))
    )


def mifflin_st_jeor_bmr(
    weight_kg: Decimal, height_cm: Decimal, age: int, sex_parameter: str
) -> Decimal:
    """Calculate Mifflin-St Jeor resting energy in kcal/day."""
    coefficient = {"male": Decimal(5), "female": Decimal(-161)}.get(sex_parameter)
    if coefficient is None:
        raise ValueError("bmr sex parameter must be female or male")
    return Decimal(10) * weight_kg + Decimal("6.25") * height_cm - Decimal(5 * age) + coefficient


def met_calories(
    met: Decimal, weight_kg: Decimal, duration_seconds: int
) -> tuple[Decimal, Decimal]:
    """Return gross and net MET calories without presentation rounding."""
    minutes = Decimal(duration_seconds) / Decimal(60)
    factor = Decimal("3.5") * weight_kg / Decimal(200) * minutes
    return met * factor, max(met - Decimal(1), Decimal(0)) * factor


def _number(value: str) -> Decimal:
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    if match is None:
        raise ValueError(f"no numeric value in: {value}")
    return Decimal(match.group())


def parse_weight_kg(value: str) -> Decimal:
    """Normalize common weight units to kilograms."""
    number = _number(value)
    return number * Decimal("0.45359237") if "lb" in value.lower() else number


def parse_height_cm(value: str) -> Decimal:
    """Normalize feet/inches, inches, meters, or centimeters."""
    lowered = value.lower().replace(" ", "")
    feet = re.fullmatch(r"(\d+(?:\.\d+)?)ft(?:(\d+(?:\.\d+)?)in)?", lowered)
    if feet:
        inches = Decimal(feet.group(1)) * 12 + Decimal(feet.group(2) or 0)
        return inches * Decimal("2.54")
    number = _number(lowered)
    if "in" in lowered:
        return number * Decimal("2.54")
    if lowered.endswith("m") and not lowered.endswith("cm"):
        return number * 100
    return number


def parse_distance_meters(value: str) -> Decimal:
    """Normalize common distance units to meters."""
    lowered = value.lower()
    number = _number(lowered)
    if "mi" in lowered:
        return number * Decimal("1609.344")
    if "km" in lowered:
        return number * 1000
    if "ft" in lowered:
        return number * Decimal("0.3048")
    return number


def parse_duration_seconds(value: str) -> int:
    """Normalize duration text to whole seconds."""
    lowered = value.lower().replace(" ", "")
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", lowered)
    if match is None or not any(match.groups()):
        raise ValueError("duration must use h, m, or s units")
    return (
        int(match.group(1) or 0) * 3600 + int(match.group(2) or 0) * 60 + int(match.group(3) or 0)
    )
