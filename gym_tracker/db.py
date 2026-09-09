"""SQLite persistence and migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE COLLATE NOCASE,
 display_name TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
);
INSERT OR IGNORE INTO users(id,slug,display_name) VALUES(1,'geoff','Geoff');
INSERT OR IGNORE INTO users(id,slug,display_name) VALUES(2,'freedom','Freedom');
CREATE TABLE IF NOT EXISTS workout_sessions (
 id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT,
 local_date TEXT NOT NULL, source TEXT NOT NULL CHECK(source IN ('scheduled','manual')),
 orientation TEXT NOT NULL CHECK(orientation IN ('horizontal','vertical')),
 status TEXT NOT NULL CHECK(status IN ('planned','active','partial','completed','skipped')),
 notes TEXT NOT NULL DEFAULT '', user_id INTEGER NOT NULL REFERENCES users(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_scheduled_day
 ON workout_sessions(local_date) WHERE source = 'scheduled';
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_workout
 ON workout_sessions(status) WHERE status = 'active';
CREATE TABLE IF NOT EXISTS equipment (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
 equipment_type TEXT NOT NULL, min_weight REAL NOT NULL, max_weight REAL NOT NULL,
 increment REAL NOT NULL CHECK(increment > 0), unit TEXT NOT NULL,
 available INTEGER NOT NULL DEFAULT 1 CHECK(available IN (0,1)), notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS exercises (
 id INTEGER PRIMARY KEY, canonical_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
 category TEXT NOT NULL CHECK(category IN ('push','pull','legs')),
 orientation TEXT NOT NULL CHECK(orientation IN ('horizontal','vertical')),
 equipment_type TEXT NOT NULL, default_unit TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS exercise_aliases (
 id INTEGER PRIMARY KEY, alias TEXT NOT NULL UNIQUE COLLATE NOCASE,
 exercise_id INTEGER NOT NULL REFERENCES exercises(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS exercise_equipment (
 exercise_id INTEGER NOT NULL REFERENCES exercises(id) ON DELETE CASCADE,
 equipment_id INTEGER NOT NULL REFERENCES equipment(id) ON DELETE CASCADE,
 required INTEGER NOT NULL DEFAULT 1 CHECK(required IN (0,1)),
 PRIMARY KEY(exercise_id,equipment_id)
);
CREATE TABLE IF NOT EXISTS workout_exercises (
 id INTEGER PRIMARY KEY, workout_session_id INTEGER NOT NULL REFERENCES workout_sessions(id),
 exercise_id INTEGER NOT NULL REFERENCES exercises(id), prescribed_weight REAL,
 unit TEXT NOT NULL, sequence INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'prescribed',
 performed_local_date TEXT,
 UNIQUE(workout_session_id,exercise_id), UNIQUE(workout_session_id,sequence)
);
CREATE TABLE IF NOT EXISTS sets (
 id INTEGER PRIMARY KEY, workout_exercise_id INTEGER NOT NULL REFERENCES workout_exercises(id),
 set_number INTEGER NOT NULL CHECK(set_number BETWEEN 1 AND 3),
 target_reps INTEGER NOT NULL DEFAULT 5,
 completed_reps INTEGER NOT NULL CHECK(completed_reps >= 0),
 weight REAL NOT NULL CHECK(weight >= 0),
 unit TEXT NOT NULL, rir INTEGER CHECK(rir >= 0), technical_failure INTEGER NOT NULL DEFAULT 0,
 notes TEXT NOT NULL DEFAULT '', UNIQUE(workout_exercise_id,set_number)
);
CREATE TABLE IF NOT EXISTS sync_state (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), pending INTEGER NOT NULL DEFAULT 0,
 last_success_at TEXT, last_error TEXT
);
INSERT OR IGNORE INTO sync_state(singleton,pending) VALUES(1,0);
CREATE TABLE IF NOT EXISTS user_identities (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), platform TEXT NOT NULL,
 external_sender_id TEXT NOT NULL, display_hint TEXT,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(platform,external_sender_id)
);
CREATE TABLE IF NOT EXISTS metabolic_profiles (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
 height_cm REAL, weight_kg REAL, birth_date TEXT,
 bmr_equation TEXT NOT NULL DEFAULT 'mifflin_st_jeor_v1',
 bmr_sex_parameter TEXT CHECK(bmr_sex_parameter IN ('female','male')),
 baseline_activity_factor REAL NOT NULL DEFAULT 1.20,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS cardio_sessions (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
 submitted_by_user_id INTEGER REFERENCES users(id), activity_type TEXT NOT NULL,
 canonical_activity TEXT NOT NULL, started_at TEXT NOT NULL, local_date TEXT NOT NULL,
 duration_seconds INTEGER NOT NULL CHECK(duration_seconds > 0), distance_meters REAL,
 elevation_gain_meters REAL, intensity_rpe INTEGER NOT NULL CHECK(intensity_rpe BETWEEN 1 AND 10),
 met_value REAL NOT NULL, met_source TEXT NOT NULL, met_table_version TEXT NOT NULL,
 gross_calories_kcal REAL NOT NULL, net_calories_kcal REAL NOT NULL,
 calorie_method TEXT NOT NULL, imported_total_calories_kcal REAL,
 source TEXT NOT NULL, source_external_id TEXT, source_message_id TEXT,
 notes TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(source,source_external_id)
);
CREATE INDEX IF NOT EXISTS ix_workout_sessions_user_date ON workout_sessions(user_id,local_date);
CREATE INDEX IF NOT EXISTS ix_cardio_sessions_user_date ON cardio_sessions(user_id,local_date);
"""


