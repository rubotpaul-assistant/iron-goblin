"""Application service integration tests against real SQLite."""

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from gym_tracker.db import Database
from gym_tracker.domain import Category, Orientation
from gym_tracker.service import (
    GymError,
    GymService,
    ScheduledStrengthResult,
    WorkoutRecordNotFound,
    WorkoutStateConflict,
)


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


def test_scheduled_begin_carries_forward_an_older_active_workout(
    service: GymService,
) -> None:
    monday = datetime(2026, 8, 24, 12, tzinfo=ZoneInfo("America/Los_Angeles"))
    thursday = datetime(2026, 8, 27, 12, tzinfo=ZoneInfo("America/Los_Angeles"))
    pending = service.begin_workout(scheduled=True, now=monday)

    carried = service.begin_workout(scheduled=True, now=thursday)

    assert carried["workout_id"] == pending["workout_id"]
    assert carried["duplicate"] is True
    assert carried["carried_forward"] is True
    assert carried["remaining_categories"] == ["push", "pull", "legs"]


def test_backdated_bodyweight_workout_is_atomic_mixed_and_deduplicated(
    service: GymService,
) -> None:
    service.add_exercise("Push-Up", Category.PUSH, Orientation.HORIZONTAL, "bodyweight")
    service.add_exercise("Bodyweight Squat", Category.LEGS, Orientation.VERTICAL, "bodyweight")
    active = service.begin_workout(
        scheduled=True,
        now=datetime(2026, 10, 12, 12, tzinfo=ZoneInfo("America/Los_Angeles")),
    )
    results = (
        ("Push-Up", (6, 6, 6), 3),
        ("Bodyweight Squat", (8, 8, 8, 8), 3),
    )

    logged = service.log_bodyweight_workout(
        "geoff", "2026-10-13", results, notes="Low grit bodyweight workout"
    )
    duplicate = service.log_bodyweight_workout(
        "geoff", "2026-10-13", results, notes="Low grit bodyweight workout"
    )

    assert logged == {
        "ok": True,
        "duplicate": False,
        "user": "geoff",
        "date": "2026-10-13",
        "workout_id": logged["workout_id"],
        "workout_number": 1,
        "orientation": "mixed",
        "status": "completed",
        "exercises": [
            {"exercise": "Push-Up", "reps": [6, 6, 6], "final_set_rir": 3},
            {
                "exercise": "Bodyweight Squat",
                "reps": [8, 8, 8, 8],
                "final_set_rir": 3,
            },
        ],
        "sync_pending": True,
    }
    assert duplicate["duplicate"] is True
    assert duplicate["workout_id"] == logged["workout_id"]
    assert service.current_workout()["workout_id"] == active["workout_id"]
    with service.database.connect() as connection:
        workout_count = connection.execute(
            "SELECT COUNT(*) FROM workout_sessions WHERE orientation='mixed'"
        ).fetchone()[0]
        sets = connection.execute(
            """SELECT e.canonical_name,s.completed_reps,s.weight,s.rir
            FROM sets s JOIN workout_exercises wx ON wx.id=s.workout_exercise_id
            JOIN exercises e ON e.id=wx.exercise_id
            JOIN workout_sessions ws ON ws.id=wx.workout_session_id
            WHERE ws.orientation='mixed' ORDER BY wx.sequence,s.set_number"""
        ).fetchall()
    assert workout_count == 1
    assert [tuple(row) for row in sets] == [
        ("Push-Up", 6, 0, None),
        ("Push-Up", 6, 0, None),
        ("Push-Up", 6, 0, 3),
        ("Bodyweight Squat", 8, 0, None),
        ("Bodyweight Squat", 8, 0, None),
        ("Bodyweight Squat", 8, 0, None),
        ("Bodyweight Squat", 8, 0, 3),
    ]


def test_bodyweight_workout_validates_complete_input_before_writing(
    service: GymService,
) -> None:
    service.add_exercise("Push-Up", Category.PUSH, Orientation.HORIZONTAL, "bodyweight")

    with pytest.raises(GymError, match="bodyweight"):
        service.log_bodyweight_workout("geoff", "2026-10-13", (("Vertical Push", (6, 6, 6), 3),))
    with pytest.raises(GymError, match="YYYY-MM-DD"):
        service.log_bodyweight_workout("geoff", "yesterday", (("Push-Up", (6, 6, 6), 3),))
    with pytest.raises(GymError, match="between 0 and 3"):
        service.log_bodyweight_workout("geoff", "2026-10-13", (("Push-Up", (6, 6, 6), 4),))

    with service.database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM workout_sessions WHERE orientation='mixed'"
            ).fetchone()[0]
            == 0
        )


