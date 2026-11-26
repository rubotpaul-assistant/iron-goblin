"""Drive invite issue, redemption, and revocation through a disposable store."""

import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from gym_tracker.pilot_account import (
    AccessPolicy,
    Account,
    Consent,
    PilotAccessDenied,
    Product,
    VerifiedAccess,
)
from gym_tracker.pilot_invites import InviteUnavailable, ProductInvites, VerifiedRecipient

NOW = datetime(2026, 11, 26, 12, tzinfo=UTC)
EXPIRES = NOW + timedelta(minutes=5)
RECIPIENT = VerifiedRecipient(Account("fixture-account", "America/Los_Angeles"), "one@example.test")


@pytest.fixture
def store(tmp_path: Path) -> ProductInvites:
    return ProductInvites(tmp_path / "invites.sqlite3", secrets.token_bytes(32))


def test_independent_product_grants_and_current_revocation(store: ProductInvites) -> None:
    def authenticate(client_id: str, product: Product) -> str:
        account = RECIPIENT.account
        scope = "workouts:read" if product == Product.IRON else "nutrition:read"
        audience = f"https://{product}.example.test/mcp"
        issuer = "https://accounts.example.test"
        subject = "fixture-subject"
        connection_id = f"{client_id}-{product}"
        policy = AccessPolicy(
            issuer,
            {product: audience},
            {(issuer, subject): account},
            store.active_grants(),
            {connection_id: Consent(account.account_id, product, client_id, frozenset({scope}))},
        )
        token = VerifiedAccess(
            issuer, subject, audience, client_id, connection_id, EXPIRES, frozenset({scope})
        )
        return policy.authorize(token, product, scope, now=NOW).account_id

    assert RECIPIENT.verified_email is not None
    iron = store.issue(Product.IRON, RECIPIENT.verified_email, expires_at=EXPIRES)
    macro = store.issue(Product.MACRO, RECIPIENT.verified_email, expires_at=EXPIRES)
    with pytest.raises(PilotAccessDenied, match="ACCESS_DENIED"):
        authenticate("one", Product.IRON)
    assert store.redeem(iron.token, RECIPIENT, now=NOW) == Product.IRON
    assert authenticate("one", Product.IRON) == authenticate("two", Product.IRON)
    with pytest.raises(PilotAccessDenied, match="ACCESS_DENIED"):
        authenticate("one", Product.MACRO)
    assert store.redeem(macro.token, RECIPIENT, now=NOW) == Product.MACRO
    assert authenticate("one", Product.MACRO) == authenticate("two", Product.IRON)

    store.revoke(iron.id)
    with pytest.raises(PilotAccessDenied, match="ACCESS_DENIED"):
        authenticate("two", Product.IRON)
    assert authenticate("one", Product.MACRO) == RECIPIENT.account.account_id


def test_expired_wrong_account_unverified_revoked_and_replayed_invites(
    store: ProductInvites,
) -> None:
    address = "one@example.test"
    expired = store.issue(Product.IRON, address, expires_at=NOW)
    revoked = store.issue(Product.IRON, address, expires_at=EXPIRES)
    valid = store.issue(Product.IRON, address, expires_at=EXPIRES)
    store.revoke(revoked.id)
    for token, account in (
        (expired.token, RECIPIENT),
        (revoked.token, RECIPIENT),
        (valid.token, VerifiedRecipient(Account("other", "UTC"), "other@example.test")),
        (valid.token, VerifiedRecipient(RECIPIENT.account, None)),
        (valid.token, VerifiedRecipient(RECIPIENT.account, " ")),
        ("invalid", RECIPIENT),
        ("é" * 43, RECIPIENT),
    ):
        with pytest.raises(InviteUnavailable, match=r"^INVITE_UNAVAILABLE$"):
            store.redeem(token, account, now=NOW)
    assert not store.active_grants()

    assert store.redeem(valid.token, RECIPIENT, now=NOW) == Product.IRON
    with pytest.raises(InviteUnavailable, match=r"^INVITE_UNAVAILABLE$"):
        store.redeem(valid.token, RECIPIENT, now=NOW)


def test_invite_store_keeps_no_raw_token_or_recipient_and_supports_reopen(tmp_path: Path) -> None:
    path = tmp_path / "invite.sqlite3"
    recipient_key = secrets.token_bytes(32)
    store = ProductInvites(path, recipient_key)
    address = "ONE@example.test"
    issued = store.issue(Product.IRON, address, expires_at=EXPIRES)
    assert issued.token not in repr(issued)
    assert address.casefold() not in repr(RECIPIENT)
    assert all(
        value.encode() not in path.read_bytes()
        for value in (issued.token, address, address.casefold())
    )
    assert (
        ProductInvites(path, recipient_key).redeem(issued.token, RECIPIENT, now=NOW) == Product.IRON
    )


def test_invite_cannot_be_redeemed_concurrently_twice(store: ProductInvites) -> None:
    invite = store.issue(Product.IRON, "one@example.test", expires_at=EXPIRES)
    waiting = Barrier(2)

    def redeem() -> Product | None:
        waiting.wait(timeout=5)
        try:
            return store.redeem(invite.token, RECIPIENT, now=NOW)
        except InviteUnavailable:
            return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(redeem)
        second = workers.submit(redeem)
        assert {first.result(timeout=10), second.result(timeout=10)} == {Product.IRON, None}
    assert store.active_grants() == {(RECIPIENT.account.account_id, Product.IRON)}


def test_redeem_rolls_back_if_grant_write_fails(tmp_path: Path) -> None:
    path = tmp_path / "invite.sqlite3"
    store = ProductInvites(path, secrets.token_bytes(32))
    invite = store.issue(Product.IRON, "one@example.test", expires_at=EXPIRES)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TRIGGER fail_grant BEFORE INSERT ON product_grants
            BEGIN SELECT RAISE(ABORT, 'fixture grant failure'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="fixture grant failure"):
        store.redeem(invite.token, RECIPIENT, now=NOW)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER fail_grant")
    assert store.redeem(invite.token, RECIPIENT, now=NOW) == Product.IRON


def test_configuration_requires_key_material_and_an_aware_expiry(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        ProductInvites(tmp_path / "invalid.sqlite3", b"")
    store = ProductInvites(tmp_path / "invites.sqlite3", secrets.token_bytes(32))
    with pytest.raises(ValueError, match="requires an offset"):
        store.issue(Product.IRON, "one@example.test", expires_at=EXPIRES.replace(tzinfo=None))
