"""Exercise the MCP protocol through HTTP, service and disposable SQLite."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_tracer_http import BINDING, FIXTURE_TOKEN, GRANT, NOW, seed, send

from gym_tracker.strength_replay import RecordedResponse
from gym_tracker.tracer_auth import PrivateTracer, VerifiedGrant, WorkoutScope
from gym_tracker.tracer_http import WORKOUTS_PATH, TracerHTTP
from gym_tracker.tracer_mcp import (
    ACTIVE_TOOL,
    HISTORY_TOOL,
    PROTOCOL_VERSION,
    RECORD_TOOL,
    TracerMCP,
    TracerRequest,
)

AUTHORIZATION = f"Bearer {FIXTURE_TOKEN}"
OPENAPI = json.loads(
    (Path(__file__).resolve().parents[1] / "contracts" / "iron-tracer-openapi.json").read_text()
)


@pytest.fixture
def grants() -> dict[str, VerifiedGrant]:
    return {FIXTURE_TOKEN: GRANT}


@pytest.fixture
def tracer(tmp_path: Path, grants: dict[str, VerifiedGrant]) -> PrivateTracer:
    tracer = PrivateTracer(tmp_path / "mcp.sqlite3", BINDING, grants.get, clock=lambda: NOW)
    seed(tracer.service)
    tracer.service.begin_workout(scheduled=True)
    return tracer


@pytest.fixture
def mcp(tracer: PrivateTracer) -> TracerMCP:
    api = TracerHTTP(tracer)

    def forward(request: TracerRequest) -> RecordedResponse:
        token = None
        if request.authorization is not None:
            scheme, token = request.authorization.split(" ", 1)
            assert scheme == "Bearer"
        status, body = send(
            api,
            request.method,
            request.path,
            request.body,
            token=token,
            key=request.idempotency_key,
        )
        return RecordedResponse(status, body.decode())

    return TracerMCP(forward, OPENAPI)


def request(
    mcp: TracerMCP,
    method: str,
    params: dict[str, Any],
    authorization: str | None = None,
) -> dict[str, Any]:
    message = {"jsonrpc": "2.0", "id": 7, "method": method, "params": params}
    response = mcp.dispatch(json.loads(json.dumps(message)), authorization=authorization)
    assert response is not None
    wire_response = json.loads(json.dumps(response))
    assert wire_response["id"] == 7
    assert wire_response["jsonrpc"] == "2.0"
    return wire_response


def call(mcp: TracerMCP, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return request(mcp, "tools/call", {"name": tool, "arguments": arguments}, AUTHORIZATION)[
        "result"
    ]


@pytest.fixture
def write(mcp: TracerMCP) -> dict[str, Any]:
    active = call(mcp, ACTIVE_TOOL, {})["structuredContent"]["workout"]
    (prescription,) = active["prescriptions"]
    return {
        "workout_id": active["workout_id"],
        "prescription_id": prescription["prescription_id"],
        "expected_exercise_id": prescription["exercise_id"],
        "sets": [{"reps": 5, "load": weight} for weight in ("70", "80", "90")],
        "unit": prescription["unit"],
        "final_set_rir": 1,
        "idempotency_key": "lost-mcp-response",
    }


def test_mcp_lifecycle_and_tool_discovery_match_openapi(mcp: TracerMCP) -> None:
    initialization = request(
        mcp,
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "fixture", "version": "1"},
        },
    )
    assert initialization["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert initialization["result"]["capabilities"]["tools"]["listChanged"] is False
    assert mcp.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert request(mcp, "ping", {})["result"] == {}
    tools = request(mcp, "tools/list", {})["result"]["tools"]
    assert request(mcp, "tools/list", {}, AUTHORIZATION)["result"]["tools"] == tools
    assert {tool["name"] for tool in tools} == {ACTIVE_TOOL, RECORD_TOOL, HISTORY_TOOL}
    (record,) = (tool for tool in tools if tool["name"] == RECORD_TOOL)
    body_schema = OPENAPI["components"]["schemas"]["ResultInput"]
    assert set(record["inputSchema"]["required"]) == (
        set(body_schema["required"]) | {"workout_id", "idempotency_key"}
    )
    assert set(record["inputSchema"]["properties"]) == (
        set(body_schema["properties"]) | {"workout_id", "idempotency_key"}
    )
    assert (
        record["inputSchema"]["$defs"]["SetInput"] == (OPENAPI["components"]["schemas"]["SetInput"])
    )
    assert all(
        FIXTURE_TOKEN not in json.dumps(tool) and "fixture press" not in json.dumps(tool)
        for tool in tools
    )


def test_lost_mcp_response_replays_original_http_body(
    mcp: TracerMCP, write: dict[str, Any], tracer: PrivateTracer
) -> None:
    # Retain the first server response as evidence and simulate the client losing it.
    first = call(mcp, RECORD_TOOL, write)
    retry = call(mcp, RECORD_TOOL, write)
    assert first == retry
    assert not retry["isError"]
    assert retry["_meta"]["creekmasons.com/http-status"] == 200
    body = retry["structuredContent"]
    assert json.loads(retry["content"][0]["text"]) == body
    assert body["prescription_id"] == write["prescription_id"]
    history = call(mcp, HISTORY_TOOL, {"day": "today"})
    assert call(mcp, HISTORY_TOOL, {"from_date": "2000-01-01", "through_date": "2000-01-01"})[
        "structuredContent"
    ] == {"workouts": []}
    assert call(mcp, HISTORY_TOOL, {"day": "tomorrow"})["structuredContent"] == {
        "error": {"code": "INVALID_HISTORY_RANGE"}
    }
    (workout,) = history["structuredContent"]["workouts"]
    assert workout["workout_id"] == body["workout_id"]
    assert workout["status"] == "active"
    assert [row["weight"] for row in workout["sets"]] == [70, 80, 90]
    assert {row["prescription_id"] for row in workout["sets"]} == {body["prescription_id"]}
    with tracer.service.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM sets").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM strength_audit_events").fetchone()[0] == 1
        (receipt,) = connection.execute(
            "SELECT client_key,response_body FROM strength_replay_receipts"
        ).fetchall()
        assert tuple(receipt) == (write["idempotency_key"], retry["content"][0]["text"])


def test_mcp_rechecks_scopes_and_revocation_on_retry(
    mcp: TracerMCP, write: dict[str, Any], grants: dict[str, VerifiedGrant]
) -> None:
    grants[FIXTURE_TOKEN] = replace(GRANT, scopes=frozenset({WorkoutScope.WRITE}))
    assert call(mcp, ACTIVE_TOOL, {})["structuredContent"]["error"]["code"] == "INSUFFICIENT_SCOPE"
    assert not call(mcp, RECORD_TOOL, write)["isError"]
    grants[FIXTURE_TOKEN] = replace(GRANT, scopes=frozenset({WorkoutScope.READ}))
    assert not call(mcp, HISTORY_TOOL, {})["isError"]
    denied = call(mcp, RECORD_TOOL, write)
    assert denied["isError"]
    assert denied["_meta"]["creekmasons.com/http-status"] == 403
    assert denied["structuredContent"]["error"]["code"] == "INSUFFICIENT_SCOPE"
    grants[FIXTURE_TOKEN] = replace(GRANT, revoked=True)
    revoked = call(mcp, RECORD_TOOL, write)
    assert revoked["isError"]
    assert revoked["_meta"]["creekmasons.com/http-status"] == 401
    assert revoked["structuredContent"]["error"]["code"] == "UNAUTHENTICATED"


def test_mcp_uses_transport_authorization_and_rejects_invalid_messages(
    mcp: TracerMCP,
    write: dict[str, Any],
) -> None:
    denied = request(
        mcp,
        "tools/call",
        {
            "name": RECORD_TOOL,
            "arguments": write,
            "authorization": AUTHORIZATION,
        },
    )["result"]
    assert denied["isError"]
    assert denied["_meta"]["creekmasons.com/http-status"] == 401

    invalid_calls = [
        {},
        {"name": ACTIVE_TOOL, "arguments": []},
        {"name": ACTIVE_TOOL, "arguments": {"account_id": 2}},
        {"name": HISTORY_TOOL, "arguments": {"from_date": 12}},
        {"name": RECORD_TOOL, "arguments": write | {"workout_id": True}},
        {"name": RECORD_TOOL, "arguments": write | {"idempotency_key": None}},
        {"name": "other"},
    ]
    for params in invalid_calls:
        assert request(mcp, "tools/call", params, AUTHORIZATION)["error"]["code"] == -32602
    assert request(mcp, "other", {})["error"]["code"] == -32601
    for message in ({"jsonrpc": "1.0", "id": 7}, {"jsonrpc": "2.0", "id": None, "method": "ping"}):
        response = mcp.dispatch(message)
        assert response is not None
        assert response["error"]["code"] == -32600
    response = mcp.dispatch({"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": []})
    assert response is not None
    assert response["error"]["code"] == -32602


def test_mcp_keeps_http_errors_and_target_path(mcp: TracerMCP, write: dict[str, Any]) -> None:
    payload = write | {"prescription_id": 99}
    miss = call(mcp, RECORD_TOOL, payload)
    assert miss["isError"]
    assert miss["content"] == [
        {"type": "text", "text": '{"error":{"code":"WORKOUT_OR_PRESCRIPTION_NOT_FOUND"}}'}
    ]
    forwarded: list[TracerRequest] = []

    def record_request(http_request: TracerRequest) -> RecordedResponse:
        forwarded.append(http_request)
        return RecordedResponse(200, "{}")

    other_mcp = TracerMCP(record_request, OPENAPI)
    call(other_mcp, RECORD_TOOL, write)
    (forwarded_request,) = forwarded
    assert forwarded_request.method == "POST"
    assert forwarded_request.path == f"{WORKOUTS_PATH}/{write['workout_id']}/exercise-results"
    assert forwarded_request.authorization == AUTHORIZATION
    assert forwarded_request.idempotency_key == write["idempotency_key"]
    assert forwarded_request.body is not None
    assert forwarded_request.body.keys() == write.keys() - {"workout_id", "idempotency_key"}