def test_mixed_workout_does_not_change_thicc_gymmy_alternation(service: GymService) -> None:
    first = service.begin_workout()
    service.skip_workout()
    service.add_exercise("Push-Up", Category.PUSH, Orientation.HORIZONTAL, "bodyweight")
    service.log_bodyweight_workout("geoff", "2026-10-13", (("Push-Up", (6, 6, 6), 3),))

    second = service.begin_workout()

    assert first["orientation"] == "vertical"
    assert second["orientation"] == "horizontal"


def test_logging_records_performance_date_in_pacific_time(service: GymService) -> None:
    service.begin_workout(now=datetime(2026, 8, 24, 12, tzinfo=ZoneInfo("America/Los_Angeles")))

    service.log_exercise(
        "vertical push",
        Decimal(90),
        (5, 5, 5),
        final_rir=1,
        now=datetime(2026, 9, 8, 23, 30, tzinfo=ZoneInfo("America/Los_Angeles")),
    )

    with service.database.connect() as connection:
        performed_date = connection.execute(
            "SELECT performed_local_date FROM workout_exercises WHERE status='completed'"
        ).fetchone()[0]
    assert performed_date == "2026-09-08"


def test_history_date_correction_uses_performed_workout_number(service: GymService) -> None:
    service.begin_workout()
    service.log_exercise("vertical push", Decimal(90), (5, 5, 5), final_rir=1)
    service.skip_workout()

    corrected = service.correct_workout_date("geoff", 1, "2026-09-08")

    assert corrected == {
        "ok": True,
        "user": "geoff",
        "workout_number": 1,
        "date": "2026-09-08",
        "exercises_updated": 1,
        "sync_pending": True,
    }

    with pytest.raises(GymError, match="at least 1"):
        service.correct_workout_date("geoff", 0, "2026-09-08")
    with pytest.raises(GymError, match="YYYY-MM-DD"):
        service.correct_workout_date("geoff", 1, "September 8")
    with pytest.raises(GymError, match="unknown performed workout"):
        service.correct_workout_date("geoff", 2, "2026-09-08")


def test_skip_distinguishes_empty_and_partial_workouts(service: GymService) -> None:
    service.begin_workout()
    assert service.skip_workout()["status"] == "skipped"
    service.begin_workout()
    service.log_exercise("horizontal push", Decimal(90), (5, 5, 5), final_rir=1)

    assert service.skip_workout()["status"] == "partial"


def test_final_set_rir_is_required_and_bounded(service: GymService) -> None:
    service.begin_workout()
    with pytest.raises(GymError, match="required"):
        service.log_exercise("vertical push", Decimal(90), (5, 5, 5))
    with pytest.raises(GymError, match="between 0 and 3"):
        service.log_exercise("vertical push", Decimal(90), (5, 5, 5), final_rir=4)


def test_all_lowercase_exercise_names_are_canonicalized_to_title_case(
    service: GymService,
) -> None:
    result = service.add_exercise(
        "standing dumbbell shoulder press",
        Category.PUSH,
        Orientation.VERTICAL,
        "dumbbell",
    )

    assert result["exercise"] == "Standing Dumbbell Shoulder Press"
    assert service.search_exercises("standing")["matches"][0]["canonical_name"] == (
        "Standing Dumbbell Shoulder Press"
    )


def test_recommendations_match_orientation_and_exclude_unavailable_equipment(
    service: GymService,
) -> None:
    service.remove_equipment("vertical-push-machine")
    workout = service.begin_workout()
    categories = {recommendation["category"] for recommendation in workout["recommendations"]}
    assert "push" not in categories
    assert {item["exercise"] for item in workout["recommendations"]} == {
        "Vertical Pull",
        "Vertical Legs",
    }


