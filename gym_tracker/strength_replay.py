"""Atomic private-tracer writes with owner-scoped replay receipts."""

import hashlib
import hmac
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from functools import partial
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from gym_tracker.service import (
    PACIFIC,
    GymError,
    GymService,
    PerformedExercise,
    PerformedWorkout,
    PriorStrengthResult,
    ScheduledStrengthResult,
    StrengthDateCorrection,
)

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
    time_zone: ZoneInfo = PACIFIC,
) -> RecordedResponse:
    """Record one authorized result or return its original response.

    The caller must check current authorization before every call. SQLite's
    write lock covers the receipt lookup, result, audit and receipt insert.
    """
    service.validate_strength_values(result)
    canonical = (result.workout_id, result.prescription_id, *_exercise_values(result))
    write = partial(
        service.record_scheduled_result,
        owner_id=owner_id,
        result=result,
        now=now,
        time_zone=time_zone,
    )
    return _commit_strength_write(
        service, owner_id, OPERATION, client_key, request_id, canonical, write
    )


def record_prior_workout_with_replay(
    service: GymService,
    owner_id: int,
    performed_date: date,
    results: tuple[PriorStrengthResult, ...],
    client_key: str,
    request_id: UUID,
    *,
    time_zone: ZoneInfo,
    now: datetime | None = None,
) -> RecordedResponse:
    """Commit one account-owned historical baseline and replay it by key."""
    service.validate_prior_strength_results(results)
    canonical = (performed_date.isoformat(), tuple(_exercise_values(result) for result in results))
    write = partial(
        service.record_prior_strength_workout,
        owner_id=owner_id,
        performed_date=performed_date,
        results=results,
        time_zone=time_zone,
        now=now,
    )
    return _commit_strength_write(
        service, owner_id, "record_prior_strength_workout", client_key, request_id, canonical, write
    )


def correct_exercise_date_with_replay(
    service: GymService,
    owner_id: int,
    correction: StrengthDateCorrection,
    client_key: str,
    request_id: UUID,
    *,
    time_zone: ZoneInfo,
    now: datetime | None = None,
) -> RecordedResponse:
    """Change a specific result once and replay its original correction response."""
    canonical = (
        correction.workout_id,
        correction.prescription_id,
        correction.expected_revision,
        correction.local_date.isoformat(),
    )
    write = partial(
        service.correct_performed_exercise_date,
        owner_id=owner_id,
        correction=correction,
        time_zone=time_zone,
        now=now,
    )
    return _commit_strength_write(
        service,
        owner_id,
        "correct_exercise_date",
        client_key,
        request_id,
        canonical,
        write,
        audit_outcome="corrected",
    )


def correct_workout_date_with_replay(
    service: GymService,
    owner_id: int,
    target: PerformedWorkout,
    local_date: date,
    client_key: str,
    request_id: UUID,
    *,
    time_zone: ZoneInfo,
    now: datetime | None = None,
) -> RecordedResponse:
    """Correct an entire performed workout by its current revision set."""
    canonical = (asdict(target), local_date.isoformat())
    write = partial(
        service.correct_performed_workout_date,
        owner_id=owner_id,
        target=target,
        local_date=local_date,
        time_zone=time_zone,
        now=now,
    )
    return _commit_strength_write(
        service,
        owner_id,
        "correct_workout_date",
        client_key,
        request_id,
        canonical,
        write,
        audit_outcome="corrected",
    )


@dataclass(frozen=True)
class DeletionPreview:
    target: PerformedExercise
    confirmation: str = field(repr=False)


@dataclass(frozen=True)
class WorkoutDeletionPreview:
    target: PerformedWorkout
    confirmation: str = field(repr=False)


