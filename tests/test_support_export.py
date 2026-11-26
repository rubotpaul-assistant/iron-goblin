"""Rehearse exporting only a verified Iron owner's records on disposable data."""

import json
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from gym_tracker.db import Database
from gym_tracker.domain import Category, Orientation
from gym_tracker.pilot_account import Account, Product
from gym_tracker.pilot_invites import ProductInvites, VerifiedRecipient
from gym_tracker.service import GymService, ScheduledStrengthResult, WorkoutRecordNotFound
from gym_tracker.strength_replay import record_with_replay
from gym_tracker.support_export import export_iron_account

NOW = datetime(2026, 11, 26, 12, tzinfo=UTC)


def test_export_is_owner_scoped_and_does_not_include_replay_secret(tmp_path: Path) -> None:
    service = GymService(Database(tmp_path / "iron.sqlite3"))
    service.add_exercise("shared default", Category.PULL, Orientation.VERTICAL, "bodyweight")
    for owner, label in ((1, "own"), (2, "foreign")):
        service.add_equipment(
            f"{label} machine",
            "machine",
            Decimal(10),
            Decimal(100),
            Decimal(10),
            supported_weights=(Decimal(10), Decimal(20)),
            owner_id=owner,
        )
        exercise = service.add_exercise(
            f"{label} exercise",
            Category.PUSH,
            Orientation.VERTICAL,
            "machine",
            equipment_name=f"{label} machine",
            owner_id=owner,
        )
        service.add_alias(f"{label} alias", f"{label} exercise", owner_id=owner)
        service.begin_workout(scheduled=True, now=NOW, owner_id=owner)
        active = service.active_scheduled_workout(owner)
        assert active is not None
        prescription = active["prescriptions"][0]
        result = ScheduledStrengthResult(
            active["workout_id"],
            prescription["prescription_id"],
            exercise["id"],
            (Decimal(10), Decimal(20), Decimal(20)),
            (5, 5, 5),
            "lb",
            1,
        )
        record_with_replay(service, owner, result, f"{label}-private-retry-key", uuid4(), now=NOW)
    service.map_identity("fixture", "own-sender-identifier", "geoff")
    service.map_identity("fixture", "foreign-sender-identifier", "freedom")

    grants = ProductInvites(tmp_path / "identity.sqlite3", secrets.token_bytes(32))
    login = VerifiedRecipient(Account("shared-fixture", "UTC"), "fixture@example.test")
    invite = grants.issue(Product.MACRO, "fixture@example.test", expires_at=NOW + timedelta(days=1))
    grants.redeem(invite.token, login, now=NOW)

    export = export_iron_account(service.database, 1)
    assert export["product"] == "iron"
    assert export["account"]["id"] == 1
    rows = export["records"]
    assert len(rows["workouts"]) == 1
    assert {row["canonical_name"] for row in rows["workout_exercises"]} == {
        "Own Exercise",
        "Shared Default",
    }
    assert [row["weight"] for row in rows["sets"]] == [10, 20, 20]
    assert rows["equipment"][0]["name"] == "own machine"
    assert [row["weight"] for row in rows["equipment_loads"]] == [10, 20]
    assert rows["exercise_aliases"][0]["alias"] == "own alias"
    assert rows["exercise_equipment"][0]["equipment_name"] == "own machine"
    assert rows["audit"][0]["operation"] == "record_scheduled_strength_result"
    assert rows["identities"][0]["external_sender_id"] == "own-sender-identifier"
    serialized = json.dumps(export)
    assert all(
        value not in serialized.lower()
        for value in ("foreign", "private-retry-key", "fixture@example.test", "macro")
    )
    assert grants.active_grants() == {("shared-fixture", Product.MACRO)}
    assert len(service.performed_history(2)[0]["sets"]) == 3


def test_export_missing_owner_has_generic_error(tmp_path: Path) -> None:
    service = GymService(Database(tmp_path / "iron.sqlite3"))
    with pytest.raises(WorkoutRecordNotFound, match=r"^Iron account not found$"):
        export_iron_account(service.database, 404)