def test_third_performed_workout_uses_prior_results_for_weights(
    service: GymService,
) -> None:
    first = service.begin_workout()
    assert first["workout_number"] == 1
    for category in Category:
        service.log_exercise(f"vertical {category}", Decimal(90), (5, 5, 5), final_rir=1)
    service.complete_workout()

    second = service.begin_workout()
    assert second["workout_number"] == 2
    for category in Category:
        service.log_exercise(f"horizontal {category}", Decimal(90), (5, 5, 5), final_rir=1)
    service.complete_workout()

    third = service.begin_workout()

    assert third["workout_number"] == 3
    assert {item["weight"] for item in third["recommendations"]} == {100}
    assert {item["weight_status"] for item in third["recommendations"]} == {"known"}
    assert {tuple(item["previous_set_weights"]) for item in third["recommendations"]} == {
        (90, 90, 90)
    }


def test_prior_final_set_is_baseline_without_equipment(tmp_path: Path) -> None:
    service = GymService(Database(tmp_path / "gym.sqlite3"))
    for category in Category:
        service.add_exercise(f"vertical {category}", category, Orientation.VERTICAL, "free_weight")
        service.add_exercise(
            f"horizontal {category}", category, Orientation.HORIZONTAL, "free_weight"
        )
    service.begin_workout()
    for category in Category:
        service.log_exercise(
            f"vertical {category}",
            (Decimal(45), Decimal(45), Decimal(65)),
            (5, 5, 5),
            final_rir=1,
        )
    service.complete_workout()
    service.begin_workout()
    for category in Category:
        service.log_exercise(f"horizontal {category}", Decimal(50), (5, 5, 5), final_rir=1)
    service.complete_workout()

    recommendation = service.begin_workout()["recommendations"][0]

    assert recommendation["weight"] == 65
    assert recommendation["previous_set_weights"] == [45, 45, 65]


def test_current_workout_recovers_legacy_null_prescription(service: GymService) -> None:
    service.begin_workout()
    for category in Category:
        service.log_exercise(f"vertical {category}", Decimal(90), (5, 5, 5), final_rir=0)
    service.complete_workout()
    service.begin_workout()
    for category in Category:
        service.log_exercise(f"horizontal {category}", Decimal(90), (5, 5, 5), final_rir=0)
    service.complete_workout()
    service.begin_workout()
    with service.database.connect() as connection:
        connection.execute(
            """UPDATE workout_exercises SET prescribed_weight=NULL
            WHERE workout_session_id=(SELECT id FROM workout_sessions WHERE status='active')"""
        )

    current = service.current_workout()

    assert {item["weight"] for item in current["recommendations"]} == {90}
    assert {item["weight_status"] for item in current["recommendations"]} == {"known"}


def test_substitute_replaces_same_category_before_logging(service: GymService) -> None:
    service.begin_workout()
    service.add_exercise(
        "standing dumbbell shoulder press",
        Category.PUSH,
        Orientation.VERTICAL,
        "dumbbell",
    )
    result = service.substitute_exercise("standing dumbbell shoulder press")
    logged = service.log_exercise(
        "standing dumbbell shoulder press", Decimal(20), (5, 5, 5), final_rir=1
    )

    assert result["replaced"] == "Vertical Push"
    assert result["exercise"] == "Standing Dumbbell Shoulder Press"
    assert logged["set_weights"] == [20, 20, 20]


def test_substitute_rejects_different_orientation(service: GymService) -> None:
    service.begin_workout()

    with pytest.raises(GymError, match="orientation"):
        service.substitute_exercise("horizontal push")


def test_completion_requires_all_categories(service: GymService) -> None:
    service.begin_workout()
    service.log_exercise("vertical push", Decimal(90), (5, 5, 5), final_rir=0)
    with pytest.raises(GymError, match="pull,legs"):
        service.complete_workout()
    service.log_exercise("vertical pull", Decimal(90), (5, 5, 5), final_rir=1)
    service.log_exercise("vertical legs", Decimal(90), (5, 5, 4), final_rir=0)
    completed = service.complete_workout()
    assert completed["status"] == "completed"
    assert completed["workout_number"] == 1


def test_logging_respects_increment_and_progression(service: GymService) -> None:
    service.begin_workout()
    with pytest.raises(GymError, match="not supported"):
        service.log_exercise("vertical push", Decimal(95), (5, 5, 5), final_rir=1)
    result = service.log_exercise("vertical push", Decimal(90), (5, 5, 5), final_rir=1)
    assert result["next_weight"] == 100
    assert result["sync_pending"] is True


