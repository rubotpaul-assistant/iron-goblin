"""Local MCP protocol bridge for the private HTTP tracer."""

import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from io import BytesIO
from typing import Any
from urllib.parse import urlencode
from wsgiref.types import WSGIEnvironment
from wsgiref.util import setup_testing_defaults

from gym_tracker.strength_replay import RecordedResponse
from gym_tracker.tracer_http import MAX_SQLITE_INTEGER, WORKOUTS_PATH, TracerHTTP

PROTOCOL_VERSION = "2025-11-25"
ACTIVE_TOOL = "iron_goblin_get_active_workout"
RECORD_TOOL = "iron_goblin_record_exercise_result"
HISTORY_TOOL = "iron_goblin_get_performed_history"
HISTORY_FILTERS = {"day", "from_date", "through_date"}


@dataclass(frozen=True)
class TracerRequest:
    method: str
    path: str
    authorization: str | None
    body: dict[str, Any] | None = None
    idempotency_key: str | None = None


def forward_wsgi(http: TracerHTTP, request: TracerRequest) -> RecordedResponse:
    environment: WSGIEnvironment = {}
    setup_testing_defaults(environment)
    path, _, query = request.path.partition("?")
    payload = json.dumps(request.body).encode() if request.body is not None else b""
    environment.update(
        REQUEST_METHOD=request.method,
        PATH_INFO=path,
        QUERY_STRING=query,
        CONTENT_TYPE="application/json",
        CONTENT_LENGTH=str(len(payload)),
    )
    environment["wsgi.input"] = BytesIO(payload)
    if request.authorization is not None:
        environment["HTTP_AUTHORIZATION"] = request.authorization
    if request.idempotency_key is not None:
        environment["HTTP_IDEMPOTENCY_KEY"] = request.idempotency_key
    statuses: list[int] = []
    parts: list[bytes] = []

    def capture(
        status: str, _headers: list[tuple[str, str]], _error: Any = None, /
    ) -> Callable[[bytes], object]:
        statuses.append(int(status.split()[0]))
        return parts.append

    parts.extend(http(environment, capture))
    return RecordedResponse(statuses[0], b"".join(parts).decode())


class TracerMCP:
    """Dispatch local JSON-RPC messages and forward authorized tool calls to HTTP."""

    def __init__(
        self, send_http: Callable[[TracerRequest], RecordedResponse], openapi: dict[str, Any]
    ) -> None:
        self._send_http = send_http
        self._tools = make_tools(openapi)

    def dispatch(
        self, request: dict[str, Any], *, authorization: str | None = None
    ) -> dict[str, Any] | None:
        """Handle a local protocol message. The transport supplies the bearer header."""
        request_id = request.get("id")
        method = request.get("method")
        if request.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return _protocol_error(request_id, -32600, "Invalid Request")
        if "id" not in request:
            return None

        if type(request_id) not in {int, str}:
            return _protocol_error(None, -32600, "Invalid Request")

        params = request.get("params", {})
        if not isinstance(params, dict):
            return _protocol_error(request_id, -32602, "Invalid params")
        result: dict[str, Any]
        if method == "initialize":
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "iron-goblin-private-tracer", "version": "1.0.0"},
            }
        elif method == "tools/list":
            result = {"tools": deepcopy(self._tools)}
        elif method == "ping":
            result = {}
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return _protocol_error(request_id, -32602, "Invalid tool call")
            tool_result = self._call_tool(name, arguments, authorization)
            if tool_result is None:
                return _protocol_error(request_id, -32602, "Invalid tool call")
            result = tool_result
        else:
            return _protocol_error(request_id, -32601, "Method not found")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _call_tool(
        self, name: str, arguments: dict[str, Any], authorization: str | None
    ) -> dict[str, Any] | None:
        if name == RECORD_TOOL:
            body = arguments.copy()
            workout_id = body.pop("workout_id", None)
            key = body.pop("idempotency_key", None)
            if type(workout_id) is not int or not 1 <= workout_id <= MAX_SQLITE_INTEGER:
                return None
            if not isinstance(key, str):
                return None
            request = TracerRequest(
                "POST", f"{WORKOUTS_PATH}/{workout_id}/exercise-results", authorization, body, key
            )
        elif name == ACTIVE_TOOL and not arguments:
            request = TracerRequest("GET", f"{WORKOUTS_PATH}/active", authorization)
        elif name == HISTORY_TOOL and not (arguments.keys() - HISTORY_FILTERS):
            if any(not isinstance(value, str) for value in arguments.values()):
                return None
            query = f"?{urlencode(arguments)}" if arguments else ""
            request = TracerRequest("GET", f"{WORKOUTS_PATH}/history{query}", authorization)
        else:
            return None

        response = self._send_http(request)
        return {
            "isError": response.status >= 400,
            "content": [{"type": "text", "text": response.body}],
            "structuredContent": json.loads(response.body),
            "_meta": {"creekmasons.com/http-status": response.status},
        }


def make_tools(openapi: dict[str, Any]) -> list[dict[str, Any]]:
    schemas = openapi["components"]["schemas"]
    post = openapi["paths"][f"{WORKOUTS_PATH}/{{workoutId}}/exercise-results"]["post"]
    parameters = {parameter["name"]: parameter["schema"] for parameter in post["parameters"]}
    history = openapi["paths"][f"{WORKOUTS_PATH}/history"]["get"]
    history_filters = {
        parameter["name"]: deepcopy(parameter["schema"]) for parameter in history["parameters"]
    }
    write_schema = deepcopy(schemas["ResultInput"])
    write_schema["$defs"] = {"SetInput": deepcopy(schemas["SetInput"])}
    write_schema["properties"]["sets"]["items"] = {"$ref": "#/$defs/SetInput"}
    write_schema["properties"]["workout_id"] = deepcopy(parameters["workoutId"])
    write_schema["properties"]["idempotency_key"] = deepcopy(parameters["Idempotency-Key"])
    write_schema["required"].extend(("workout_id", "idempotency_key"))
    return [
        {
            "name": ACTIVE_TOOL,
            "description": "Get the active scheduled workout and its prescriptions.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": RECORD_TOOL,
            "description": "Record three sets for a prescribed exercise. Reuse the key on retry.",
            "inputSchema": write_schema,
            "annotations": {
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": True,
            },
        },
        {
            "name": HISTORY_TOOL,
            "description": "Get performed workouts for today, a local-date range, or recent days.",
            "inputSchema": {
                "type": "object",
                "properties": history_filters,
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True},
        },
    ]


def _protocol_error(request_id: object, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
