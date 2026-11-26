"""Exercise local pilot limits, privacy-safe telemetry, and incident revocation."""

import json
import logging
import secrets
from pathlib import Path
from uuid import uuid4

import pytest
from test_tracer_http import BINDING, FIXTURE_TOKEN, GRANT, NOW, send

from gym_tracker.pilot_account import (
    AccessPolicy,
    Account,
    Consent,
    PilotAccessDenied,
    Product,
    VerifiedAccess,
)
from gym_tracker.pilot_controls import (
    GuardedPilotEndpoint,
    PilotEndpoint,
    PilotEvent,
    PilotMetrics,
    PilotOutcome,
    PilotRateLimiter,
    WindowLimit,
)
from gym_tracker.pilot_invites import ProductInvites, VerifiedRecipient
from gym_tracker.tracer_auth import PrivateTracer
from gym_tracker.tracer_http import WORKOUTS_PATH, TracerHTTP


@pytest.fixture
def metrics(caplog: pytest.LogCaptureFixture) -> PilotMetrics:
    caplog.set_level(logging.INFO, logger="fixture-pilot")
    return PilotMetrics(secrets.token_bytes(32), logging.getLogger("fixture-pilot"))


def test_ingress_limit_is_visible_without_recording_request_fields(
    tmp_path: Path, metrics: PilotMetrics, caplog: pytest.LogCaptureFixture
) -> None:
    instant = [0.0]
    limiter = PilotRateLimiter(
        {
            PilotEndpoint.MCP: WindowLimit(30, 2, 4),
            PilotEndpoint.INVITES: WindowLimit(60, 1, 2),
        },
        clock=lambda: instant[0],
    )
    issue = "fixture-private-identity@example.test"
    tracer = PrivateTracer(
        tmp_path / "tracer.sqlite3", BINDING, {FIXTURE_TOKEN: GRANT}.get, clock=lambda: NOW
    )
    api = GuardedPilotEndpoint(
        TracerHTTP(tracer), Product.IRON, PilotEndpoint.MCP, limiter, metrics
    )
    path = f"{WORKOUTS_PATH}/history"
    assert send(api, "GET", path)[0] == 200
    assert send(api, "GET", path, token=issue)[0] == 401
    assert send(api, "GET", path, token=issue) == (429, b'{"error":{"code":"RATE_LIMITED"}}')
    assert metrics.counts(Product.IRON) == {(PilotEvent.INGRESS, PilotOutcome.RATE_LIMITED): 1}
    assert metrics.counts(Product.MACRO) == {}
    assert issue not in caplog.text
    assert FIXTURE_TOKEN not in caplog.text
    assert path not in caplog.text
    assert json.loads(caplog.records[-1].getMessage()).keys() == {
        "product",
        "event",
        "outcome",
        "request_id",
    }
    instant[0] = 30.0
    assert send(api, "GET", path)[0] == 200


def test_limits_isolate_product_and_endpoint_and_bound_source_count() -> None:
    limiter = PilotRateLimiter(
        {
            PilotEndpoint.INVITES: WindowLimit(60, 1, 2),
            PilotEndpoint.MCP: WindowLimit(60, 1, 2),
        },
        clock=lambda: 0.0,
    )
    assert limiter.retry_after(Product.IRON, PilotEndpoint.INVITES, "a") == 0
    assert limiter.retry_after(Product.IRON, PilotEndpoint.INVITES, "a") == 60
    assert limiter.retry_after(Product.IRON, PilotEndpoint.INVITES, "b") == 0
    assert limiter.retry_after(Product.IRON, PilotEndpoint.INVITES, "c") == 60
    assert limiter.retry_after(Product.IRON, PilotEndpoint.MCP, "a") == 0
    assert limiter.retry_after(Product.MACRO, PilotEndpoint.INVITES, "a") == 0
    with pytest.raises(ValueError, match="invalid pilot rate limit"):
        WindowLimit(60, 3, 2)


def test_privacy_safe_milestones_and_replay(
    metrics: PilotMetrics, caplog: pytest.LogCaptureFixture
) -> None:
    subject = "fixture identity with a private note"
    for product, event in (
        (Product.IRON, PilotEvent.WRITE),
        (Product.IRON, PilotEvent.REPLAY),
        (Product.IRON, PilotEvent.WRITE),
        (Product.MACRO, PilotEvent.WRITE),
    ):
        metrics.record(product, event, PilotOutcome.OK, uuid4(), account_id=subject)
    assert metrics.counts(Product.IRON) == {
        (PilotEvent.WRITE, PilotOutcome.OK): 2,
        (PilotEvent.FIRST_WRITE, PilotOutcome.OK): 1,
        (PilotEvent.REPLAY, PilotOutcome.OK): 1,
        (PilotEvent.REPEAT_WRITE, PilotOutcome.OK): 1,
    }
    assert metrics.counts(Product.MACRO)[(PilotEvent.FIRST_WRITE, PilotOutcome.OK)] == 1
    identities = [json.loads(record.getMessage())["account"] for record in caplog.records]
    assert len(set(identities[:3])) == 1
    assert identities[0] != identities[-1]
    assert subject not in caplog.text
    with pytest.raises(ValueError, match="must be a UUID"):
        metrics.record(Product.IRON, PilotEvent.WRITE, PilotOutcome.OK, subject)
    with pytest.raises(ValueError, match="at least 32 bytes"):
        PilotMetrics(b"", logging.getLogger("fixture-pilot"))


def test_incident_drill_revokes_affected_grant_without_logging_identity(
    tmp_path: Path, metrics: PilotMetrics, caplog: pytest.LogCaptureFixture
) -> None:
    store = ProductInvites(tmp_path / "invites.sqlite3", secrets.token_bytes(32))
    account = Account("fixture-affected-account", "UTC")
    email = "affected@example.test"
    invites = {
        product: store.issue(product, email, expires_at=GRANT.expires_at) for product in Product
    }
    for invite in invites.values():
        store.redeem(invite.token, VerifiedRecipient(account, email), now=NOW)

    def probe(product: Product) -> None:
        scope = "workouts:read" if product == Product.IRON else "nutrition:read"
        claims = VerifiedAccess(
            "fixture-issuer",
            "fixture-subject",
            product.value,
            "client",
            "session",
            GRANT.expires_at,
            frozenset({scope}),
        )
        policy = AccessPolicy(
            claims.issuer,
            {product: claims.audience},
            {(claims.issuer, claims.subject): account},
            store.active_grants(),
            {"session": Consent(account.account_id, product, claims.client_id, claims.scopes)},
        )
        try:
            admitted = policy.authorize(claims, product, scope, now=NOW)
        except PilotAccessDenied:
            metrics.record(product, PilotEvent.CONNECT, PilotOutcome.DENIED, uuid4())
            raise
        metrics.record(
            product, PilotEvent.CONNECT, PilotOutcome.OK, uuid4(), account_id=admitted.account_id
        )

    probe(Product.IRON)
    store.revoke(invites[Product.IRON].id)
    with pytest.raises(PilotAccessDenied, match="ACCESS_DENIED"):
        probe(Product.IRON)
    probe(Product.MACRO)
    assert metrics.counts(Product.IRON)[(PilotEvent.CONNECT, PilotOutcome.DENIED)] == 1
    assert metrics.counts(Product.MACRO)[(PilotEvent.CONNECT, PilotOutcome.OK)] == 1
    assert all(
        value not in caplog.text
        for value in (
            account.account_id,
            email,
            invites[Product.IRON].token,
            invites[Product.MACRO].token,
        )
    )