def test_bodyweight_recommendation_progresses_completed_reps(tmp_path: Path) -> None:
    service = GymService(Database(tmp_path / "gym.sqlite3"))
    for category in Category:
        service.add_exercise(f"vertical {category}", category, Orientation.VERTICAL, "bodyweight")
        service.add_exercise(
            f"horizontal {category}", category, Orientation.HORIZONTAL, "bodyweight"
        )
    service.begin_workout()
    for category in Category:
        service.log_exercise(f"vertical {category}", Decimal(0), (7, 7, 7), final_rir=3)
    service.complete_workout()
    service.begin_workout()
    for category in Category:
        service.log_exercise(f"horizontal {category}", Decimal(0), (5, 5, 5), final_rir=0)
    service.complete_workout()

    recommendations = service.begin_workout()["recommendations"]

    assert {item["reps"] for item in recommendations} == {9}


def test_equipment_can_store_irregular_loads_and_attach_existing_exercise(
    tmp_path: Path,
) -> None:
    service = GymService(Database(tmp_path / "gym.sqlite3"))
    service.add_exercise("Assisted Dip", Category.PUSH, Orientation.VERTICAL, "machine")
    service.add_equipment(
        "assisted dip stack",
        "machine",
        Decimal(30),
        Decimal(70),
        Decimal(5),
        supported_weights=(Decimal(30), Decimal(45), Decimal(55), Decimal(70)),
    )
    service.assign_equipment("Assisted Dip", "assisted dip stack")

    listed = service.list_equipment()["equipment"][0]
    assert listed["supported_weights"] == [30, 45, 55, 70]


def test_equipment_rejects_unknown_progression_direction(tmp_path: Path) -> None:
    service = GymService(Database(tmp_path / "gym.sqlite3"))
    with pytest.raises(GymError, match="direction"):
        service.add_equipment(
            "bad machine",
            "machine",
            Decimal(10),
            Decimal(100),
            Decimal(10),
            progression_direction="sideways",
        )


def test_equipment_rejects_supported_load_outside_declared_range(tmp_path: Path) -> None:
    service = GymService(Database(tmp_path / "gym.sqlite3"))

    with pytest.raises(GymError, match="within the weight range"):
        service.add_equipment(
            "bad stack",
            "machine",
            Decimal(10),
            Decimal(100),
            Decimal(5),
            supported_weights=(Decimal(10), Decimal(115)),
        )


def test_logging_preserves_independent_weight_for_each_set(service: GymService) -> None:
    """Warm-up or ramping sets retain their actual loads."""
    service.begin_workout()
    result = service.log_exercise(
        "vertical legs",
        (Decimal(40), Decimal(40), Decimal(60)),
        (5, 5, 5),
        final_rir=0,
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
        == "Vertical Push"
    )
    service.begin_workout()
    assert service.log_exercise("pushy up thing", Decimal(90), (5, 5, 5), final_rir=1)["ok"] is True


def test_history_and_input_boundaries(service: GymService) -> None:
    service.begin_workout()
    assert service.current_workout()["remaining_categories"] == ["push", "pull", "legs"]
    assert len(service.recent_history(1)["workouts"]) == 1
    with pytest.raises(GymError, match="between 1 and 100"):
        service.recent_history(0)
    with pytest.raises(GymError, match="non-negative"):
        service.log_exercise("vertical push", Decimal(90), (5, -1, 5), final_rir=1)


def test_history_summary_counts_recorded_sets_not_lifecycle_shells(
    service: GymService,
) -> None:
    service.begin_workout()
    for category in Category:
        service.log_exercise(f"vertical {category}", Decimal(90), (5, 5, 5), final_rir=1)
    service.complete_workout()

    service.begin_workout()
    for category in Category:
        service.log_exercise(f"horizontal {category}", Decimal(90), (5, 5, 5), final_rir=1)
    with service.database.connect() as connection:
        connection.execute(
            """INSERT INTO workout_sessions
            (started_at,local_date,source,orientation,status,user_id)
            VALUES('2026-08-20T09:00:00-07:00','2026-08-20','manual','vertical','skipped',1)"""
        )

    result = service.history_summary("geoff")

    assert result["strength"] == {
        "workouts": 2,
        "exercises": 6,
        "sets": 18,
        "raw_session_rows": 3,
        "status_counts": {"active": 1, "completed": 1},
    }
    assert result["cardio_sessions"] == 0


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


