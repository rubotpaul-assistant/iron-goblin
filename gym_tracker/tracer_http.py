"""WSGI entry point for the fixture-only Iron Goblin HTTP tracer."""

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs
from uuid import uuid4
from wsgiref.types import StartResponse, WSGIEnvironment

from gym_tracker.service import (
    GymError,
    ScheduledStrengthResult,
    WorkoutRecordNotFound,
    WorkoutStateConflict,
)
from gym_tracker.strength_replay import IdempotencyKeyReused, RecordedResponse, record_with_replay
from gym_tracker.tracer_auth import TracerAccessDenied, TracerContext, WorkoutScope

WORKOUTS_PATH = "/v1/iron-goblin/workouts"
RESULT_PATH = re.compile(WORKOUTS_PATH + r"/([1-9][0-9]{0,18})/exercise-results")
DECIMAL_STRING = re.compile(r"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
MAX_SQLITE_INTEGER = 2**63 - 1
MAX_BODY_BYTES = 4096
RESULT_FIELDS = {
    "prescription_id",
    "expected_exercise_id",
    "sets",
    "unit",
    "final_set_rir",
}


@dataclass(frozen=True)
class RequestRoute:
    scope: WorkoutScope
    workout_id: int | None = None


class InvalidRequest(Exception):
    """Malformed input with a fixed public error response."""


class InvalidHistoryRange(Exception):
    """The supplied local-date filter is invalid."""


class TracerHTTP:
    """Serve the three private operations using an explicitly supplied tracer."""

    def __init__(self, tracer: TracerContext) -> None:
        self._tracer = tracer

    def __call__(self, environ: WSGIEnvironment, start_response: StartResponse) -> list[bytes]:
        response = self._handle(environ)
        body = response.body.encode("utf-8")
        headers = [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
        ]
        if response.status == HTTPStatus.UNAUTHORIZED:
            headers.append(("WWW-Authenticate", "Bearer"))
        start_response(f"{response.status} {HTTPStatus(response.status).phrase}", headers)
        return [body]

    def _handle(self, environ: WSGIEnvironment) -> RecordedResponse:
        path = environ["PATH_INFO"]
        route = _route(environ["REQUEST_METHOD"], path)
        if route is None:
            return _error(404, "WORKOUT_OR_PRESCRIPTION_NOT_FOUND")
        try:
            owner_id, time_zone = self._tracer.context(_bearer(environ), route.scope)
            service = self._tracer.service
            if route.workout_id is not None:
                return record_with_replay(
                    service,
                    owner_id,
                    _read_result(environ, route.workout_id),
                    environ.get("HTTP_IDEMPOTENCY_KEY", ""),
                    uuid4(),
                    now=self._tracer.now(),
                    time_zone=time_zone,
                )
            if path == f"{WORKOUTS_PATH}/active":
                return _json_response(200, {"workout": service.active_scheduled_workout(owner_id)})
            today = self._tracer.now().astimezone(time_zone).date()
            start, end = _history_dates(environ.get("QUERY_STRING", ""), today)
            return _json_response(
                200,
                {
                    "workouts": service.performed_history(
                        owner_id, from_date=start, through_date=end
                    )
                },
            )
        except TracerAccessDenied as error:
            if str(error) == "INSUFFICIENT_SCOPE":
                return _error(403, "INSUFFICIENT_SCOPE")
            return _error(401, "UNAUTHENTICATED")
        except WorkoutRecordNotFound:
            return _error(404, "WORKOUT_OR_PRESCRIPTION_NOT_FOUND")
        except IdempotencyKeyReused:
            return _error(409, "IDEMPOTENCY_KEY_REUSED")
        except WorkoutStateConflict:
            return _error(409, "WORKOUT_STATE_CONFLICT")
        except InvalidHistoryRange:
            return _error(422, "INVALID_HISTORY_RANGE")
        except (GymError, InvalidRequest):
            return _error(422, "INVALID_RESULT")


def _route(method: str, path: str) -> RequestRoute | None:
    if method == "GET" and path in {f"{WORKOUTS_PATH}/active", f"{WORKOUTS_PATH}/history"}:
        return RequestRoute(WorkoutScope.READ)
    match = RESULT_PATH.fullmatch(path) if method == "POST" else None
    if match is not None:
        return RequestRoute(WorkoutScope.WRITE, int(match[1]))
    return None


def _bearer(environ: WSGIEnvironment) -> str | None:
    authorization: str = environ.get("HTTP_AUTHORIZATION", "")
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


def _history_dates(query: str, today: date) -> tuple[date | None, date | None]:
    try:
        if len(query) > 128:
            raise InvalidHistoryRange
        params = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=3)
        if params == {"day": ["today"]}:
            return today, today
        if params.keys() - {"from_date", "through_date"}:
            raise InvalidHistoryRange
        dates: dict[str, date] = {}
        for key, values in params.items():
            if len(values) != 1:
                raise InvalidHistoryRange
            parsed = date.fromisoformat(values[0])
            if parsed.isoformat() != values[0]:
                raise InvalidHistoryRange
            dates[key] = parsed
    except ValueError:
        raise InvalidHistoryRange from None
    start, end = dates.get("from_date"), dates.get("through_date")
    if start is not None and end is not None and start > end:
        raise InvalidHistoryRange
    return start, end


def _read_result(environ: WSGIEnvironment, workout_id: int) -> ScheduledStrengthResult:
    content_type = environ.get("CONTENT_TYPE", "").partition(";")[0].strip().lower()
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        if content_type != "application/json" or not 1 <= length <= MAX_BODY_BYTES:
            raise InvalidRequest
        raw = environ["wsgi.input"].read(length)
        if len(raw) != length:
            raise InvalidRequest
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise InvalidRequest from None
    if not isinstance(data, dict) or data.keys() - RESULT_FIELDS - {"technical_failure"}:
        raise InvalidRequest
    if RESULT_FIELDS - data.keys():
        raise InvalidRequest
    sets = data["sets"]
    if not isinstance(sets, list) or len(sets) != 3:
        raise InvalidRequest
    first, second, third = (_read_set(result_set) for result_set in sets)
    unit = data["unit"]
    technical_failure = data.get("technical_failure", False)
    if not isinstance(unit, str) or not 1 <= len(unit) <= 32 or type(technical_failure) is not bool:
        raise InvalidRequest
    return ScheduledStrengthResult(
        _integer(workout_id, 1),
        _integer(data["prescription_id"], 1),
        _integer(data["expected_exercise_id"], 1),
        (first[1], second[1], third[1]),
        (first[0], second[0], third[0]),
        unit,
        _integer(data["final_set_rir"], 0, 3),
        technical_failure,
    )


def _integer(value: Any, minimum: int, maximum: int = MAX_SQLITE_INTEGER) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise InvalidRequest
    return value


def _read_set(value: Any) -> tuple[int, Decimal]:
    if not isinstance(value, dict) or value.keys() != {"reps", "load"}:
        raise InvalidRequest
    load = value["load"]
    if not isinstance(load, str) or len(load) > 64 or DECIMAL_STRING.fullmatch(load) is None:
        raise InvalidRequest
    try:
        return _integer(value["reps"], 0), Decimal(load)
    except InvalidOperation:
        raise InvalidRequest from None


def _json_response(status: int, value: dict[str, Any]) -> RecordedResponse:
    return RecordedResponse(status, json.dumps(value, separators=(",", ":")))


def _error(status: int, code: str) -> RecordedResponse:
    return _json_response(status, {"error": {"code": code}})
