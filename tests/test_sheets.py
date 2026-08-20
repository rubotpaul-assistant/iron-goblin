"""Sheets projection tests."""

from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gym_tracker.db import Database
from gym_tracker.domain import Category, Orientation
from gym_tracker.service import GymService
from gym_tracker.sheets import GoogleSheetsWriter, SheetsService


class FakeWriter:
    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.tabs: dict[str, list[list[Any]]] = {}

    def replace_rows(self, spreadsheet_id: str, tab: str, rows: list[list[Any]]) -> None:
        if self.fails:
            raise ConnectionError("offline")
        assert spreadsheet_id == "sheet-id"
        self.tabs[tab] = rows


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
    assert writer.tabs["Strength History"][1][1] == "Geoff"
    assert writer.tabs["Strength History"][1][5] == "overhead press"
    assert writer.tabs["Strength History"][1][11] == 15
    assert set(writer.tabs) == {"Strength History", "Cardio History", "Daily Energy", "Users"}


def test_daily_energy_sheet_separates_user_and_exposes_estimate_components(tmp_path: Path) -> None:
    database = Database(tmp_path / "gym.sqlite3")
    service = GymService(database)
    service.set_profile("freedom", "5ft 6in", "150 lb", "1987-04-12", "female")
    service.log_cardio(
        user="freedom",
        activity="hiking",
        duration="41m",
        rpe=7,
        activity_date="2026-08-18",
        source="manual",
    )
    writer = FakeWriter()
    SheetsService(database, "sheet-id", writer).sync()
    row = writer.tabs["Daily Energy"][1]
    assert row[1] == "Freedom"
    assert row[2] > 0
    assert row[8] == round(row[4] + row[5])


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


def test_google_writer_uses_service_account_json_and_creates_missing_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Google adapter authenticates explicitly and bootstraps its destination tab."""
    monkeypatch.setenv(
        "GOOGLE_SERVICE_ACCOUNT_CREDENTIALS_JSON",
        '{"type":"service_account","client_email":"gym@example.test","private_key":"secret"}',
    )
    credentials = object()
    credentials_factory = MagicMock(return_value=credentials)
    service = MagicMock()
    service.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "Sheet1"}}]
    }
    build = MagicMock(return_value=service)
    discovery = MagicMock(build=build)
    service_account = MagicMock()
    service_account.Credentials.from_service_account_info = credentials_factory

    def fake_import(name: str) -> MagicMock:
        return service_account if name == "google.oauth2.service_account" else discovery

    with patch("gym_tracker.sheets.import_module", side_effect=fake_import):
        GoogleSheetsWriter().replace_rows("sheet-id", "Workout History", [["header"]])

    credentials_factory.assert_called_once_with(
        {
            "type": "service_account",
            "client_email": "gym@example.test",
            "private_key": "secret",
        },
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    build.assert_called_once_with("sheets", "v4", credentials=credentials, cache_discovery=False)
    service.spreadsheets.return_value.batchUpdate.assert_called_once_with(
        spreadsheetId="sheet-id",
        body={"requests": [{"addSheet": {"properties": {"title": "Workout History"}}}]},
    )


def test_google_writer_requires_service_account_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing credentials fail without falling back to ambient credential discovery."""
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_CREDENTIALS_JSON", raising=False)
    with pytest.raises(ValueError, match="GOOGLE_SERVICE_ACCOUNT_CREDENTIALS_JSON"):
        GoogleSheetsWriter().replace_rows("sheet-id", "Workout History", [["header"]])


def test_google_writer_accepts_service_account_file_reference(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A runtime file reference is delegated to Google's credential loader."""
    credential_path = tmp_path / "service-account.json"
    credential_path.touch()
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_CREDENTIALS_JSON", str(credential_path))
    credentials = object()
    credentials_factory = MagicMock(return_value=credentials)
    service_account = MagicMock()
    service_account.Credentials.from_service_account_file = credentials_factory
    discovery = MagicMock()
    service = MagicMock()
    service.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "Workout History"}}]
    }
    discovery.build.return_value = service

    def fake_import(name: str) -> MagicMock:
        return service_account if name == "google.oauth2.service_account" else discovery

    with patch("gym_tracker.sheets.import_module", side_effect=fake_import):
        GoogleSheetsWriter().replace_rows("sheet-id", "Workout History", [["header"]])

    credentials_factory.assert_called_once_with(
        credential_path,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
