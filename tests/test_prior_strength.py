"""Exercise the local prior-workout onboarding path and existing progression."""

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
from gym_tracker.service import GymError, GymService, PriorStrengthResult, WorkoutRecordNotFound
from gym_tracker.strength_replay import IdempotencyKeyReused, record_prior_workout_with_replay

NOW = datetime(2026, 11, 26, 12, tzinfo=UTC)
PRIOR_DATE = date(2026, 11, 20)
LONDON = ZoneInfo("Europe/London")


@pytest.fixture
def service(tmp_path: Path) -> GymService:
    return GymService(Database(tmp_path / "onboarding.sqlite3"))


def create_result(
    service: GymService, owner_id: int, category: Category, *, technical_failure: bool = False
) -> PriorStrengthResult:
    machine = f"{category} machine"
    service.add_equipment(
        machine, "machine", Decimal(10), Decimal(150), Decimal(10), owner_id=owner_id
    )
    exercise = service.add_exercise(
        f"{category} press",
        category,
        Orientation.VERTICAL,
        "machine",
        equipment_name=machine,
        owner_id=owner_id,
    )
    return PriorStrengthResult(
        exercise["id"],
        (Decimal(70), Decimal(80), Decimal(90)),
        (5, 5, 5),
        "lb",
        1,
        technical_failure,
    )


def test_prior_workout_drives_next_prescription_and_replays_once(service: GymService) -> None:
    push = create_result(service, 1, Category.PUSH)
    pull = create_result(service, 1, Category.PULL, technical_failure=True)
    create_result(service, 2, Category.PUSH)
    previous = (push, pull)
    first = record_prior_workout_with_replay(
        service, 1, PRIOR_DATE, previous, "prior", uuid4(), time_zone=LONDON, now=NOW
    )
    assert first.status == 200
    reloaded = GymService(Database(service.database.path))
    assert (
        record_prior_workout_with_replay(
            reloaded, 1, PRIOR_DATE, previous, "prior", uuid4(), time_zone=LONDON, now=NOW
        )
        == first
    )
    with pytest.raises(IdempotencyKeyReused):
        record_prior_workout_with_replay(
            reloaded,
            1,
            PRIOR_DATE,
            (replace(push, final_rir=3), pull),
            "prior",
            uuid4(),
            time_zone=LONDON,
            now=NOW,
        )

    (workout,) = reloaded.performed_history(1, from_date=PRIOR_DATE, through_date=PRIOR_DATE)
    assert workout["workout_id"] == json.loads(first.body)["workout_id"]
    assert workout["status"] == "completed"
    assert len(workout["sets"]) == 6
    assert reloaded.performed_history(2) == []
    scheduled = reloaded.begin_workout(scheduled=True, now=NOW, owner_id=1)
    assert scheduled["orientation"] == "vertical"
    recommendations = {row["category"]: row for row in scheduled["recommendations"]}
    assert recommendations["push"]["weight"] == 100
    assert recommendations["pull"]["weight"] == 90
    assert recommendations["push"]["previous_set_weights"] == [70, 80, 90]
    fresh = reloaded.begin_workout(scheduled=True, now=NOW, owner_id=2)
    assert fresh["recommendations"][0]["weight_status"] == "baseline_required"
    with reloaded.database.connect() as connection:
        stored = connection.execute(
            "SELECT performed_local_date,performed_at_utc,performed_time_zone,"
            "performed_date_explicit "
            "FROM workout_exercises WHERE workout_session_id=?",
            (workout["workout_id"],),
        ).fetchall()
        assert {tuple(row) for row in stored} == {("2026-11-20", None, LONDON.key, 1)}
        assert connection.execute("SELECT COUNT(*) FROM strength_audit_events").fetchone()[0] == 2
        assert (
            connection.execute("SELECT COUNT(*) FROM strength_replay_receipts").fetchone()[0] == 1
        )


def test_invalid_prior_workout_leaves_no_partial_data(service: GymService) -> None:
    first = create_result(service, 1, Category.PUSH)
    foreign = create_result(service, 2, Category.PULL)
    unlinked = service.add_exercise(
        "unlinked", Category.LEGS, Orientation.VERTICAL, "machine", owner_id=1
    )
    for day, results, message in (
        (PRIOR_DATE, (), "between 1 and 20"),
        (PRIOR_DATE, (first, first), "only once"),
        (date(2026, 11, 27), (first,), "cannot be in the future"),
        (PRIOR_DATE, (first, foreign), "exercise not found"),
        (PRIOR_DATE, (replace(first, exercise_id=unlinked["id"]),), "known equipment"),
        (PRIOR_DATE, (replace(first, unit="kg"),), "unit must match"),
        (PRIOR_DATE, (replace(first, final_rir=4),), "between 0 and 3"),
        (PRIOR_DATE, (replace(first, final_rir=None),), "RIR is required"),
        (PRIOR_DATE, (replace(first, weights=(Decimal(75),) * 3),), "not supported"),
    ):
        with pytest.raises(GymError, match=message):
            record_prior_workout_with_replay(
                service, 1, day, results, "same", uuid4(), time_zone=LONDON, now=NOW
            )
        assert service.recent_history()["workouts"] == []
    with pytest.raises(WorkoutRecordNotFound, match="exercise not found"):
        record_prior_workout_with_replay(
            service,
            1,
            PRIOR_DATE,
            (replace(first, exercise_id=99),),
            "same",
            uuid4(),
            time_zone=LONDON,
            now=NOW,
        )
    assert (
        record_prior_workout_with_replay(
            service, 1, PRIOR_DATE, (first,), "same", uuid4(), time_zone=LONDON, now=NOW
        ).status
        == 200
    )
    service.add_equipment(
        "push machine", "machine", Decimal(10), Decimal(150), Decimal(10), unit="kg", owner_id=1
    )
    with pytest.raises(GymError, match="unit must match the equipment"):
        record_prior_workout_with_replay(
            service, 1, PRIOR_DATE, (first,), "wrong-grid", uuid4(), time_zone=LONDON, now=NOW
        )
    assert len(service.performed_history(1)) == 1
