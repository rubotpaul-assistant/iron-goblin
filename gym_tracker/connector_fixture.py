"""Loopback-only MCP connector rehearsal over the fixture strength service."""

import argparse
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from secrets import compare_digest
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, make_server
from wsgiref.types import StartResponse, WSGIEnvironment
from wsgiref.util import setup_testing_defaults

from gym_tracker.domain import Category, Orientation
from gym_tracker.strength_replay import RecordedResponse
from gym_tracker.tracer_auth import PrivateTracer, TracerBinding, VerifiedGrant, WorkoutScope
from gym_tracker.tracer_http import TracerHTTP
from gym_tracker.tracer_mcp import PROTOCOL_VERSION, TracerMCP, TracerRequest

MAX_MESSAGE_BYTES = 16_384


class ConnectorFixture:
    """Expose the private tracer through a stateless local MCP HTTP endpoint."""

    def __init__(
        self, tracer: PrivateTracer, openapi: dict[str, Any], *, allowed_origin: str
    ) -> None:
        self._http = TracerHTTP(tracer)
        self._mcp = TracerMCP(self._forward, openapi)
        self._allowed_origin = allowed_origin

    def __call__(self, environ: WSGIEnvironment, start_response: StartResponse) -> list[bytes]:
        status, response = self._message(environ)
        body = json.dumps(response, separators=(",", ":")).encode() if response is not None else b""
        headers = [
            ("Cache-Control", "no-store"),
            ("Content-Length", str(len(body))),
            ("Content-Type", "application/json"),
        ]
        if status == HTTPStatus.METHOD_NOT_ALLOWED:
            headers.append(("Allow", "GET" if environ["PATH_INFO"] == "/healthz" else "POST"))
        start_response(f"{status} {status.phrase}", headers)
        return [body]

    def _message(self, environ: WSGIEnvironment) -> tuple[HTTPStatus, dict[str, Any] | None]:
        origin = environ.get("HTTP_ORIGIN")
        if origin is not None and origin != self._allowed_origin:
            return HTTPStatus.FORBIDDEN, {"error": "ORIGIN_DENIED"}
        path, method = environ["PATH_INFO"], environ["REQUEST_METHOD"]
        if path == "/healthz":
            if method != "GET":
                return HTTPStatus.METHOD_NOT_ALLOWED, None
            return HTTPStatus.OK, {"status": "ready"}
        if path != "/mcp":
            return HTTPStatus.NOT_FOUND, None
        if method != "POST":
            return HTTPStatus.METHOD_NOT_ALLOWED, None
        if environ.get("HTTP_MCP_PROTOCOL_VERSION", PROTOCOL_VERSION) != PROTOCOL_VERSION:
            return HTTPStatus.BAD_REQUEST, {"error": "UNSUPPORTED_PROTOCOL_VERSION"}
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
            media_type = environ.get("CONTENT_TYPE", "").partition(";")[0].strip().lower()
            if media_type != "application/json" or not 1 <= length <= MAX_MESSAGE_BYTES:
                return HTTPStatus.BAD_REQUEST, {"error": "INVALID_MESSAGE"}
            raw = environ["wsgi.input"].read(length)
            if len(raw) != length:
                return HTTPStatus.BAD_REQUEST, {"error": "INVALID_MESSAGE"}
            request = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            return HTTPStatus.BAD_REQUEST, {"error": "INVALID_MESSAGE"}
        if not isinstance(request, dict):
            return HTTPStatus.BAD_REQUEST, {"error": "INVALID_MESSAGE"}
        response = self._mcp.dispatch(request, authorization=environ.get("HTTP_AUTHORIZATION"))
        return (HTTPStatus.OK, response) if response is not None else (HTTPStatus.ACCEPTED, None)

    def _forward(self, request: TracerRequest) -> RecordedResponse:
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

        parts.extend(self._http(environment, capture))
        return RecordedResponse(statuses[0], b"".join(parts).decode())


def create_connector_fixture(
    database_path: Path, openapi: dict[str, Any], access_token: str, *, allowed_origin: str
) -> ConnectorFixture:
    """Seed one synthetic scheduled workout in a fresh database."""
    if len(access_token) < 32:
        raise ValueError("fixture token must contain at least 32 characters")
    binding = TracerBinding("loopback-fixture", "synthetic-tester", "iron-connector-fixture", 1)
    grant = VerifiedGrant(
        binding.issuer,
        binding.subject,
        binding.audience,
        datetime.now(UTC) + timedelta(minutes=30),
        frozenset({WorkoutScope.READ, WorkoutScope.WRITE}),
        revoked=False,
    )
    token_bytes = access_token.encode()

    def verify(token: str) -> VerifiedGrant | None:
        return grant if compare_digest(token.encode(), token_bytes) else None

    tracer = PrivateTracer(database_path, binding, verify)
    tracer.service.add_equipment(
        "fixture machine", "machine", Decimal(10), Decimal(100), Decimal(10)
    )
    tracer.service.add_exercise(
        "fixture press",
        Category.PUSH,
        Orientation.VERTICAL,
        "machine",
        equipment_name="fixture machine",
    )
    tracer.service.begin_workout(scheduled=True)
    return ConnectorFixture(tracer, openapi, allowed_origin=allowed_origin)


class QuietFixtureHandler(WSGIRequestHandler):
    """Keep raw request lines and connection details out of fixture logs."""

    def log_message(self, _format: str, *args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--openapi", default=Path("contracts/iron-tracer-openapi.json"), type=Path)
    parser.add_argument("--port", default=8765, type=int)
    options = parser.parse_args()
    token = os.environ.get("IRON_CONNECTOR_FIXTURE_TOKEN", "")
    if len(token) < 32:
        parser.error(
            "set IRON_CONNECTOR_FIXTURE_TOKEN to a fixture token of at least 32 characters"
        )
    app = create_connector_fixture(
        options.database,
        json.loads(options.openapi.read_text()),
        token,
        allowed_origin=f"http://127.0.0.1:{options.port}",
    )
    with make_server("127.0.0.1", options.port, app, handler_class=QuietFixtureHandler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
