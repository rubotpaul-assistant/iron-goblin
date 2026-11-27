"""Fixture-only rehearsal for confirmed Iron account closure and recovery."""

import hashlib
import hmac
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from gym_tracker.db import Database
from gym_tracker.pilot_account import PilotAccessDenied, Product
from gym_tracker.pilot_invites import ProductInvites
from gym_tracker.service import WorkoutRecordNotFound, WorkoutStateConflict

COUNT_QUERIES = {
    "workouts": "SELECT COUNT(*) FROM workout_sessions WHERE user_id=? AND deleted_at IS NULL",
    "sets": """
        SELECT COUNT(*) FROM sets s JOIN workout_exercises wx ON wx.id=s.workout_exercise_id
        JOIN workout_sessions ws ON ws.id=wx.workout_session_id WHERE ws.user_id=?
    """,
    "equipment": "SELECT COUNT(*) FROM equipment WHERE owner_id=?",
    "exercises": "SELECT COUNT(*) FROM exercises WHERE owner_id=?",
    "aliases": "SELECT COUNT(*) FROM exercise_aliases WHERE owner_id=?",
    "cardio": "SELECT COUNT(*) FROM cardio_sessions WHERE user_id=?",
    "profile": "SELECT COUNT(*) FROM metabolic_profiles WHERE user_id=?",
    "identities": "SELECT COUNT(*) FROM user_identities WHERE user_id=?",
    "replay": "SELECT COUNT(*) FROM strength_replay_receipts WHERE owner_id=?",
    "audit": "SELECT COUNT(*) FROM strength_audit_events WHERE actor_id=?",
}
DELETE_QUERIES = (
    """DELETE FROM sets WHERE workout_exercise_id IN (
        SELECT wx.id FROM workout_exercises wx JOIN workout_sessions ws
        ON ws.id=wx.workout_session_id WHERE ws.user_id=?)""",
    """DELETE FROM workout_exercises WHERE workout_session_id IN (
        SELECT id FROM workout_sessions WHERE user_id=?)""",
    "DELETE FROM workout_sessions WHERE user_id=?",
    "DELETE FROM cardio_sessions WHERE user_id=?",
    "DELETE FROM metabolic_profiles WHERE user_id=?",
    "DELETE FROM user_identities WHERE user_id=?",
    "DELETE FROM exercise_aliases WHERE owner_id=?",
    "DELETE FROM exercises WHERE owner_id=?",
    "DELETE FROM equipment WHERE owner_id=?",
    "DELETE FROM strength_replay_receipts WHERE owner_id=?",
    "DELETE FROM strength_audit_events WHERE actor_id=?",
)


@dataclass(frozen=True)
class ClosurePreview:
    """Owner-bound counts shown to the verified requester for confirmation."""

    owner_id: int
    database_id: str
    counts: tuple[tuple[str, int], ...]
    expires_at: int
    confirmation: str = field(repr=False)


