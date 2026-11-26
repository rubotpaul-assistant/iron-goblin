# Pilot traffic and incident controls

This rehearsal supports [#26](https://github.com/rubotpaul-assistant/iron-goblin/issues/26).
The fixture accepts enumerated events and outcomes, server-generated request IDs,
and optional product-specific account pseudonyms. Turn the same account into
different pseudonyms for Iron and Macro. Keep the HMAC key in protected runtime
configuration and restrict access to event logs. Record product counts for
invite and connection attempts, first and repeat successful writes, history,
and replays. Count a replay separately from a new successful write.

At the public boundary, use independent per-source and total allowances for
invite attempts and MCP requests per product. The fixture gate answers a
throttled request with `429` and `Retry-After` before parsing the request.
Use the address from trusted ingress configuration. The fixture ignores
forwarded headers. The in-memory limits and milestones are for a single
disposable process. Pick and rehearse shared rate-limit state and reporting
retention before using several instances or collecting pilot evidence across
restarts.

Keep request and response bodies, credentials, invite links, notes, images,
workout or food values, and personal identifiers out of routine telemetry.
Restrict reverse-proxy access logs to safe status and correlation fields.
Suppress query strings, raw paths with IDs, bearer headers and exception
payloads. Verify both successful and rejected requests on each product in
approved staging. Use product-level counts for the pilot review.

## Operator drill

1. Ask the tester for the product, rough time, and safe request ID. Search
   only the affected product's outcomes. Check invite expiration and current
   consent without copying the invite URL or credentials.
2. For suspected cross-account access, disable the affected connection or
   product invite/grant immediately. If the scope is uncertain, pause the
   affected endpoint and deploy the last safe build through the approved
   rollback process. Preserve minimal audit evidence and record the release
   version and known affected endpoints.
3. Verify the denied read, write and attempted replay using synthetic data.
   Confirm that the other product still behaves independently. If rollback
   needs a database restore, use the isolated product rehearsal in
   [the hosting and recovery runbook](pilot-hosting-recovery.md).
4. Use a known-safe connection and ask the tester to retry only after the
   issue is understood and access is restored through approved operations.
   Record the outcome and visible friction. Avoid copying user payloads into
   an incident ticket.

Run `uv run pytest -q -x --no-cov tests/test_pilot_controls.py` for the
disposable drill. The hosted app and per-product ingestion still need
staging integration. Provisioning, live telemetry and production runbooks
remain approval-gated under [#6](https://github.com/rubotpaul-assistant/iron-goblin/issues/6).
