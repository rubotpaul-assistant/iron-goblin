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


def test_logging_preserves_independent_weight_for_each_set(service: GymService) -> None:
    """Warm-up or ramping sets retain their actual loads."""
    service.begin_workout()
    result = service.log_exercise(
        "vertical legs",
        (Decimal(40), Decimal(40), Decimal(60)),
        (5, 5, 5),
    )
    with service.database.connect() as connection:
        weights = connection.execute(
            """SELECT s.weight FROM sets s
            JOIN workout_exercises wx ON wx.id=s.workout_exercise_id
            JOIN exercises e ON e.id=wx.exercise_id
            WHERE e.canonical_name='vertical legs' ORDER BY s.set_number"""
        ).fetchall()
    assert [row["weight"] for row in weights] == [40, 40, 60]
    assert result["set_weights"] == [40, 40, 60]
    assert result["next_weight"] == 60


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


def test_legacy_database_migrates_every_workout_to_geoff(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """CREATE TABLE workout_sessions (
        id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT,
        local_date TEXT NOT NULL, source TEXT NOT NULL, orientation TEXT NOT NULL,
        status TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '');
        INSERT INTO workout_sessions VALUES
        (7,'2026-08-01T10:00:00-07:00',NULL,'2026-08-01','manual','vertical','active','keep');
        CREATE TABLE workout_exercises (
        id INTEGER PRIMARY KEY,
        workout_session_id INTEGER NOT NULL REFERENCES workout_sessions(id));
        INSERT INTO workout_exercises VALUES(9,7);"""
    )
    connection.close()
    database = Database(path)
    database.initialize()
    with database.connect() as migrated:
        row = migrated.execute(
            """SELECT ws.id,ws.notes,u.slug FROM workout_sessions ws
            JOIN users u ON u.id=ws.user_id"""
        ).fetchone()
        nullable = next(
            item["notnull"]
            for item in migrated.execute("PRAGMA table_info(workout_sessions)")
            if item["name"] == "user_id"
        )
        foreign_keys = migrated.execute("PRAGMA foreign_key_check").fetchall()
    assert dict(row) == {"id": 7, "notes": "keep", "slug": "geoff"}
    assert nullable == 1
    assert foreign_keys == []


def test_users_identity_profiles_cardio_and_daily_energy_are_isolated(
    service: GymService,
) -> None:
    assert [user["slug"] for user in service.list_users()["users"]] == ["freedom", "geoff"]
    service.map_identity("discord", "579188864804192268", "geoff", "Geoff")
    assert service.resolve_identity("discord", "579188864804192268")["user"] == "geoff"
    assert service.resolve_identity("discord", "unknown")["requires_clarification"] is True
    service.set_profile("freedom", "5ft 6in", "150 lb", "1987-04-12", "female")
    logged = service.log_cardio(
        user="freedom",
        activity="hike",
        duration="41m",
        distance="1.41 mi",
        elevation_gain="240 ft",
        rpe=7,
        activity_date="2026-08-18",
        source="alltrails_screenshot",
        source_message_id="abc",
        notes="Senador Mine loop",
        submitted_by="geoff",
    )
    assert logged["user"] == "freedom"
    assert logged["canonical_activity"] == "hiking"
    assert logged["net_calories_kcal"] < logged["gross_calories_kcal"]
    assert service.recent_cardio("geoff")["sessions"] == []
    assert len(service.recent_cardio("freedom")["sessions"]) == 1
    duplicate = service.log_cardio(
        user="freedom",
        activity="hiking",
        duration="41m",
        distance="1.41 mi",
        elevation_gain="240 ft",
        rpe=7,
        activity_date="2026-08-18",
        source="alltrails_screenshot",
    )
    assert duplicate["possible_duplicate"] is True
    daily = service.daily_calories("freedom", "2026-08-18")
    assert daily["cardio_net_kcal"] == logged["net_calories_kcal"]
    assert (
        abs(daily["estimated_total_kcal"] - (daily["baseline_kcal"] + daily["cardio_net_kcal"]))
        <= 1
    )
    assert daily["strength_net_kcal"] is None


def test_imported_calories_take_precedence_and_missing_profile_is_structured(
    service: GymService,
) -> None:
    with pytest.raises(GymError, match="missing profile fields: height_cm,weight_kg,birth_date"):
        service.daily_calories("freedom", "2026-08-18")
    service.set_profile("freedom", "66 in", "150 lb", "1987-04-12", "female")
    result = service.log_cardio(
        user="freedom",
        activity="walking",
        duration="30m",
        rpe=3,
        activity_date="2026-08-18",
        source="health_connect",
        source_external_id="hc-1",
        imported_calories=Decimal("222"),
    )
    assert result["calorie_method"] == "health_connect"
    assert service.daily_calories("freedom", "2026-08-18")["cardio_net_kcal"] == 222