@pytest.fixture
def scheduled_result(service: GymService) -> ScheduledStrengthResult:
    service.begin_workout(scheduled=True)
    active = service.active_scheduled_workout(1)
    assert active is not None
    assert active["status"] == "active"
    prescription = active["prescriptions"][0]
    assert prescription["status"] == "prescribed"
    return ScheduledStrengthResult(
        active["workout_id"],
        prescription["prescription_id"],
        prescription["exercise_id"],
        (Decimal(70), Decimal(80), Decimal(90)),
        (6, 5, 5),
        prescription["unit"],
        1,
    )


def test_scheduled_strength_result_is_owner_scoped_and_matches_cli_progression(
    service: GymService, scheduled_result: ScheduledStrengthResult
) -> None:
    assert service.active_scheduled_workout(2) is None
    assert service.performed_history(1) == []
    now = datetime(2026, 11, 27, 1, tzinfo=UTC)
    with service.database.connect() as connection:
        result = service.record_scheduled_result(connection, 1, scheduled_result, now=now)

    assert result["workout_id"] == scheduled_result.workout_id
    assert result["prescription_id"] == scheduled_result.prescription_id
    assert result["performed_local_date"] == "2026-11-26"
    active = service.active_scheduled_workout(1)
    assert active is not None
    assert active["prescriptions"][0]["status"] == "completed"
    (workout,) = service.performed_history(1)
    assert workout["workout_id"] == result["workout_id"]
    assert workout["status"] == "active"
    assert workout["local_date"] == "2026-11-26"
    assert [row["weight"] for row in workout["sets"]] == [70, 80, 90]
    assert [row["completed_reps"] for row in workout["sets"]] == [6, 5, 5]
    assert [row["rir"] for row in workout["sets"]] == [None, None, 1]
    assert {row["prescription_id"] for row in workout["sets"]} == {result["prescription_id"]}
    assert service.performed_history(2) == []
    with pytest.raises(WorkoutStateConflict), service.database.connect() as connection:
        service.record_scheduled_result(connection, 1, scheduled_result)

    cli_result = service.log_exercise(
        result["exercise"], scheduled_result.weights, scheduled_result.reps, final_rir=1, now=now
    )
    assert cli_result == {
        key: value
        for key, value in result.items()
        if key not in {"workout_id", "prescription_id", "performed_local_date"}
    }
    service.skip_workout()
    assert service.performed_history(1)[0]["status"] == "partial"
    assert service.active_scheduled_workout(1) is None
    with pytest.raises(WorkoutStateConflict), service.database.connect() as connection:
        service.record_scheduled_result(connection, 1, scheduled_result)


def test_foreign_workout_and_missing_prescription_have_same_error(
    service: GymService, scheduled_result: ScheduledStrengthResult
) -> None:
    for owner_id, result in (
        (2, scheduled_result),
        (1, replace(scheduled_result, workout_id=-1)),
        (1, replace(scheduled_result, prescription_id=-1)),
    ):
        with (
            pytest.raises(WorkoutRecordNotFound, match=r"^workout or prescription not found$"),
            service.database.connect() as connection,
        ):
            service.record_scheduled_result(connection, owner_id, result)


