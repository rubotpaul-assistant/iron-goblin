"""Fixture sign-in orchestration for the shared Creek Masons pilot."""

import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from gym_tracker.pilot_account import Account, PilotAccessDenied, Product
from gym_tracker.pilot_invites import IssuedInvite, ProductInvites, VerifiedRecipient

SCHEMA = """
CREATE TABLE IF NOT EXISTS clerk_accounts (
 issuer TEXT NOT NULL,
 subject TEXT NOT NULL,
 account_id TEXT NOT NULL UNIQUE,
 time_zone TEXT NOT NULL,
 PRIMARY KEY (issuer, subject)
);
"""


@dataclass(frozen=True)
class VerifiedSignIn:
    """Normalized browser-session claims from a trusted Clerk verifier."""

    issuer: str
    subject: str
    audience: str
    expires_at: datetime
    verified_email: str | None = field(repr=False)


@dataclass(frozen=True)
class PilotSignInResult:
    account: Account
    connections: dict[Product, str]


class PilotSignIn:
    """Bind fixture browser sessions to a stable account and independent grants."""

    def __init__(
        self,
        identity_database: Path,
        invites: ProductInvites,
        verify_session: Callable[[str], VerifiedSignIn | None],
        *,
        issuer: str,
        audience: str,
        operator_subjects: frozenset[str],
        connection_instructions: Mapping[Product, str],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._identity_database = identity_database
        self._invites = invites
        self._verify_session = verify_session
        self._issuer = issuer
        self._audience = audience
        self._operator_subjects = operator_subjects
        self._instructions = dict(connection_instructions)
        self._clock = clock
        with closing(sqlite3.connect(self._identity_database)) as connection:
            connection.executescript(SCHEMA)

    def sign_in(
        self, browser_session: str, *, confirmed_zone: str | None = None
    ) -> PilotSignInResult:
        """Resolve or create the account after the tester confirms a valid time zone."""
        claims = self._claims(browser_session)
        with closing(sqlite3.connect(self._identity_database)) as connection, connection:
            if confirmed_zone is None:
                account = self._account(connection, claims)
            else:
                new = Account(str(uuid4()), confirmed_zone)
                row = connection.execute(
                    """INSERT INTO clerk_accounts(issuer,subject,account_id,time_zone)
                    VALUES(?,?,?,?) ON CONFLICT(issuer,subject) DO UPDATE SET
                    time_zone=excluded.time_zone RETURNING account_id,time_zone""",
                    (claims.issuer, claims.subject, new.account_id, new.time_zone),
                ).fetchone()
                account = Account(*row)
        return self._result(account)

    def redeem(self, browser_session: str, invite: str) -> PilotSignInResult:
        """Redeem for the verified account and expose its granted connection instructions."""
        claims = self._claims(browser_session)
        with closing(sqlite3.connect(self._identity_database)) as connection:
            account = self._account(connection, claims)
        self._invites.redeem(
            invite, VerifiedRecipient(account, claims.verified_email), now=self._clock()
        )
        return self._result(account)

    def issue(
        self, browser_session: str, product: Product, recipient_email: str, *, expires_at: datetime
    ) -> IssuedInvite:
        self._require_operator(browser_session)
        return self._invites.issue(product, recipient_email, expires_at=expires_at)

    def revoke(self, browser_session: str, invitation_id: UUID) -> None:
        self._require_operator(browser_session)
        self._invites.revoke(invitation_id)

    @staticmethod
    def _account(connection: sqlite3.Connection, claims: VerifiedSignIn) -> Account:
        row = connection.execute(
            "SELECT account_id,time_zone FROM clerk_accounts WHERE issuer=? AND subject=?",
            (claims.issuer, claims.subject),
        ).fetchone()
        if row is None:
            raise PilotAccessDenied("TIME_ZONE_REQUIRED")
        return Account(*row)

    def _require_operator(self, browser_session: str) -> None:
        claims = self._claims(browser_session)
        if claims.subject not in self._operator_subjects:
            raise PilotAccessDenied("ACCESS_DENIED")

    def _claims(self, browser_session: str) -> VerifiedSignIn:
        claims = self._verify_session(browser_session) if browser_session else None
        if (
            claims is None
            or claims.issuer != self._issuer
            or claims.audience != self._audience
            or not claims.subject
            or claims.expires_at.utcoffset() is None
            or claims.expires_at <= self._clock()
        ):
            raise PilotAccessDenied("UNAUTHENTICATED")
        return claims

    def _result(self, account: Account) -> PilotSignInResult:
        grants = self._invites.active_grants()
        return PilotSignInResult(
            account,
            {
                product: instructions
                for product, instructions in self._instructions.items()
                if (account.account_id, product) in grants
            },
        )
