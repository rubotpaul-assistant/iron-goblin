"""Drive local MCP Streamable HTTP through the canonical fixture service."""

import json
import secrets
from io import BytesIO
from pathlib import Path
from typing import Any
from wsgiref.util import setup_testing_defaults
from wsgiref.validate import validator

import pytest

from gym_tracker.connector_fixture import ConnectorFixture, create_connector_fixture
from gym_tracker.tracer_mcp import ACTIVE_TOOL, HISTORY_TOOL, PROTOCOL_VERSION, RECORD_TOOL

ORIGIN = "http://127.0.0.1:8765"
CONTRACT = json.loads(
    (Path(__file__).resolve().parents[1] / "contracts" / "iron-tracer-openapi.json").read_text()
)


def send(
    app: ConnectorFixture,
    method: str,
    path: str,
    message: dict[str, Any] | bytes | None = None,
    **headers: str,
) -> tuple[int, bytes]:
    payload = message if isinstance(message, bytes) else json.dumps(message).encode()
    environment: dict[str, Any] = {}
    setup_testing_defaults(environment)
    environment.update(
        REQUEST_METHOD=method,
        PATH_INFO=path,
        QUERY_STRING="",
        CONTENT_TYPE="application/json",
        CONTENT_LENGTH=str(len(payload)),
    )
    environment.update(headers)
    environment["wsgi.input"] = BytesIO(payload)
    statuses: list[int] = []

    def capture(status: str, headers: list[tuple[str, str]], error: Any = None) -> None:
        assert dict(headers)["Cache-Control"] == "no-store"
        statuses.append(int(status.split()[0]))

    response = validator(app)(environment, capture)
    try:
        return statuses[0], b"".join(response)
    finally:
        response.close()


def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


def test_connector_fixture_round_trip_and_replay(tmp_path: Path) -> None:
    token = secrets.token_urlsafe(32)
    fixture = create_connector_fixture(
        tmp_path / "fixture.sqlite3", CONTRACT, token, allowed_origin=ORIGIN
    )
    status, body = send(fixture, "GET", "/healthz")
    assert (status, json.loads(body)) == (200, {"status": "ready"})
    initialized = send(fixture, "POST", "/mcp", request("initialize", {}), HTTP_ORIGIN=ORIGIN)
    assert json.loads(initialized[1])["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert send(
        fixture,
        "POST",
        "/mcp",
        {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        },
    ) == (202, b"")
    discovered = send(fixture, "POST", "/mcp", request("tools/list", {}))
    assert {tool["name"] for tool in json.loads(discovered[1])["result"]["tools"]} == {
        ACTIVE_TOOL,
        HISTORY_TOOL,
        RECORD_TOOL,
    }

    def call(tool: str, arguments: dict[str, Any], auth: str) -> dict[str, Any]:
        status, body = send(
            fixture,
            "POST",
            "/mcp",
            request("tools/call", {"name": tool, "arguments": arguments}),
            HTTP_AUTHORIZATION=f"Bearer {auth}",
            HTTP_MCP_PROTOCOL_VERSION=PROTOCOL_VERSION,
        )
        assert status == 200
        return json.loads(body)["result"]

    assert call(ACTIVE_TOOL, {}, "wrong-token")["structuredContent"] == {
        "error": {"code": "UNAUTHENTICATED"}
    }
    active = call(ACTIVE_TOOL, {}, token)["structuredContent"]["workout"]
    (prescription,) = active["prescriptions"]
    write = {
        "workout_id": active["workout_id"],
        "prescription_id": prescription["prescription_id"],
        "expected_exercise_id": prescription["exercise_id"],
        "unit": prescription["unit"],
        "sets": [{"reps": 5, "load": "50"}] * 3,
        "final_set_rir": 1,
        "idempotency_key": "fixture-retry",
    }
    first = call(RECORD_TOOL, write, token)
    assert call(RECORD_TOOL, write, token) == first
    (workout,) = call(HISTORY_TOOL, {"day": "today"}, token)["structuredContent"]["workouts"]
    assert workout["workout_id"] == active["workout_id"]
    assert len(workout["sets"]) == 3


def test_connector_fixture_rejects_external_origins_and_bad_messages(tmp_path: Path) -> None:
    token = secrets.token_urlsafe(32)
    fixture = create_connector_fixture(
        tmp_path / "fixture.sqlite3", CONTRACT, token, allowed_origin=ORIGIN
    )
    assert (
        send(
            fixture,
            "POST",
            "/mcp",
            request("tools/list", {}),
            HTTP_ORIGIN="https://other.example.test",
        )[0]
        == 403
    )
    assert send(fixture, "GET", "/mcp")[0] == 405
    assert send(fixture, "POST", "/healthz")[0] == 405
    assert send(fixture, "GET", "/unknown")[0] == 404
    assert (
        send(
            fixture, "POST", "/mcp", request("tools/list", {}), HTTP_MCP_PROTOCOL_VERSION="unknown"
        )[0]
        == 400
    )
    for message in (b"[", b"[]", b"x" * 20_000):
        assert send(fixture, "POST", "/mcp", message)[0] == 400
    assert send(fixture, "POST", "/mcp", request("tools/list", {}), CONTENT_LENGTH="5000")[0] == 400
    with pytest.raises(ValueError, match="at least 32 characters"):
        create_connector_fixture(tmp_path / "wrong.sqlite3", CONTRACT, "", allowed_origin=ORIGIN)