class Database:
    """Connection factory for the canonical local database."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        """Create the parent directory and current schema."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_workouts()
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            columns = {
                str(column["name"])
                for column in connection.execute("PRAGMA table_info(workout_exercises)")
            }
            if "performed_local_date" not in columns:
                connection.execute(
                    "ALTER TABLE workout_exercises ADD COLUMN performed_local_date TEXT"
                )
            if "status" in columns:
                connection.execute(
                    """UPDATE workout_exercises SET performed_local_date=(
                     SELECT local_date FROM workout_sessions
                     WHERE workout_sessions.id=workout_exercises.workout_session_id)
                    WHERE status='completed' AND performed_local_date IS NULL AND EXISTS(
                     SELECT 1 FROM sets WHERE sets.workout_exercise_id=workout_exercises.id)"""
                )
            connection.execute(
                """UPDATE workout_sessions SET status='partial' WHERE status='skipped' AND EXISTS(
                 SELECT 1 FROM workout_exercises wx JOIN sets s ON s.workout_exercise_id=wx.id
                 WHERE wx.workout_session_id=workout_sessions.id)"""
            )
            names = connection.execute("SELECT id,canonical_name FROM exercises").fetchall()
            for exercise in names:
                name = str(exercise["canonical_name"])
                if name == name.lower():
                    connection.execute(
                        "UPDATE exercises SET canonical_name=? WHERE id=?",
                        (name.title(), exercise["id"]),
                    )

    def _migrate_legacy_workouts(self) -> None:
        """Restart-safely rebuild the legacy parent with non-null Geoff ownership."""
        connection = sqlite3.connect(self.path)
        try:
            existing_table = connection.execute(
                """SELECT sql FROM sqlite_master
                WHERE type='table' AND name='workout_sessions'"""
            ).fetchone()
            columns = (
                []
                if existing_table is None
                else connection.execute("PRAGMA table_info(workout_sessions)").fetchall()
            )
            user_column = next((column for column in columns if column[1] == "user_id"), None)
            supports_partial = existing_table is not None and "'partial'" in existing_table[0]
            if existing_table is None or (
                user_column is not None and user_column[3] == 1 and supports_partial
            ):
                return
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("PRAGMA legacy_alter_table = ON")
            connection.executescript(
                """BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS users (
                 id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE COLLATE NOCASE,
                 display_name TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)));
                INSERT OR IGNORE INTO users(id,slug,display_name) VALUES(1,'geoff','Geoff');
                INSERT OR IGNORE INTO users(id,slug,display_name) VALUES(2,'freedom','Freedom');
                ALTER TABLE workout_sessions RENAME TO workout_sessions_legacy;
                CREATE TABLE workout_sessions (
                 id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT,
                 local_date TEXT NOT NULL,
                 source TEXT NOT NULL CHECK(source IN ('scheduled','manual')),
                 orientation TEXT NOT NULL CHECK(orientation IN ('horizontal','vertical')),
                 status TEXT NOT NULL CHECK(
                  status IN ('planned','active','partial','completed','skipped')),
                 notes TEXT NOT NULL DEFAULT '', user_id INTEGER NOT NULL REFERENCES users(id));"""
            )
            if user_column is None:
                connection.execute(
                    """INSERT INTO workout_sessions
                    (id,started_at,completed_at,local_date,source,orientation,status,notes,user_id)
                    SELECT id,started_at,completed_at,local_date,source,orientation,status,notes,1
                    FROM workout_sessions_legacy"""
                )
            else:
                connection.execute(
                    """INSERT INTO workout_sessions
                    (id,started_at,completed_at,local_date,source,orientation,status,notes,user_id)
                    SELECT id,started_at,completed_at,local_date,source,orientation,status,notes,
                    COALESCE(user_id,1) FROM workout_sessions_legacy"""
                )
            connection.execute("DROP TABLE workout_sessions_legacy")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a transactional connection with foreign keys enabled."""
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()
