"""Deterministic workout application service."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from math import isfinite
from typing import Any, cast
from zoneinfo import ZoneInfo

from gym_tracker.db import Database
from gym_tracker.domain import (
    Category,
    EquipmentRange,
    Orientation,
    Performance,
    age_on_date,
    met_calories,
    mifflin_st_jeor_bmr,
    parse_distance_meters,
    parse_duration_seconds,
    parse_height_cm,
    parse_weight_kg,
    progression_weight,
)
from gym_tracker.pilot_account import EventDay

PACIFIC = ZoneInfo("America/Los_Angeles")

ACTIVITY_ALIASES = {
    "walk": "walking",
    "walking": "walking",
    "hike": "hiking",
    "hiking": "hiking",
    "trail hike": "hiking",
    "run": "running",
    "running": "running",
    "bike ride": "cycling",
    "cycling": "cycling",
    "stationary bike": "cycling",
    "elliptical": "elliptical",
    "stairs": "stair_climber",
    "stair climber": "stair_climber",
    "rowing": "rowing",
    "swimming": "swimming",
    "treadmill": "treadmill",
    "dance": "dance",
    "other cardio": "other_cardio",
    "other_cardio": "other_cardio",
}
MET_TABLE = {
    "walking": (Decimal("2.8"), Decimal("3.8"), Decimal("5.0")),
    "hiking": (Decimal("4.5"), Decimal("6.0"), Decimal("7.3")),
    "running": (Decimal("6.0"), Decimal("8.3"), Decimal("11.0")),
    "cycling": (Decimal("4.0"), Decimal("6.8"), Decimal("10.0")),
    "elliptical": (Decimal("4.0"), Decimal("5.0"), Decimal("8.0")),
    "stair_climber": (Decimal("4.0"), Decimal("6.0"), Decimal("9.0")),
    "rowing": (Decimal("4.8"), Decimal("7.0"), Decimal("9.0")),
    "swimming": (Decimal("4.8"), Decimal("6.0"), Decimal("9.8")),
    "treadmill": (Decimal("3.5"), Decimal("5.0"), Decimal("8.0")),
    "dance": (Decimal("3.0"), Decimal("5.0"), Decimal("7.8")),
    "other_cardio": (Decimal("3.0"), Decimal("5.0"), Decimal("8.0")),
}
BodyweightResult = tuple[str, tuple[int, ...], int]


class GymError(Exception):
    """Expected domain or input error."""


class WorkoutRecordNotFound(GymError):
    """A requested workout or prescription is missing or inaccessible."""


class WorkoutStateConflict(GymError):
    """The owned workout or prescription changed since discovery."""


@dataclass(frozen=True)
class ScheduledStrengthResult:
    workout_id: int
    prescription_id: int
    expected_exercise_id: int
    weights: tuple[Decimal, Decimal, Decimal]
    reps: tuple[int, int, int]
    unit: str
    final_rir: int
    technical_failure: bool = False


@dataclass(frozen=True)
class PriorStrengthResult:
    exercise_id: int
    weights: tuple[Decimal, Decimal, Decimal]
    reps: tuple[int, int, int]
    unit: str
    final_rir: int
    technical_failure: bool = False


@dataclass(frozen=True)
class StrengthDateCorrection:
    workout_id: int
    prescription_id: int
    expected_revision: int
    local_date: date


@dataclass(frozen=True)
class PerformedExercise:
    workout_id: int
    prescription_id: int
    result_revision: int


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
        supported_weights: tuple[Decimal, ...] = (),
        progression_direction: str = "increase",
        *,
        owner_id: int | None = None,
    ) -> dict[str, Any]:
        """Create or restore a piece of available equipment."""
        normalized_loads = tuple(sorted(set(supported_weights)))
        if progression_direction not in {"increase", "decrease"}:
            raise GymError("progression direction must be increase or decrease")
        if any(weight < minimum or weight > maximum for weight in normalized_loads):
            raise GymError("supported equipment loads must be within the weight range")
        equipment_range = EquipmentRange(
            minimum, maximum, increment, normalized_loads, progression_direction
        )
        if maximum < minimum or not equipment_range.contains(minimum):
            raise GymError("invalid equipment weight range")
        with self.database.connect() as connection:
            row = connection.execute(
                """INSERT INTO equipment(
                name,equipment_type,min_weight,max_weight,increment,unit,progression_direction,
                owner_id)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET
                equipment_type=excluded.equipment_type,min_weight=excluded.min_weight,
                max_weight=excluded.max_weight,increment=excluded.increment,unit=excluded.unit,
                progression_direction=excluded.progression_direction,available=1 RETURNING id""",
                (
                    name,
                    equipment_type,
                    float(minimum),
                    float(maximum),
                    float(increment),
                    unit,
                    progression_direction,
                    owner_id,
                ),
            ).fetchone()
            equipment_id = int(row["id"])
            connection.execute("DELETE FROM equipment_loads WHERE equipment_id=?", (equipment_id,))
            connection.executemany(
                "INSERT INTO equipment_loads(equipment_id,weight) VALUES(?,?)",
                ((equipment_id, float(weight)) for weight in normalized_loads),
            )
        return {"ok": True, "equipment": name, "available": True}

    def list_users(self) -> dict[str, Any]:
        """List active workout owners without profile data."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT slug,display_name FROM users WHERE active=1 ORDER BY slug"
            ).fetchall()
        return {"ok": True, "users": [dict(row) for row in rows]}

    def show_user(self, user: str) -> dict[str, Any]:
        """Show one user without sensitive profile data."""
        user_id = self._user_id(user)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT slug,display_name,active FROM users WHERE id=?", (user_id,)
            ).fetchone()
        return {"ok": True, "user": dict(row)}

    def map_identity(
        self, platform: str, sender_id: str, user: str, hint: str | None = None
    ) -> dict[str, Any]:
        """Bind one stable platform sender to a user."""
        user_id = self._user_id(user)
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO user_identities(user_id,platform,external_sender_id,display_hint)
                VALUES(?,?,?,?) ON CONFLICT(platform,external_sender_id) DO UPDATE SET
                user_id=excluded.user_id,display_hint=excluded.display_hint""",
                (user_id, platform.lower(), sender_id, hint),
            )
        return {"ok": True, "platform": platform.lower(), "sender_id": sender_id, "user": user}

    def resolve_identity(self, platform: str, sender_id: str) -> dict[str, Any]:
        """Resolve a stable sender or explicitly request clarification."""
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT u.slug FROM user_identities i JOIN users u ON u.id=i.user_id
                WHERE i.platform=? AND i.external_sender_id=?""",
                (platform.lower(), sender_id),
            ).fetchone()
        if row is None:
            return {
                "ok": True,
                "user": None,
                "requires_clarification": True,
                "question": "Is this for Geoff or Freedom?",
            }
        return {"ok": True, "user": row["slug"], "requires_clarification": False}

    def set_profile(
        self,
        user: str,
        height: str,
        weight: str,
        birth_date: str,
        sex_parameter: str,
        baseline_activity_factor: Decimal = Decimal("1.20"),
    ) -> dict[str, Any]:
        """Normalize and upsert one metabolic profile."""
        if baseline_activity_factor < 1:
            raise GymError("baseline activity factor must be at least 1.0")
        try:
            parsed_birth_date = date.fromisoformat(birth_date)
            height_cm = parse_height_cm(height)
            weight_kg = parse_weight_kg(weight)
            mifflin_st_jeor_bmr(weight_kg, height_cm, 0, sex_parameter)
        except ValueError as error:
            raise GymError(str(error)) from error
        user_id = self._user_id(user)
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO metabolic_profiles(
                user_id,height_cm,weight_kg,birth_date,bmr_sex_parameter,baseline_activity_factor)
                VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                height_cm=excluded.height_cm,weight_kg=excluded.weight_kg,
                birth_date=excluded.birth_date,bmr_sex_parameter=excluded.bmr_sex_parameter,
                baseline_activity_factor=excluded.baseline_activity_factor,
                updated_at=CURRENT_TIMESTAMP""",
                (
                    user_id,
                    float(height_cm),
                    float(weight_kg),
                    parsed_birth_date.isoformat(),
                    sex_parameter,
                    float(baseline_activity_factor),
                ),
            )
        return {"ok": True, "user": user, "profile_complete": True}

    def profile(self, user: str) -> dict[str, Any]:
        """Return normalized profile fields for explicit profile requests."""
        user_id = self._user_id(user)
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT height_cm,weight_kg,birth_date,bmr_equation,bmr_sex_parameter,
                baseline_activity_factor FROM metabolic_profiles WHERE user_id=?""",
                (user_id,),
            ).fetchone()
        if row is None:
            raise GymError(f"no metabolic profile for user: {user}")
        return {"ok": True, "user": user, "profile": dict(row)}

    def bmr(self, user: str, requested_date: str) -> dict[str, Any]:
        """Calculate BMR dynamically for the requested date."""
        profile = self._required_profile(user)
        calculation_date = (
            datetime.now(tz=PACIFIC).date()
            if requested_date == "today"
            else date.fromisoformat(requested_date)
        )
        value = self._bmr_value(profile, calculation_date)
        return {
            "ok": True,
            "user": user,
            "date": calculation_date.isoformat(),
            "bmr_kcal": round(value),
            "equation": profile["bmr_equation"],
        }

    def log_cardio(
        self,
        *,
        user: str,
        activity: str,
        duration: str,
        rpe: int,
        activity_date: str,
        distance: str | None = None,
        elevation_gain: str | None = None,
        source: str = "manual",
        source_external_id: str | None = None,
        source_message_id: str | None = None,
        notes: str = "",
        submitted_by: str | None = None,
        imported_calories: Decimal | None = None,
    ) -> dict[str, Any]:
        """Validate, deduplicate, calculate, and persist a cardio session."""
        canonical = ACTIVITY_ALIASES.get(activity.lower())
        if canonical is None:
            raise GymError(f"unsupported cardio activity: {activity}")
        if rpe < 1 or rpe > 10:
            raise GymError("RPE must be between 1 and 10")
        try:
            duration_seconds = parse_duration_seconds(duration)
            local_date = date.fromisoformat(activity_date).isoformat()
            distance_meters = None if distance is None else parse_distance_meters(distance)
            elevation_meters = (
                None if elevation_gain is None else parse_distance_meters(elevation_gain)
            )
        except ValueError as error:
            raise GymError(str(error)) from error
        user_id = self._user_id(user)
        profile = self._required_profile(user)
        met = MET_TABLE[canonical][0 if rpe <= 3 else 1 if rpe <= 6 else 2]
        gross, net = met_calories(met, Decimal(str(profile["weight_kg"])), duration_seconds)
        with self.database.connect() as connection:
            duplicate = connection.execute(
                """SELECT id FROM cardio_sessions WHERE user_id=? AND local_date=?
                AND canonical_activity=? AND ABS(duration_seconds-?)<=60
                AND (distance_meters IS NULL AND ? IS NULL OR ABS(distance_meters-?)<=50)""",
                (
                    user_id,
                    local_date,
                    canonical,
                    duration_seconds,
                    None if distance_meters is None else float(distance_meters),
                    None if distance_meters is None else float(distance_meters),
                ),
            ).fetchone()
            if duplicate is not None:
                return {"ok": False, "possible_duplicate": True, "existing_id": duplicate["id"]}
            method = source if imported_calories is not None else "met_v1"
            cursor = connection.execute(
                """INSERT INTO cardio_sessions(
                user_id,submitted_by_user_id,activity_type,canonical_activity,started_at,local_date,
                duration_seconds,distance_meters,elevation_gain_meters,intensity_rpe,met_value,
                met_source,met_table_version,gross_calories_kcal,net_calories_kcal,calorie_method,
                imported_total_calories_kcal,source,source_external_id,source_message_id,notes)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    user_id,
                    None if submitted_by is None else self._user_id(submitted_by),
                    activity,
                    canonical,
                    f"{local_date}T12:00:00",
                    local_date,
                    duration_seconds,
                    None if distance_meters is None else float(distance_meters),
                    None if elevation_meters is None else float(elevation_meters),
                    rpe,
                    float(met),
                    "Compendium-derived static table",
                    "iron_goblin_met_v1",
                    float(gross),
                    float(net),
                    method,
                    None if imported_calories is None else float(imported_calories),
                    source,
                    source_external_id,
                    source_message_id,
                    notes,
                ),
            )
            connection.execute("UPDATE sync_state SET pending=1 WHERE singleton=1")
        return {
            "ok": True,
            "id": int(cursor.lastrowid or 0),
            "user": user,
            "canonical_activity": canonical,
            "gross_calories_kcal": round(gross),
            "net_calories_kcal": round(imported_calories if imported_calories is not None else net),
            "calorie_method": method,
            "possible_duplicate": False,
        }

    def recent_cardio(self, user: str, limit: int = 10) -> dict[str, Any]:
        """Return only one user's recent cardio history."""
        user_id = self._user_id(user)
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT id,local_date,canonical_activity,duration_seconds,distance_meters,
                elevation_gain_meters,intensity_rpe,source FROM cardio_sessions
                WHERE user_id=? ORDER BY started_at DESC,id DESC LIMIT ?""",
                (user_id, limit),
            ).fetchall()
        return {"ok": True, "user": user, "sessions": [dict(row) for row in rows]}

    def daily_calories(self, user: str, requested_date: str) -> dict[str, Any]:
        """Aggregate modeled baseline and one selected calorie value per cardio session."""
        calculation_date_value = (
            datetime.now(tz=PACIFIC).date()
            if requested_date == "today"
            else date.fromisoformat(requested_date)
        )
        calculation_date = calculation_date_value.isoformat()
        profile = self._required_profile(user)
        bmr_value = self._bmr_value(profile, calculation_date_value)
        factor = Decimal(str(profile["baseline_activity_factor"]))
        baseline = bmr_value * factor
        with self.database.connect() as connection:
            cardio = connection.execute(
                """SELECT COALESCE(SUM(COALESCE(imported_total_calories_kcal,
                net_calories_kcal)),0) AS calories FROM cardio_sessions
                WHERE user_id=? AND local_date=?""",
                (self._user_id(user), calculation_date),
            ).fetchone()["calories"]
        cardio_value = Decimal(str(cardio))
        return {
            "ok": True,
            "label": "Estimated daily burn",
            "user": user,
            "date": calculation_date,
            "bmr_kcal": round(bmr_value),
            "baseline_activity_factor": float(factor),
            "baseline_kcal": round(baseline),
            "cardio_net_kcal": round(cardio_value),
            "strength_net_kcal": None,
            "other_activity_kcal": 0,
            "estimated_total_kcal": round(baseline + cardio_value),
            "completeness": "modeled baseline plus logged exercise; passive movement unavailable",
            "methods": [profile["bmr_equation"], "net_exercise_or_imported_source"],
        }

    def list_equipment(self, *, owner_id: int | None = None) -> dict[str, Any]:
        """List shared equipment and the supplied owner's private equipment."""
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                """SELECT id,name,equipment_type,min_weight,max_weight,increment,unit,
                progression_direction,available
                FROM equipment WHERE owner_id IS NULL OR owner_id=?
                ORDER BY name,owner_id IS NULL""",
                (owner_id,),
            ).fetchall()
            loads = connection.execute(
                """SELECT q.id,l.weight FROM equipment_loads l
                JOIN equipment q ON q.id=l.equipment_id WHERE q.owner_id IS NULL OR q.owner_id=?
                ORDER BY q.id,l.weight""",
                (owner_id,),
            ).fetchall()
        by_id: dict[int, list[float]] = {}
        for load in loads:
            by_id.setdefault(int(load["id"]), []).append(float(load["weight"]))
        equipment = []
        for row in rows:
            payload = dict(row)
            payload["supported_weights"] = by_id.get(int(payload.pop("id")), [])
            equipment.append(payload)
        return {"ok": True, "equipment": equipment}

    @staticmethod
    def _visible_equipment(
        connection: sqlite3.Connection, name: str, owner_id: int | None
    ) -> sqlite3.Row:
        row = connection.execute(
            """SELECT id,name FROM equipment WHERE name=? COLLATE NOCASE
            AND (owner_id IS NULL OR owner_id=?) ORDER BY owner_id IS NULL LIMIT 1""",
            (name, owner_id),
        ).fetchone()
        if row is None:
            raise GymError(f"unknown equipment: {name}")
        return cast("sqlite3.Row", row)

    def assign_equipment(
        self, exercise: str, equipment: str, *, owner_id: int | None = None
    ) -> dict[str, Any]:
        """Attach a visible load grid to an exercise the caller owns."""
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exercise_id = self._exercise_id(connection, exercise, owner_id, allow_shared=False)
            equipment_row = self._visible_equipment(connection, equipment, owner_id)
            connection.execute(
                """INSERT INTO exercise_equipment(exercise_id,equipment_id)
                VALUES(?,?) ON CONFLICT(exercise_id,equipment_id) DO UPDATE SET required=1""",
                (exercise_id, equipment_row["id"]),
            )
        return {"ok": True, "exercise": exercise, "equipment": equipment_row["name"]}

    def remove_equipment(self, name: str, *, owner_id: int | None = None) -> dict[str, Any]:
        """Mark equipment unavailable without losing history."""
        with self.database.connect() as connection:
            result = connection.execute(
                "UPDATE equipment SET available=0 WHERE name=? COLLATE NOCASE AND owner_id IS ?",
                (name, owner_id),
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
        *,
        owner_id: int | None = None,
    ) -> dict[str, Any]:
        """Add a classified exercise and optional visible equipment requirement."""
        canonical_name = name.title() if name == name.lower() else name
        with self.database.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO exercises(
                canonical_name,category,orientation,equipment_type,default_unit,owner_id
                ) VALUES(?,?,?,?,?,?)""",
                (canonical_name, category.value, orientation.value, equipment_type, unit, owner_id),
            )
            exercise_id = int(cursor.lastrowid or 0)
            if equipment_name:
                equipment = self._visible_equipment(connection, equipment_name, owner_id)
                connection.execute(
                    "INSERT INTO exercise_equipment(exercise_id,equipment_id) VALUES(?,?)",
                    (exercise_id, equipment["id"]),
                )
        return {"ok": True, "exercise": canonical_name, "id": exercise_id}

    def add_alias(
        self, alias: str, exercise: str, *, owner_id: int | None = None
    ) -> dict[str, Any]:
        """Persist a user's wording for a visible exercise."""
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exercise_id = self._exercise_id(connection, exercise, owner_id)
            connection.execute(
                "INSERT INTO exercise_aliases(alias,exercise_id,owner_id) VALUES(?,?,?)",
                (alias, exercise_id, owner_id),
            )
        return {"ok": True, "alias": alias, "exercise": exercise}

    def search_exercises(self, query: str, *, owner_id: int | None = None) -> dict[str, Any]:
        """Search shared and private exercise names and aliases."""
        pattern = f"%{query}%"
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT DISTINCT e.canonical_name,e.category,e.orientation,e.equipment_type
                FROM exercises e LEFT JOIN exercise_aliases a ON a.exercise_id=e.id
                AND (a.owner_id IS NULL OR a.owner_id=?)
                WHERE e.active=1 AND (e.owner_id IS NULL OR e.owner_id=?)
                AND (e.canonical_name LIKE ? OR a.alias LIKE ?)
                ORDER BY e.canonical_name""",
                (owner_id, owner_id, pattern, pattern),
            ).fetchall()
        return {"ok": True, "matches": [dict(row) for row in rows]}

    def begin_workout(
        self,
        *,
        scheduled: bool = False,
        now: datetime | None = None,
        owner_id: int | None = None,
    ) -> dict[str, Any]:
        """Begin one workout, carrying the owner's active workout across scheduled dates."""
        owner_id = self._strength_owner(owner_id)
        timestamp = (now or datetime.now(tz=PACIFIC)).astimezone(PACIFIC)
        local_date = timestamp.date().isoformat()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._existing_workout(connection, owner_id, local_date, scheduled)
            if existing is not None:
                return self._workout_payload(
                    connection,
                    int(existing["id"]),
                    duplicate=True,
                    carried_forward=bool(
                        scheduled
                        and existing["status"] == "active"
                        and existing["local_date"] != local_date
                    ),
                )
            latest = connection.execute(
                """SELECT orientation FROM workout_sessions
                WHERE user_id=? AND orientation IN ('horizontal','vertical')
                ORDER BY started_at DESC,id DESC LIMIT 1""",
                (owner_id,),
            ).fetchone()
            orientation = (
                Orientation.VERTICAL
                if latest is None
                else Orientation(latest["orientation"]).opposite()
            )
            try:
                cursor = connection.execute(
                    """INSERT INTO workout_sessions(
                    started_at,local_date,source,orientation,status,user_id
                    ) VALUES(?,?,?,?,?,?)""",
                    (
                        timestamp.isoformat(),
                        local_date,
                        "scheduled" if scheduled else "manual",
                        orientation.value,
                        "active",
                        owner_id,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise GymError("an active workout already exists") from error
            workout_id = int(cursor.lastrowid or 0)
            self._prescribe(connection, owner_id, workout_id, orientation)
            return self._workout_payload(connection, workout_id, duplicate=False)

    def log_bodyweight_workout(
        self,
        user: str,
        requested_date: str,
        results: tuple[BodyweightResult, ...],
        *,
        notes: str = "",
    ) -> dict[str, Any]:
        """Atomically log a completed, possibly mixed-plane bodyweight workout."""
        try:
            activity_date = date.fromisoformat(requested_date)
        except ValueError as error:
            raise GymError("date must use YYYY-MM-DD") from error
        if not results:
            raise GymError("at least one bodyweight exercise is required")
        user_id = self._user_id(user)
        timestamp = datetime(
            activity_date.year,
            activity_date.month,
            activity_date.day,
            12,
            tzinfo=PACIFIC,
        ).isoformat()
        with self.database.connect() as connection:
            normalized = self._validate_bodyweight_results(connection, results, user_id)
            duplicate_id = self._matching_bodyweight_workout(
                connection, user_id, activity_date.isoformat(), normalized
            )
            if duplicate_id is not None:
                return self._bodyweight_workout_payload(
                    connection, duplicate_id, user, normalized, duplicate=True
                )
            cursor = connection.execute(
                """INSERT INTO workout_sessions(
                started_at,completed_at,local_date,source,orientation,status,notes,user_id)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    timestamp,
                    timestamp,
                    activity_date.isoformat(),
                    "manual",
                    "mixed",
                    "completed",
                    notes,
                    user_id,
                ),
            )
            workout_id = int(cursor.lastrowid or 0)
            for sequence, (exercise, reps, final_rir) in enumerate(normalized, start=1):
                workout_exercise = connection.execute(
                    """INSERT INTO workout_exercises(
                    workout_session_id,exercise_id,prescribed_weight,unit,sequence,status,
                    performed_local_date,performed_time_zone,performed_date_explicit)
                    VALUES(?,?,?,?,?,'completed',?,?,1)""",
                    (
                        workout_id,
                        exercise["id"],
                        0,
                        exercise["default_unit"],
                        sequence,
                        activity_date.isoformat(),
                        PACIFIC.key,
                    ),
                )
                workout_exercise_id = int(workout_exercise.lastrowid or 0)
                for set_number, completed_reps in enumerate(reps, start=1):
                    connection.execute(
                        """INSERT INTO sets(
                        workout_exercise_id,set_number,completed_reps,weight,unit,rir,
                        technical_failure) VALUES(?,?,?,?,?,?,0)""",
                        (
                            workout_exercise_id,
                            set_number,
                            completed_reps,
                            0,
                            exercise["default_unit"],
                            final_rir if set_number == len(reps) else None,
                        ),
                    )
            connection.execute("UPDATE sync_state SET pending=1 WHERE singleton=1")
            return self._bodyweight_workout_payload(
                connection, workout_id, user, normalized, duplicate=False
            )

    def current_workout(self, *, owner_id: int | None = None) -> dict[str, Any]:
        """Return the owner's active workout and remaining categories."""
        owner_id = self._strength_owner(owner_id)
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT id FROM workout_sessions WHERE user_id=? AND status='active'",
                (owner_id,),
            ).fetchone()
            if row is None:
                raise GymError("no active workout")
            return self._workout_payload(connection, int(row["id"]), duplicate=False)

    def substitute_exercise(self, exercise: str, *, owner_id: int | None = None) -> dict[str, Any]:
        """Replace an unlogged active prescription with a same-plane/category exercise."""
        owner_id = self._strength_owner(owner_id)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exercise_id = self._exercise_id(connection, exercise, owner_id)
            workout = connection.execute(
                "SELECT id,orientation FROM workout_sessions WHERE user_id=? AND status='active'",
                (owner_id,),
            ).fetchone()
            if workout is None:
                raise GymError("no active workout")
            candidate = connection.execute(
                """SELECT id,canonical_name,category,orientation,default_unit
                FROM exercises WHERE id=? AND active=1""",
                (exercise_id,),
            ).fetchone()
            if candidate is None:
                raise GymError(f"unknown exercise: {exercise}")
            if candidate["orientation"] != workout["orientation"]:
                raise GymError("substitute must match the active workout orientation")
            prescribed = connection.execute(
                """SELECT wx.id,wx.status,e.canonical_name FROM workout_exercises wx
                JOIN exercises e ON e.id=wx.exercise_id
                WHERE wx.workout_session_id=? AND e.category=?""",
                (workout["id"], candidate["category"]),
            ).fetchone()
            if prescribed is None:
                raise GymError("no active prescription for the substitute category")
            already_logged = connection.execute(
                "SELECT 1 FROM sets WHERE workout_exercise_id=? LIMIT 1", (prescribed["id"],)
            ).fetchone()
            if prescribed["status"] == "completed" or already_logged is not None:
                raise GymError("cannot substitute a category after results are logged")
            prescribed_weight, _ = self._prior_prescription(
                connection, int(workout["id"]), exercise_id
            )
            connection.execute(
                """UPDATE workout_exercises SET exercise_id=?,prescribed_weight=?,unit=?
                WHERE id=?""",
                (exercise_id, prescribed_weight, candidate["default_unit"], prescribed["id"]),
            )
        return {
            "ok": True,
            "workout_id": workout["id"],
            "replaced": prescribed["canonical_name"],
            "exercise": candidate["canonical_name"],
            "category": candidate["category"],
        }

    def log_exercise(
        self,
        exercise: str,
        weight: Decimal | tuple[Decimal, Decimal, Decimal],
        reps: tuple[int, int, int],
        unit: str = "lb",
        final_rir: int | None = None,
        technical_failure: bool = False,
        now: datetime | None = None,
        *,
        owner_id: int | None = None,
    ) -> dict[str, Any]:
        """Atomically attach a three-set result to the active workout."""
        self._validate_strength_result(reps, final_rir)
        performed = self._strength_event(now, PACIFIC)
        set_weights = weight if isinstance(weight, tuple) else (weight, weight, weight)
        owner_id = self._strength_owner(owner_id)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            workout_exercise = self._active_workout_exercise(connection, owner_id, exercise)
            return self._store_strength_result(
                connection,
                workout_exercise,
                set_weights,
                Performance(reps, final_rir, technical_failure),
                unit,
                performed,
            )

    def active_scheduled_workout(self, owner_id: int) -> dict[str, Any] | None:
        """Discover the owner's active scheduled workout and its prescription references."""
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            workout = connection.execute(
                """SELECT id AS workout_id,status FROM workout_sessions
                WHERE user_id=? AND source='scheduled' AND status='active'""",
                (owner_id,),
            ).fetchone()
            if workout is None:
                return None
            prescriptions = connection.execute(
                """SELECT id AS prescription_id,exercise_id,status,unit FROM workout_exercises
                WHERE workout_session_id=? ORDER BY sequence""",
                (workout["workout_id"],),
            )
            return dict(workout) | {"prescriptions": [dict(row) for row in prescriptions]}

    def record_scheduled_result(
        self,
        connection: sqlite3.Connection,
        owner_id: int,
        result: ScheduledStrengthResult,
        *,
        now: datetime | None = None,
        time_zone: ZoneInfo = PACIFIC,
    ) -> dict[str, Any]:
        """Record one owned prescription in the caller's write transaction.

        A caller with additional work, such as replay receipt storage, can acquire
        the write lock first with BEGIN IMMEDIATE. The connection context owns commit.
        """
        self.validate_strength_values(result)
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        prescription = connection.execute(
            """SELECT wx.id,wx.exercise_id,wx.unit,wx.status,e.canonical_name,
            ws.status AS workout_status,
            EXISTS(SELECT 1 FROM sets s WHERE s.workout_exercise_id=wx.id) AS has_sets
            FROM workout_exercises wx JOIN workout_sessions ws
            ON ws.id=wx.workout_session_id JOIN exercises e ON e.id=wx.exercise_id
            WHERE ws.id=? AND ws.user_id=? AND ws.source='scheduled' AND wx.id=?""",
            (result.workout_id, owner_id, result.prescription_id),
        ).fetchone()
        if prescription is None:
            raise WorkoutRecordNotFound("workout or prescription not found")
        if (
            prescription["workout_status"] != "active"
            or prescription["status"] != "prescribed"
            or prescription["has_sets"]
            or prescription["exercise_id"] != result.expected_exercise_id
        ):
            raise WorkoutStateConflict("workout or prescription has changed")
        if result.unit != prescription["unit"]:
            raise GymError("weight unit must match the prescription")
        performed = self._strength_event(now, time_zone)
        payload = self._store_strength_result(
            connection,
            prescription,
            result.weights,
            Performance(result.reps, result.final_rir, result.technical_failure),
            result.unit,
            performed,
        )
        return payload | {
            "workout_id": result.workout_id,
            "prescription_id": result.prescription_id,
            "performed_local_date": performed.local_date.isoformat(),
        }

    def record_prior_strength_workout(
        self,
        connection: sqlite3.Connection,
        owner_id: int,
        performed_date: date,
        results: tuple[PriorStrengthResult, ...],
        *,
        time_zone: ZoneInfo,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Record a date-only baseline without advancing the scheduled orientation cycle."""
        self.validate_prior_strength_results(results)
        if performed_date > self._strength_event(now, time_zone).local_date:
            raise GymError("a prior workout cannot be in the future")
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        local_date = performed_date.isoformat()
        event = EventDay(None, None, time_zone.key, performed_date, True)
        cursor = connection.execute(
            """INSERT INTO workout_sessions(started_at,local_date,source,orientation,status,user_id)
            VALUES(?,?,'manual','mixed','completed',?)""",
            (local_date, local_date, owner_id),
        )
        workout_id = int(cursor.lastrowid or 0)
        performed = []
        for sequence, result in enumerate(results, start=1):
            exercise = connection.execute(
                """SELECT canonical_name,default_unit,equipment_type FROM exercises
                WHERE id=? AND active=1 AND (owner_id IS NULL OR owner_id=?)""",
                (result.exercise_id, owner_id),
            ).fetchone()
            if exercise is None:
                raise WorkoutRecordNotFound("exercise not found")
            if result.unit != exercise["default_unit"]:
                raise GymError("weight unit must match the exercise")
            equipment = connection.execute(
                """SELECT q.unit FROM exercise_equipment ee JOIN equipment q ON q.id=ee.equipment_id
                JOIN exercises e ON e.id=ee.exercise_id WHERE ee.exercise_id=? AND ee.required=1
                AND (q.owner_id IS NULL OR q.owner_id=e.owner_id) LIMIT 2""",
                (result.exercise_id,),
            ).fetchall()
            if exercise["equipment_type"] != "bodyweight" and len(equipment) != 1:
                raise GymError("prior strength requires a known equipment load grid")
            if equipment and equipment[0]["unit"] != result.unit:
                raise GymError("weight unit must match the equipment")
            cursor = connection.execute(
                """INSERT INTO workout_exercises(workout_session_id,exercise_id,unit,sequence)
                VALUES(?,?,?,?)""",
                (workout_id, result.exercise_id, result.unit, sequence),
            )
            prescription_id = int(cursor.lastrowid or 0)
            prescription = {
                "id": prescription_id,
                "exercise_id": result.exercise_id,
                "canonical_name": exercise["canonical_name"],
            }
            logged = self._store_strength_result(
                connection,
                prescription,
                result.weights,
                Performance(result.reps, result.final_rir, result.technical_failure),
                result.unit,
                event,
            )
            performed.append(
                logged
                | {
                    "prescription_id": prescription_id,
                    "exercise_id": result.exercise_id,
                }
            )
        return {
            "ok": True,
            "workout_id": workout_id,
            "status": "completed",
            "performed_local_date": local_date,
            "exercises": performed,
        }

    @staticmethod
    def validate_prior_strength_results(results: tuple[PriorStrengthResult, ...]) -> None:
        if not 1 <= len(results) <= 20:
            raise GymError("a prior workout requires between 1 and 20 exercises")
        if len({result.exercise_id for result in results}) != len(results):
            raise GymError("an exercise may appear only once in a prior workout")
        for result in results:
            GymService.validate_strength_values(result)

    @staticmethod
    def _strength_event(now: datetime | None, time_zone: ZoneInfo) -> EventDay:
        performed = (now or datetime.now(tz=time_zone)).astimezone(time_zone)
        return EventDay(
            performed.astimezone(UTC), performed.utcoffset(), time_zone.key, performed.date(), False
        )

    @staticmethod
    def validate_strength_values(result: ScheduledStrengthResult | PriorStrengthResult) -> None:
        """Check intrinsic input before fingerprinting or accessing mutable workout state."""
        GymService._validate_strength_result(result.reps, result.final_rir)
        if len(result.weights) != 3 or any(
            not weight.is_finite() or weight < 0 or not isfinite(float(weight))
            for weight in result.weights
        ):
            raise GymError("three finite non-negative loads are required")

    @staticmethod
    def _validate_strength_result(reps: tuple[int, int, int], final_rir: int | None) -> None:
        if len(reps) != 3:
            raise GymError("three rep counts are required")
        if any(repetition < 0 for repetition in reps):
            raise GymError("reps must be non-negative")
        if final_rir is None:
            raise GymError("final-set RIR is required")
        if final_rir < 0 or final_rir > 3:
            raise GymError("final-set RIR must be between 0 and 3")

    def _store_strength_result(
        self,
        connection: sqlite3.Connection,
        workout_exercise: sqlite3.Row | dict[str, Any],
        set_weights: tuple[Decimal, Decimal, Decimal],
        performance: Performance,
        unit: str,
        performed: EventDay,
    ) -> dict[str, Any]:
        reps, final_rir, technical_failure = (
            performance.reps,
            performance.final_rir,
            performance.technical_failure,
        )
        equipment = self._equipment_for_exercise(connection, int(workout_exercise["exercise_id"]))
        if equipment and any(not equipment.contains(set_weight) for set_weight in set_weights):
            raise GymError("weight is not supported by the required equipment")
        for set_number, (completed_reps, set_weight) in enumerate(
            zip(reps, set_weights, strict=True), start=1
        ):
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
                    float(set_weight),
                    unit,
                    final_rir if set_number == 3 else None,
                    int(technical_failure and set_number == 3),
                ),
            )
        connection.execute(
            """UPDATE workout_exercises
            SET status='completed',performed_local_date=?,performed_at_utc=?,performed_time_zone=?,
            performed_date_explicit=?,result_revision=result_revision+1 WHERE id=?""",
            (
                performed.local_date.isoformat(),
                performed.occurred_at_utc.isoformat() if performed.occurred_at_utc else None,
                performed.account_time_zone,
                int(performed.explicit_local_date),
                workout_exercise["id"],
            ),
        )
        working_weight = set_weights[-1]
        next_weight, reason = (working_weight, "baseline_recorded")
        if equipment:
            next_weight, reason = progression_weight(working_weight, performance, equipment)
        connection.execute("UPDATE sync_state SET pending=1 WHERE singleton=1")
        return {
            "ok": True,
            "exercise": workout_exercise["canonical_name"],
            "total_reps": sum(reps),
            "set_weights": [float(set_weight) for set_weight in set_weights],
            "next_weight": float(next_weight),
            "unit": unit,
            "reason_code": reason,
            "sync_pending": True,
        }

    def complete_workout(self, *, owner_id: int | None = None) -> dict[str, Any]:
        """Complete only when push, pull, and legs have recorded results."""
        owner_id = self._strength_owner(owner_id)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            workout = connection.execute(
                "SELECT id FROM workout_sessions WHERE user_id=? AND status='active'",
                (owner_id,),
            ).fetchone()
            if workout is None:
                raise GymError("no active workout")
            remaining = self._remaining(connection, int(workout["id"]))
            if remaining:
                raise GymError(f"workout incomplete; remaining: {','.join(remaining)}")
            workout_number = self._workout_number(connection, int(workout["id"]))
            completed_at = datetime.now(tz=PACIFIC).isoformat()
            connection.execute(
                "UPDATE workout_sessions SET status='completed',completed_at=? WHERE id=?",
                (completed_at, workout["id"]),
            )
            connection.execute("UPDATE sync_state SET pending=1 WHERE singleton=1")
        return {
            "ok": True,
            "workout_id": workout["id"],
            "workout_number": workout_number,
            "status": "completed",
            "sync_pending": True,
        }

    def skip_workout(self, *, owner_id: int | None = None) -> dict[str, Any]:
        """Close the active workout as partial when it contains performed results."""
        owner_id = self._strength_owner(owner_id)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            workout = connection.execute(
                "SELECT id FROM workout_sessions WHERE user_id=? AND status='active'",
                (owner_id,),
            ).fetchone()
            if workout is None:
                raise GymError("no active workout")
            has_results = connection.execute(
                """SELECT 1 FROM workout_exercises wx JOIN sets s ON s.workout_exercise_id=wx.id
                WHERE wx.workout_session_id=? LIMIT 1""",
                (workout["id"],),
            ).fetchone()
            status = "partial" if has_results is not None else "skipped"
            connection.execute(
                "UPDATE workout_sessions SET status=? WHERE id=?", (status, workout["id"])
            )
            connection.execute("UPDATE sync_state SET pending=1 WHERE singleton=1")
        return {"ok": True, "status": status}

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

    def performed_history(
        self,
        owner_id: int,
        limit: int = 10,
        *,
        from_date: date | None = None,
        through_date: date | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent owned workouts and sets in an optional inclusive local-date range."""
        if not 1 <= limit <= 100:
            raise GymError("limit must be between 1 and 100")
        if from_date is not None and through_date is not None and from_date > through_date:
            raise GymError("from_date must be on or before through_date")
        start = from_date.isoformat() if from_date is not None else None
        end = through_date.isoformat() if through_date is not None else None
        with self.database.connect() as connection:
            rows = connection.execute(
                """WITH owned_exercises AS (
                    SELECT ws.id AS workout_id,ws.status,ws.started_at,wx.id AS prescription_id,
                    wx.exercise_id,wx.sequence,wx.result_revision,
                    COALESCE(wx.performed_local_date,ws.local_date) AS effective_date
                    FROM workout_sessions ws JOIN workout_exercises wx
                    ON wx.workout_session_id=ws.id WHERE ws.user_id=?
                    AND EXISTS(SELECT 1 FROM sets s WHERE s.workout_exercise_id=wx.id)
                ), matching_exercises AS (
                    SELECT * FROM owned_exercises
                    WHERE (? IS NULL OR effective_date>=?) AND (? IS NULL OR effective_date<=?)
                ), performed AS (
                    SELECT workout_id,status,started_at,MIN(effective_date) AS local_date
                    FROM matching_exercises GROUP BY workout_id
                    ORDER BY started_at DESC,workout_id DESC LIMIT ?
                )
                SELECT p.workout_id,p.status,p.local_date,wx.prescription_id,
                wx.exercise_id,wx.result_revision,wx.effective_date AS performed_local_date,
                s.id AS set_id,
                s.set_number,s.completed_reps,s.weight,s.unit,s.rir,s.technical_failure
                FROM performed p JOIN matching_exercises wx ON wx.workout_id=p.workout_id
                JOIN sets s ON s.workout_exercise_id=wx.prescription_id
                ORDER BY p.started_at DESC,p.workout_id DESC,wx.sequence,s.set_number""",
                (owner_id, start, start, end, end, limit),
            ).fetchall()
        workouts: dict[int, dict[str, Any]] = {}
        for row in rows:
            evidence = dict(row)
            workout_id = evidence.pop("workout_id")
            status = evidence.pop("status")
            local_date = evidence.pop("local_date")
            workout = workouts.setdefault(
                workout_id,
                {"workout_id": workout_id, "status": status, "local_date": local_date, "sets": []},
            )
            workout["sets"].append(evidence)
        return list(workouts.values())

    def history_summary(self, user: str) -> dict[str, Any]:
        """Summarize user history from recorded sets, excluding empty session shells."""
        user_id = self._user_id(user)
        with self.database.connect() as connection:
            raw_session_rows = int(
                connection.execute(
                    "SELECT COUNT(*) FROM workout_sessions WHERE user_id=?", (user_id,)
                ).fetchone()[0]
            )
            workout_rows = connection.execute(
                """SELECT ws.id,COALESCE(MIN(wx.performed_local_date),ws.local_date) AS local_date,
                ws.orientation,ws.status,
                COUNT(DISTINCT wx.id) AS exercises,COUNT(s.id) AS sets
                FROM workout_sessions ws
                JOIN workout_exercises wx ON wx.workout_session_id=ws.id
                JOIN sets s ON s.workout_exercise_id=wx.id
                WHERE ws.user_id=?
                GROUP BY ws.id
                HAVING COUNT(s.id)>0
                ORDER BY ws.started_at,ws.id""",
                (user_id,),
            ).fetchall()
            cardio_sessions = int(
                connection.execute(
                    "SELECT COUNT(*) FROM cardio_sessions WHERE user_id=?", (user_id,)
                ).fetchone()[0]
            )
        workouts = [dict(row) for row in workout_rows]
        status_counts: dict[str, int] = {}
        for workout in workouts:
            status = str(workout["status"])
            status_counts[status] = status_counts.get(status, 0) + 1
        return {
            "ok": True,
            "user": user,
            "strength": {
                "workouts": len(workouts),
                "exercises": sum(int(workout["exercises"]) for workout in workouts),
                "sets": sum(int(workout["sets"]) for workout in workouts),
                "raw_session_rows": raw_session_rows,
                "status_counts": status_counts,
            },
            "cardio_sessions": cardio_sessions,
            "workouts": workouts,
        }

    def correct_workout_date(
        self, user: str, workout_number: int, requested_date: str
    ) -> dict[str, Any]:
        """Correct the performed date for one recorded workout through a governed command."""
        if workout_number < 1:
            raise GymError("workout number must be at least 1")
        try:
            corrected_date = date.fromisoformat(requested_date).isoformat()
        except ValueError as error:
            raise GymError("date must use YYYY-MM-DD") from error
        user_id = self._user_id(user)
        with self.database.connect() as connection:
            workouts = connection.execute(
                """SELECT ws.id FROM workout_sessions ws WHERE ws.user_id=? AND EXISTS(
                 SELECT 1 FROM workout_exercises wx JOIN sets s ON s.workout_exercise_id=wx.id
                 WHERE wx.workout_session_id=ws.id)
                ORDER BY ws.started_at,ws.id""",
                (user_id,),
            ).fetchall()
            if workout_number > len(workouts):
                raise GymError(f"unknown performed workout number: {workout_number}")
            workout_id = int(workouts[workout_number - 1]["id"])
            result = connection.execute(
                """UPDATE workout_exercises SET performed_local_date=?,performed_date_explicit=1,
                result_revision=result_revision+1 WHERE workout_session_id=?
                AND status='completed' AND EXISTS(
                 SELECT 1 FROM sets WHERE sets.workout_exercise_id=workout_exercises.id)""",
                (corrected_date, workout_id),
            )
            connection.execute("UPDATE sync_state SET pending=1 WHERE singleton=1")
        return {
            "ok": True,
            "user": user,
            "workout_number": workout_number,
            "date": corrected_date,
            "exercises_updated": result.rowcount,
            "sync_pending": True,
        }

    def correct_performed_exercise_date(
        self,
        connection: sqlite3.Connection,
        owner_id: int,
        correction: StrengthDateCorrection,
        *,
        time_zone: ZoneInfo,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Change one owned result's effective date while keeping the original occurrence."""
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        row = self._owned_performed_exercise(
            connection, owner_id, correction.workout_id, correction.prescription_id
        )
        if row["result_revision"] != correction.expected_revision:
            raise WorkoutStateConflict("workout result has changed")
        if correction.local_date > self._strength_event(now, time_zone).local_date:
            raise GymError("a performance date cannot be in the future")
        local_date = correction.local_date.isoformat()
        connection.execute(
            """UPDATE workout_exercises SET performed_local_date=?,performed_date_explicit=1,
            result_revision=result_revision+1 WHERE id=?""",
            (local_date, correction.prescription_id),
        )
        connection.execute("UPDATE sync_state SET pending=1 WHERE singleton=1")
        return {
            "ok": True,
            "workout_id": correction.workout_id,
            "prescription_id": correction.prescription_id,
            "performed_local_date": local_date,
            "result_revision": correction.expected_revision + 1,
        }

    def preview_exercise_deletion(
        self, owner_id: int, workout_id: int, prescription_id: int
    ) -> PerformedExercise:
        """Resolve one performed result for an explicit deletion confirmation."""
        with self.database.connect() as connection:
            row = self._owned_performed_exercise(connection, owner_id, workout_id, prescription_id)
            return PerformedExercise(workout_id, prescription_id, int(row["result_revision"]))

    def delete_performed_exercise(
        self, connection: sqlite3.Connection, owner_id: int, target: PerformedExercise
    ) -> dict[str, Any]:
        """Remove owned sets and prevent old mutation keys from restoring them."""
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        row = self._owned_performed_exercise(
            connection, owner_id, target.workout_id, target.prescription_id
        )
        if row["result_revision"] != target.result_revision:
            raise WorkoutStateConflict("workout result has changed")
        connection.execute(
            "DELETE FROM sets WHERE workout_exercise_id=?", (target.prescription_id,)
        )
        status = "prescribed" if row["status"] == "active" else "deleted"
        connection.execute(
            """UPDATE workout_exercises SET status=?,performed_local_date=NULL,
            performed_at_utc=NULL,performed_time_zone=NULL,performed_date_explicit=0,
            result_revision=result_revision+1 WHERE id=?""",
            (status, target.prescription_id),
        )
        connection.execute(
            """UPDATE strength_replay_receipts
            SET response_status=404,response_body=?
            WHERE owner_id=? AND response_status=200 AND operation<>'delete_performed_exercise'
            AND json_extract(response_body,'$.workout_id')=?
            AND (json_extract(response_body,'$.prescription_id')=? OR EXISTS(
                SELECT 1 FROM json_each(response_body,'$.exercises') AS exercise
                WHERE json_extract(exercise.value,'$.prescription_id')=?))""",
            (
                '{"error":{"code":"WORKOUT_OR_PRESCRIPTION_NOT_FOUND"}}',
                owner_id,
                target.workout_id,
                target.prescription_id,
                target.prescription_id,
            ),
        )
        connection.execute(
            """UPDATE workout_exercises SET prescribed_weight=NULL
            WHERE exercise_id=? AND status='prescribed' AND workout_session_id IN (
                SELECT id FROM workout_sessions WHERE user_id=? AND status IN ('active','planned')
            ) AND NOT EXISTS (
                SELECT 1 FROM sets WHERE sets.workout_exercise_id=workout_exercises.id
            )""",
            (row["exercise_id"], owner_id),
        )
        connection.execute("UPDATE sync_state SET pending=1 WHERE singleton=1")
        return {
            "ok": True,
            "workout_id": target.workout_id,
            "prescription_id": target.prescription_id,
            "deleted": True,
        }

    @staticmethod
    def _owned_performed_exercise(
        connection: sqlite3.Connection, owner_id: int, workout_id: int, prescription_id: int
    ) -> sqlite3.Row:
        row = connection.execute(
            """SELECT wx.result_revision,wx.exercise_id,ws.status FROM workout_exercises wx
            JOIN workout_sessions ws ON ws.id=wx.workout_session_id
            WHERE ws.user_id=? AND ws.id=? AND wx.id=?
            AND EXISTS(SELECT 1 FROM sets s WHERE s.workout_exercise_id=wx.id)""",
            (owner_id, workout_id, prescription_id),
        ).fetchone()
        if row is None:
            raise WorkoutRecordNotFound("workout or prescription not found")
        return cast("sqlite3.Row", row)

    @staticmethod
    def _exercise_id(
        connection: sqlite3.Connection,
        name: str,
        owner_id: int | None,
        *,
        allow_shared: bool = True,
    ) -> int:
        row = connection.execute(
            """SELECT e.id FROM exercises e LEFT JOIN exercise_aliases a ON a.exercise_id=e.id
            AND a.alias=? COLLATE NOCASE AND (a.owner_id IS NULL OR a.owner_id=?)
            WHERE (e.owner_id IS ? OR (? AND e.owner_id IS NULL))
            AND (e.canonical_name=? COLLATE NOCASE OR a.id IS NOT NULL)
            ORDER BY e.canonical_name<>? COLLATE NOCASE,e.owner_id IS NULL,a.owner_id IS NULL,e.id
            LIMIT 1""",
            (name, owner_id, owner_id, allow_shared, name, name),
        ).fetchone()
        if row is None:
            raise GymError(f"unknown exercise: {name}")
        return int(row["id"])

    @staticmethod
    def _validate_bodyweight_results(
        connection: sqlite3.Connection, results: tuple[BodyweightResult, ...], owner_id: int
    ) -> tuple[tuple[sqlite3.Row, tuple[int, ...], int], ...]:
        normalized: list[tuple[sqlite3.Row, tuple[int, ...], int]] = []
        exercise_ids: set[int] = set()
        for name, reps, final_rir in results:
            if not reps or len(reps) > 20:
                raise GymError("reps must contain between 1 and 20 sets per exercise")
            if any(repetition < 0 for repetition in reps):
                raise GymError("reps must be non-negative")
            if final_rir < 0 or final_rir > 3:
                raise GymError("final-set RIR must be between 0 and 3")
            exercise = connection.execute(
                """SELECT e.id,e.canonical_name,e.equipment_type,e.default_unit
                FROM exercises e LEFT JOIN exercise_aliases a ON a.exercise_id=e.id
                AND a.alias=? COLLATE NOCASE AND (a.owner_id IS NULL OR a.owner_id=?)
                WHERE e.active=1 AND (e.owner_id IS NULL OR e.owner_id=?)
                AND (e.canonical_name=? COLLATE NOCASE OR a.id IS NOT NULL)
                ORDER BY e.canonical_name<>? COLLATE NOCASE,e.owner_id IS NULL,a.owner_id IS NULL,
                e.id LIMIT 1""",
                (name, owner_id, owner_id, name, name),
            ).fetchone()
            if exercise is None:
                raise GymError(f"unknown exercise: {name}")
            if exercise["equipment_type"] != "bodyweight":
                raise GymError(f"exercise is not bodyweight: {exercise['canonical_name']}")
            exercise_id = int(exercise["id"])
            if exercise_id in exercise_ids:
                raise GymError(f"duplicate exercise: {exercise['canonical_name']}")
            exercise_ids.add(exercise_id)
            normalized.append((exercise, reps, final_rir))
        return tuple(normalized)

    @staticmethod
    def _matching_bodyweight_workout(
        connection: sqlite3.Connection,
        user_id: int,
        activity_date: str,
        results: tuple[tuple[sqlite3.Row, tuple[int, ...], int], ...],
    ) -> int | None:
        expected = tuple(
            (int(exercise["id"]), reps, final_rir) for exercise, reps, final_rir in results
        )
        candidates = connection.execute(
            """SELECT id FROM workout_sessions WHERE user_id=? AND local_date=?
            AND source='manual' AND orientation='mixed' AND status='completed'""",
            (user_id, activity_date),
        ).fetchall()
        for candidate in candidates:
            rows = connection.execute(
                """SELECT wx.exercise_id,wx.sequence,s.set_number,s.completed_reps,s.rir
                FROM workout_exercises wx JOIN sets s ON s.workout_exercise_id=wx.id
                WHERE wx.workout_session_id=? ORDER BY wx.sequence,s.set_number""",
                (candidate["id"],),
            ).fetchall()
            grouped: dict[int, list[sqlite3.Row]] = {}
            for row in rows:
                grouped.setdefault(int(row["sequence"]), []).append(row)
            actual = tuple(
                (
                    int(group[0]["exercise_id"]),
                    tuple(int(row["completed_reps"]) for row in group),
                    int(group[-1]["rir"]),
                )
                for group in grouped.values()
                if group and group[-1]["rir"] is not None
            )
            if actual == expected:
                return int(candidate["id"])
        return None

    def _bodyweight_workout_payload(
        self,
        connection: sqlite3.Connection,
        workout_id: int,
        user: str,
        results: tuple[tuple[sqlite3.Row, tuple[int, ...], int], ...],
        *,
        duplicate: bool,
    ) -> dict[str, Any]:
        workout = connection.execute(
            "SELECT local_date,orientation,status FROM workout_sessions WHERE id=?", (workout_id,)
        ).fetchone()
        return {
            "ok": True,
            "duplicate": duplicate,
            "user": user,
            "date": workout["local_date"],
            "workout_id": workout_id,
            "workout_number": self._workout_number(connection, workout_id),
            "orientation": workout["orientation"],
            "status": workout["status"],
            "exercises": [
                {
                    "exercise": exercise["canonical_name"],
                    "reps": list(reps),
                    "final_set_rir": final_rir,
                }
                for exercise, reps, final_rir in results
            ],
            "sync_pending": True,
        }

    def _user_id(self, slug: str) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id FROM users WHERE slug=? COLLATE NOCASE AND active=1", (slug,)
            ).fetchone()
        if row is None:
            raise GymError(f"unknown user: {slug}")
        return int(row["id"])

    def _required_profile(self, user: str) -> sqlite3.Row:
        user_id = self._user_id(user)
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT height_cm,weight_kg,birth_date,bmr_equation,bmr_sex_parameter,
                baseline_activity_factor FROM metabolic_profiles WHERE user_id=?""",
                (user_id,),
            ).fetchone()
        required = ("height_cm", "weight_kg", "birth_date", "bmr_sex_parameter")
        missing = (
            list(required) if row is None else [field for field in required if row[field] is None]
        )
        if missing:
            raise GymError(f"missing profile fields: {','.join(missing)}")
        return cast("sqlite3.Row", row)

    @staticmethod
    def _bmr_value(profile: sqlite3.Row, calculation_date: date) -> Decimal:
        age = age_on_date(date.fromisoformat(profile["birth_date"]), calculation_date)
        return mifflin_st_jeor_bmr(
            Decimal(str(profile["weight_kg"])),
            Decimal(str(profile["height_cm"])),
            age,
            str(profile["bmr_sex_parameter"]),
        )

    def _strength_owner(self, owner_id: int | None) -> int:
        """Resolve the local CLI default. Hosted callers supply an authorized owner."""
        return self._user_id("geoff") if owner_id is None else owner_id

    @staticmethod
    def _existing_workout(
        connection: sqlite3.Connection, owner_id: int, local_date: str, scheduled: bool
    ) -> sqlite3.Row | None:
        active = connection.execute(
            "SELECT id,local_date,status FROM workout_sessions WHERE user_id=? AND status='active'",
            (owner_id,),
        ).fetchone()
        if active is not None:
            return cast("sqlite3.Row", active)
        if scheduled:
            return cast(
                "sqlite3.Row | None",
                connection.execute(
                    """SELECT id,local_date,status FROM workout_sessions
                    WHERE user_id=? AND local_date=? AND source='scheduled'""",
                    (owner_id, local_date),
                ).fetchone(),
            )
        return None

    def _prescribe(
        self,
        connection: sqlite3.Connection,
        owner_id: int,
        workout_id: int,
        orientation: Orientation,
    ) -> None:
        for sequence, category in enumerate(Category, start=1):
            candidate = connection.execute(
                """SELECT e.id,e.default_unit FROM exercises e
                WHERE e.active=1 AND (e.owner_id IS NULL OR e.owner_id=?)
                AND e.category=? AND e.orientation=? AND NOT EXISTS(
                 SELECT 1 FROM exercise_equipment ee JOIN equipment q ON q.id=ee.equipment_id
                 WHERE ee.exercise_id=e.id AND ee.required=1 AND q.available=0
                 AND (q.owner_id IS NULL OR q.owner_id=e.owner_id))
                ORDER BY COALESCE((SELECT MAX(ws.started_at) FROM workout_exercises wx
                 JOIN workout_sessions ws ON ws.id=wx.workout_session_id
                 WHERE wx.exercise_id=e.id AND ws.user_id=? AND wx.status<>'deleted'),'') ASC,
                 e.owner_id IS NULL,e.canonical_name ASC LIMIT 1""",
                (owner_id, category.value, orientation.value, owner_id),
            ).fetchone()
            if candidate:
                prescribed_weight, _ = self._prior_prescription(
                    connection, workout_id, int(candidate["id"])
                )
                connection.execute(
                    """INSERT INTO workout_exercises(
                    workout_session_id,exercise_id,prescribed_weight,unit,sequence
                    ) VALUES(?,?,?,?,?)""",
                    (
                        workout_id,
                        candidate["id"],
                        prescribed_weight,
                        candidate["default_unit"],
                        sequence,
                    ),
                )

    def _prior_prescription(
        self, connection: sqlite3.Connection, workout_id: int, exercise_id: int
    ) -> tuple[float | None, list[float]]:
        """Derive a prescription from the user's latest recorded result."""
        rows = connection.execute(
            """SELECT s.completed_reps,s.weight,s.rir,s.technical_failure
            FROM sets s WHERE s.workout_exercise_id=(
             SELECT wx.id FROM workout_exercises wx
             JOIN workout_sessions previous ON previous.id=wx.workout_session_id
             JOIN workout_sessions current ON current.id=?
             WHERE wx.exercise_id=? AND previous.user_id=current.user_id
             AND previous.id<>current.id AND EXISTS(
              SELECT 1 FROM sets recorded WHERE recorded.workout_exercise_id=wx.id)
             ORDER BY previous.started_at DESC,previous.id DESC LIMIT 1)
            ORDER BY s.set_number""",
            (workout_id, exercise_id),
        ).fetchall()
        if not rows:
            return None, []
        previous_weights = [float(row["weight"]) for row in rows]
        working_weight = Decimal(str(rows[-1]["weight"]))
        equipment = self._equipment_for_exercise(connection, exercise_id)
        if equipment is None:
            return float(working_weight), previous_weights
        performance = Performance(
            cast("tuple[int, int, int]", tuple(int(row["completed_reps"]) for row in rows)),
            None if rows[-1]["rir"] is None else int(rows[-1]["rir"]),
            bool(rows[-1]["technical_failure"]),
        )
        prescribed_weight, _ = progression_weight(working_weight, performance, equipment)
        return float(prescribed_weight), previous_weights

    def _workout_payload(
        self,
        connection: sqlite3.Connection,
        workout_id: int,
        *,
        duplicate: bool,
        carried_forward: bool = False,
    ) -> dict[str, Any]:
        workout = connection.execute(
            "SELECT * FROM workout_sessions WHERE id=?", (workout_id,)
        ).fetchone()
        recommendations = connection.execute(
            """SELECT e.id AS exercise_id,e.category,e.canonical_name AS exercise,
            wx.prescribed_weight AS weight,
            wx.unit,3 AS sets FROM workout_exercises wx
            JOIN exercises e ON e.id=wx.exercise_id
            WHERE wx.workout_session_id=? ORDER BY wx.sequence""",
            (workout_id,),
        ).fetchall()
        prior_results = {
            int(row["exercise_id"]): self._prior_prescription(
                connection, workout_id, int(row["exercise_id"])
            )
            for row in recommendations
        }
        workout_number = self._workout_number(connection, workout_id)
        recommendation_payloads = []
        for row in recommendations:
            prior_weight, previous_set_weights = prior_results[int(row["exercise_id"])]
            weight = row["weight"] if row["weight"] is not None else prior_weight
            recommendation_payloads.append(
                {key: value for key, value in dict(row).items() if key != "exercise_id"}
                | {
                    "weight": weight,
                    "weight_status": "baseline_required" if weight is None else "known",
                    "previous_set_weights": previous_set_weights,
                    "reps": self._recommended_reps(connection, workout_id, int(row["exercise_id"])),
                }
            )
        return {
            "ok": True,
            "workout_id": workout_id,
            "workout_number": workout_number,
            "orientation": workout["orientation"],
            "status": workout["status"],
            "duplicate": duplicate,
            "carried_forward": carried_forward,
            "remaining_categories": self._remaining(connection, workout_id),
            "recommendations": recommendation_payloads,
        }

    @staticmethod
    def _recommended_reps(connection: sqlite3.Connection, workout_id: int, exercise_id: int) -> int:
        rows = connection.execute(
            """SELECT s.completed_reps,s.rir,s.technical_failure,e.equipment_type
            FROM sets s JOIN workout_exercises wx ON wx.id=s.workout_exercise_id
            JOIN workout_sessions previous ON previous.id=wx.workout_session_id
            JOIN workout_sessions current ON current.id=?
            JOIN exercises e ON e.id=wx.exercise_id
            WHERE wx.exercise_id=? AND previous.user_id=current.user_id
            AND previous.id<>current.id ORDER BY previous.started_at DESC,previous.id DESC,
            s.set_number DESC LIMIT 1""",
            (workout_id, exercise_id),
        ).fetchall()
        if not rows or rows[0]["equipment_type"] != "bodyweight":
            return 5
        last = rows[0]
        reps = int(last["completed_reps"])
        if last["technical_failure"] or last["rir"] is None:
            return reps
        rir = int(last["rir"])
        return reps + 2 if rir >= 3 else reps + 1 if rir >= 1 else reps

    @staticmethod
    def _workout_number(connection: sqlite3.Connection, workout_id: int) -> int:
        """Return the user's performed-workout ordinal, ignoring empty lifecycle shells."""
        workout = connection.execute(
            "SELECT user_id,started_at FROM workout_sessions WHERE id=?", (workout_id,)
        ).fetchone()
        performed_before = int(
            connection.execute(
                """SELECT COUNT(*) FROM workout_sessions previous
                WHERE previous.user_id=? AND previous.id<>? AND EXISTS(
                 SELECT 1 FROM workout_exercises wx JOIN sets s ON s.workout_exercise_id=wx.id
                 WHERE wx.workout_session_id=previous.id)
                AND (previous.started_at<? OR previous.started_at=? AND previous.id<?)""",
                (
                    workout["user_id"],
                    workout_id,
                    workout["started_at"],
                    workout["started_at"],
                    workout_id,
                ),
            ).fetchone()[0]
        )
        return performed_before + 1

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
        self, connection: sqlite3.Connection, owner_id: int, name: str
    ) -> sqlite3.Row:
        exercise_id = self._exercise_id(connection, name, owner_id)
        row = connection.execute(
            """SELECT wx.id,wx.exercise_id,e.canonical_name
            FROM workout_exercises wx JOIN workout_sessions ws
            ON ws.id=wx.workout_session_id JOIN exercises e ON e.id=wx.exercise_id
            WHERE ws.user_id=? AND ws.status='active' AND (e.owner_id IS NULL OR e.owner_id=?)
            AND (wx.exercise_id=? OR e.canonical_name=? COLLATE NOCASE)
            ORDER BY wx.exercise_id<>?,wx.sequence LIMIT 1""",
            (owner_id, owner_id, exercise_id, name, exercise_id),
        ).fetchone()
        if row is None:
            raise GymError("exercise is not prescribed in the active workout")
        return cast("sqlite3.Row", row)

    @staticmethod
    def _equipment_for_exercise(
        connection: sqlite3.Connection, exercise_id: int
    ) -> EquipmentRange | None:
        row = connection.execute(
            """SELECT q.id,q.min_weight,q.max_weight,q.increment,q.progression_direction
            FROM exercise_equipment ee
            JOIN equipment q ON q.id=ee.equipment_id JOIN exercises e ON e.id=ee.exercise_id
            WHERE ee.exercise_id=? AND ee.required=1
            AND (q.owner_id IS NULL OR q.owner_id=e.owner_id)""",
            (exercise_id,),
        ).fetchone()
        if row is None:
            return None
        loads = connection.execute(
            "SELECT weight FROM equipment_loads WHERE equipment_id=? ORDER BY weight", (row["id"],)
        ).fetchall()
        return EquipmentRange(
            Decimal(str(row["min_weight"])),
            Decimal(str(row["max_weight"])),
            Decimal(str(row["increment"])),
            tuple(Decimal(str(load["weight"])) for load in loads),
            str(row["progression_direction"]),
        )
