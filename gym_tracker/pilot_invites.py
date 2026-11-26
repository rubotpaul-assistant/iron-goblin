"""Fixture persistence for product-specific Creek Masons invitation redemption."""

import hashlib
import hmac
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from gym_tracker.pilot_account import Account, Product

SCHEMA = """
CREATE TABLE IF NOT EXISTS invitations (
 id TEXT PRIMARY KEY,
 token_digest TEXT NOT NULL UNIQUE,
 recipient_digest TEXT NOT NULL,
 product TEXT NOT NULL,
 expires_at TEXT NOT NULL,
 used INTEGER NOT NULL DEFAULT 0 CHECK (used IN (0,1)),
 revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0,1))
);
CREATE TABLE IF NOT EXISTS product_grants (
 account_id TEXT NOT NULL,
 product TEXT NOT NULL,
 invitation_id TEXT NOT NULL REFERENCES invitations(id),
 PRIMARY KEY (account_id, product)
);
"""


class InviteUnavailable(Exception):
    """Safe error shared by absent, expired, revoked and ineligible invitations."""


@dataclass(frozen=True)
class VerifiedRecipient:
    """Account and verified invite address resolved by the trusted sign-in adapter."""

    account: Account
    verified_email: str | None = field(repr=False)


@dataclass(frozen=True)
class IssuedInvite:
    id: UUID
    token: str = field(repr=False)


class ProductInvites:
    """Model issue, redeem and revoke using an explicitly supplied fixture store."""

    def __init__(self, path: Path, recipient_key: bytes) -> None:
        if len(recipient_key) < 32:
            raise ValueError("invite recipient key must contain at least 32 bytes")
        self._path = path
        self._recipient_key = recipient_key
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def issue(
        self, product: Product, recipient_email: str, *, expires_at: datetime
    ) -> IssuedInvite:
        """Create an invite for delivery by the authorized operator."""
        invite = IssuedInvite(uuid4(), secrets.token_urlsafe(32))
        recipient = self._recipient_digest(recipient_email)
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO invitations(id,token_digest,recipient_digest,product,expires_at)
                VALUES(?,?,?,?,?)""",
                (
                    str(invite.id),
                    _token_digest(invite.token),
                    recipient,
                    product,
                    _instant(expires_at),
                ),
            )
        return invite

    def redeem(self, token: str, recipient: VerifiedRecipient, *, now: datetime) -> Product:
        """Redeem once and activate only the product named by the invitation."""
        if not token.isascii() or len(token) != 43 or recipient.verified_email is None:
            raise InviteUnavailable("INVITE_UNAVAILABLE")
        recipient_digest = self._recipient_digest(recipient.verified_email)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            invite = connection.execute(
                """SELECT id,recipient_digest,product FROM invitations
                WHERE token_digest=? AND expires_at>? AND used=0 AND revoked=0""",
                (_token_digest(token), _instant(now)),
            ).fetchone()
            if invite is None or not hmac.compare_digest(
                invite["recipient_digest"], recipient_digest
            ):
                raise InviteUnavailable("INVITE_UNAVAILABLE")
            product = Product(invite["product"])
            connection.execute("UPDATE invitations SET used=1 WHERE id=?", (invite["id"],))
            connection.execute(
                """INSERT INTO product_grants(account_id,product,invitation_id) VALUES(?,?,?)
                ON CONFLICT(account_id,product) DO UPDATE SET
                invitation_id=excluded.invitation_id""",
                (recipient.account.account_id, product, invite["id"]),
            )
        return product

    def revoke(self, invite_id: UUID) -> None:
        """Invalidate the invite and withdraw a grant still sourced to it."""
        with self._connect() as connection:
            connection.execute("UPDATE invitations SET revoked=1 WHERE id=?", (str(invite_id),))
            connection.execute(
                "DELETE FROM product_grants WHERE invitation_id=?", (str(invite_id),)
            )

    def active_grants(self) -> frozenset[tuple[str, Product]]:
        """Provide the current grant keys used by the shared access policy."""
        with self._connect() as connection:
            return frozenset(
                (row["account_id"], Product(row["product"]))
                for row in connection.execute("SELECT account_id,product FROM product_grants")
            )

    def _recipient_digest(self, email: str) -> str:
        normalized = email.strip().casefold()
        if not normalized or len(normalized) > 320:
            raise InviteUnavailable("INVITE_UNAVAILABLE")
        return hmac.new(self._recipient_key, normalized.encode(), hashlib.sha256).hexdigest()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _instant(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("invite timestamp requires an offset")
    return value.astimezone(UTC).isoformat(timespec="microseconds")
