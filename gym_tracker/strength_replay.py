"""Atomic private-tracer writes with owner-scoped replay receipts."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from gym_tracker.service import GymError, GymService, ScheduledStrengthResult

OPERATION = "record_scheduled_strength_result"
CLIENT_KEY = re.compile(r"[!-~]{1,128}")


class IdempotencyKeyReused(GymError):
    """The account already used this operation key with different input."""


@dataclass(frozen=True)
class RecordedResponse:
    status: int
    body: str


def record_with_replay(
    service: GymService,
    owner_id: int,
    result: ScheduledStrengthResult,
    client_key: str,
    request_id: UUID,
    *,
    now: datetime | None = None,
) -> RecordedResponse:
    """Record one authorized result or return its original response.

    The caller must check current authorization before every call. SQLite's
    write lock covers the receipt lookup, result, audit and receipt insert.
    """
    if CLIENT_KEY.fullmatch(client_key) is None:
        raise GymError("idempotency key must contain 1 to 128 visible ASCII characters")
    service.validate_scheduled_result(result)
    fingerprint = _fingerprint(result)
    with service.database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        receipt = connection.execute(
            """SELECT fingerprint,response_status,response_body FROM strength_replay_receipts
            WHERE owner_id=? AND operation=? AND client_key=?""",
            (owner_id, OPERATION, client_key),
        ).fetchone()
        if receipt is not None:
            if receipt["fingerprint"] != fingerprint:
                raise IdempotencyKeyReused("idempotency key was already used for different input")
            return RecordedResponse(receipt["response_status"], receipt["response_body"])

        payload = service.record_scheduled_result(connection, owner_id, result, now=now)
        response = RecordedResponse(
            200, json.dumps(payload, separators=(",", ":"), allow_nan=False)
        )
        connection.execute(
            """INSERT INTO strength_audit_events(
            actor_id,operation,workout_id,prescription_id,outcome,request_id)
            VALUES(?,?,?,?,'recorded',?)""",
            (owner_id, OPERATION, result.workout_id, result.prescription_id, str(request_id)),
        )
        connection.execute(
            """INSERT INTO strength_replay_receipts(
            owner_id,operation,client_key,fingerprint,response_status,response_body)
            VALUES(?,?,?,?,?,?)""",
            (owner_id, OPERATION, client_key, fingerprint, response.status, response.body),
        )
        return response


def _fingerprint(result: ScheduledStrengthResult) -> str:
    canonical = (
        result.workout_id,
        result.prescription_id,
        result.expected_exercise_id,
        [_canonical_load(weight) for weight in result.weights],
        result.reps,
        result.unit,
        result.final_rir,
        result.technical_failure,
    )
    return hashlib.sha256(json.dumps(canonical, separators=(",", ":")).encode()).hexdigest()


def _canonical_load(weight: Decimal) -> tuple[str, int]:
    """Canonicalize finite nonnegative decimal values without context rounding."""
    if weight.is_zero():
        return "0", 0
    _, digits, exponent = weight.as_tuple()
    significant_digits = "".join(map(str, digits))
    trimmed = significant_digits.rstrip("0")
    return trimmed, int(exponent) + len(significant_digits) - len(trimmed)
