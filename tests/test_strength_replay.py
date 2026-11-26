"""Exercise durable scheduled writes through the private tracer replay service."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest

from gym_tracker.db import Database
from gym_tracker.domain import Category, Orientation
from gym_tracker.service import GymError, GymService, ScheduledStrengthResult, WorkoutStateConflict
from gym_tracker.strength_replay import IdempotencyKeyReused, RecordedResponse, record_with_replay

PERFORMED = datetime(2026, 11, 27, 7, 59, tzinfo=UTC)
AFTER_MIDNIGHT = datetime(2026, 11, 27, 8, 1, tzinfo=UTC)


def scheduled_result(service: GymService, owner_id: int, now: datetime) -> ScheduledStrengthResult:
    workout_id = service.begin_workout(scheduled=True, now=now)["workout_id"]
    with service.database.connect() as connection:
        connection.execute(
            "UPDATE workout_sessions SET user_id=? WHERE id=?", (owner_id, workout_id)
        )
    active = service.active_scheduled_workout(owner_id)
    assert active is not None
    prescription = active["prescriptions"][0]
    return ScheduledStrengthResult(
        workout_id,
        prescription["prescription_id"],
        prescription["exercise_id"],
        (Decimal(70), Decimal(80), Decimal(90)),
        (5, 5, 5),
        prescription["unit"],
        1,
    )


@pytest.fixture
def service(tmp_path: Path) -> GymService:
    instance = GymService(Database(tmp_path / "strength.sqlite3"))
    for orientation in Orientation:
        for category in Category:
            instance.add_exercise(f"{orientation} {category}", category, orientation, "machine")
    return instance


def test_lost_response_replays_original_bytes_after_midnight_and_completion(
    service: GymService,
) -> None:
    result = scheduled_result(service, 1, PERFORMED)
    request_id = uuid4()
    # The transport loses the response after this call returned and committed.
    original = record_with_replay(service, 1, result, "lost", request_id, now=PERFORMED)
    current = service.current_workout()
    for recommendation in current["recommendations"]:
        if recommendation["category"] in current["remaining_categories"]:
            service.log_exercise(recommendation["exercise"], Decimal(90), (5, 5, 5), final_rir=1)
    service.complete_workout()
    reopened = GymService(Database(service.database.path))
    equivalent = replace(result, weights=(Decimal("70.00"), Decimal("8E1"), Decimal("90.0")))
    replay = record_with_replay(reopened, 1, equivalent, "lost", uuid4(), now=AFTER_MIDNIGHT)
    assert replay == original
    assert replay.status == 200
    body = json.loads(replay.body)
    assert body["performed_local_date"] == "2026-11-26"
    (workout,) = reopened.performed_history(1)
    assert workout["workout_id"] == body["workout_id"] == result.workout_id
    assert workout["status"] == "completed"
    sets = [row for row in workout["sets"] if row["prescription_id"] == result.prescription_id]
    assert [row["weight"] for row in sets] == [70, 80, 90]
    assert {row["performed_local_date"] for row in sets} == {"2026-11-26"}
    with reopened.database.connect() as connection:
        (audit,) = connection.execute(
            "SELECT actor_id,operation,workout_id,prescription_id,outcome,request_id "
            "FROM strength_audit_events"
        ).fetchall()
        assert tuple(audit) == (
            1,
            "record_scheduled_strength_result",
            result.workout_id,
            result.prescription_id,
            "recorded",
            str(request_id),
        )
        (receipt,) = connection.execute(
            "SELECT response_status,response_body FROM strength_replay_receipts"
        ).fetchall()
        assert tuple(receipt) == (original.status, original.body)


def test_key_collision_and_distinct_key_against_completed_prescription(service: GymService) -> None:
    result = scheduled_result(service, 1, PERFORMED)
    original = record_with_replay(service, 1, result, "key", uuid4())
    for changed in (
        replace(result, reps=(5, 6, 5)),
        replace(result, workout_id=result.workout_id + 1),
        replace(result, prescription_id=result.prescription_id + 1),
    ):
        with pytest.raises(IdempotencyKeyReused):
            record_with_replay(service, 1, changed, "key", uuid4())
    with pytest.raises(WorkoutStateConflict):
        record_with_replay(service, 1, result, "new-key", uuid4())
    assert record_with_replay(service, 1, result, "key", uuid4()) == original


def test_different_accounts_can_use_the_same_key(service: GymService) -> None:
    first = scheduled_result(service, 1, PERFORMED)
    first_reply = record_with_replay(service, 1, first, "key", uuid4())
    service.skip_workout()
    second = scheduled_result(service, 2, AFTER_MIDNIGHT)
    second_reply = record_with_replay(service, 2, second, "key", uuid4())
    assert first_reply != second_reply
    assert record_with_replay(service, 1, first, "key", uuid4()) == first_reply
    assert record_with_replay(service, 2, second, "key", uuid4()) == second_reply
    with service.database.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM strength_replay_receipts").fetchone()[0] == 2
        )


def test_concurrent_identical_requests_commit_one_result(service: GymService) -> None:
    result = scheduled_result(service, 1, PERFORMED)
    started = Barrier(2)

    def send(request_id: UUID) -> RecordedResponse:
        started.wait(timeout=5)
        return record_with_replay(service, 1, result, "concurrent", request_id, now=PERFORMED)

    with ThreadPoolExecutor(max_workers=2) as worker:
        first = worker.submit(send, uuid4())
        second = worker.submit(send, uuid4())
        assert first.result(timeout=10) == second.result(timeout=10)
    with service.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM sets").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM strength_audit_events").fetchone()[0] == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM strength_replay_receipts").fetchone()[0] == 1
        )


def test_receipt_failure_rolls_back_mutation_audit_and_sync_state(service: GymService) -> None:
    result = scheduled_result(service, 1, PERFORMED)
    with service.database.connect() as connection:
        connection.execute(
            """CREATE TRIGGER fail_receipt BEFORE INSERT ON strength_replay_receipts
            BEGIN SELECT RAISE(ABORT, 'fixture receipt failure'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="fixture receipt failure"):
        record_with_replay(service, 1, result, "fault", uuid4())
    assert service.performed_history(1) == []
    with service.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM strength_audit_events").fetchone()[0] == 0
        assert connection.execute("SELECT pending FROM sync_state").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM strength_replay_receipts").fetchone()[0] == 0
        )
        connection.execute("DROP TRIGGER fail_receipt")
    assert record_with_replay(service, 1, result, "fault", uuid4()).status == 200


def test_replay_validates_key_and_canonicalizes_zero_load(service: GymService) -> None:
    result = scheduled_result(service, 1, PERFORMED)
    with pytest.raises(GymError, match="idempotency key"):
        record_with_replay(service, 1, result, "spaces are invalid", uuid4())
    zero = replace(result, weights=(Decimal(0), Decimal(0), Decimal(0)))
    first = record_with_replay(service, 1, zero, "zero", uuid4())
    alternate = replace(zero, weights=(Decimal("-0.00"), Decimal("0E3"), Decimal("0.0")))
    assert record_with_replay(service, 1, alternate, "zero", uuid4()) == first
