"""Read-only account export for the Iron support workflow."""

from typing import Any

from gym_tracker.db import Database
from gym_tracker.service import WorkoutRecordNotFound

EXPORT_QUERIES = {
    "workouts": """
        SELECT * FROM workout_sessions WHERE user_id=? AND deleted_at IS NULL ORDER BY id
    """,
    "workout_exercises": """
        SELECT wx.*,e.canonical_name,e.category,e.orientation,e.equipment_type
        FROM workout_exercises wx JOIN workout_sessions ws ON ws.id=wx.workout_session_id
        JOIN exercises e ON e.id=wx.exercise_id WHERE ws.user_id=?
        AND (e.owner_id IS NULL OR e.owner_id=ws.user_id) AND wx.status<>'deleted'
        ORDER BY ws.id,wx.sequence
    """,
    "sets": """
        SELECT s.* FROM sets s JOIN workout_exercises wx ON wx.id=s.workout_exercise_id
        JOIN workout_sessions ws ON ws.id=wx.workout_session_id
        JOIN exercises e ON e.id=wx.exercise_id WHERE ws.user_id=?
        AND (e.owner_id IS NULL OR e.owner_id=ws.user_id) AND wx.status<>'deleted'
        ORDER BY ws.id,wx.sequence,s.set_number
    """,
    "equipment": "SELECT * FROM equipment WHERE owner_id=? ORDER BY id",
    "equipment_loads": """
        SELECT l.* FROM equipment_loads l JOIN equipment q ON q.id=l.equipment_id
        WHERE q.owner_id=? ORDER BY l.equipment_id,l.weight
    """,
    "exercise_definitions": "SELECT * FROM exercises WHERE owner_id=? ORDER BY id",
    "exercise_aliases": """
        SELECT a.id,a.alias,a.exercise_id,e.canonical_name
        FROM exercise_aliases a JOIN exercises e ON e.id=a.exercise_id
        WHERE a.owner_id=? AND (e.owner_id IS NULL OR e.owner_id=a.owner_id) ORDER BY a.id
    """,
    "exercise_equipment": """
        SELECT ee.*,q.name AS equipment_name FROM exercise_equipment ee
        JOIN exercises e ON e.id=ee.exercise_id JOIN equipment q ON q.id=ee.equipment_id
        WHERE e.owner_id=? AND (q.owner_id IS NULL OR q.owner_id=e.owner_id)
        ORDER BY ee.exercise_id,ee.equipment_id
    """,
    "cardio": """
        SELECT id,activity_type,canonical_activity,started_at,local_date,duration_seconds,
        distance_meters,elevation_gain_meters,intensity_rpe,met_value,met_source,
        met_table_version,gross_calories_kcal,net_calories_kcal,calorie_method,
        imported_total_calories_kcal,source,notes,created_at
        FROM cardio_sessions WHERE user_id=? ORDER BY id
    """,
    "profile": "SELECT * FROM metabolic_profiles WHERE user_id=? ORDER BY id",
    "identities": """
        SELECT platform,external_sender_id,display_hint,created_at
        FROM user_identities WHERE user_id=? ORDER BY id
    """,
    "audit": """
        SELECT operation,workout_id,prescription_id,outcome,created_at
        FROM strength_audit_events WHERE actor_id=? ORDER BY id
    """,
}


def export_iron_account(database: Database, verified_owner_id: int) -> dict[str, Any]:
    """Return one approved Iron snapshot. The support caller verifies requester and owner."""
    with database.connect() as connection:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        account = connection.execute(
            "SELECT id,slug,display_name,created_at FROM users WHERE id=?",
            (verified_owner_id,),
        ).fetchone()
        if account is None:
            raise WorkoutRecordNotFound("Iron account not found")
        records = {
            name: [dict(row) for row in connection.execute(query, (verified_owner_id,))]
            for name, query in EXPORT_QUERIES.items()
        }
    return {"version": 1, "product": "iron", "account": dict(account), "records": records}
