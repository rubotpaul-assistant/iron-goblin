"""Exercise explicit, scoped strength deletion and durable replay on SQLite."""

import json
import secrets
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
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
    ExerciseDeletion,
    correct_exercise_date_with_replay,
    record_prior_workout_with_replay,
    record_with_replay,
)

NOW = datetime(2026, 11, 26, 12, tzinfo=UTC)
ZONE = ZoneInfo("Europe/London")


@pytest.fixture
def scheduled(tmp_path: Path) -> tuple[GymService, ScheduledStrengthResult]:
    service = GymService(Database(tmp_path / "delete.sqlite3"))
    exercise = service.add_exercise("fixture press", Category.PUSH, Orientation.VERTICAL, "machine")
    workout = service.begin_workout(scheduled=True, now=NOW)
    active = service.active_scheduled_workout(1)
    assert active is not None
    (prescription,) = active["prescriptions"]
    result = ScheduledStrengthResult(
        workout["workout_id"],
        prescription["prescription_id"],
        exercise["id"],
        (Decimal(70), Decimal(80), Decimal(90)),
        (5, 5, 5),
        "lb",
        1,
    )
    record_with_replay(service, 1, result, "original", uuid4(), now=NOW)
    return service, result


def test_confirmed_deletion_redacts_old_write_receipt_and_replays_itself(
    scheduled: tuple[GymService, ScheduledStrengthResult],
) -> None:
    service, result = scheduled
    deletion = ExerciseDeletion(secrets.token_bytes(32))
    preview = deletion.prepare(service, 1, result.workout_id, result.prescription_id, now=NOW)
    assert preview.confirmation not in repr(preview)
    set_ids = {row["set_id"] for row in service.performed_history(1)[0]["sets"]}
    assert len(set_ids) == 3
    response = deletion.confirm(service, 1, preview, "delete", uuid4(), now=NOW)
    assert response.status == 200
    assert json.loads(response.body)["deleted"] is True
    assert service.performed_history(1) == []
    assert (
        deletion.confirm(service, 1, preview, "delete", uuid4(), now=NOW + timedelta(hours=1))
        == response
    )
    old = record_with_replay(service, 1, result, "original", uuid4(), now=NOW)
    assert old.status == 404
    assert service.performed_history(1) == []

    # The active workout can accept a new confirmed intent using a new key.
    assert record_with_replay(service, 1, result, "new", uuid4(), now=NOW).status == 200
    new_ids = {row["set_id"] for row in service.performed_history(1)[0]["sets"]}
    assert len(new_ids) == 3
    assert new_ids.isdisjoint(set_ids)
    with service.database.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM strength_audit_events WHERE outcome='deleted'"
            ).fetchone()[0]
            == 1
        )


def test_deletion_requires_a_current_owner_bound_preview(
    scheduled: tuple[GymService, ScheduledStrengthResult],
) -> None:
    service, result = scheduled
    with pytest.raises(ValueError, match="at least 32 bytes"):
        ExerciseDeletion(b"")
    deletion = ExerciseDeletion(secrets.token_bytes(32))
    with pytest.raises(WorkoutRecordNotFound):
        deletion.prepare(service, 2, result.workout_id, result.prescription_id, now=NOW)
    preview = deletion.prepare(service, 1, result.workout_id, result.prescription_id, now=NOW)
    for account, proof, instant in (
        (2, preview, NOW),
        (1, replace(preview, confirmation=""), NOW),
        (1, preview, NOW + timedelta(minutes=10)),
    ):
        with pytest.raises(GymError, match="invalid deletion confirmation"):
            deletion.confirm(service, account, proof, "denied", uuid4(), now=instant)
    correction = StrengthDateCorrection(
        result.workout_id,
        result.prescription_id,
        preview.target.result_revision,
        date(2026, 11, 20),
    )
    assert (
        correct_exercise_date_with_replay(
            service, 1, correction, "correct", uuid4(), time_zone=ZONE, now=NOW
        ).status
        == 200
    )
    with pytest.raises(WorkoutStateConflict, match="result has changed"):
        deletion.confirm(service, 1, preview, "stale", uuid4(), now=NOW)
    updated = deletion.prepare(service, 1, result.workout_id, result.prescription_id, now=NOW)
    deletion.confirm(service, 1, updated, "delete", uuid4(), now=NOW)
    assert (
        correct_exercise_date_with_replay(
            service, 1, correction, "correct", uuid4(), time_zone=ZONE, now=NOW
        ).status
        == 404
    )


