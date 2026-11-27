"""Rehearse confirmed deletion and restore without exposing other accounts or products."""

import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from gym_tracker.db import Database
from gym_tracker.domain import Category, Orientation
from gym_tracker.pilot_account import Account, PilotAccessDenied, Product
from gym_tracker.pilot_invites import ProductInvites, VerifiedRecipient
from gym_tracker.service import GymService, ScheduledStrengthResult, WorkoutStateConflict
from gym_tracker.strength_replay import record_with_replay
from gym_tracker.support_closure import IronSupportClosure
from gym_tracker.support_export import export_iron_account

NOW = datetime(2026, 11, 26, 12, tzinfo=UTC)


def test_iron_closure_and_restore_preserve_another_account_and_macro(tmp_path: Path) -> None:
    current = GymService(Database(tmp_path / "iron.sqlite3"))
    for owner_id in (1, 2):
        current.add_equipment(
            "press", "machine", Decimal(10), Decimal(100), Decimal(10), owner_id=owner_id
        )
        exercise = current.add_exercise(
            "press",
            Category.PUSH,
            Orientation.VERTICAL,
            "machine",
            equipment_name="press",
            owner_id=owner_id,
        )
        current.begin_workout(scheduled=True, now=NOW, owner_id=owner_id)
        active = current.active_scheduled_workout(owner_id)
        assert active is not None
        result = ScheduledStrengthResult(
            active["workout_id"],
            active["prescriptions"][0]["prescription_id"],
            exercise["id"],
            (Decimal(50),) * 3,
            (5, 5, 5),
            "lb",
            1,
        )
        record_with_replay(current, owner_id, result, "fixture", uuid4(), now=NOW)

    restored = Database(tmp_path / "restored.sqlite3")
    with current.database.connect() as source, restored.connect() as backup:
        source.backup(backup)

    invites = ProductInvites(tmp_path / "grants.sqlite3", secrets.token_bytes(32))
    account = Account("fixture-verified-owner", "UTC")
    address = "fixture@example.test"
    issued = {
        product: invites.issue(product, address, expires_at=NOW + timedelta(hours=1))
        for product in Product
    }
    for invite in issued.values():
        invites.redeem(invite.token, VerifiedRecipient(account, address), now=NOW)

    closure = IronSupportClosure(tmp_path / "closures.sqlite3", secrets.token_bytes(32))
    preview = closure.preview(current.database, 1, account.account_id, now=NOW)
    assert dict(preview.counts)["sets"] == 3
    assert dict(preview.counts)["replay"] == 1
    assert len(current.performed_history(1)[0]["sets"]) == 3
    with pytest.raises(PilotAccessDenied, match="IRON_GRANT_MUST_BE_REVOKED"):
        closure.confirm(current.database, invites, account.account_id, preview, now=NOW)
    invites.revoke(issued[Product.IRON].id)
    with pytest.raises(PilotAccessDenied, match="INVALID_CONFIRMATION"):
        closure.confirm(current.database, invites, "other-account", preview, now=NOW)
    with pytest.raises(PilotAccessDenied, match="INVALID_CONFIRMATION"):
        closure.confirm(
            current.database, invites, account.account_id, replace(preview, owner_id=2), now=NOW
        )
    with pytest.raises(PilotAccessDenied, match="INVALID_CONFIRMATION"):
        closure.confirm(
            current.database, invites, account.account_id, preview, now=NOW + timedelta(minutes=10)
        )

    foreign_before = export_iron_account(current.database, 2)
    closure.confirm(current.database, invites, account.account_id, preview, now=NOW)
    assert invites.active_grants() == {(account.account_id, Product.MACRO)}
    assert export_iron_account(current.database, 2) == foreign_before
    assert not any(export_iron_account(current.database, 1)["records"].values())
    assert len(GymService(restored).performed_history(1)[0]["sets"]) == 3

    closure.apply_prior_closures(restored)
    closed = export_iron_account(restored, 1)
    closure.apply_prior_closures(restored)
    assert export_iron_account(restored, 1) == closed
    assert not any(closed["records"].values())
    assert export_iron_account(restored, 2) == foreign_before
    with restored.connect() as connection:
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert connection.execute("SELECT active FROM users WHERE id=1").fetchone()[0] == 0


def test_closure_dry_run_is_read_only_and_detects_changed_counts(tmp_path: Path) -> None:
    database = Database(tmp_path / "iron.sqlite3")
    service = GymService(database)
    invites = ProductInvites(tmp_path / "grants.sqlite3", secrets.token_bytes(32))
    closure = IronSupportClosure(tmp_path / "closures.sqlite3", secrets.token_bytes(32))
    preview = closure.preview(database, 1, "fixture-verified-owner", now=NOW)
    assert dict(preview.counts)["workouts"] == 0
    service.begin_workout(scheduled=True, now=NOW)
    with pytest.raises(WorkoutStateConflict, match="changed since the dry run"):
        closure.confirm(database, invites, "fixture-verified-owner", preview, now=NOW)
    closure.apply_prior_closures(database)
    assert service.current_workout()["status"] == "active"
    with pytest.raises(ValueError, match="require a separate store"):
        closure.preview(
            Database(tmp_path / "closures.sqlite3"), 1, "fixture-verified-owner", now=NOW
        )


def test_different_database_with_same_owner_id_cannot_use_confirmation_or_marker(
    tmp_path: Path,
) -> None:
    one = GymService(Database(tmp_path / "first.sqlite3"))
    other = GymService(Database(tmp_path / "another.sqlite3"))
    invites = ProductInvites(tmp_path / "grants.sqlite3", secrets.token_bytes(32))
    closure = IronSupportClosure(tmp_path / "closures.sqlite3", secrets.token_bytes(32))
    preview = closure.preview(one.database, 1, "fixture-verified-owner", now=NOW)
    with pytest.raises(PilotAccessDenied, match="INVALID_CONFIRMATION"):
        closure.confirm(other.database, invites, "fixture-verified-owner", preview, now=NOW)
    closure.confirm(one.database, invites, "fixture-verified-owner", preview, now=NOW)
    foreign = export_iron_account(other.database, 1)
    closure.apply_prior_closures(other.database)
    assert export_iron_account(other.database, 1) == foreign
    assert other.show_user("geoff")["user"]["active"] == 1
