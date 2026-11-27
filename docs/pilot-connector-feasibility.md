# Pilot connector feasibility

Source review on 2026-11-26 for [Iron #16](https://github.com/rubotpaul-assistant/iron-goblin/issues/16)
and [Macro #5](https://github.com/rubotpaul-assistant/macro-goblin/issues/5).
All four tester runs are pending approval for a reachable fixture endpoint.
Confirm the tester's effective plan, market, device, workspace policy and
access immediately before each run.

| Client | Documented access and first route to test | Evidence still needed |
| --- | --- | --- |
| Claude Duet | Mobile research preview with a waitlist invitation. Claude documents Pro, Max and Ultra in the US, India, Indonesia, Brazil and Japan. Add a remote custom connector in Claude settings and try Claude's published OAuth client identity first. | Confirm that Duet itself discovers and can write through the unlisted connector. The Claude connector settings and Duet's existing connector support establish a candidate route. Test consent, disconnect, and a write approval on the actual Duet app. [Duet access](https://support.claude.com/en/articles/17631495-get-started-with-claude-duet) · [Claude custom connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp) |
| Meta Muse | Meta announced a US rollout on iOS, Android and the web. Meta's Help Centre documents third-party and custom connectors and controls for read and write actions. Start from the tester's own Connectors settings and look for an unlisted custom endpoint. | Confirm availability on the tester's device, whether a custom connector can use remote MCP with OAuth discovery, and whether distribution needs listing or review. If the settings do not offer a supported route, record the exact restriction and seek Meta's published onboarding path before making any registration request. [Muse rollout](https://about.fb.com/news/2026/09/introducing-muse-personal-ai-agent/) · [Muse connector controls](https://www.meta.com/en-gb/help/superintelligence/1687253048996149/) |
| OpenAI Dots | Dots launched in eligible markets on Pro, Business Premium and Enterprise, subject to workspace controls. ChatGPT's private custom MCP route documents writes on eligible Business and Enterprise/Edu workspaces. Start in an already eligible workspace using a private workspace plugin for the fixture. | Confirm that Dots can discover and write via this exact plugin, who can add it, whether a prospective tester can connect independently, and whether another distribution route is necessary for a personal-plan tester. ChatGPT documents Pro's custom MCP route as read/fetch. Verify access on Dots itself. [Dots rollout](https://openai.com/index/introducing-dots/) · [ChatGPT custom MCP](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt) · [Dots workspace controls](https://help.openai.com/en/articles/20001554-managing-dots-in-chatgpt-workspaces) |
| OpenClaw | A tester can configure remote Streamable HTTP and OAuth. Prefer a pre-registered ID or OpenClaw's documented client metadata route where supported. For a shared gateway, use per-requester OAuth identity. | Record OpenClaw and gateway versions and the setup steps a tester can complete. Verify tool discovery and a confirmed write from the assistant interaction as well as authentication. Check revocation in both local credentials and the account binding. [OpenClaw transports and OAuth](https://docs.openclaw.ai/cli/mcp/transports) · [Gateway identity mode](https://docs.openclaw.ai/gateway/config-extensions) |

## Fixture run

1. Use a fresh fixture account, a second account, separate product grants and
   a disposable database. Serve the fixture-equivalent read and write tools
   from the approved endpoint. The local Iron tracer in #4 and #13 exercises
   the same canonical write, response-loss retry and history observation.
2. Record whether an individual tester can add the connector using a URL or
   private workspace distribution. For each route, capture tool discovery,
   OAuth protected-resource metadata, issuer, the client registration method,
   the verified resource audience, visible consent, effective scopes, and
   setup friction. Record whether an administrator must act. Keep bearer
   values, user identity and request bodies out of notes and screenshots.
3. Revoke one product grant and reconnect the same account through a second
   allowed client. Check that both allowed connections resolve to the same
   account. Exercise wrong product, wrong account and revoked access on a read,
   write and retry. Use synthetic records and safe correlation IDs.
4. Ask for a synthetic write. Record the confirmation shown and which action
   required it. Return a fixture response-loss condition after commit, then
   ask the client to retry the same intent. Prove whether the same key reached
   the server using fixture-only correlation. Verify one canonical result.
   Ask for a distinct write and verify that it uses a new key.
5. For Macro, attach a synthetic nutrition-label photo through the client.
   Ask it to show the extracted fields for confirmation and pass only those
   structured fields to the service. Confirm that the tool schema and request
   carry no image bytes, image URLs, or upload identifiers and the backend
   stores no image. Record whether the client could attach a photo and whether
   confirmation was needed for the subsequent write.

Record each actual run in #16 with plan, market, device, plugin visibility,
registration method, OAuth and revocation result, confirmation evidence,
retry result, label-photo result, and any intervention. A pass requires the
client surface itself. If private setup is unavailable, document the least
privileged supported distribution route and keep product endpoints invite-gated.

The reachable fixture, registration, any public listing and the actual
connection runs require separate approval. [Iron's hosted work order](https://github.com/rubotpaul-assistant/iron-goblin/issues/6)
tracks the release gate, and #24 tracks Iron's own staging acceptance.

## Loopback transport rehearsal

Start the disposable MCP fixture from the repository root. Supply a fresh
synthetic bearer value of at least 32 characters through
`IRON_CONNECTOR_FIXTURE_TOKEN` and choose a new temporary database path.

```sh
uv run python -m gym_tracker.connector_fixture --database /tmp/iron-connector-rehearsal.sqlite3
```

The fixture binds to `http://127.0.0.1:8765`, serves readiness at `/healthz`
and accepts stateless MCP POST requests at `/mcp`. It seeds a single synthetic
prescription. Tests cover initialization, discovery, denied credentials,
one recorded result, identical replay and a history read with three sets.

```sh
uv run pytest -q -x --no-cov tests/test_connector_fixture.py
```

The loopback bearer exercises transport and record behavior. The real
client discovery, OAuth and permission flows remain part of the approved
reachable fixture trial.
