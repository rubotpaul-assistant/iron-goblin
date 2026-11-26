"""Exercise the reference policy with the shared JSON contract vectors."""

import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from gym_tracker.pilot_account import (
    AccessPolicy,
    Account,
    Consent,
    PilotAccessDenied,
    Product,
    VerifiedAccess,
    event_day,
)

VECTORS = json.loads(
    (Path(__file__).resolve().parents[1] / "contracts" / "creek-masons-pilot-v1.json").read_text()
)


@pytest.mark.parametrize("case", VECTORS["authorization"], ids=lambda case: case["name"])
def test_shared_authorization_vectors(case: dict[str, Any]) -> None:
    account = Account(**VECTORS["account"])
    claims = VECTORS["base_access"]
    consents = {
        key: Consent(
            account.account_id,
            Product(consent["product"]),
            consent["client_id"],
            frozenset(consent["scopes"]),
        )
        for key, consent in VECTORS["consents"].items()
        if key != case.get("revoked_connection")
    }
    if "consent_scopes" in case:
        key = claims["connection_id"]
        consents[key] = replace(consents[key], scopes=frozenset(case["consent_scopes"]))
    policy = AccessPolicy(
        issuer=claims["issuer"],
        audiences={Product(key): value for key, value in VECTORS["audiences"].items()},
        accounts={(claims["issuer"], claims["subject"]): account},
        active_grants=frozenset(
            (account.account_id, Product(key)) for key in case.get("grants", VECTORS["grants"])
        ),
        active_consents=consents,
    )
    access = None
    if case.get("access", {}) is not None:
        fields = claims | case.get("access", {})
        fields["expires_at"] = datetime.fromisoformat(fields["expires_at"])
        fields["scopes"] = frozenset(fields["scopes"])
        access = VerifiedAccess(**fields)
    product = Product(case.get("product", "iron"))
    scope = case.get("scope", "workouts:read")
    now = datetime.fromisoformat(VECTORS["now"])
    if "error" in case:
        with pytest.raises(PilotAccessDenied, match=case["error"]):
            policy.authorize(access, product, scope, now=now)
    else:
        assert (
            policy.authorize(access, product, scope, now=now).account_id == case["expected_account"]
        )


@pytest.mark.parametrize("case", VECTORS["timed_events"], ids=lambda case: case["name"])
def test_shared_event_day_vectors(case: dict[str, Any]) -> None:
    def resolve_day() -> Any:
        return event_day(
            Account("fixture-account", case["time_zone"]),
            datetime.fromisoformat(case["occurred_at"]) if "occurred_at" in case else None,
            date.fromisoformat(case["historical_date"]) if "historical_date" in case else None,
        )

    if "error" in case:
        with pytest.raises(ValueError, match=case["error"]):
            resolve_day()
        return
    day = resolve_day()
    instant = datetime.fromisoformat(case["occurred_at_utc"]) if case["occurred_at_utc"] else None
    offset = (
        timedelta(minutes=case["offset_minutes"]) if case["offset_minutes"] is not None else None
    )
    assert day.occurred_at_utc == instant
    assert day.event_offset == offset
    assert day.account_time_zone == case["time_zone"]
    assert day.local_date.isoformat() == case["local_date"]
    assert day.explicit_local_date == ("historical_date" in case)


def test_zone_changes_apply_to_new_writes() -> None:
    account = Account("fixture-account", "America/Los_Angeles")
    instant = datetime.fromisoformat("2026-08-17T01:00:00Z")
    original = event_day(account, instant)
    new_event = event_day(replace(account, time_zone="Europe/London"), instant)
    assert original.local_date.isoformat() == "2026-08-16"
    assert original.account_time_zone == "America/Los_Angeles"
    assert new_event.local_date.isoformat() == "2026-08-17"
