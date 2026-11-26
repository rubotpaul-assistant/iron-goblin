# Creek Masons pilot account contract v1

Iron and Macro share a Creek Masons identity and each owns its records. The
private Iron tracer continues to use its single fixture owner. The hosted
pilot starts with fresh accounts.

## Identity, product access, and consent

- `account_id` is an opaque, stable ID allocated by Creek Masons. Resolve it
  from the unique verified Clerk `(issuer, subject)` pair. Sign-in and client
  authorization for the same pair reuse this account. Do not accept an
  account ID, email, display name, or assistant session ID from a tool as
  proof of identity.
- The account has a validated IANA `time_zone`. Require the tester to confirm
  it during onboarding.
- Store product entitlements by `(account_id, product)`, with product keys
  `iron` and `macro`. Redeeming an invite activates that product's free grant.
  A later billing source may update grants. Each record and replay receipt
  stays keyed to the same account.
- Each client connection has an opaque `connection_id` bound server-side
  to `account_id`, `product`, verified `client_id`, and the consented scopes.
  A new connection after revocation gets a fresh ID. Each product request
  checks expiry, issuer, audience, active account mapping, active product
  grant, and active consent before reading domain records or replay receipts.
  A record query includes the authorized account. Missing and foreign
  records return the same public error.
- Scope names are `workouts:read` and `workouts:write` for Iron and
  `nutrition:read` and `nutrition:write` for Macro. The route or tool selects
  the required scope. Read and write permissions are independent. Require the
  exact scope in both verified access and current consent.

## Token and client boundary

Normalize verified access to `issuer`, `subject`, `audience`, `client_id`,
`connection_id`, `expires_at` (UTC instant), and a set of `scopes`. The issuer is
the exact configured Clerk OAuth issuer for the environment. Audience identifies
the individual hosted product resource. Pin that audience from verified token
data or a trusted server-side resource binding proven during OAuth. Require
issuer, audience and expiry even when the connection has a stored consent.
The adapter rejects an ID token, browser session token, or refresh token as an
API bearer. If the connector's Clerk path cannot prove the resource and
connection binding, keep it blocked pending an approved alternative.

Prefer scoped opaque access tokens where the client path supports them.
Introspection checks token activity and immediate invalidation, at the cost
of an online dependency. JWTs can support verification without an online
introspection call when the server can still enforce current grant and
connection revocation. The fixture uses normalized claims and stores current
authorization separately, so the choice can change per verified client path.
Fail closed if authoritative verification or grant state is unavailable.

Use authorization code with S256 PKCE and user consent for supported interactive
clients. Prefer registered client IDs or approved Client ID Metadata Documents.
Track any required dynamic registration in #16, including its approval and
resource binding. Refresh stays with the OAuth client and authorization server.
Request offline access only for a client that needs it and bind refreshed access
to the same current grant and connection. Disconnect removes local consent
immediately and requests provider-side invalidation of the associated tokens.
Product grant revocation denies all connections for that product, including
authorized retries from before revocation. A revoked product grant does not
alter the account or the other product's grant.

Verification deliberately uses fixture claims. #16 proves client capabilities
and #25 owns invite redemption. Geoff approves real Clerk settings separately.
[Clerk OAuth configuration and token behavior](https://clerk.com/docs/guides/configure/auth-strategies/oauth/how-clerk-implements-oauth)

## Time zone and historical events

- A timed event carries an offset-bearing RFC 3339 timestamp. Its offset
  disambiguates the event instant, including the two occurrences of a
  fall-back hour. Resolve its day in the account's zone, which can differ
  from the event offset.
- A user-confirmed historical date overrides the derived day. A date-only
  entry stores the explicit date and account zone without inventing the
  occurrence time. An adapter may store its separate ingestion timestamp.
- Persist the effective `local_date`, the account time zone as it was on the
  write, and whether a history date was explicit. Timed events also keep the
  UTC instant and submitted offset. A correction updates the explicit day
  without inventing a new instant. Zone changes apply to new writes and to
  the meaning of "today". Existing rows retain their effective dates.
- Iron history groups performed exercise records by effective day. Macro
  groups consumption records by effective day. Range filters use those
  persisted dates. Automatic retries reuse the original canonical response
  and day after checking current access.

Both repositories can consume [the language-independent vectors](creek-masons-pilot-v1.json).
The Python reference applies current authorization and resolves timed events.
