"""Exercise shared account sign-in and invites with verified session fixtures."""

import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gym_tracker.pilot_account import PilotAccessDenied, Product
from gym_tracker.pilot_invites import InviteUnavailable, ProductInvites
from gym_tracker.pilot_signin import PilotSignIn, VerifiedSignIn

NOW = datetime(2026, 11, 26, 12, tzinfo=UTC)
EXPIRES = NOW + timedelta(minutes=5)
ISSUER = "https://accounts.example.test"
AUDIENCE = "creek-masons-fixture"
TESTER = VerifiedSignIn(ISSUER, "fixture-tester", AUDIENCE, EXPIRES, "tester@example.test")
INSTRUCTIONS = {
    Product.IRON: "Iron fixture instructions",
    Product.MACRO: "Macro fixture instructions",
}


@pytest.fixture
def sessions() -> dict[str, VerifiedSignIn]:
    return {
        "tester": TESTER,
        "second-client": TESTER,
        "operator": replace(TESTER, subject="fixture-operator", verified_email=None),
        "wrong-account": replace(
            TESTER, subject="other-subject", verified_email="other@example.test"
        ),
    }


@pytest.fixture
def sign_in(tmp_path: Path, sessions: dict[str, VerifiedSignIn]) -> PilotSignIn:
    invites = ProductInvites(tmp_path / "invites.sqlite3", secrets.token_bytes(32))
    return PilotSignIn(
        tmp_path / "accounts.sqlite3",
        invites,
        sessions.get,
        issuer=ISSUER,
        audience=AUDIENCE,
        operator_subjects=frozenset({"fixture-operator"}),
        connection_instructions=INSTRUCTIONS,
        clock=lambda: NOW,
    )


def test_fresh_tester_signs_in_and_connects_two_independently_granted_products(
    sign_in: PilotSignIn,
) -> None:
    iron = sign_in.issue("operator", Product.IRON, "tester@example.test", expires_at=EXPIRES)
    macro = sign_in.issue("operator", Product.MACRO, "tester@example.test", expires_at=EXPIRES)
    with pytest.raises(PilotAccessDenied, match="TIME_ZONE_REQUIRED"):
        sign_in.redeem("tester", iron.token)
    with pytest.raises(ValueError, match="invalid account time zone"):
        sign_in.sign_in("tester", confirmed_zone="America/Nowhere")
    initial = sign_in.sign_in("tester", confirmed_zone="America/Los_Angeles")
    assert initial.connections == {}
    assert initial.account.time_zone == "America/Los_Angeles"

    first = sign_in.redeem("tester", iron.token)
    assert first.account == initial.account
    assert first.connections == {Product.IRON: INSTRUCTIONS[Product.IRON]}
    both = sign_in.redeem("second-client", macro.token)
    assert both.account == first.account
    assert both.connections == INSTRUCTIONS
    with pytest.raises(InviteUnavailable):
        sign_in.redeem("tester", iron.token)

    sign_in.revoke("operator", iron.id)
    updated = sign_in.sign_in("second-client", confirmed_zone="Europe/London")
    assert updated.account.account_id == initial.account.account_id
    assert updated.account.time_zone == "Europe/London"
    assert updated.connections == {Product.MACRO: INSTRUCTIONS[Product.MACRO]}


def test_only_the_configured_operator_and_verified_recipient_can_act(sign_in: PilotSignIn) -> None:
    with pytest.raises(PilotAccessDenied, match="ACCESS_DENIED"):
        sign_in.issue("tester", Product.IRON, "tester@example.test", expires_at=EXPIRES)
    invite = sign_in.issue("operator", Product.IRON, "tester@example.test", expires_at=EXPIRES)
    with pytest.raises(PilotAccessDenied, match="ACCESS_DENIED"):
        sign_in.revoke("wrong-account", invite.id)
    other = sign_in.sign_in("wrong-account", confirmed_zone="UTC")
    expected = sign_in.sign_in("tester", confirmed_zone="UTC")
    assert other.account.account_id != expected.account.account_id
    with pytest.raises(InviteUnavailable):
        sign_in.redeem("wrong-account", invite.token)
    assert sign_in.sign_in("wrong-account").connections == {}
    assert sign_in.redeem("tester", invite.token).connections == {
        Product.IRON: INSTRUCTIONS[Product.IRON]
    }


def test_sign_in_rejects_sessions_outside_fixture_issuer_audience_and_expiry(
    sign_in: PilotSignIn, sessions: dict[str, VerifiedSignIn]
) -> None:
    for invalid in (
        replace(TESTER, issuer="untrusted"),
        replace(TESTER, audience="other"),
        replace(TESTER, subject=""),
        replace(TESTER, expires_at=NOW),
        replace(TESTER, expires_at=NOW.replace(tzinfo=None)),
    ):
        sessions["invalid"] = invalid
        with pytest.raises(PilotAccessDenied, match="UNAUTHENTICATED"):
            sign_in.sign_in("invalid", confirmed_zone="UTC")
    for missing in ("", "unknown"):
        with pytest.raises(PilotAccessDenied, match="UNAUTHENTICATED"):
            sign_in.sign_in(missing, confirmed_zone="UTC")


def test_products_can_show_connection_setup_independently(
    tmp_path: Path, sessions: dict[str, VerifiedSignIn]
) -> None:
    store = ProductInvites(tmp_path / "invites.sqlite3", secrets.token_bytes(32))
    sign_in = PilotSignIn(
        tmp_path / "accounts.sqlite3",
        store,
        sessions.get,
        issuer=ISSUER,
        audience=AUDIENCE,
        operator_subjects=frozenset({"fixture-operator"}),
        connection_instructions={Product.IRON: INSTRUCTIONS[Product.IRON]},
        clock=lambda: NOW,
    )
    member = sign_in.sign_in("tester", confirmed_zone="UTC")
    for product in Product:
        invite = sign_in.issue("operator", product, "tester@example.test", expires_at=EXPIRES)
        sign_in.redeem("tester", invite.token)
    assert sign_in.sign_in("tester").connections == {Product.IRON: INSTRUCTIONS[Product.IRON]}
    assert store.active_grants() == {(member.account.account_id, product) for product in Product}