def test_delete_prior_result_keeps_unrelated_sets_and_prevents_repeat_import(
    tmp_path: Path,
) -> None:
    service = GymService(Database(tmp_path / "prior.sqlite3"))
    results = []
    for category in (Category.PUSH, Category.PULL):
        service.add_equipment(
            category, "machine", Decimal(10), Decimal(100), Decimal(10), owner_id=1
        )
        exercise = service.add_exercise(
            f"fixture {category}",
            category,
            Orientation.VERTICAL,
            "machine",
            equipment_name=category,
            owner_id=1,
        )
        results.append(PriorStrengthResult(exercise["id"], (Decimal(50),) * 3, (5, 5, 5), "lb", 1))
    performed = date(2026, 11, 25)
    response = record_prior_workout_with_replay(
        service, 1, performed, tuple(results), "prior", uuid4(), time_zone=ZONE, now=NOW
    )
    created = json.loads(response.body)
    deletion = ExerciseDeletion(secrets.token_bytes(32))
    target = created["exercises"][0]["prescription_id"]
    preview = deletion.prepare(service, 1, created["workout_id"], target, now=NOW)
    deletion.confirm(service, 1, preview, "delete", uuid4(), now=NOW)
    retry = record_prior_workout_with_replay(
        service, 1, performed, tuple(results), "prior", uuid4(), time_zone=ZONE, now=NOW
    )
    assert retry.status == 404
    (history,) = service.performed_history(1)
    assert {row["prescription_id"] for row in history["sets"]} == {
        created["exercises"][1]["prescription_id"]
    }
    assert len(history["sets"]) == 3
    next_workout = service.begin_workout(scheduled=True, now=NOW, owner_id=1)
    recommendations = {row["category"]: row for row in next_workout["recommendations"]}
    assert recommendations["push"]["weight_status"] == "baseline_required"
    assert recommendations["pull"]["weight"] == 60


def test_existing_set_rows_keep_ids_when_installing_non_reuse_migration(
    scheduled: tuple[GymService, ScheduledStrengthResult],
) -> None:
    service, _ = scheduled
    (original,) = service.performed_history(1)
    with service.database.connect() as connection:
        statement = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sets'"
        ).fetchone()[0]
        connection.execute("ALTER TABLE sets RENAME TO sets_recent")
        connection.execute(statement.replace(" AUTOINCREMENT", ""))
        connection.execute("INSERT INTO sets SELECT * FROM sets_recent")
        connection.execute("DROP TABLE sets_recent")
    migrated = GymService(Database(service.database.path))
    assert migrated.performed_history(1) == [original]
    with migrated.database.connect() as connection:
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='sets'"
        ).fetchone()[0]
        assert "AUTOINCREMENT" in sql


def test_delete_result_refreshes_a_workout_already_prescribed(tmp_path: Path) -> None:
    service = GymService(Database(tmp_path / "recommendation.sqlite3"))
    service.add_equipment("press", "machine", Decimal(10), Decimal(100), Decimal(10), owner_id=1)
    exercise = service.add_exercise(
        "press",
        Category.PUSH,
        Orientation.VERTICAL,
        "machine",
        equipment_name="press",
        owner_id=1,
    )
    records = []
    for day, load in ((date(2026, 11, 24), 50), (date(2026, 11, 25), 70)):
        result = PriorStrengthResult(exercise["id"], (Decimal(load),) * 3, (5, 5, 5), "lb", 1)
        created = record_prior_workout_with_replay(
            service,
            1,
            day,
            (result,),
            day.isoformat(),
            uuid4(),
            time_zone=ZONE,
            now=NOW,
        )
        records.append(json.loads(created.body))
    assert (
        service.begin_workout(scheduled=True, now=NOW, owner_id=1)["recommendations"][0]["weight"]
        == 80
    )

    deletion = ExerciseDeletion(secrets.token_bytes(32))
    for record, expected in ((records[-1], 60), (records[0], None)):
        preview = deletion.prepare(
            service, 1, record["workout_id"], record["exercises"][0]["prescription_id"], now=NOW
        )
        deletion.confirm(service, 1, preview, str(record["workout_id"]), uuid4(), now=NOW)
        recommendation = service.current_workout(owner_id=1)["recommendations"][0]
        assert recommendation["weight"] == expected
    assert recommendation["weight_status"] == "baseline_required"
