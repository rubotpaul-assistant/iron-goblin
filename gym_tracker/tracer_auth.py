"""Authorization boundary for the private, single-account tracer."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from gym_tracker.db import Database
from gym_tracker.service import GymService


class WorkoutScope(StrEnum):
    READ = "workouts:read"
    WRITE = "workouts:write"


@dataclass(frozen=True)
class VerifiedGrant:
    """Claims and current grant state supplied by a trusted verifier."""

    issuer: str
    subject: str
    audience: str
    expires_at: datetime
    scopes: frozenset[str]
    revoked: bool


class GrantVerifier(Protocol):
    def __call__(self, token: str, /) -> VerifiedGrant | None:
        """Verify a token and check current revocation state on every call."""


@dataclass(frozen=True)
class TracerBinding:
    issuer: str
    subject: str
    audience: str
    owner_id: int


class TracerAccessDenied(Exception):
    """Public denial code with no credential, grant, or account details."""


class PrivateTracer:
    """Use an explicit fixture database and server-controlled owner binding."""

    def __init__(
        self,
        database_path: Path,
        binding: TracerBinding,
        verify_grant: GrantVerifier,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        database_path.touch(mode=0o600, exist_ok=False)
        self.service = GymService(Database(database_path))
        self._binding = binding
        self._verify_grant = verify_grant
        self._clock = clock

    def authorize(self, token: str | None, scope: WorkoutScope) -> int:
        """Authorize each request before looking up domain records or replay receipts."""
        grant = self._verify_grant(token) if token else None
        binding = self._binding
        if (
            grant is None
            or grant.revoked
            or grant.issuer != binding.issuer
            or grant.subject != binding.subject
            or grant.audience != binding.audience
            or grant.expires_at.utcoffset() is None
            or grant.expires_at <= self._clock()
        ):
            raise TracerAccessDenied("UNAUTHENTICATED")
        if scope not in grant.scopes:
            raise TracerAccessDenied("INSUFFICIENT_SCOPE")
        with self.service.database.connect() as connection:
            owner = connection.execute(
                "SELECT id FROM users WHERE id=? AND active=1", (binding.owner_id,)
            ).fetchone()
        if owner is None:
            raise TracerAccessDenied("UNAUTHENTICATED")
        return binding.owner_id
