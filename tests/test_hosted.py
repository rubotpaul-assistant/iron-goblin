import json
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from gym_tracker.db import Database
from gym_tracker.hosted import SAMPLE_TOOL, HostedAuthorizer, create_hosted_app
from gym_tracker.service import GymService
from gym_tracker.tracer_mcp import ACTIVE_TOOL, HISTORY_TOOL, RECORD_TOOL

ORIGIN = "https://mcp.example.test"
ENDPOINT = f"{ORIGIN}/iron-goblin"
ISSUER = "https://issuer.example.test"
AUTH = "http://accounts.example.test/api/internal/pilot/iron/authorize"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_iron_hosted_workout_isolated_and_revocable(tmp_path: Path) -> None:
    accounts = {
        "fixture-a": "e1983ba3-3de2-4822-ba81-a63c5ec74ec0",
        "fixture-b": "72d39b1f-5aaa-4470-a674-99a18f194b5c",
    }

    def authority(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == AUTH
        assert request.headers["X-Creekmasons-Service-Key"] == "fixture-service-key"
        presented = request.headers["Authorization"].removeprefix("Bearer ")
        if presented == "wrong-resource":
            return httpx.Response(401)
        if presented not in accounts:
            return httpx.Response(403)
        return httpx.Response(
            200,
            json={
                "account_id": accounts[presented],
                "time_zone": "Europe/London",
                "scopes": ["workouts:read", "workouts:write"],
            },
        )

    path = Path(__file__).resolve().parents[1] / "contracts" / "iron-tracer-openapi.json"
    service = GymService(Database(tmp_path / "iron.sqlite3"))
    with httpx.Client(transport=httpx.MockTransport(authority)) as upstream:
        authorizer = HostedAuthorizer(service, AUTH, "fixture-service-key", upstream)
        app = create_hosted_app(
            authorizer,
            json.loads(path.read_text()),
            audience=ENDPOINT,
            issuer=ISSUER,
            sample_enabled=True,
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client,
        ):
            assert (await client.get("/healthz")).json() == {"status": "ready"}
            unauthorized = await client.post("/iron-goblin")
            assert unauthorized.status_code == 401
            assert (
                "oauth-protected-resource/iron-goblin" in unauthorized.headers["WWW-Authenticate"]
            )
            client.headers["Authorization"] = "Bearer wrong-resource"
            assert (await client.post("/iron-goblin")).status_code == 401
            client.headers["Authorization"] = "Bearer fixture-a"
            async with (
                streamable_http_client(ENDPOINT, http_client=client) as (incoming, outgoing, _),
                ClientSession(incoming, outgoing) as session,
            ):
                await session.initialize()
                initial = await session.call_tool(ACTIVE_TOOL, {})
                assert initial.structuredContent == {"workout": None}
                rejected = await session.call_tool(SAMPLE_TOOL, {"confirmed": 1})
                assert rejected.structuredContent == {"error": {"code": "CONFIRMATION_REQUIRED"}}
                assert not (await session.call_tool(SAMPLE_TOOL, {"confirmed": True})).isError
                workout = (await session.call_tool(ACTIVE_TOOL, {})).structuredContent["workout"]
                (prescription,) = workout["prescriptions"]
                values = {
                    "workout_id": workout["workout_id"],
                    "prescription_id": prescription["prescription_id"],
                    "expected_exercise_id": prescription["exercise_id"],
                    "unit": prescription["unit"],
                    "sets": [{"reps": 5, "load": "50"}] * 3,
                    "final_set_rir": 1,
                    "idempotency_key": "fixture-retry",
                }
                result = await session.call_tool(RECORD_TOOL, values)
                assert not result.isError
                retry = await session.call_tool(RECORD_TOOL, values)
                assert retry.structuredContent == result.structuredContent
                history = await session.call_tool(HISTORY_TOOL, {"day": "today"})
                (first,) = history.structuredContent["workouts"]
                assert len(first["sets"]) == 3

            client.headers["Authorization"] = "Bearer fixture-b"
            async with (
                streamable_http_client(ENDPOINT, http_client=client) as (incoming, outgoing, _),
                ClientSession(incoming, outgoing) as session,
            ):
                await session.initialize()
                history = await session.call_tool(HISTORY_TOOL, {})
                assert history.structuredContent == {"workouts": []}
                error = await session.call_tool(RECORD_TOOL, values)
                assert error.structuredContent == {
                    "error": {"code": "WORKOUT_OR_PRESCRIPTION_NOT_FOUND"},
                }

            del accounts["fixture-a"]
            client.headers["Authorization"] = "Bearer fixture-a"
            assert (await client.post("/iron-goblin")).status_code == 403
    with service.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM strength_audit_events").fetchone()[0] == 1
