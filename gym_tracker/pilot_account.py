"""Reference policy for the shared Creek Masons pilot contract."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class Product(StrEnum):
    IRON = "iron"
    MACRO = "macro"


class PilotAccessDenied(Exception):
    """Safe rejection at the product boundary."""


@dataclass(frozen=True)
class Account:
    account_id: str
    time_zone: str

    def __post_init__(self) -> None:
        try:
            ZoneInfo(self.time_zone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("invalid account time zone") from error


@dataclass(frozen=True)
class VerifiedAccess:
    """Normalized claims from a trusted access-token verifier."""

    issuer: str
    subject: str
    audience: str
    client_id: str
    connection_id: str
    expires_at: datetime
    scopes: frozenset[str]


@dataclass(frozen=True)
class Consent:
    account_id: str
    product: Product
    client_id: str
    scopes: frozenset[str]


@dataclass(frozen=True)
class AccessPolicy:
    issuer: str
    audiences: Mapping[Product, str]
    accounts: Mapping[tuple[str, str], Account]
    active_grants: frozenset[tuple[str, Product]]
    active_consents: Mapping[str, Consent]

    def authorize(
        self, access: VerifiedAccess | None, product: Product, scope: str, *, now: datetime
    ) -> Account:
        """Authorize one operation against current grants and consent, including replay."""
        if (
            access is None
            or access.issuer != self.issuer
            or access.audience != self.audiences[product]
            or access.expires_at.utcoffset() is None
            or access.expires_at <= now
        ):
            raise PilotAccessDenied("UNAUTHENTICATED")
        account = self.accounts.get((access.issuer, access.subject))
        consent = self.active_consents.get(access.connection_id)
        if (
            account is None
            or (account.account_id, product) not in self.active_grants
            or consent is None
            or (consent.account_id, consent.product, consent.client_id)
            != (account.account_id, product, access.client_id)
        ):
            raise PilotAccessDenied("ACCESS_DENIED")
        if scope not in access.scopes or scope not in consent.scopes:
            raise PilotAccessDenied("INSUFFICIENT_SCOPE")
        return account


@dataclass(frozen=True)
class EventDay:
    occurred_at_utc: datetime | None
    event_offset: timedelta | None
    account_time_zone: str
    local_date: date
    explicit_local_date: bool


def event_day(
    account: Account, occurred_at: datetime | None, historical_date: date | None = None
) -> EventDay:
    """Resolve the local day once and keep the context needed for stable history."""
    if occurred_at is None:
        if historical_date is None:
            raise ValueError("event requires a timestamp or historical date")
        return EventDay(None, None, account.time_zone, historical_date, True)
    offset = occurred_at.utcoffset()
    if offset is None:
        raise ValueError("event timestamp must include an offset")
    return EventDay(
        occurred_at.astimezone(UTC),
        offset,
        account.time_zone,
        historical_date or occurred_at.astimezone(ZoneInfo(account.time_zone)).date(),
        historical_date is not None,
    )
