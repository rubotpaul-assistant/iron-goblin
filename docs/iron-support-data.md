# Iron support data requests

For [#23](https://github.com/rubotpaul-assistant/iron-goblin/issues/23), broad
account data requests go through support. Confirm the requester through
Creek Masons sign-in or the approved identity recovery process. Resolve the
Iron owner from that verified account. An ID, name or email typed into a
ticket does not establish record ownership. Record a safe correlation ID,
operation, verification outcome and product in the support case.

## Export rehearsal

Use an Iron-only, read-only database connection and a verified owner.
The exporter reads one consistent snapshot of the account, workouts, exercises,
sets, private catalog, applicable shared catalog labels, cardio, profile,
identities and audit summaries. Replay responses and client keys stay in the
service. Use the approved private delivery channel, and expire intermediate
copies under the retention policy. Keep all raw data out of operator logs
and issue threads. The local rehearsal uses disposable records and no delivery.

```sh
uv run pytest -q -x --no-cov tests/test_support_export.py
```

## Account deletion and restore rehearsal

Show the affected product and record counts in a dry run using the verified
owner. Obtain explicit confirmation for that product, then disable its
connections and grant before deleting. Pause issuance and invalidate outstanding
invitations for that Iron account during the operation. Apply the approved retention to Iron
canonical rows, owned catalog, replay, audit and backups. Preserve separate
Macro access and the shared identity while another product grant remains valid.
For the deletion rehearsal, cover two accounts and both product grants.

Record a protected deletion marker with the minimum information necessary
to apply prior deletions on restore. Store the expected database identity in
the protected backup manifest before taking a snapshot. Restore into isolation,
check the copy against that manifest, then apply all closure markers for that
lineage. A snapshot that predates a verified lineage stays quarantined.
Verify both account and product isolation before returning the database to service. Document
the approved backup expiry and verify that no older copy is retained beyond it.
The default recovery exercise uses
[the local recovery runbook](pilot-hosting-recovery.md).

The local closure requires prior revocation of the Iron grant and a signed,
count-only confirmation. It clears owned canonical rows and receipts in one
transaction, retires the Iron owner ID, and records a minimal marker
outside the product database. The fixture restores a copy taken before
deletion, reapplies the marker, and verifies that the other Iron owner and
the separate Macro grant survive.

```sh
uv run pytest -q -x --no-cov tests/test_support_closure.py
```

Hosted operator role binding, agreed retention and backup expiry verification
remain tracked by #23. Production execution needs separate approval under
[the pilot work order](https://github.com/rubotpaul-assistant/iron-goblin/issues/6).
