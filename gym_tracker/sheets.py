"""Optional Google Sheets projection of canonical SQLite data."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from importlib import import_module
from json import JSONDecodeError, loads
from os import getenv
from typing import Any, Protocol, cast

from gym_tracker.db import Database


class SheetsWriter(Protocol):
    """Small boundary around the Google client for deterministic testing."""

    def replace_rows(self, spreadsheet_id: str, tab: str, rows: list[list[Any]]) -> None:
        """Replace a tab with normalized rows."""


class SheetsService:
    """Project workout history to Sheets without controlling persistence."""

    def __init__(
        self, database: Database, spreadsheet_id: str | None, writer: SheetsWriter
    ) -> None:
        self.database = database
        self.spreadsheet_id = spreadsheet_id
        self.writer = writer

    def status(self) -> dict[str, Any]:
        """Return sync metadata without secret-bearing configuration."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT pending,last_success_at,last_error FROM sync_state WHERE singleton=1"
            ).fetchone()
        return {"ok": True, "configured": bool(self.spreadsheet_id), **dict(row)}

    def sync(self) -> dict[str, Any]:
        """Replace derived rows and preserve pending state when Google fails."""
        if not self.spreadsheet_id:
            raise ValueError("GYM_SPREADSHEET_ID is not configured")
        try:
            with self.database.connect() as connection:
                rows = self._history_rows(connection)
            self.writer.replace_rows(self.spreadsheet_id, "Workout History", rows)
        except Exception as error:
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE sync_state SET pending=1,last_error=? WHERE singleton=1",
                    (type(error).__name__,),
                )
            raise
        completed_at = datetime.now(tz=UTC).isoformat()
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE sync_state SET pending=0,last_success_at=?,last_error=NULL
                WHERE singleton=1""",
                (completed_at,),
            )
        return {"ok": True, "rows": len(rows) - 1, "pending": False}

    @staticmethod
    def _history_rows(connection: sqlite3.Connection) -> list[list[Any]]:
        header = [
            "Date",
            "Workout ID",
            "Orientation",
            "Category",
            "Exercise",
            "Weight",
            "Unit",
            "Set 1",
            "Set 2",
            "Set 3",
            "Total Reps",
            "RIR Final Set",
            "Technical Failure",
            "Notes",
        ]
        records = connection.execute(
            """SELECT ws.local_date,ws.id,ws.orientation,e.category,e.canonical_name,
            MAX(s.weight),MAX(s.unit),MAX(CASE WHEN s.set_number=1 THEN s.completed_reps END),
            MAX(CASE WHEN s.set_number=2 THEN s.completed_reps END),
            MAX(CASE WHEN s.set_number=3 THEN s.completed_reps END),
            COALESCE(SUM(s.completed_reps),0),
            MAX(CASE WHEN s.set_number=3 THEN s.rir END),MAX(s.technical_failure),ws.notes
            FROM workout_sessions ws JOIN workout_exercises wx ON wx.workout_session_id=ws.id
            JOIN exercises e ON e.id=wx.exercise_id LEFT JOIN sets s ON s.workout_exercise_id=wx.id
            GROUP BY ws.id,wx.id ORDER BY ws.started_at,wx.sequence"""
        ).fetchall()
        return [header, *[list(record) for record in records]]


class GoogleSheetsWriter:
    """Google API adapter authenticated from the runtime service-account payload."""

    @staticmethod
    def _credentials() -> Any:
        """Build scoped credentials without logging or persisting secret material."""
        service_account_json = getenv("GOOGLE_SERVICE_ACCOUNT_CREDENTIALS_JSON")
        if not service_account_json:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_CREDENTIALS_JSON is not configured")
        try:
            service_account_info = loads(service_account_json)
        except JSONDecodeError as error:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_CREDENTIALS_JSON is not valid JSON") from error
        service_account = import_module("google.oauth2.service_account")
        credentials_factory = cast("Any", service_account.Credentials.from_service_account_info)
        return credentials_factory(
            service_account_info,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )

    @staticmethod
    def _ensure_tab(service: Any, spreadsheet_id: str, tab: str) -> None:
        """Create the projection tab when syncing to a new spreadsheet."""
        metadata = (
            service.spreadsheets()
            .get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title")
            .execute()
        )
        titles = {sheet["properties"]["title"] for sheet in metadata.get("sheets", [])}
        if tab not in titles:
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
            ).execute()

    def replace_rows(self, spreadsheet_id: str, tab: str, rows: list[list[Any]]) -> None:
        """Clear and replace a tab through the official API client."""
        build = cast("Any", import_module("googleapiclient.discovery").build)
        service = build("sheets", "v4", credentials=self._credentials(), cache_discovery=False)
        self._ensure_tab(service, spreadsheet_id, tab)
        values = service.spreadsheets().values()
        values.clear(spreadsheetId=spreadsheet_id, range=f"'{tab}'").execute()
        values.update(
            spreadsheetId=spreadsheet_id,
            range=f"'{tab}'!A1",
            valueInputOption="RAW",
            body={"values": rows},
        ).execute()
