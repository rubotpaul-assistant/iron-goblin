"""Application service integration tests against real SQLite."""

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from gym_tracker.db import Database
from gym_tracker.domain import Category, Orientation
from gym_tracker.service import GymError, GymService


@pytest.fixture
def service(tmp_path: Path) -> GymService:
    instance = GymService(Database(tmp_path / "gym.sqlite3"))
    for orientation in Orientation:
        for category in Category:
            equipment = f"{orientation}-{category}-machine"
            exercise = f"{orientation} {category}"
            instance.add_equipment(equipment, "machine", Decimal(10), Decimal(200), Decimal(10))
            instance.add_exercise(
                exercise, category, orientation, "machine", equipment_name=equipment
            )
    return instance


def test_workouts_alternate_even_after_skip(service: GymService) -> None:
    first = service.begin_workout(
        now=datetime(2026, 8, 10, 12, tzinfo=ZoneInfo("America/Los_Angeles"))
    )
    service.skip_workout()
    second = service.begin_workout(
        now=datetime(2026, 8, 11, 12, tzinfo=ZoneInfo("America/Los_Angeles"))
    )
    service.skip_workout()
    third = service.begin_workout(
        now=datetime(2026, 8, 12, 12, tzinfo=ZoneInfo("America/Los_Angeles"))
    )
    assert [first["orientation"], second["orientation"], third["orientation"]] == [
        "vertical",
        "horizontal",
        "vertical",
    ]


def test_scheduled_begin_is_idempotent_for_pacific_date(service: GymService) -> None:
    now = datetime(2026, 8, 10, 12, tzinfo=ZoneInfo("America/Los_Angeles"))
    first = service.begin_workout(scheduled=True, now=now)
    second = service.begin_workout(scheduled=True, now=now)
    assert second["workout_id"] == first["workout_id"]
    assert second["duplicate"] is True


def test_recommendations_match_orientation_and_exclude_unavailable_equipment(
    service: GymService,
) -> None:
    service.remove_equipment("vertical-push-machine")
    workout = service.begin_workout()
    categories = {recommendation["category"] for recommendation in workout["recommendations"]}
    assert "push" not in categories
    assert {item["exercise"] for item in workout["recommendations"]} == {
        "vertical pull",
        "vertical legs",
    }


def test_completion_requires_all_categories(service: GymService) -> None:
    service.begin_workout()
    service.log_exercise("vertical push", Decimal(90), (5, 5, 5), final_rir=0)
    with pytest.raises(GymError, match="pull,legs"):
        service.complete_workout()
    service.log_exercise("vertical pull", Decimal(90), (5, 5, 5), final_rir=1)
    service.log_exercise("vertical legs", Decimal(90), (5, 5, 4), final_rir=0)
    assert service.complete_workout()["status"] == "completed"


def test_logging_respects_increment_and_progression(service: GymService) -> None:
    service.begin_workout()
    with pytest.raises(GymError, match="not supported"):
        service.log_exercise("vertical push", Decimal(95), (5, 5, 5), final_rir=1)
    result = service.log_exercise("vertical push", Decimal(90), (5, 5, 5), final_rir=1)
    assert result["next_weight"] == 100
    assert result["sync_pending"] is True


def test_aliases_resolve_consistently(service: GymService) -> None:
    service.add_alias("pushy up thing", "vertical push")
    assert (
        service.search_exercises("pushy up thing")["matches"][0]["canonical_name"]
        == "vertical push"
    )
    service.begin_workout()
    assert service.log_exercise("pushy up thing", Decimal(90), (5, 5, 5))["ok"] is True


def test_history_and_input_boundaries(service: GymService) -> None:
    service.begin_workout()
    assert service.current_workout()["remaining_categories"] == ["push", "pull", "legs"]
    assert len(service.recent_history(1)["workouts"]) == 1
    with pytest.raises(GymError, match="between 1 and 100"):
        service.recent_history(0)
    with pytest.raises(GymError, match="non-negative"):
        service.log_exercise("vertical push", Decimal(90), (5, -1, 5))


def test_unknown_entities_are_clear_errors(service: GymService) -> None:
    with pytest.raises(GymError, match="unknown equipment"):
        service.remove_equipment("missing")
    with pytest.raises(GymError, match="unknown exercise"):
        service.add_alias("alias", "missing")
