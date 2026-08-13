"""Pure domain rules for workouts and progression."""

from __future__ import annotations

from dataclasses import dataclass
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
