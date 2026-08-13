# Iron Goblin

Deterministic workout tracking for OpenClaw:

`PLAIN ENGLISH → OPENCLAW SKILL → CLI → SQLITE → GOOGLE SHEETS`

SQLite owns workout state and rules. Sheets is a derived presentation/backup layer. The OpenClaw
skill translates conversation into structured CLI calls; it never calculates or mutates state.
The companion `iron-goblin` OpenClaw skill is managed through Skill Workshop rather than installed
from this repository.

## Development

```bash
uv sync --all-extras --dev
uv run pytest
uv run pre-commit run --all-files
```

Use `GYM_DATABASE_PATH` to override `data/gym.sqlite3`. Sheets sync additionally requires
`GYM_SPREADSHEET_ID` and the service-account payload supplied at runtime through
`GOOGLE_SERVICE_ACCOUNT_CREDENTIALS_JSON`. Never commit credential material.

## Scheduling

Configure OpenClaw to run `gym workout scheduled-begin` at `0 12 * * 1,4` with timezone
`America/Los_Angeles`. Delivery is a separate conversational action based on the returned JSON.

The core loop is **RECOMMEND → PERFORM → LOG → ADJUST → REPEAT**.

**LINE GOES UP.**