def test_substitution_invalidates_discovered_exercise_identity(
    service: GymService, scheduled_result: ScheduledStrengthResult
) -> None:
    active = service.active_scheduled_workout(1)
    assert active is not None
    current = service.current_workout()["recommendations"][0]
    service.add_exercise(
        "replacement", Category(current["category"]), Orientation.VERTICAL, "machine"
    )
    service.substitute_exercise("replacement")
    changed = service.active_scheduled_workout(1)
    assert changed is not None
    assert changed["prescriptions"][0]["prescription_id"] == scheduled_result.prescription_id
    assert changed["prescriptions"][0]["exercise_id"] != scheduled_result.expected_exercise_id
    with pytest.raises(WorkoutStateConflict), service.database.connect() as connection:
        service.record_scheduled_result(connection, 1, scheduled_result)
    assert service.performed_history(1) == []


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"weights": (Decimal(70), Decimal(80), Decimal("NaN"))}, "finite non-negative"),
        ({"weights": (Decimal(70), Decimal(80), Decimal("1e400"))}, "finite non-negative"),
        ({"weights": (Decimal(70), Decimal(80), Decimal(-1))}, "finite non-negative"),
        ({"weights": (Decimal(70), Decimal(80))}, "three finite"),
        ({"weights": (Decimal(70), Decimal(80), Decimal(95))}, "not supported"),
        ({"final_rir": 4}, "between 0 and 3"),
        ({"reps": (5, -1, 5)}, "non-negative"),
        ({"reps": (5, 5)}, "three rep counts"),
        ({"unit": "kg"}, "unit must match"),
    ],
)
def test_scheduled_result_rejects_invalid_values(
    service: GymService,
    scheduled_result: ScheduledStrengthResult,
    changes: dict[str, object],
    message: str,
) -> None:
    result = replace(scheduled_result, **changes)
    with pytest.raises(GymError, match=message), service.database.connect() as connection:
        service.record_scheduled_result(connection, 1, result)
    assert service.performed_history(1) == []


def test_scheduled_result_rolls_back_with_caller_transaction(
    service: GymService, scheduled_result: ScheduledStrengthResult
) -> None:
    def write_then_fail() -> None:
        with service.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            service.record_scheduled_result(connection, 1, scheduled_result)
            assert connection.execute("SELECT COUNT(*) FROM sets").fetchone()[0] == 3
            raise RuntimeError("receipt failed")

    with pytest.raises(RuntimeError, match="receipt failed"):
        write_then_fail()
    assert service.performed_history(1) == []
    workout = service.active_scheduled_workout(1)
    assert workout is not None
    assert workout["prescriptions"][0]["status"] == "prescribed"
    with service.database.connect() as connection:
        assert connection.execute("SELECT pending FROM sync_state").fetchone()[0] == 0


def test_performed_history_limit_is_applied_to_workouts_before_sets(
    service: GymService, scheduled_result: ScheduledStrengthResult
) -> None:
    with service.database.connect() as connection:
        service.record_scheduled_result(connection, 1, scheduled_result)
    service.skip_workout()
    second = service.begin_workout()
    assert service.active_scheduled_workout(1) is None
    service.log_exercise(
        second["recommendations"][0]["exercise"], Decimal(90), (5, 5, 5), final_rir=0
    )
    # Tie the starting timestamps to exercise the canonical ID tiebreaker.
    with service.database.connect() as connection:
        connection.execute("UPDATE workout_sessions SET started_at='2026-11-26T12:00:00-08:00'")
    (recent,) = service.performed_history(1, limit=1)
    assert recent["workout_id"] == second["workout_id"]
    assert len(recent["sets"]) == 3
    with pytest.raises(GymError, match="limit must be between"):
        service.performed_history(1, limit=0)


def test_workout_lifecycle_is_isolated_across_owners(service: GymService) -> None:
    now = datetime(2026, 11, 26, 12, tzinfo=UTC)
    geoff = service.begin_workout(scheduled=True, now=now)
    second = service.begin_workout(scheduled=True, now=now, owner_id=2)
    assert geoff["workout_id"] != second["workout_id"]
    assert service.current_workout()["workout_id"] == geoff["workout_id"]
    assert service.current_workout(owner_id=2)["workout_id"] == second["workout_id"]

    service.add_exercise("other press", Category.PUSH, Orientation.VERTICAL, "machine")
    service.substitute_exercise("other press", owner_id=2)
    with pytest.raises(GymError, match="not prescribed"):
        service.log_exercise("other press", Decimal(90), (5, 5, 5), final_rir=1)
    for category in Category:
        service.log_exercise(f"vertical {category}", Decimal(90), (5, 5, 5), final_rir=1)
    assert service.complete_workout()["workout_id"] == geoff["workout_id"]
    assert service.performed_history(2) == []
    assert service.current_workout(owner_id=2)["workout_id"] == second["workout_id"]
    with pytest.raises(GymError, match="no active workout"):
        service.current_workout()

    service.log_exercise("other press", Decimal(80), (5, 5, 5), final_rir=1, owner_id=2)
    assert service.skip_workout(owner_id=2)["status"] == "partial"
    assert service.performed_history(2)[0]["workout_id"] == second["workout_id"]
    assert service.performed_history(1)[0]["workout_id"] == geoff["workout_id"]
    assert service.begin_workout(scheduled=True, now=now)["workout_id"] == geoff["workout_id"]
    assert (
        service.begin_workout(scheduled=True, now=now, owner_id=2)["workout_id"]
        == (second["workout_id"])
    )


