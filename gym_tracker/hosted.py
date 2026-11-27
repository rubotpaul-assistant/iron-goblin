"""Private Iron MCP process for the Creekmasons staging pilot."""

import os
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from sqlite3 import Error as SQLiteError
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import uvicorn
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from gym_tracker.db import Database
from gym_tracker.domain import Category, Orientation
from gym_tracker.service import GymError, GymService
from gym_tracker.tracer_auth import TracerAccessDenied, WorkoutScope
from gym_tracker.tracer_http import TracerHTTP
from gym_tracker.tracer_mcp import TracerMCP, forward_wsgi, make_tools

SAMPLE_TOOL = "iron_goblin_stage_sample_workout"


class AccountDecision(BaseModel):
    account_id: UUID
    time_zone: str
    scopes: list[WorkoutScope]


class DecisionError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status


@dataclass(frozen=True)
class AuthorizedOwner:
    service: GymService
    owner_id: int
    time_zone: ZoneInfo
    scopes: frozenset[WorkoutScope]

    def now(self) -> datetime:
        return datetime.now(UTC)

    def context(self, _token: str | None, scope: WorkoutScope) -> tuple[int, ZoneInfo]:
        if scope not in self.scopes:
            raise TracerAccessDenied("INSUFFICIENT_SCOPE")
        return self.owner_id, self.time_zone

    def sample(self) -> dict[str, Any]:
        self.context(None, WorkoutScope.WRITE)
        equipment_name = "Staging fixture machine"
        exercise_name = "Staging fixture press"
        if not self.service.search_exercises(exercise_name, owner_id=self.owner_id)["matches"]:
            self.service.add_equipment(
                equipment_name,
                "machine",
                Decimal(10),
                Decimal(100),
                Decimal(10),
                owner_id=self.owner_id,
            )
            self.service.add_exercise(
                exercise_name,
                Category.PUSH,
                Orientation.VERTICAL,
                "machine",
                equipment_name=equipment_name,
                owner_id=self.owner_id,
            )
        return self.service.begin_workout(scheduled=True, owner_id=self.owner_id)


@dataclass(frozen=True)
class HostedAuthorizer:
    service: GymService
    decision_url: str
    service_key: str
    client: httpx.Client

    def authorize(self, authorization: str) -> AuthorizedOwner:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token or any(char.isspace() for char in token):
            raise DecisionError(401, "UNAUTHENTICATED")
        try:
            response = self.client.post(
                self.decision_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Creekmasons-Service-Key": self.service_key,
                },
            )
            if response.status_code == 401:
                raise DecisionError(401, "UNAUTHENTICATED")
            if response.status_code == 403:
                raise DecisionError(403, "ACCESS_DENIED")
            response.raise_for_status()
            account = AccountDecision.model_validate_json(response.content)
            zone = ZoneInfo(account.time_zone)
            slug = f"pilot-{account.account_id}"
            with self.service.database.connect() as connection:
                connection.execute(
                    """INSERT INTO users (slug, display_name) VALUES (?, 'Pilot account')
                       ON CONFLICT(slug) DO NOTHING""",
                    (slug,),
                )
                owner = connection.execute(
                    "SELECT id FROM users WHERE slug=? AND active=1", (slug,)
                ).fetchone()
        except (httpx.HTTPError, SQLiteError, ValidationError, ZoneInfoNotFoundError) as exc:
            raise DecisionError(503, "UNAVAILABLE") from exc
        if owner is None:
            raise DecisionError(403, "ACCESS_DENIED")
        return AuthorizedOwner(self.service, int(owner["id"]), zone, frozenset(account.scopes))


def _tool_error(code: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[], structuredContent={"error": {"code": code}}, isError=True
    )


