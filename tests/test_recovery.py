"""Rehearse the canonical SQLite rollback and restore on disposable data."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from gym_tracker.db import Database
from gym_tracker.domain import Category, Orientation
from gym_tracker.service import GymService, ScheduledStrengthResult
from gym_tracker.strength_replay import record_with_replay

NOW = datetime(2026, 11, 26, 12, tzinfo=UTC)


def copy_snapshot(source: Database, destination: Database) -> None:
    with source.connect() as reader, destination.connect() as writer:
        reader.backup(writer)


def test_backup_restore_recovers_canonical_workout_and_original_receipt(tmp_path: Path) -> None:
    live = GymService(Database(tmp_path / "original.sqlite3"))
    live.add_exercise("fixture press", Category.PUSH, Orientation.VERTICAL, "machine")
    live.begin_workout(scheduled=True, now=NOW)
    active = live.active_scheduled_workout(1)
    assert active is not None
    (prescription,) = active["prescriptions"]
    result = ScheduledStrengthResult(
        active["workout_id"],
        prescription["prescription_id"],
        prescription["exercise_id"],
        (Decimal(10), Decimal(15), Decimal(20)),
        (5, 5, 5),
        prescription["unit"],
        1,
    )
    original = record_with_replay(live, 1, result, "fixture-key", uuid4(), now=NOW)
    backup = Database(tmp_path / "snapshot.sqlite3")
    copy_snapshot(live.database, backup)

    # Simulate writes after the backup and restore elsewhere first.
    live.skip_workout()
    assert live.performed_history(1)[0]["status"] == "partial"
    restored = Database(tmp_path / "restored.sqlite3")
    copy_snapshot(backup, restored)
    recovered = GymService(restored)

    assert record_with_replay(recovered, 1, result, "fixture-key", uuid4()) == original
    (workout,) = recovered.performed_history(1)
    assert workout["workout_id"] == json.loads(original.body)["workout_id"]
    assert workout["status"] == "active"
    assert [row["weight"] for row in workout["sets"]] == [10, 15, 20]
    assert recovered.performed_history(2) == []
    with restored.connect() as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert connection.execute("SELECT COUNT(*) FROM strength_audit_events").fetchone()[0] == 1
