"""Deterministic workout application service."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from decimal import Decimal
from typing import Any, cast
from zoneinfo import ZoneInfo

from gym_tracker.db import Database
from gym_tracker.domain import (
    Category,
    EquipmentRange,
    Orientation,
    Performance,
    progression_weight,
)

PACIFIC = ZoneInfo("America/Los_Angeles")


class GymError(Exception):
    """Expected domain or input error."""


class GymService:
    """Own workout selection, mutation, history, and progression."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.database.initialize()

    def add_equipment(
        self,
        name: str,
        equipment_type: str,
        minimum: Decimal,
        maximum: Decimal,
        increment: Decimal,
        unit: str = "lb",
    ) -> dict[str, Any]:
        """Create or restore a piece of available equipment."""
        equipment_range = EquipmentRange(minimum, maximum, increment)
        if maximum < minimum or not equipment_range.contains(minimum):
            raise GymError("invalid equipment weight range")
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO equipment(name,equipment_type,min_weight,max_weight,increment,unit)
                VALUES(?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET available=1""",
                (name, equipment_type, float(minimum), float(maximum), float(increment), unit),
            )
        return {"ok": True, "equipment": name, "available": True}

    def list_equipment(self) -> dict[str, Any]:
        """List equipment without exposing configuration or credentials."""
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT name,equipment_type,min_weight,max_weight,increment,unit,available
                FROM equipment ORDER BY name"""
            ).fetchall()
        return {"ok": True, "equipment": [dict(row) for row in rows]}

    def remove_equipment(self, name: str) -> dict[str, Any]:
        """Mark equipment unavailable without losing history."""
        with self.database.connect() as connection:
            result = connection.execute(
                "UPDATE equipment SET available=0 WHERE name=? COLLATE NOCASE", (name,)
            )
            if result.rowcount == 0:
                raise GymError(f"unknown equipment: {name}")
        return {"ok": True, "equipment": name, "available": False}

    def add_exercise(
        self,
        name: str,
        category: Category,
        orientation: Orientation,
        equipment_type: str,
        unit: str = "lb",
        equipment_name: str | None = None,
    ) -> dict[str, Any]:
        """Add a canonical classified exercise and optional equipment requirement."""
        with self.database.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO exercises(
                canonical_name,category,orientation,equipment_type,default_unit
                ) VALUES(?,?,?,?,?)""",
                (name, category.value, orientation.value, equipment_type, unit),
            )
            exercise_id = int(cursor.lastrowid or 0)
            if equipment_name:
                equipment = connection.execute(
                    "SELECT id FROM equipment WHERE name=? COLLATE NOCASE", (equipment_name,)
                ).fetchone()
                if equipment is None:
                    raise GymError(f"unknown equipment: {equipment_name}")
                connection.execute(
                    "INSERT INTO exercise_equipment(exercise_id,equipment_id) VALUES(?,?)",
                    (exercise_id, equipment["id"]),
                )
        return {"ok": True, "exercise": name, "id": exercise_id}

    def add_alias(self, alias: str, exercise: str) -> dict[str, Any]:
        """Persist a user's wording for a known exercise."""
        exercise_id = self._exercise_id(exercise)
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO exercise_aliases(alias,exercise_id) VALUES(?,?)", (alias, exercise_id)
            )
        return {"ok": True, "alias": alias, "exercise": exercise}

    def search_exercises(self, query: str) -> dict[str, Any]:
        """Search canonical names and aliases deterministically."""
        pattern = f"%{query}%"
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT DISTINCT e.canonical_name,e.category,e.orientation,e.equipment_type
                FROM exercises e LEFT JOIN exercise_aliases a ON a.exercise_id=e.id
                WHERE e.active=1 AND (e.canonical_name LIKE ? OR a.alias LIKE ?)
                ORDER BY e.canonical_name""",
                (pattern, pattern),
            ).fetchall()
        return {"ok": True, "matches": [dict(row) for row in rows]}

    def begin_workout(
        self, *, scheduled: bool = False, now: datetime | None = None
    ) -> dict[str, Any]:
        """Begin one workout, idempotently for scheduled local dates."""
        timestamp = (now or datetime.now(tz=PACIFIC)).astimezone(PACIFIC)
        local_date = timestamp.date().isoformat()
        with self.database.connect() as connection:
            existing = self._existing_workout(connection, local_date, scheduled)
            if existing is not None:
                return self._workout_payload(connection, int(existing["id"]), duplicate=True)
            latest = connection.execute(
                "SELECT orientation FROM workout_sessions ORDER BY started_at DESC,id DESC LIMIT 1"
            ).fetchone()
            orientation = (
                Orientation.VERTICAL
                if latest is None
                else Orientation(latest["orientation"]).opposite()
            )
            try:
                cursor = connection.execute(
                    """INSERT INTO workout_sessions(
                    started_at,local_date,source,orientation,status
                    ) VALUES(?,?,?,?,?)""",
                    (
                        timestamp.isoformat(),
                        local_date,
                        "scheduled" if scheduled else "manual",
                        orientation.value,
                        "active",
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise GymError("an active workout already exists") from error
            workout_id = int(cursor.lastrowid or 0)
            self._prescribe(connection, workout_id, orientation)
            return self._workout_payload(connection, workout_id, duplicate=False)

    def current_workout(self) -> dict[str, Any]:
        """Return the active workout and remaining categories."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id FROM workout_sessions WHERE status='active'"
            ).fetchone()
            if row is None:
                raise GymError("no active workout")
            return self._workout_payload(connection, int(row["id"]), duplicate=False)

    def log_exercise(
        self,
        exercise: str,
        weight: Decimal,
        reps: tuple[int, int, int],
        unit: str = "lb",
        final_rir: int | None = None,
        technical_failure: bool = False,
    ) -> dict[str, Any]:
        """Atomically attach a three-set result to the active workout."""
        if any(repetition < 0 for repetition in reps) or (final_rir is not None and final_rir < 0):
            raise GymError("reps and RIR must be non-negative")
        with self.database.connect() as connection:
            workout_exercise = self._active_workout_exercise(connection, exercise)
            equipment = self._equipment_for_exercise(
                connection, int(workout_exercise["exercise_id"])
            )
            if equipment and not equipment.contains(weight):
                raise GymError("weight is not supported by the required equipment")
            for set_number, completed_reps in enumerate(reps, start=1):
                connection.execute(
                    """INSERT INTO sets(
                    workout_exercise_id,set_number,completed_reps,weight,unit,rir,technical_failure)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(workout_exercise_id,set_number) DO UPDATE SET
                    completed_reps=excluded.completed_reps,weight=excluded.weight,unit=excluded.unit,
                    rir=excluded.rir,technical_failure=excluded.technical_failure""",
                    (
                        workout_exercise["id"],
                        set_number,
                        completed_reps,
                        float(weight),
                        unit,
                        final_rir if set_number == 3 else None,
                        int(technical_failure and set_number == 3),
                    ),
                )
            connection.execute(
                "UPDATE workout_exercises SET status='completed' WHERE id=?",
                (workout_exercise["id"],),
            )
            next_weight, reason = (weight, "baseline_recorded")
            if equipment:
                next_weight, reason = progression_weight(
                    weight, Performance(reps, final_rir, technical_failure), equipment
                )
            connection.execute("UPDATE sync_state SET pending=1 WHERE singleton=1")
        return {
            "ok": True,
            "exercise": exercise,
            "total_reps": sum(reps),
            "next_weight": float(next_weight),
            "unit": unit,
            "reason_code": reason,
            "sync_pending": True,
        }

    def complete_workout(self) -> dict[str, Any]:
        """Complete only when push, pull, and legs have recorded results."""
        with self.database.connect() as connection:
            workout = connection.execute(
                "SELECT id FROM workout_sessions WHERE status='active'"
            ).fetchone()
            if workout is None:
                raise GymError("no active workout")
            remaining = self._remaining(connection, int(workout["id"]))
            if remaining:
                raise GymError(f"workout incomplete; remaining: {','.join(remaining)}")
            completed_at = datetime.now(tz=PACIFIC).isoformat()
            connection.execute(
                "UPDATE workout_sessions SET status='completed',completed_at=? WHERE id=?",
                (completed_at, workout["id"]),
            )
            connection.execute("UPDATE sync_state SET pending=1 WHERE singleton=1")
        return {
            "ok": True,
            "workout_id": workout["id"],
            "status": "completed",
            "sync_pending": True,
        }

    def skip_workout(self) -> dict[str, Any]:
        """Skip the active workout while preserving it for alternation."""
        with self.database.connect() as connection:
            result = connection.execute(
                "UPDATE workout_sessions SET status='skipped' WHERE status='active'"
            )
            if result.rowcount == 0:
                raise GymError("no active workout")
        return {"ok": True, "status": "skipped"}

    def recent_history(self, limit: int = 10) -> dict[str, Any]:
        """Return recent workout sessions."""
        if limit < 1 or limit > 100:
            raise GymError("limit must be between 1 and 100")
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT id,started_at,completed_at,source,orientation,status
                FROM workout_sessions ORDER BY started_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return {"ok": True, "workouts": [dict(row) for row in rows]}

    def _exercise_id(self, name: str) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT e.id FROM exercises e LEFT JOIN exercise_aliases a ON a.exercise_id=e.id
                WHERE e.canonical_name=? COLLATE NOCASE OR a.alias=? COLLATE NOCASE LIMIT 1""",
                (name, name),
            ).fetchone()
        if row is None:
            raise GymError(f"unknown exercise: {name}")
        return int(row["id"])

    @staticmethod
    def _existing_workout(
        connection: sqlite3.Connection, local_date: str, scheduled: bool
    ) -> sqlite3.Row | None:
        if scheduled:
            return cast(
                "sqlite3.Row | None",
                connection.execute(
                    "SELECT id FROM workout_sessions WHERE local_date=? AND source='scheduled'",
                    (local_date,),
                ).fetchone(),
            )
        return cast(
            "sqlite3.Row | None",
            connection.execute("SELECT id FROM workout_sessions WHERE status='active'").fetchone(),
        )

    def _prescribe(
        self, connection: sqlite3.Connection, workout_id: int, orientation: Orientation
    ) -> None:
        for sequence, category in enumerate(Category, start=1):
            candidate = connection.execute(
                """SELECT e.id,e.default_unit FROM exercises e
                WHERE e.active=1 AND e.category=? AND e.orientation=? AND NOT EXISTS(
                 SELECT 1 FROM exercise_equipment ee JOIN equipment q ON q.id=ee.equipment_id
                 WHERE ee.exercise_id=e.id AND ee.required=1 AND q.available=0)
                ORDER BY COALESCE((SELECT MAX(ws.started_at) FROM workout_exercises wx
                 JOIN workout_sessions ws ON ws.id=wx.workout_session_id
                 WHERE wx.exercise_id=e.id),'') ASC,
                 e.canonical_name ASC LIMIT 1""",
                (category.value, orientation.value),
            ).fetchone()
            if candidate:
                connection.execute(
                    """INSERT INTO workout_exercises(
                    workout_session_id,exercise_id,unit,sequence
                    ) VALUES(?,?,?,?)""",
                    (workout_id, candidate["id"], candidate["default_unit"], sequence),
                )

    def _workout_payload(
        self, connection: sqlite3.Connection, workout_id: int, *, duplicate: bool
    ) -> dict[str, Any]:
        workout = connection.execute(
            "SELECT * FROM workout_sessions WHERE id=?", (workout_id,)
        ).fetchone()
        recommendations = connection.execute(
            """SELECT e.category,e.canonical_name AS exercise,wx.prescribed_weight AS weight,
            wx.unit,3 AS sets,5 AS reps FROM workout_exercises wx
            JOIN exercises e ON e.id=wx.exercise_id
            WHERE wx.workout_session_id=? ORDER BY wx.sequence""",
            (workout_id,),
        ).fetchall()
        return {
            "ok": True,
            "workout_id": workout_id,
            "orientation": workout["orientation"],
            "status": workout["status"],
            "duplicate": duplicate,
            "remaining_categories": self._remaining(connection, workout_id),
            "recommendations": [
                dict(row)
                | {"weight_status": "baseline_required" if row["weight"] is None else "known"}
                for row in recommendations
            ],
        }

    @staticmethod
    def _remaining(connection: sqlite3.Connection, workout_id: int) -> list[str]:
        completed = {
            row["category"]
            for row in connection.execute(
                """SELECT e.category FROM workout_exercises wx
            JOIN exercises e ON e.id=wx.exercise_id
            WHERE wx.workout_session_id=? AND wx.status='completed'""",
                (workout_id,),
            )
        }
        return [category.value for category in Category if category.value not in completed]

    def _active_workout_exercise(
        self, connection: sqlite3.Connection, exercise: str
    ) -> sqlite3.Row:
        exercise_id = self._exercise_id(exercise)
        row = connection.execute(
            """SELECT wx.id,wx.exercise_id FROM workout_exercises wx JOIN workout_sessions ws
            ON ws.id=wx.workout_session_id WHERE ws.status='active' AND wx.exercise_id=?""",
            (exercise_id,),
        ).fetchone()
        if row is None:
            raise GymError("exercise is not prescribed in the active workout")
        return cast("sqlite3.Row", row)

    @staticmethod
    def _equipment_for_exercise(
        connection: sqlite3.Connection, exercise_id: int
    ) -> EquipmentRange | None:
        row = connection.execute(
            """SELECT q.min_weight,q.max_weight,q.increment FROM exercise_equipment ee
            JOIN equipment q ON q.id=ee.equipment_id WHERE ee.exercise_id=? AND ee.required=1""",
            (exercise_id,),
        ).fetchone()
        if row is None:
            return None
        return EquipmentRange(
            Decimal(str(row["min_weight"])),
            Decimal(str(row["max_weight"])),
            Decimal(str(row["increment"])),
        )