class IronSupportClosure:
    """Exercise a verified support workflow against isolated fixture stores."""

    def __init__(self, marker_store: Path, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("closure confirmation key must contain at least 32 bytes")
        self._marker_store = marker_store
        self._key = key
        with closing(sqlite3.connect(self._marker_store)) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS iron_closures(
                database_id TEXT NOT NULL,owner_id INTEGER NOT NULL,retired_slug TEXT NOT NULL,
                PRIMARY KEY(database_id,owner_id))"""
            )

    def preview(
        self, database: Database, verified_owner_id: int, verified_account_id: str, *, now: datetime
    ) -> ClosurePreview:
        """Build a count-only, read-only dry run for a verified support request."""
        self._require_separate_store(database)
        with database.connect() as connection:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
            database_id = self._database_id(connection)
            counts = self._counts(connection, verified_owner_id)
        expires_at = int(now.timestamp()) + 600
        return ClosurePreview(
            verified_owner_id,
            database_id,
            counts,
            expires_at,
            self._signature(
                database_id, verified_owner_id, verified_account_id, counts, expires_at
            ),
        )

    def confirm(
        self,
        database: Database,
        invites: ProductInvites,
        verified_account_id: str,
        preview: ClosurePreview,
        *,
        now: datetime,
    ) -> None:
        """Close Iron after confirmation and prior revocation of the Iron grant."""
        self._require_separate_store(database)
        signature = self._signature(
            preview.database_id,
            preview.owner_id,
            verified_account_id,
            preview.counts,
            preview.expires_at,
        )
        if (
            not preview.confirmation.isascii()
            or not hmac.compare_digest(signature, preview.confirmation)
            or now.timestamp() >= preview.expires_at
        ):
            raise PilotAccessDenied("INVALID_CONFIRMATION")
        if (verified_account_id, Product.IRON) in invites.active_grants():
            raise PilotAccessDenied("IRON_GRANT_MUST_BE_REVOKED")
        with database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._database_id(connection) != preview.database_id:
                raise PilotAccessDenied("INVALID_CONFIRMATION")
            if self._counts(connection, preview.owner_id) != preview.counts:
                raise WorkoutStateConflict("Iron records have changed since the dry run")
            # Persist the approved request outside the product backup before removing any rows.
            with closing(sqlite3.connect(self._marker_store)) as journal, journal:
                journal.execute(
                    """INSERT OR IGNORE INTO iron_closures(database_id,owner_id,retired_slug)
                    VALUES(?,?,?)""",
                    (preview.database_id, preview.owner_id, f"closed-iron-account-{uuid4()}"),
                )
                retired_slug = journal.execute(
                    "SELECT retired_slug FROM iron_closures WHERE database_id=? AND owner_id=?",
                    (preview.database_id, preview.owner_id),
                ).fetchone()[0]
            self._purge(connection, preview.owner_id, retired_slug)

    def apply_prior_closures(self, restored_database: Database) -> None:
        """Reapply confirmed closures before a restored copy is made available."""
        self._require_separate_store(restored_database)
        with restored_database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            database_id = self._database_id(connection)
            with closing(sqlite3.connect(self._marker_store)) as journal:
                markers = journal.execute(
                    """SELECT owner_id,retired_slug FROM iron_closures
                    WHERE database_id=? ORDER BY owner_id""",
                    (database_id,),
                ).fetchall()
            for owner_id, retired_slug in markers:
                self._purge(connection, int(owner_id), retired_slug)

    def _require_separate_store(self, database: Database) -> None:
        if database.path.resolve() == self._marker_store.resolve():
            raise ValueError("closure markers require a separate store")

    def _signature(
        self,
        database_id: str,
        owner_id: int,
        account_id: str,
        counts: tuple[tuple[str, int], ...],
        expires_at: int,
    ) -> str:
        body = json.dumps(("iron", database_id, owner_id, account_id, counts, expires_at)).encode()
        return hmac.new(self._key, body, hashlib.sha256).hexdigest()

    @staticmethod
    def _database_id(connection: sqlite3.Connection) -> str:
        return str(
            connection.execute(
                "SELECT database_id FROM iron_database_identity WHERE singleton=1"
            ).fetchone()[0]
        )

    @staticmethod
    def _counts(connection: sqlite3.Connection, owner_id: int) -> tuple[tuple[str, int], ...]:
        owner = connection.execute("SELECT 1 FROM users WHERE id=?", (owner_id,)).fetchone()
        if owner is None:
            raise WorkoutRecordNotFound("Iron account not found")
        return tuple(
            (name, int(connection.execute(query, (owner_id,)).fetchone()[0]))
            for name, query in COUNT_QUERIES.items()
        )

    @staticmethod
    def _purge(connection: sqlite3.Connection, owner_id: int, retired_slug: str) -> None:
        for statement in DELETE_QUERIES:
            connection.execute(statement, (owner_id,))
        connection.execute(
            """UPDATE users SET active=0,slug=?,display_name='Closed Iron account' WHERE id=?""",
            (retired_slug, owner_id),
        )
        connection.execute("UPDATE sync_state SET pending=1 WHERE singleton=1")
