# Goblin pilot hosting and recovery

[Iron #17](https://github.com/rubotpaul-assistant/iron-goblin/issues/17)
supports both pilots. This slice records the target topology, budget
assumptions and a repeatable local SQLite restore. The hosted packaging and
PostgreSQL trial still need a hosted entry point and approved staging.

## Railway proposal

Run independent Iron and Macro Python services built from their locked
dependencies. Use application routing through one small ingress service for
both path families. The ingress can serve both hostnames and route by host and
path while the product services use private networking. Give each product
its own database, runtime credential, migration credential and backup
destination. The identity service may manage Clerk mappings and grants.
Product services only need the account, time zone, and their own current
entitlement and consent. Keep staging and production databases, identities,
secrets and backup destinations independent.

| Public endpoint | Purpose |
| --- | --- |
| `app.creekmasons.com/iron-goblin`, `app.creekmasons.com/macro-goblin` | Sign in, redeem that product's invite, confirm time zone and find connection instructions |
| `mcp.creekmasons.com/iron-goblin`, `mcp.creekmasons.com/macro-goblin` | Product-specific MCP resources and OAuth discovery |

Use routing tests to verify the exact discovery URL, audience, path and
forwarded protocol on both hosts. Test unknown paths and cross-product
audiences. Publish the Railway-issued CNAME and ownership TXT records for
each subdomain through Squarespace only after approval. Check certificate
issuance and HTTPS before client acceptance. Railway routes the custom domain
to the ingress service, which owns the path mapping.
[Railway domain configuration](https://docs.railway.com/networking/domains/working-with-domains)

Use `/healthz` for readiness after migration and a simple database check.
Return minimal status and keep product data and credentials out of health
responses. Configure each release to run migration under a single migrator
before new instances accept traffic. Apply additive migrations first and
verify the previous build can still read the schema when code rollback is
required. Record the release SHA and migration version with the snapshot.

## Database decision to prove on staging

PostgreSQL is the first hosted candidate. It supports independent database
roles and multiple app instances. Prove a transaction-scoped mechanism that
serializes receipt creation for the same account, operation and client key
before examining mutable workout state. Verify one canonical write, audit row
and receipt under competing submissions, a rollback after an injected failure,
and replay after restart. Add indexes for owner and lookup keys and run
cross-tenant tests against the actual PostgreSQL implementation. Cover
performed-date queries and canonical IDs with the same fixtures as SQLite.
The SQLite transaction uses `BEGIN IMMEDIATE`. The PostgreSQL implementation
must provide the same invariant through PostgreSQL locking and uniqueness.

A persistent Railway volume with one service instance preserves the existing
SQLite semantics and is a candidate for a limited staging fallback. Measure
write contention and restart downtime and demonstrate a restorable consistent
snapshot. Avoid ephemeral storage for canonical data. If Railway cannot meet
the product isolation, restore, ingress or budget criteria, repeat the same
checks on Fly before proposing it.
[Railway volumes](https://docs.railway.com/volumes/reference) ·
[Railway backups](https://docs.railway.com/volumes/backups)

## Restore rehearsal

Run `uv run pytest -q tests/test_recovery.py --no-cov` on the Iron checkout.
It uses the SQLite online backup API to copy a consistent disposable
database, performs a later write, then restores into a new disposable file.
The test reopens the schema, checks SQLite integrity and foreign keys, and
proves the original status/body, canonical workout IDs, three sets, single
audit event and owner isolation after restore.

For approved staging, take a dated snapshot before each migration and propose
a backup schedule, retention, recovery-point and recovery-time goals. Restore a
single product into a separate database and use the same smoke and isolation
tests. Exercise rollback with an old application build on the migrated schema.
If the migration is irreversible, stop writes and evaluate restore with an
operator and a record of which writes would be lost. Confirm the other
product and its backups are unaffected. Open production only after the
product's rollback and support evidence pass. No legacy import is needed.

## Initial estimate

Railway lists a $20 monthly Pro usage minimum and a $5 Hobby minimum.
Assume a low-volume, always-on Pro workspace, with five processes per
environment (ingress, two apps, two databases), each averaging 0.025 vCPU
and 0.125 GB RAM, two 1 GB volumes and 2 GB public egress per environment.
At published unit rates this is about $9.15 in measured resources per
environment per month. Two environments therefore fall near the $20 Pro
minimum. Doubling the compute and RAM assumptions brings the two-environment
estimate to about $36 per month. These are planning assumptions. Measure
actual service memory and storage before setting a ceiling and include any
Clerk, backup storage or platform charges with the approval request.
[Railway pricing](https://docs.railway.com/pricing)

Deployment, paid provisioning, DNS/TLS configuration, live credentials and
production recovery operations remain approval-gated under [#6](https://github.com/rubotpaul-assistant/iron-goblin/issues/6).
