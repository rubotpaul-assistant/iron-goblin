"""Sheets projection tests."""

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from gym_tracker.db import Database
from gym_tracker.domain import Category, Orientation
from gym_tracker.service import GymService
from gym_tracker.sheets import SheetsService


class FakeWriter:
    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.rows: list[list[Any]] = []

    def replace_rows(self, spreadsheet_id: str, tab: str, rows: list[list[Any]]) -> None:
        if self.fails:
            raise ConnectionError("offline")
        assert spreadsheet_id == "sheet-id"
        assert tab == "Workout History"
        self.rows = rows


def _database_with_result(tmp_path: Path) -> Database:
    database = Database(tmp_path / "gym.sqlite3")
    service = GymService(database)
    service.add_equipment("press", "machine", Decimal(10), Decimal(100), Decimal(10))
    service.add_exercise(
        "overhead press", Category.PUSH, Orientation.VERTICAL, "machine", equipment_name="press"
    )
    service.begin_workout()
    service.log_exercise("overhead press", Decimal(50), (5, 5, 5), final_rir=0)
    return database


def test_successful_sync_exports_real_rows_and_clears_pending(tmp_path: Path) -> None:
    database = _database_with_result(tmp_path)
    writer = FakeWriter()
    sync = SheetsService(database, "sheet-id", writer)
    assert sync.sync()["rows"] == 1
    assert sync.status()["pending"] == 0
    assert writer.rows[1][4] == "overhead press"
    assert writer.rows[1][10] == 15


def test_sync_failure_never_rolls_back_local_workout_data(tmp_path: Path) -> None:
    database = _database_with_result(tmp_path)
    sync = SheetsService(database, "sheet-id", FakeWriter(fails=True))
    with pytest.raises(ConnectionError, match="offline"):
        sync.sync()
    assert sync.status()["pending"] == 1
    assert GymService(database).recent_history()["workouts"]


def test_sync_requires_spreadsheet_reference(tmp_path: Path) -> None:
    database = Database(tmp_path / "gym.sqlite3")
    database.initialize()
    with pytest.raises(ValueError, match="GYM_SPREADSHEET_ID"):
        SheetsService(database, None, FakeWriter()).sync()
