"""Exercise the private tracer authorization with fake grants and real SQLite."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gym_tracker.tracer_auth import (
    PrivateTracer,
    TracerAccessDenied,
    TracerBinding,
    VerifiedGrant,
    WorkoutScope,
)

NOW = datetime(2026, 11, 26, tzinfo=UTC)
BINDING = TracerBinding("fixture-issuer", "fixture-subject", "iron-fixture", 2)
GRANT = VerifiedGrant(
    BINDING.issuer,
    BINDING.subject,
    BINDING.audience,
    NOW + timedelta(minutes=5),
    frozenset({WorkoutScope.READ, WorkoutScope.WRITE}),
    revoked=False,
)
FIXTURE_TOKEN = "fixture-token"  # noqa: S105


@pytest.fixture
def grants() -> dict[str, VerifiedGrant]:
    return {FIXTURE_TOKEN: GRANT}


@pytest.fixture
def tracer(tmp_path: Path, grants: dict[str, VerifiedGrant]) -> PrivateTracer:
    return PrivateTracer(tmp_path / "tracer.sqlite3", BINDING, grants.get, clock=lambda: NOW)


@pytest.mark.parametrize(
    "grant",
    [
        replace(GRANT, issuer="untrusted"),
        replace(GRANT, subject="unbound"),
        replace(GRANT, audience="other-product"),
        replace(GRANT, expires_at=NOW),
        replace(GRANT, expires_at=NOW - timedelta(seconds=1)),
        replace(GRANT, expires_at=NOW.replace(tzinfo=None)),
        replace(GRANT, revoked=True),
    ],
)
def test_ineligible_grant_is_denied(
    tracer: PrivateTracer, grants: dict[str, VerifiedGrant], grant: VerifiedGrant
) -> None:
    grants[FIXTURE_TOKEN] = grant
    with pytest.raises(TracerAccessDenied, match=r"^UNAUTHENTICATED$"):
        tracer.authorize(FIXTURE_TOKEN, WorkoutScope.READ)


@pytest.mark.parametrize("token", [None, "", "unrecognized"])
def test_missing_grant_is_denied(tracer: PrivateTracer, token: str | None) -> None:
    with pytest.raises(TracerAccessDenied, match=r"^UNAUTHENTICATED$"):
        tracer.authorize(token, WorkoutScope.READ)


@pytest.mark.parametrize(
    ("allowed", "denied"),
    [(WorkoutScope.READ, WorkoutScope.WRITE), (WorkoutScope.WRITE, WorkoutScope.READ)],
)
def test_scopes_are_independent(
    tracer: PrivateTracer,
    grants: dict[str, VerifiedGrant],
    allowed: WorkoutScope,
    denied: WorkoutScope,
) -> None:
    grants[FIXTURE_TOKEN] = replace(GRANT, scopes=frozenset({allowed}))
    assert tracer.authorize(FIXTURE_TOKEN, allowed) == BINDING.owner_id
    with pytest.raises(TracerAccessDenied, match=r"^INSUFFICIENT_SCOPE$"):
        tracer.authorize(FIXTURE_TOKEN, denied)


def test_authorization_is_checked_again_after_revocation(
    tracer: PrivateTracer, grants: dict[str, VerifiedGrant]
) -> None:
    assert tracer.authorize(FIXTURE_TOKEN, WorkoutScope.WRITE) == BINDING.owner_id
    grants[FIXTURE_TOKEN] = replace(GRANT, revoked=True)
    with pytest.raises(TracerAccessDenied, match=r"^UNAUTHENTICATED$"):
        tracer.authorize(FIXTURE_TOKEN, WorkoutScope.WRITE)


def test_disabled_owner_is_denied(tracer: PrivateTracer) -> None:
    with tracer.service.database.connect() as connection:
        connection.execute("UPDATE users SET active=0 WHERE id=?", (BINDING.owner_id,))
    with pytest.raises(TracerAccessDenied, match=r"^UNAUTHENTICATED$"):
        tracer.authorize(FIXTURE_TOKEN, WorkoutScope.READ)


def test_tracer_database_is_independent_of_cli_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    grants: dict[str, VerifiedGrant],
) -> None:
    cli_database = tmp_path / "cli.sqlite3"
    fixture_database = tmp_path / "tracer.sqlite3"
    monkeypatch.setenv("GYM_DATABASE_PATH", str(cli_database))
    monkeypatch.setenv("GYM_SPREADSHEET_ID", "fixture-sheet")
    tracer = PrivateTracer(fixture_database, BINDING, grants.get)
    with pytest.raises(TracerAccessDenied, match=r"^UNAUTHENTICATED$"):
        tracer.authorize(None, WorkoutScope.READ)
    assert fixture_database.exists()
    assert not cli_database.exists()
    assert not caplog.records


def test_tracer_refuses_to_open_an_existing_database(
    tmp_path: Path, grants: dict[str, VerifiedGrant]
) -> None:
    database_path = tmp_path / "existing.sqlite3"
    database_path.write_bytes(b"existing content")
    with pytest.raises(FileExistsError):
        PrivateTracer(database_path, BINDING, grants.get)
    assert database_path.read_bytes() == b"existing content"
