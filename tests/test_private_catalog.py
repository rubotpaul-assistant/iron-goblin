"""Verify owner-scoped catalog, workout selection, and migration on SQLite."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from gym_tracker.db import Database
from gym_tracker.domain import Category, Orientation
from gym_tracker.service import GymError, GymService

NOW = datetime(2026, 11, 26, 12, tzinfo=UTC)


def test_two_owners_have_independent_equipment_exercises_and_aliases(tmp_path: Path) -> None:
    service = GymService(Database(tmp_path / "catalog.sqlite3"))
    service.add_exercise("shared push", Category.PUSH, Orientation.VERTICAL, "bodyweight")
    for owner, maximum, private in ((1, 80, "first only"), (2, 150, "second only")):
        service.add_equipment(
            "press",
            "machine",
            Decimal(10),
            Decimal(maximum),
            Decimal(10),
            supported_weights=(Decimal(10), Decimal(maximum)),
            owner_id=owner,
        )
        service.add_exercise(
            private,
            Category.PUSH,
            Orientation.VERTICAL,
            "machine",
            equipment_name="PRESS",
            owner_id=owner,
        )
        service.add_alias("my press", private, owner_id=owner)

    assert service.list_equipment()["equipment"] == []
    with pytest.raises(GymError, match="unknown equipment"):
        service.remove_equipment("press")
    with pytest.raises(GymError, match="unknown exercise"):
        service.assign_equipment("shared push", "press", owner_id=1)
    with pytest.raises(GymError, match="unknown exercise"):
        service.add_alias("foreign", "first only", owner_id=2)

    for owner, maximum, own, hidden in (
        (1, 80, "First Only", "Second Only"),
        (2, 150, "Second Only", "First Only"),
    ):
        (equipment,) = service.list_equipment(owner_id=owner)["equipment"]
        assert equipment["supported_weights"] == [10.0, float(maximum)]
        expected = {own, "Shared Push"}
        assert {
            row["canonical_name"] for row in service.search_exercises("", owner_id=owner)["matches"]
        } == expected
        assert service.search_exercises(hidden, owner_id=owner)["matches"] == []
        assert (
            service.search_exercises("my press", owner_id=owner)["matches"][0]["canonical_name"]
            == own
        )
        assert (
            service.begin_workout(scheduled=True, now=NOW, owner_id=owner)["recommendations"][0][
                "exercise"
            ]
            == own
        )
        with pytest.raises(GymError, match="unknown exercise"):
            service.substitute_exercise(hidden, owner_id=owner)

    with pytest.raises(GymError, match="weight is not supported"):
        service.log_exercise("my press", Decimal(150), (5, 5, 5), final_rir=1, owner_id=1)
    assert (
        service.log_exercise("my press", Decimal(150), (5, 5, 5), final_rir=1, owner_id=2)[
            "exercise"
        ]
        == "Second Only"
    )
    assert service.performed_history(1) == []
    assert service.performed_history(2)[0]["sets"][0]["weight"] == 150
    service.remove_equipment("press", owner_id=1)
    assert not service.list_equipment(owner_id=1)["equipment"][0]["available"]
    assert service.list_equipment(owner_id=2)["equipment"][0]["available"]


def test_private_names_can_match_and_shared_prescription_still_logs(tmp_path: Path) -> None:
    service = GymService(Database(tmp_path / "catalog.sqlite3"))
    shared = service.add_exercise("press", Category.PUSH, Orientation.VERTICAL, "machine")
    service.begin_workout(scheduled=True, now=NOW, owner_id=1)
    first = service.add_exercise(
        "PRESS", Category.PUSH, Orientation.VERTICAL, "machine", owner_id=1
    )
    second = service.add_exercise(
        "press", Category.PUSH, Orientation.VERTICAL, "machine", owner_id=2
    )
    assert len({shared["id"], first["id"], second["id"]}) == 3
    service.begin_workout(scheduled=True, now=NOW, owner_id=2)
    active = service.active_scheduled_workout(2)
    assert active is not None
    assert active["prescriptions"][0]["exercise_id"] == second["id"]
    service.log_exercise("press", Decimal(20), (5, 5, 5), final_rir=1, owner_id=1)
    assert service.performed_history(1)[0]["sets"][0]["exercise_id"] == shared["id"]


def test_legacy_catalog_migration_preserves_ids_loads_and_aliases(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """CREATE TABLE equipment (
             id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
             equipment_type TEXT NOT NULL, min_weight REAL NOT NULL, max_weight REAL NOT NULL,
             increment REAL NOT NULL, unit TEXT NOT NULL, available INTEGER NOT NULL,
             notes TEXT NOT NULL);
            INSERT INTO equipment VALUES (5,'legacy rack','machine',10,20,10,'lb',1,'');
            CREATE TABLE exercises (
             id INTEGER PRIMARY KEY, canonical_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
             category TEXT NOT NULL, orientation TEXT NOT NULL, equipment_type TEXT NOT NULL,
             default_unit TEXT NOT NULL, active INTEGER NOT NULL, notes TEXT NOT NULL);
            INSERT INTO exercises VALUES (7,'Legacy Press','push','vertical','machine','lb',1,'');
            CREATE TABLE exercise_aliases (
             id INTEGER PRIMARY KEY, alias TEXT NOT NULL UNIQUE COLLATE NOCASE,
             exercise_id INTEGER NOT NULL REFERENCES exercises(id) ON DELETE CASCADE);
            INSERT INTO exercise_aliases VALUES (9,'legacy alias',7);
            CREATE TABLE equipment_loads (
             equipment_id INTEGER NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
             weight REAL NOT NULL, PRIMARY KEY(equipment_id,weight));
            INSERT INTO equipment_loads VALUES (5,10),(5,20);
            CREATE TABLE exercise_equipment (
             exercise_id INTEGER NOT NULL REFERENCES exercises(id) ON DELETE CASCADE,
             equipment_id INTEGER NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
             required INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(exercise_id,equipment_id));
            INSERT INTO exercise_equipment VALUES (7,5,1);"""
        )
    service = GymService(Database(path))
    service.database.initialize()
    assert service.list_equipment()["equipment"][0]["supported_weights"] == [10, 20]
    assert (
        service.search_exercises("legacy alias")["matches"][0]["canonical_name"] == "Legacy Press"
    )
    service.begin_workout(scheduled=True, now=NOW)
    result = service.log_exercise("legacy alias", Decimal(10), (5, 5, 5), final_rir=1)
    assert result["next_weight"] == 20
    with service.database.connect() as connection:
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        assert connection.execute("SELECT id FROM exercise_aliases").fetchone()[0] == 9
        assert connection.execute("SELECT exercise_id FROM workout_exercises").fetchone()[0] == 7
