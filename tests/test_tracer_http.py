"""Exercise the fixture tracer through its WSGI HTTP contract."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any
from wsgiref.util import setup_testing_defaults
from wsgiref.validate import validator

import pytest
from typer.testing import CliRunner

from gym_tracker.cli import app
from gym_tracker.db import Database
from gym_tracker.domain import Category, Orientation
from gym_tracker.service import GymService
from gym_tracker.tracer_auth import PrivateTracer, TracerBinding, VerifiedGrant, WorkoutScope
from gym_tracker.tracer_http import WORKOUTS_PATH, TracerHTTP

NOW = datetime(2026, 11, 26, tzinfo=UTC)
BINDING = TracerBinding("fixture-issuer", "fixture-subject", "fixture-iron", 1)
GRANT = VerifiedGrant(
    BINDING.issuer,
    BINDING.subject,
    BINDING.audience,
    NOW + timedelta(minutes=5),
    frozenset({WorkoutScope.READ, WorkoutScope.WRITE}),
    revoked=False,
)
FIXTURE_TOKEN = "fixture-token"  # noqa: S105


def send(
    api: TracerHTTP,
    method: str,
    path: str,
    payload: dict[str, Any] | list[Any] | bytes | None = None,
    *,
    token: str | None = FIXTURE_TOKEN,
    key: str | None = None,
    content_type: str = "application/json",
) -> tuple[int, bytes]:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    environ: dict[str, Any] = {}
    setup_testing_defaults(environ)
    environ.update(
        REQUEST_METHOD=method,
        PATH_INFO=path,
        QUERY_STRING="",
        CONTENT_TYPE=content_type,
        CONTENT_LENGTH=str(len(body)),
    )
    environ["wsgi.input"] = BytesIO(body)
    if token is not None:
        environ["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    if key is not None:
        environ["HTTP_IDEMPOTENCY_KEY"] = key
    statuses: list[int] = []

    def start_response(status: str, headers: list[tuple[str, str]], error: Any = None) -> None:
        assert dict(headers)["Cache-Control"] == "no-store"
        statuses.append(int(status.split()[0]))

    response = validator(api)(environ, start_response)
    try:
        return statuses[0], b"".join(response)
    finally:
        response.close()


def seed(service: GymService) -> None:
    service.add_equipment("fixture machine", "machine", Decimal(10), Decimal(200), Decimal(10))
    service.add_exercise(
        "fixture press",
        Category.PUSH,
        Orientation.VERTICAL,
        "machine",
        equipment_name="fixture machine",
    )


@pytest.fixture
def grants() -> dict[str, VerifiedGrant]:
    return {FIXTURE_TOKEN: GRANT}


@pytest.fixture
def tracer(tmp_path: Path, grants: dict[str, VerifiedGrant]) -> PrivateTracer:
    instance = PrivateTracer(tmp_path / "fixture.sqlite3", BINDING, grants.get, clock=lambda: NOW)
    seed(instance.service)
    return instance


@pytest.fixture
def scheduled(tracer: PrivateTracer) -> tuple[str, dict[str, Any]]:
    tracer.service.begin_workout(scheduled=True)
    api = TracerHTTP(tracer)
    status, body = send(api, "GET", f"{WORKOUTS_PATH}/active")
    assert status == 200
    workout = json.loads(body)["workout"]
    (prescription,) = workout["prescriptions"]
    assert prescription["status"] == "prescribed"
    return f"{WORKOUTS_PATH}/{workout['workout_id']}/exercise-results", {
        "prescription_id": prescription["prescription_id"],
        "expected_exercise_id": prescription["exercise_id"],
        "sets": [{"reps": 5, "load": load} for load in ("70", "80", "90")],
        "unit": prescription["unit"],
        "final_set_rir": 1,
    }


def test_read_initial_fixture_and_read_contract(tracer: PrivateTracer) -> None:
    api = TracerHTTP(tracer)
    assert send(api, "GET", f"{WORKOUTS_PATH}/active") == (200, b'{"workout":null}')
    assert send(api, "GET", f"{WORKOUTS_PATH}/history") == (200, b'{"workouts":[]}')
    assert send(api, "GET", f"{WORKOUTS_PATH}/other")[0] == 404


def test_lost_http_response_replays_and_matches_cli(
    tracer: PrivateTracer,
    scheduled: tuple[str, dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = TracerHTTP(tracer)
    path, payload = scheduled
    first = send(api, "POST", path, payload, key="lost-response")
    # The client lost the original response. Retry using exactly the same request.
    retry = send(api, "POST", path, payload, key="lost-response")
    assert retry == first
    assert retry[0] == 200
    result = json.loads(retry[1])
    status, body = send(api, "GET", f"{WORKOUTS_PATH}/history")
    assert status == 200
    (workout,) = json.loads(body)["workouts"]
    assert workout["workout_id"] == result["workout_id"]
    assert workout["status"] == "active"
    assert [(row["weight"], row["completed_reps"]) for row in workout["sets"]] == [
        (70, 5),
        (80, 5),
        (90, 5),
    ]
    assert {row["prescription_id"] for row in workout["sets"]} == {result["prescription_id"]}
    with tracer.service.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM strength_audit_events").fetchone()[0] == 1

    cli_database = tmp_path / "cli.sqlite3"
    seed(GymService(Database(cli_database)))
    monkeypatch.setenv("GYM_DATABASE_PATH", str(cli_database))
    runner = CliRunner()
    assert runner.invoke(app, ["workout", "scheduled-begin"]).exit_code == 0
    cli_reply = runner.invoke(
        app,
        [
            "log",
            "exercise",
            "--exercise",
            "fixture press",
            "--weights",
            "70,80,90",
            "--reps",
            "5,5,5",
            "--final-set-rir",
            "1",
        ],
    )
    assert cli_reply.exit_code == 0, cli_reply.stdout
    cli = json.loads(cli_reply.stdout)
    assert (cli["next_weight"], cli["reason_code"], cli["set_weights"]) == (
        result["next_weight"],
        result["reason_code"],
        result["set_weights"],
    )
    cli_history = runner.invoke(app, ["history", "summary", "--user", "geoff", "--json"])
    assert cli_history.exit_code == 0
    assert json.loads(cli_history.stdout)["workouts"][0]["status"] == workout["status"]


def test_http_authorizes_before_replay_and_distinguishes_scopes(
    tracer: PrivateTracer, scheduled: tuple[str, dict[str, Any]], grants: dict[str, VerifiedGrant]
) -> None:
    api = TracerHTTP(tracer)
    path, payload = scheduled
    assert send(api, "POST", path, payload, key="original")[0] == 200
    grants[FIXTURE_TOKEN] = replace(GRANT, scopes=frozenset({WorkoutScope.READ}))
    assert send(api, "POST", path, payload, key="original") == (
        403,
        b'{"error":{"code":"INSUFFICIENT_SCOPE"}}',
    )
    assert send(api, "GET", f"{WORKOUTS_PATH}/history")[0] == 200
    grants[FIXTURE_TOKEN] = replace(GRANT, scopes=frozenset({WorkoutScope.WRITE}))
    assert send(api, "GET", f"{WORKOUTS_PATH}/active")[0] == 403
    grants[FIXTURE_TOKEN] = replace(GRANT, revoked=True)
    assert send(api, "POST", path, payload, key="original") == (
        401,
        b'{"error":{"code":"UNAUTHENTICATED"}}',
    )
    assert send(api, "GET", f"{WORKOUTS_PATH}/history", token=None)[0] == 401


def test_http_missing_and_foreign_errors_match(
    tracer: PrivateTracer, scheduled: tuple[str, dict[str, Any]]
) -> None:
    api = TracerHTTP(tracer)
    path, payload = scheduled
    missing = send(api, "POST", path, payload | {"prescription_id": 99}, key="key")
    with tracer.service.database.connect() as connection:
        connection.execute("UPDATE workout_sessions SET user_id=2")
    foreign = send(api, "POST", path, payload, key="key")
    assert foreign == missing == (404, b'{"error":{"code":"WORKOUT_OR_PRESCRIPTION_NOT_FOUND"}}')


def test_http_conflicts_are_stable(
    tracer: PrivateTracer, scheduled: tuple[str, dict[str, Any]]
) -> None:
    api = TracerHTTP(tracer)
    path, payload = scheduled
    assert send(api, "POST", path, payload, key="original")[0] == 200
    assert send(api, "POST", path, payload | {"final_set_rir": 0}, key="original") == (
        409,
        b'{"error":{"code":"IDEMPOTENCY_KEY_REUSED"}}',
    )
    assert send(api, "POST", path, payload, key="other") == (
        409,
        b'{"error":{"code":"WORKOUT_STATE_CONFLICT"}}',
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"account_id": 2},
        {"final_set_rir": True},
        {"final_set_rir": 4},
        {"unit": ""},
        {"sets": []},
        {"sets": [{"reps": 5, "load": "NaN"}] * 3},
        {"sets": [{"reps": 5, "load": "1e400"}] * 3},
        {"sets": [{"reps": 5, "load": "1e9999999999999999999999"}] * 3},
        {"sets": [{"reps": 5, "load": 90}] * 3},
        {"sets": [{"reps": -1, "load": "90"}] * 3},
        {"sets": [{"reps": 5, "load": "90", "extra": None}] * 3},
        {"technical_failure": 1},
    ],
)
def test_http_validates_input(
    tracer: PrivateTracer, scheduled: tuple[str, dict[str, Any]], changes: dict[str, Any]
) -> None:
    path, payload = scheduled
    assert send(TracerHTTP(tracer), "POST", path, payload | changes, key="key") == (
        422,
        b'{"error":{"code":"INVALID_RESULT"}}',
    )
    assert tracer.service.performed_history(1) == []


def test_http_rejects_missing_fields_key_and_invalid_content(
    tracer: PrivateTracer, scheduled: tuple[str, dict[str, Any]]
) -> None:
    api = TracerHTTP(tracer)
    path, payload = scheduled
    for data in ({}, [], b"{", b"x" * 4097):
        assert send(api, "POST", path, data, key="key")[0] == 422
    assert send(api, "POST", path, payload)[0] == 422
    assert send(api, "POST", path, payload, key="key", content_type="text/plain")[0] == 422


def test_openapi_documents_the_same_private_contract() -> None:
    contract = json.loads(
        (Path(__file__).resolve().parents[1] / "contracts" / "iron-tracer-openapi.json").read_text()
    )
    assert contract["openapi"] == "3.1.0"
    assert set(contract["paths"]) == {
        f"{WORKOUTS_PATH}/active",
        f"{WORKOUTS_PATH}/history",
        f"{WORKOUTS_PATH}/{{workoutId}}/exercise-results",
    }
    post = contract["paths"][f"{WORKOUTS_PATH}/{{workoutId}}/exercise-results"]["post"]
    assert post["x-required-scope"] == WorkoutScope.WRITE
    assert any(
        parameter["name"] == "Idempotency-Key" and parameter["required"]
        for parameter in post["parameters"]
    )
    for suffix in ("active", "history"):
        assert contract["paths"][f"{WORKOUTS_PATH}/{suffix}"]["get"]["x-required-scope"] == (
            WorkoutScope.READ
        )
