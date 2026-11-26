"""Local privacy-safe pilot counters and request limits shared by both products."""

import hashlib
import hmac
import json
import logging
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from math import ceil
from threading import Lock
from time import monotonic
from uuid import UUID, uuid4
from wsgiref.types import StartResponse, WSGIApplication, WSGIEnvironment

from gym_tracker.pilot_account import Product


class PilotEvent(StrEnum):
    INVITE = "invite"
    CONNECT = "connect"
    HISTORY = "history"
    WRITE = "write"
    FIRST_WRITE = "first_write"
    REPEAT_WRITE = "repeat_write"
    REPLAY = "replay"
    INGRESS = "ingress"


class PilotOutcome(StrEnum):
    OK = "ok"
    DENIED = "denied"
    INVALID = "invalid"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"


class PilotMetrics:
    """Capture small product counts and use product-bound account pseudonyms."""

    def __init__(self, key: bytes, logger: logging.Logger) -> None:
        if len(key) < 32:
            raise ValueError("pilot metrics key must contain at least 32 bytes")
        self._key = key
        self._logger = logger
        self._counts: Counter[tuple[Product, PilotEvent, PilotOutcome]] = Counter()
        self._writers: set[str] = set()
        self._lock = Lock()

    def record(
        self,
        product: Product,
        event: PilotEvent,
        outcome: PilotOutcome,
        request_id: UUID,
        *,
        account_id: str | None = None,
    ) -> None:
        """Accept only enumerated metadata and a server-issued request identifier."""
        product, event, outcome = Product(product), PilotEvent(event), PilotOutcome(outcome)
        if not isinstance(request_id, UUID):
            raise ValueError("pilot request identifier must be a UUID")
        message = {
            "product": product,
            "event": event,
            "outcome": outcome,
            "request_id": str(request_id),
        }
        with self._lock:
            self._counts[product, event, outcome] += 1
            if account_id is not None:
                pseudonym = hmac.new(
                    self._key, f"{product}:{account_id}".encode(), hashlib.sha256
                ).hexdigest()
                message["account"] = pseudonym
                if event == PilotEvent.WRITE and outcome == PilotOutcome.OK:
                    milestone = (
                        PilotEvent.REPEAT_WRITE
                        if pseudonym in self._writers
                        else PilotEvent.FIRST_WRITE
                    )
                    self._writers.add(pseudonym)
                    self._counts[product, milestone, outcome] += 1
        self._logger.info("%s", json.dumps(message, separators=(",", ":")))

    def counts(self, product: Product) -> dict[tuple[PilotEvent, PilotOutcome], int]:
        """Give the operator aggregate counts for one product."""
        with self._lock:
            return {
                (event, outcome): count
                for (key, event, outcome), count in self._counts.items()
                if key == product
            }


class PilotEndpoint(StrEnum):
    INVITES = "invites"
    MCP = "mcp"


@dataclass(frozen=True)
class WindowLimit:
    seconds: int
    per_source: int
    total: int

    def __post_init__(self) -> None:
        if self.seconds < 1 or not 1 <= self.per_source <= self.total:
            raise ValueError("invalid pilot rate limit")


@dataclass
class RequestWindow:
    started: float
    total: int = 0
    by_source: Counter[str] = field(default_factory=Counter)


class PilotRateLimiter:
    """Fixed-window fixture gate with bounded per-source state."""

    def __init__(
        self,
        rules: Mapping[PilotEndpoint, WindowLimit],
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._rules = dict(rules)
        self._clock = clock
        self._windows: dict[tuple[Product, PilotEndpoint], RequestWindow] = {}
        self._lock = Lock()

    def retry_after(self, product: Product, endpoint: PilotEndpoint, source: str) -> int:
        """Allow one request or return the seconds before the window resets."""
        key = Product(product), PilotEndpoint(endpoint)
        rule = self._rules[endpoint]
        source_key = hashlib.sha256(source.encode()).hexdigest()
        with self._lock:
            now = self._clock()
            window = self._windows.get(key)
            if window is None or now - window.started >= rule.seconds:
                window = self._windows[key] = RequestWindow(now)
            if window.total >= rule.total or window.by_source[source_key] >= rule.per_source:
                return max(1, ceil(window.started + rule.seconds - now))
            window.total += 1
            window.by_source[source_key] += 1
            return 0


class GuardedPilotEndpoint:
    """Attach product limits to a public WSGI fixture before processing requests."""

    def __init__(
        self,
        app: WSGIApplication,
        product: Product,
        endpoint: PilotEndpoint,
        limiter: PilotRateLimiter,
        metrics: PilotMetrics,
    ) -> None:
        self._app = app
        self._product = product
        self._endpoint = endpoint
        self._limiter = limiter
        self._metrics = metrics

    def __call__(self, environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        # A hosted ingress must supply a trusted client address. Ignore forwarded headers here.
        retry = self._limiter.retry_after(
            self._product, self._endpoint, environ.get("REMOTE_ADDR") or "unknown"
        )
        if not retry:
            return self._app(environ, start_response)
        self._metrics.record(self._product, PilotEvent.INGRESS, PilotOutcome.RATE_LIMITED, uuid4())
        body = b'{"error":{"code":"RATE_LIMITED"}}'
        start_response(
            "429 Too Many Requests",
            [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
                ("Retry-After", str(retry)),
            ],
        )
        return [body]