def test_other_owners_do_not_affect_orientation_or_exercise_rotation(
    service: GymService,
) -> None:
    service.add_exercise("zz alternate press", Category.PUSH, Orientation.VERTICAL, "machine")
    day_one = datetime(2026, 11, 26, 12, tzinfo=UTC)
    day_two = datetime(2026, 11, 27, 12, tzinfo=UTC)
    first = service.begin_workout(scheduled=True, now=day_one)
    assert first["orientation"] == "vertical"
    service.log_exercise("vertical push", Decimal(90), (5, 5, 5), final_rir=1)
    service.skip_workout()
    assert service.begin_workout(scheduled=True, now=day_two)["orientation"] == "horizontal"

    other = service.begin_workout(scheduled=True, now=day_one, owner_id=2)
    assert other["orientation"] == "vertical"
    push = next(row for row in other["recommendations"] if row["category"] == "push")
    assert push["exercise"] == "Vertical Push"
    assert push["weight"] is None
    carried = service.begin_workout(scheduled=True, now=day_two, owner_id=2)
    assert carried["workout_id"] == other["workout_id"]
    assert carried["carried_forward"] is True


def test_owner_index_migration_preserves_existing_active_and_scheduled_rows(
    service: GymService,
) -> None:
    now = datetime(2026, 11, 26, 12, tzinfo=UTC)
    original = service.begin_workout(scheduled=True, now=now)
    with service.database.connect() as connection:
        connection.executescript(
            """DROP INDEX uq_owner_scheduled_day;
            DROP INDEX uq_owner_active_workout;
            CREATE UNIQUE INDEX uq_scheduled_day ON workout_sessions(local_date)
                WHERE source='scheduled';
            CREATE UNIQUE INDEX uq_active_workout ON workout_sessions(status)
                WHERE status='active';"""
        )

    reopened = GymService(Database(service.database.path))
    assert reopened.current_workout() == original
    assert (
        reopened.begin_workout(scheduled=True, now=now, owner_id=2)["workout_id"]
        != (original["workout_id"])
    )
    reopened.database.initialize()
    assert reopened.current_workout()["workout_id"] == original["workout_id"]
    with reopened.database.connect() as connection:
        indexes = {
            index["name"] for index in connection.execute("PRAGMA index_list(workout_sessions)")
        }
        assert {"uq_owner_scheduled_day", "uq_owner_active_workout"} <= indexes
        assert not ({"uq_scheduled_day", "uq_active_workout"} & indexes)


def test_history_range_filters_per_exercise_and_preserves_stored_day(
    service: GymService, scheduled_result: ScheduledStrengthResult
) -> None:
    london = ZoneInfo("Europe/London")
    midnight_utc = datetime(2026, 11, 27, 0, 10, tzinfo=UTC)
    active = service.active_scheduled_workout(1)
    assert active is not None
    other = active["prescriptions"][1]
    next_result = replace(
        scheduled_result,
        prescription_id=other["prescription_id"],
        expected_exercise_id=other["exercise_id"],
    )
    with service.database.connect() as connection:
        service.record_scheduled_result(connection, 1, scheduled_result, now=midnight_utc)
        service.record_scheduled_result(
            connection, 1, next_result, now=midnight_utc, time_zone=london
        )
    day = midnight_utc.astimezone(london).date()
    previous_day = date(2026, 11, 26)
    yesterday = service.performed_history(1, through_date=previous_day)
    today = service.performed_history(1, from_date=day)
    assert {row["prescription_id"] for row in yesterday[0]["sets"]} == {
        scheduled_result.prescription_id
    }
    assert {row["prescription_id"] for row in today[0]["sets"]} == {next_result.prescription_id}
    assert len(service.performed_history(1)[0]["sets"]) == 6
    assert service.performed_history(2, from_date=day) == []
    with pytest.raises(GymError, match="from_date must be on or before"):
        service.performed_history(1, from_date=day, through_date=previous_day)