def create_hosted_app(
    authorizer: HostedAuthorizer,
    openapi: dict[str, Any],
    *,
    audience: str,
    issuer: str,
    sample_enabled: bool = False,
) -> Starlette:
    resource = urlsplit(audience)
    origin = f"{resource.scheme}://{resource.netloc}"
    metadata_path = "/.well-known/oauth-protected-resource" + resource.path
    server: Server[None, Request] = Server("Iron Goblin")
    tools = [types.Tool.model_validate(tool) for tool in make_tools(openapi)]
    if sample_enabled:
        tools.append(
            types.Tool(
                name=SAMPLE_TOOL,
                description="Prepare a disposable workout for the staging rehearsal. "
                "Use only when the tester explicitly requests synthetic data.",
                inputSchema={
                    "type": "object",
                    "properties": {"confirmed": {"type": "boolean", "const": True}},
                    "required": ["confirmed"],
                    "additionalProperties": False,
                },
                annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False),
            )
        )

    async def list_tools() -> list[types.Tool]:
        return tools

    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        request = server.request_context.request
        if request is None:
            return _tool_error("UNAUTHENTICATED")
        owner = cast(AuthorizedOwner, request.state.iron_owner)
        if name == SAMPLE_TOOL and sample_enabled:
            if arguments.keys() != {"confirmed"} or arguments["confirmed"] is not True:
                return _tool_error("CONFIRMATION_REQUIRED")
            try:
                sample = await run_in_threadpool(owner.sample)
            except TracerAccessDenied:
                return _tool_error("INSUFFICIENT_SCOPE")
            except (SQLiteError, GymError):
                return _tool_error("UNAVAILABLE")
            return types.CallToolResult(content=[], structuredContent=sample)
        http = TracerHTTP(owner)
        bridge = TracerMCP(lambda forwarded: forward_wsgi(http, forwarded), openapi)
        message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        try:
            result: dict[str, Any] | None = await run_in_threadpool(bridge.dispatch, message)
        except SQLiteError:
            return _tool_error("UNAVAILABLE")
        if result is None or "result" not in result:
            return _tool_error("INVALID_INPUT")
        return types.CallToolResult.model_validate(result["result"])

    server.list_tools()(list_tools)  # type: ignore[no-untyped-call]
    server.call_tool(validate_input=False)(call_tool)

    manager = StreamableHTTPSessionManager(
        app=server,
        stateless=True,
        json_response=True,
        security_settings=TransportSecuritySettings(
            allowed_hosts=[resource.netloc], allowed_origins=[origin]
        ),
        max_request_body_size=64 * 1024,
    )

    async def transport(scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive=receive)
        try:
            request.state.iron_owner = await run_in_threadpool(
                authorizer.authorize, request.headers.get("Authorization", "")
            )
        except DecisionError as exc:
            headers = (
                {"WWW-Authenticate": f'Bearer resource_metadata="{origin}{metadata_path}"'}
                if exc.status == 401
                else None
            )
            response = JSONResponse(
                {"error": {"code": str(exc)}}, status_code=exc.status, headers=headers
            )
            await response(scope, receive, send)
            return
        await manager.handle_request(scope, receive, send)

    async def metadata(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "resource": audience,
                "authorization_servers": [issuer],
                "scopes_supported": list(WorkoutScope),
                "bearer_methods_supported": ["header"],
            }
        )

    async def health(_request: Request) -> JSONResponse:
        try:
            with authorizer.service.database.connect() as connection:
                connection.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        except SQLiteError:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    # A callable instance is needed so Starlette passes the ASGI scope to transport.
    class AuthorizedTransport:
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await transport(scope, receive, send)

    return Starlette(
        lifespan=lifespan,
        routes=[
            Route(resource.path, AuthorizedTransport(), methods=["GET", "POST", "DELETE"]),
            Route(metadata_path, metadata),
            Route("/healthz", health),
        ],
    )


def main() -> None:
    import json

    os.umask(0o077)
    path = Path(os.environ["IRON_DATABASE_PATH"])
    issuer = os.environ["CLERK_ISSUER"]
    audience = os.environ["IRON_RESOURCE_URL"]
    origin = os.environ["CREEKMASONS_PRIVATE_ORIGIN"].rstrip("/")
    service_key = os.environ["CREEKMASONS_IRON_SERVICE_KEY"]
    resource, upstream, identity = urlsplit(audience), urlsplit(origin), urlsplit(issuer)
    if (
        not path.is_absolute()
        or resource.scheme != "https"
        or not resource.hostname
        or resource.path != "/iron-goblin"
        or resource.query
        or resource.fragment
        or identity.scheme != "https"
        or not identity.hostname
        or identity.path
        or upstream.scheme not in ("http", "https")
        or not upstream.hostname
        or upstream.username is not None
        or upstream.path
        or upstream.query
        or upstream.fragment
        or len(service_key) < 32
    ):
        raise ValueError("Invalid private Iron staging configuration")
    openapi = json.loads(Path("contracts/iron-tracer-openapi.json").read_text())
    service = GymService(Database(path))
    with httpx.Client(trust_env=False, timeout=5, follow_redirects=False) as client:
        authorizer = HostedAuthorizer(
            service, origin + "/api/internal/pilot/iron/authorize", service_key, client
        )
        app = create_hosted_app(
            authorizer,
            openapi,
            audience=audience,
            issuer=issuer,
            sample_enabled=os.environ.get("IRON_STAGING_SAMPLE") == "1",
        )
        host = os.environ.get("IRON_BIND_HOST", "::")
        port = int(os.environ.get("PORT", "8080"))
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            access_log=False,
            proxy_headers=False,
        )
        server = uvicorn.Server(config)
        if host != "::":
            server.run()
            return
        # asyncio's default IPv6 socket is IPv6-only. Railway health checks can
        # arrive over IPv4 while private services reach the listener over IPv6.
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            listener.bind((host, port))
            server.run(sockets=[listener])


if __name__ == "__main__":
    main()
