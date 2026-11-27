"""Exercise owner-scoped corrections and replay with real recorded sets."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from gym_tracker.db import Database
from gym_tracker.domain import Category, Orientation
from gym_tracker.service import (
    GymError,
    GymService,
    PriorStrengthResult,
    ScheduledStrengthResult,
    StrengthDateCorrection,
    WorkoutRecordNotFound,
    WorkoutStateConflict,
)
from gym_tracker.strength_replay import (
    IdempotencyKeyReused,
    correct_exercise_date_with_replay,
    correct_workout_date_with_replay,
    record_prior_workout_with_replay,
    record_with_replay,
)

NOW = datetime(2026, 11, 26, 12, tzinfo=UTC)
LONDON = ZoneInfo("Europe/London")
EARLIER = date(2026, 11, 20)


@pytest.fixture
def service(tmp_path: Path) -> GymService:
    return GymService(Database(tmp_path / "correction.sqlite3"))


@pytest.fixture
def correction(service: GymService) -> StrengthDateCorrection:
    service.add_equipment("press", "machine", Decimal(10), Decimal(100), Decimal(10), owner_id=1)
    exercise = service.add_exercise(
        "owner press",
        Category.PUSH,
        Orientation.VERTICAL,
        "machine",
        equipment_name="press",
        owner_id=1,
    )
    service.begin_workout(scheduled=True, now=NOW, owner_id=1)
    active = service.active_scheduled_workout(1)
    assert active is not None
    (prescription,) = active["prescriptions"]
    result = ScheduledStrengthResult(
        active["workout_id"],
        prescription["prescription_id"],
        exercise["id"],
        (Decimal(70), Decimal(80), Decimal(90)),
        (5, 5, 5),
        "lb",
        1,
    )
    record_with_replay(service, 1, result, "log", uuid4(), now=NOW, time_zone=LONDON)
    (history,) = service.performed_history(1)
    revision = history["sets"][0]["result_revision"]
    return StrengthDateCorrection(result.workout_id, result.prescription_id, revision, EARLIER)


def test_correction_updates_history_and_replays_once(
    service: GymService, correction: StrengthDateCorrection
) -> None:
    (original,) = service.performed_history(1)
    result = correct_exercise_date_with_replay(
        service, 1, correction, "correction", uuid4(), time_zone=LONDON, now=NOW
    )
    assert json.loads(result.body)["result_revision"] == correction.expected_revision + 1
    assert (
        correct_exercise_date_with_replay(
            service, 1, correction, "correction", uuid4(), time_zone=LONDON, now=NOW
        )
        == result
    )
    with pytest.raises(IdempotencyKeyReused):
        correct_exercise_date_with_replay(
            service,
            1,
            replace(correction, local_date=date(2026, 11, 19)),
            "correction",
            uuid4(),
            time_zone=LONDON,
            now=NOW,
        )

    assert service.performed_history(1, from_date=NOW.date()) == []
    (corrected,) = service.performed_history(1, through_date=EARLIER)
    assert corrected["workout_id"] == original["workout_id"]
    assert corrected["local_date"] == EARLIER.isoformat()
    assert [row["set_id"] for row in corrected["sets"]] == [
        row["set_id"] for row in original["sets"]
    ]
    assert [row["weight"] for row in corrected["sets"]] == [70, 80, 90]
    with service.database.connect() as connection:
        context = connection.execute(
            """SELECT performed_at_utc,performed_time_zone,performed_date_explicit
            FROM workout_exercises WHERE id=?""",
            (correction.prescription_id,),
        ).fetchone()
        assert tuple(context) == (NOW.isoformat(), "Europe/London", 1)
        audit = connection.execute(
            """SELECT COUNT(*) FROM strength_audit_events
            WHERE actor_id=1 AND operation='correct_exercise_date' AND outcome='corrected'"""
        ).fetchone()[0]
        assert audit == 1


def test_stale_and_foreign_date_corrections_are_denied(
    service: GymService, correction: StrengthDateCorrection
) -> None:
    for owner, candidate in (
        (2, correction),
        (1, replace(correction, prescription_id=-1)),
    ):
        with pytest.raises(WorkoutRecordNotFound, match=r"^workout or prescription not found$"):
            correct_exercise_date_with_replay(
                service, owner, candidate, "correct", uuid4(), time_zone=LONDON, now=NOW
            )
    with pytest.raises(GymError, match="cannot be in the future"):
        correct_exercise_date_with_replay(
            service,
            1,
            replace(correction, local_date=date(2026, 11, 27)),
            "correct",
            uuid4(),
            time_zone=LONDON,
            now=NOW,
        )

    # A correction from the existing CLI also advances the result revision.
    service.correct_workout_date("geoff", 1, "2026-11-21")
    with pytest.raises(WorkoutStateConflict, match="result has changed"):
        correct_exercise_date_with_replay(
            service, 1, correction, "correct", uuid4(), time_zone=LONDON, now=NOW
        )
    (workout,) = service.performed_history(1)
    assert workout["local_date"] == "2026-11-21"
    assert service.performed_history(2) == []


def test_atomic_workout_date_correction_keeps_every_set_and_rechecks_all_revisions(
    service: GymService,
) -> None:
    result = []
    for category in (Category.PUSH, Category.PULL):
        exercise = service.add_exercise(
            f"fixture {category}",
            category,
            Orientation.VERTICAL,
            "bodyweight",
            owner_id=1,
        )
        result.append(PriorStrengthResult(exercise["id"], (Decimal(0),) * 3, (5, 5, 5), "lb", 1))
    logged = record_prior_workout_with_replay(
        service, 1, EARLIER, tuple(result), "prior", uuid4(), time_zone=LONDON, now=NOW
    )
    workout_id = json.loads(logged.body)["workout_id"]
    with pytest.raises(WorkoutRecordNotFound):
        service.performed_workout_reference(2, workout_id)
    original = service.performed_workout_reference(1, workout_id)
    old_sets = {row["set_id"] for row in service.performed_history(1)[0]["sets"]}

    first = original.results[0]
    correction = StrengthDateCorrection(
        workout_id, first.prescription_id, first.result_revision, date(2026, 11, 21)
    )
    correct_exercise_date_with_replay(
        service, 1, correction, "partial", uuid4(), time_zone=LONDON, now=NOW
    )
    corrected_date = date(2026, 11, 18)
    with pytest.raises(WorkoutStateConflict, match="workout results have changed"):
        correct_workout_date_with_replay(
            service, 1, original, corrected_date, "whole", uuid4(), time_zone=LONDON, now=NOW
        )
    assert {row["performed_local_date"] for row in service.performed_history(1)[0]["sets"]} == {
        "2026-11-20",
        "2026-11-21",
    }
    current = service.performed_workout_reference(1, workout_id)
    corrected = correct_workout_date_with_replay(
        service, 1, current, corrected_date, "whole", uuid4(), time_zone=LONDON, now=NOW
    )
    assert (
        correct_workout_date_with_replay(
            service, 1, current, corrected_date, "whole", uuid4(), time_zone=LONDON, now=NOW
        )
        == corrected
    )
    (history,) = service.performed_history(1, from_date=corrected_date, through_date=corrected_date)
    assert {row["set_id"] for row in history["sets"]} == old_sets
    assert {row["performed_local_date"] for row in history["sets"]} == {"2026-11-18"}
    assert service.performed_history(2) == []
