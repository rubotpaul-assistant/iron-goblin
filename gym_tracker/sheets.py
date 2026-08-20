"""Optional Google Sheets projection of canonical SQLite data."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from importlib import import_module
from json import JSONDecodeError, loads
from os import getenv
from pathlib import Path
from typing import Any, Protocol, cast

from gym_tracker.db import Database
from gym_tracker.domain import age_on_date, mifflin_st_jeor_bmr


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
                tabs = {
                    "Strength History": self._history_rows(connection),
                    "Cardio History": self._cardio_rows(connection),
                    "Daily Energy": self._daily_energy_rows(connection),
                    "Users": self._user_rows(connection),
                }
            for tab, rows in tabs.items():
                self.writer.replace_rows(self.spreadsheet_id, tab, rows)
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
        return {
            "ok": True,
            "rows": len(tabs["Strength History"]) - 1,
            "tabs": list(tabs),
            "pending": False,
        }

    @staticmethod
    def _history_rows(connection: sqlite3.Connection) -> list[list[Any]]:
        header = [
            "Date",
            "User",
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
            """SELECT ws.local_date,u.display_name,ws.id,ws.orientation,e.category,e.canonical_name,
            MAX(s.weight),MAX(s.unit),MAX(CASE WHEN s.set_number=1 THEN s.completed_reps END),
            MAX(CASE WHEN s.set_number=2 THEN s.completed_reps END),
            MAX(CASE WHEN s.set_number=3 THEN s.completed_reps END),
            COALESCE(SUM(s.completed_reps),0),
            MAX(CASE WHEN s.set_number=3 THEN s.rir END),MAX(s.technical_failure),ws.notes
            FROM workout_sessions ws JOIN users u ON u.id=ws.user_id
            JOIN workout_exercises wx ON wx.workout_session_id=ws.id
            JOIN exercises e ON e.id=wx.exercise_id LEFT JOIN sets s ON s.workout_exercise_id=wx.id
            GROUP BY ws.id,wx.id ORDER BY ws.started_at,wx.sequence"""
        ).fetchall()
        return [header, *[list(record) for record in records]]

    @staticmethod
    def _cardio_rows(connection: sqlite3.Connection) -> list[list[Any]]:
        header = [
            "Date",
            "User",
            "Activity",
            "Duration Seconds",
            "Distance Meters",
            "Elevation Gain Meters",
            "RPE",
            "MET",
            "Gross Calories",
            "Net Calories",
            "Counted Calories",
            "Method",
            "Source",
            "Notes",
        ]
        records = connection.execute(
            """SELECT c.local_date,u.display_name,c.canonical_activity,c.duration_seconds,
            c.distance_meters,c.elevation_gain_meters,c.intensity_rpe,c.met_value,
            ROUND(c.gross_calories_kcal),ROUND(c.net_calories_kcal),
            ROUND(COALESCE(c.imported_total_calories_kcal,c.net_calories_kcal)),
            c.calorie_method,c.source,c.notes FROM cardio_sessions c
            JOIN users u ON u.id=c.user_id ORDER BY c.started_at,c.id"""
        ).fetchall()
        return [header, *[list(record) for record in records]]

    @staticmethod
    def _daily_energy_rows(connection: sqlite3.Connection) -> list[list[Any]]:
        header = [
            "Date",
            "User",
            "BMR",
            "Baseline Factor",
            "Baseline Calories",
            "Cardio Calories",
            "Strength Calories",
            "Other Imported Activity",
            "Estimated Total Calories",
            "Completeness",
        ]
        records = connection.execute(
            """SELECT c.local_date,u.display_name,p.height_cm,p.weight_kg,p.birth_date,
            p.bmr_sex_parameter,p.baseline_activity_factor,
            ROUND(SUM(COALESCE(c.imported_total_calories_kcal,c.net_calories_kcal)))
            FROM cardio_sessions c JOIN users u ON u.id=c.user_id
            LEFT JOIN metabolic_profiles p ON p.user_id=c.user_id
            GROUP BY c.local_date,c.user_id ORDER BY c.local_date,u.display_name"""
        ).fetchall()
        return [header, *[SheetsService._daily_energy_record(record) for record in records]]

    @staticmethod
    def _daily_energy_record(record: sqlite3.Row) -> list[Any]:
        """Build a truthful daily row, leaving baseline blank without a complete profile."""
        profile_values = record[2:6]
        cardio = record[7]
        if any(value is None for value in profile_values):
            return [
                record[0],
                record[1],
                None,
                record[6],
                None,
                cardio,
                None,
                0,
                None,
                "logged cardio only; metabolic profile incomplete",
            ]
        requested_date = date.fromisoformat(record[0])
        age = age_on_date(date.fromisoformat(record[4]), requested_date)
        bmr = mifflin_st_jeor_bmr(
            Decimal(str(record[3])), Decimal(str(record[2])), age, str(record[5])
        )
        factor = Decimal(str(record[6]))
        baseline = bmr * factor
        total = baseline + Decimal(str(cardio))
        return [
            record[0],
            record[1],
            round(bmr),
            float(factor),
            round(baseline),
            cardio,
            None,
            0,
            round(total),
            "modeled baseline plus logged exercise",
        ]

    @staticmethod
    def _user_rows(connection: sqlite3.Connection) -> list[list[Any]]:
        records = connection.execute(
            "SELECT slug,display_name,active FROM users ORDER BY slug"
        ).fetchall()
        return [["Slug", "Display Name", "Active"], *[list(record) for record in records]]


class GoogleSheetsWriter:
    """Google API adapter authenticated from the runtime service-account payload."""

    @staticmethod
    def _credentials() -> Any:
        """Build scoped credentials without logging or persisting secret material."""
        service_account_json = getenv("GOOGLE_SERVICE_ACCOUNT_CREDENTIALS_JSON")
        if not service_account_json:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_CREDENTIALS_JSON is not configured")
        service_account = import_module("google.oauth2.service_account")
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        credential_path = Path(service_account_json)
        if credential_path.is_file():
            credentials_factory = cast("Any", service_account.Credentials.from_service_account_file)
            return credentials_factory(credential_path, scopes=scopes)
        try:
            service_account_info = loads(service_account_json)
        except JSONDecodeError as error:
            raise ValueError("GOOGLE_SERVICE_ACCOUNT_CREDENTIALS_JSON is not valid JSON") from error
        credentials_factory = cast("Any", service_account.Credentials.from_service_account_info)
        return credentials_factory(service_account_info, scopes=scopes)

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
