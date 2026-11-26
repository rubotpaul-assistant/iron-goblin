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
    ScheduledStrengthResult,
    StrengthDateCorrection,
    WorkoutRecordNotFound,
    WorkoutStateConflict,
)
from gym_tracker.strength_replay import (
    IdempotencyKeyReused,
    correct_exercise_date_with_replay,
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
