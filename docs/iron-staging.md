# Iron Goblin staging

The hosted MCP adapter resolves the verified Creekmasons account on every request
and uses its own Iron owner, exact tool scope and time zone. It reuses the
canonical workout tracer for recording, idempotent replay and performed history.
Sheets is outside the deployed process.

## Railway configuration

Attach a fresh persistent volume to the private `iron` service. Run one replica.
Set `IRON_DATABASE_PATH=/data/iron.sqlite3`, `PORT=8080`, `CLERK_ISSUER` to the
Development issuer and `IRON_RESOURCE_URL` to the exact staging HTTPS endpoint
ending in `/iron-goblin`. Set `CREEKMASONS_PRIVATE_ORIGIN` to the auth service's
private origin and reference only `CREEKMASONS_IRON_SERVICE_KEY` from that service.
Set the auth service's corresponding Iron resource, approved client ID and
operator as documented in Creekmasons Auth.

The shared ingress keeps `IRON_ENABLED=0` while services start. With the
operator's approval, enable it only for a reachable synthetic trial once the
Clerk resource, client and opaque token are verified. Keep Macro's flag and
storage independent. Configure the product scopes `workouts:read` and
`workouts:write` and approve that client only.

## Synthetic trial

Set `IRON_STAGING_SAMPLE=1` on the staging Iron service to make the sample
workout tool available. The tool requires write permission and explicit
confirmation. It creates sample equipment and an exercise only in the
authorized owner's private catalog. Use a fresh trial account.

1. Redeem an Iron invite and confirm your time zone in Creekmasons Auth.
2. Connect from the isolated OpenClaw profile and authorize the Iron resource.
3. Ask to prepare a synthetic workout, confirm it, and read the prescription.
   Log three sample sets with a final-set RIR and retry using the same key.
   Confirm one result and one audit event.
4. From a second invited account, check empty performed history and attempt
   the same record ID. Confirm it gives the public missing-record response.
5. Revoke the first user's Iron invite and check that reads, writes and replays
   fail. A separately granted Macro account can continue to use Macro.

Run `uv run pytest -q --no-cov tests/test_hosted.py` to exercise the hosted
transport with synthetic authorization. Full equipment onboarding, prior
workouts, self-service edits, hosted recovery and all four real connector
results are still required for release.

[Shared Railway stage](https://github.com/rubotpaul-assistant/iron-goblin/issues/17) ·
[Iron staging acceptance](https://github.com/rubotpaul-assistant/iron-goblin/issues/24)
