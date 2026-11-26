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

## Account deletion to rehearse

Show the affected product and record counts in a dry run using the verified
owner. Obtain explicit confirmation for that product, then disable its
connections and grant before deleting. Apply the approved retention to Iron
canonical rows, owned catalog, replay, audit and backups. Preserve separate
Macro access and the shared identity while another product grant remains valid.
For the deletion rehearsal, cover two accounts and both product grants.

Record a protected deletion marker with the minimum information necessary
to apply prior deletions on restore. Restore only into isolation, apply all
deletion markers newer than the snapshot, verify both account and product
isolation, and then consider returning the database to service. Document
the approved backup expiry and verify that no older copy is retained beyond it.
The default recovery exercise uses
[the local recovery runbook](pilot-hosting-recovery.md).

The support export fixture is local. Hosted operator role binding, agreed
retention, account deletion and backup expiry/restoration verification remain
tracked by #23. Production execution needs separate approval under
[the pilot work order](https://github.com/rubotpaul-assistant/iron-goblin/issues/6).
