# Iron Goblin contributor instructions

## Development

Run commands from the repository root with Python 3.12 and uv.

```sh
uv sync --locked --all-extras --dev
uv run pytest
uv run pre-commit run --all-files
```

Use a focused pytest run to iterate and the full configured gate to verify a change. Keep fixes covered by a reproduction and test public behavior through its normal entry point. For migration work, include representative legacy fixtures and verify canonical records after reopen.

## Domain and data

- Keep the deterministic domain behavior in `gym_tracker/`. The `gym` CLI and any future adapters should share service validation and state transitions.
- Preserve scheduled carry-forward, final-set RIR, progression, and the difference between lifecycle sessions and performed history. A session can appear in performed history while active once it has recorded sets.
- Use `GYM_DATABASE_PATH` to direct integration tests to disposable SQLite. Sheets is a derived projection. Use a fake writer in tests.
- The live OpenClaw skill is managed in Skill Workshop. Preserve documented CLI contracts and verify live skill changes separately when they are in scope.

Keep credentials and user data out of fixtures, diffs, and routine logs. Run local work against fixture databases and fake external integrations. Production data changes, external deployment, and live integration changes require explicit authorization.

## GitHub workflow

Read the associated issue and check the current Git status before editing. Preserve unrelated work. Use a scoped diff, follow existing project names and style, and link the issue and verification results in the PR. Reference related work across the two Goblin repos where appropriate.

Repository-local Codex workflows live in `.agents/skills/`. See the [Adepthood import inventory](docs/adepthood-import.md) for provenance.
