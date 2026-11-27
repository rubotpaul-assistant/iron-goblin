# Launch readiness

**Audited:** 2026-11-27 at `bb903a0`

## Recommendation

Iron Goblin is ready for continued local development and a controlled, operator-assisted
loopback pilot rehearsal. It is **not ready for an external or paid pilot** and is not ready for a
public launch.

The deterministic product core and private tracer seam are well tested. The remaining launch
work is chiefly operational and identity infrastructure, but it is launch-critical: there is no
approved hosted package, production OAuth validation, reachable connector trial, hosted recovery
rehearsal, or production operator control plane yet.

## Evidence on current main

- The repository gate passes: Ruff check and format, strict mypy, Bandit, and 209 tests.
- Test coverage is 94.77%, above the configured 90% floor.
- Strength and cardio records are owner-scoped, and account-local history/catalog behavior is
  covered.
- Replay-safe writes, versioned corrections, confirmed deletion, support export, and confirmed
  account closure have service-level and fixture coverage.
- The private HTTP tracer and MCP adapter exercise the canonical service rather than implementing
  a second set of domain rules.
- The loopback connector fixture covers readiness, tool discovery, denied credentials, writes,
  idempotent replay, and subsequent reads. Client-visible confirmation remains a reachable-trial
  gate.
- SQLite recovery and deletion-after-restore rehearsals reject backups without verified lineage.

## Launch gates still open

1. **Hosted runtime:** package and deploy the approved private entry point with separate runtime
   and migration identities, health checks, TLS, secret management, and bounded logs.
2. **Production identity:** validate issuer, audience, client and connection binding against the
   selected provider. The loopback bearer fixture is transport evidence, not production OAuth.
3. **Reachable connector trials:** complete the documented Claude Duet, Meta Muse, OpenAI Dots,
   and OpenClaw trials from fresh accounts, including revocation and replay after response loss.
4. **Hosted persistence and recovery:** select the pilot database, rehearse migration and restore
   in approved staging, verify backup retention/expiry, and prove account closure remains enforced
   after restore.
5. **Operator controls:** deploy rate limits, invite disablement, product kill switches, audit
   event retention, support-role authorization, incident rollback, and cross-instance coordination.
6. **Pilot evidence:** run a small consented cohort through sign-in, invitation, logging,
   correction, deletion, export, closure, and recovery before accepting payment or widening access.

The executable fixture and runbook detail live in
[pilot connector feasibility](pilot-connector-feasibility.md),
[pilot hosting and recovery](pilot-hosting-recovery.md),
[pilot traffic and incident controls](pilot-incident-controls.md), and
[Iron support data](iron-support-data.md).

## Release decision

Do not describe the current repository as production-ready or generally available. A private
external pilot becomes supportable only after gates 1 through 5 have evidence from the actual
hosted environment. Public launch requires successful pilot evidence and an explicit security,
privacy, reliability, and support review.