class StrengthDeletion:
    """Prepare and confirm short-lived, owner-bound strength deletions."""

    def __init__(self, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("deletion confirmation key must contain at least 32 bytes")
        self._key = key

    def prepare(
        self,
        service: GymService,
        owner_id: int,
        workout_id: int,
        prescription_id: int,
        *,
        now: datetime,
    ) -> DeletionPreview:
        target = service.preview_exercise_deletion(owner_id, workout_id, prescription_id)
        expires_at = int(now.timestamp()) + 600
        return DeletionPreview(
            target, f"{expires_at}.{self._signature(owner_id, target, expires_at)}"
        )

    def confirm(
        self,
        service: GymService,
        owner_id: int,
        preview: DeletionPreview,
        client_key: str,
        request_id: UUID,
        *,
        now: datetime,
    ) -> RecordedResponse:
        """Check the specific preview before deletion. Retries preserve the successful reply."""
        target = preview.target
        canonical = (
            target.workout_id,
            target.prescription_id,
            target.result_revision,
            preview.confirmation,
        )

        def delete(connection: sqlite3.Connection) -> dict[str, Any]:
            self._verify(owner_id, preview, now)
            return service.delete_performed_exercise(connection, owner_id, target)

        return _commit_strength_write(
            service,
            owner_id,
            "delete_performed_exercise",
            client_key,
            request_id,
            canonical,
            delete,
            audit_outcome="deleted",
        )

    def prepare_workout(
        self,
        service: GymService,
        owner_id: int,
        workout_id: int,
        *,
        now: datetime,
    ) -> WorkoutDeletionPreview:
        target = service.performed_workout_reference(owner_id, workout_id)
        expires_at = int(now.timestamp()) + 600
        return WorkoutDeletionPreview(
            target, f"{expires_at}.{self._signature(owner_id, target, expires_at)}"
        )

    def confirm_workout(
        self,
        service: GymService,
        owner_id: int,
        preview: WorkoutDeletionPreview,
        client_key: str,
        request_id: UUID,
        *,
        now: datetime,
    ) -> RecordedResponse:
        """Remove the complete workout only if its confirmed result revisions still match."""
        canonical = (asdict(preview.target), preview.confirmation)

        def delete(connection: sqlite3.Connection) -> dict[str, Any]:
            self._verify(owner_id, preview, now)
            return service.delete_performed_workout(connection, owner_id, preview.target)

        return _commit_strength_write(
            service,
            owner_id,
            "delete_performed_workout",
            client_key,
            request_id,
            canonical,
            delete,
            audit_outcome="deleted",
        )

    def _signature(
        self, owner_id: int, target: PerformedExercise | PerformedWorkout, expires_at: int
    ) -> str:
        if isinstance(target, PerformedExercise):
            payload = (
                f"delete_exercise:{owner_id}:{target.workout_id}:{target.prescription_id}:"
                f"{target.result_revision}:{expires_at}"
            )
        else:
            encoded = json.dumps(asdict(target), separators=(",", ":"))
            payload = f"delete_workout:{owner_id}:{encoded}:{expires_at}"
        return hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()

    def _verify(
        self, owner_id: int, preview: DeletionPreview | WorkoutDeletionPreview, now: datetime
    ) -> None:
        expiry, _, supplied = preview.confirmation.partition(".")
        if not (
            1 <= len(expiry) <= 20
            and expiry.isascii()
            and expiry.isdecimal()
            and len(supplied) == 64
            and supplied.isascii()
        ):
            raise GymError("invalid deletion confirmation")
        expires_at = int(expiry)
        signature = self._signature(owner_id, preview.target, expires_at)
        if not hmac.compare_digest(signature, supplied) or now.timestamp() >= expires_at:
            raise GymError("invalid deletion confirmation")


ExerciseDeletion = StrengthDeletion


def _commit_strength_write(
    service: GymService,
    owner_id: int,
    operation: str,
    client_key: str,
    request_id: UUID,
    canonical: tuple[Any, ...],
    write: Callable[[sqlite3.Connection], dict[str, Any]],
    *,
    audit_outcome: str = "recorded",
) -> RecordedResponse:
    if CLIENT_KEY.fullmatch(client_key) is None:
        raise GymError("idempotency key must contain 1 to 128 visible ASCII characters")
    fingerprint = hashlib.sha256(json.dumps(canonical, separators=(",", ":")).encode()).hexdigest()
    with service.database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        receipt = connection.execute(
            """SELECT fingerprint,response_status,response_body FROM strength_replay_receipts
            WHERE owner_id=? AND operation=? AND client_key=?""",
            (owner_id, operation, client_key),
        ).fetchone()
        if receipt is not None:
            if receipt["fingerprint"] != fingerprint:
                raise IdempotencyKeyReused("idempotency key was already used for different input")
            return RecordedResponse(receipt["response_status"], receipt["response_body"])

        payload = write(connection)
        response = RecordedResponse(
            200, json.dumps(payload, separators=(",", ":"), allow_nan=False)
        )
        prescriptions = (
            [payload["prescription_id"]]
            if "prescription_id" in payload
            else [row["prescription_id"] for row in payload["exercises"]]
        )
        connection.executemany(
            """INSERT INTO strength_audit_events(
            actor_id,operation,workout_id,prescription_id,outcome,request_id)
            VALUES(?,?,?,?,?,?)""",
            (
                (
                    owner_id,
                    operation,
                    payload["workout_id"],
                    prescription_id,
                    audit_outcome,
                    str(request_id),
                )
                for prescription_id in prescriptions
            ),
        )
        connection.execute(
            """INSERT INTO strength_replay_receipts(
            owner_id,operation,client_key,fingerprint,response_status,response_body)
            VALUES(?,?,?,?,?,?)""",
            (owner_id, operation, client_key, fingerprint, response.status, response.body),
        )
        return response


def _exercise_values(result: ScheduledStrengthResult | PriorStrengthResult) -> tuple[Any, ...]:
    exercise_id = (
        result.expected_exercise_id
        if isinstance(result, ScheduledStrengthResult)
        else result.exercise_id
    )
    return (
        exercise_id,
        [_canonical_load(weight) for weight in result.weights],
        result.reps,
        result.unit,
        result.final_rir,
        result.technical_failure,
    )


def _canonical_load(weight: Decimal) -> tuple[str, int]:
    """Canonicalize finite nonnegative decimal values without context rounding."""
    if weight.is_zero():
        return "0", 0
    _, digits, exponent = weight.as_tuple()
    significant_digits = "".join(map(str, digits))
    trimmed = significant_digits.rstrip("0")
    return trimmed, int(exponent) + len(significant_digits) - len(trimmed)
