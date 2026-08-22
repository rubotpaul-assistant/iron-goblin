# Iron Goblin

Deterministic workout tracking for OpenClaw:

`PLAIN ENGLISH → OPENCLAW SKILL → CLI → SQLITE → GOOGLE SHEETS`

SQLite owns workout, user, metabolic profile, cardio, and energy state and rules. Sheets is a
derived presentation/backup layer. The OpenClaw
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
`SPREADSHEET_ID` (with `GYM_SPREADSHEET_ID` retained as a compatibility fallback) and the
service-account payload supplied at runtime through
`GOOGLE_SERVICE_ACCOUNT_CREDENTIALS_JSON`. The variable may contain inline JSON or reference a
credential file; the application delegates either form to Google's authentication library. Never
commit credential material.

Use `gym history summary --user <slug> --json` for total workout history. It counts
strength workouts from recorded sets and reports raw session lifecycle rows separately.

## Multi-user cardio

```bash
uv run gym user list --json
uv run gym identity map --platform discord --sender-id 123 --user geoff --json
uv run gym user profile set freedom --height "5ft 6in" --weight "150 lb" \
  --birth-date 1987-04-12 --bmr-sex female --json
uv run gym cardio log --user freedom --activity hiking --duration 41m \
  --distance "1.41 mi" --elevation-gain "240 ft" --rpe 7 \
  --date 2026-08-18 --source alltrails_screenshot --json
uv run gym calories day --user freedom --date 2026-08-18 --json
```

Daily totals are estimates: modeled BMR baseline plus logged exercise. They do not claim to include
every step or spontaneous movement. Imported health-platform exercise calories take precedence
over MET estimates and are counted once.

Database initialization performs the restart-safe legacy migration: it seeds Geoff and Freedom,
backfills every legacy strength session to Geoff, preserves child rows, and installs non-null
ownership and user/date indexes. Back up a production database before running a new build against
it and retain that backup after verification.

## Scheduling

Configure OpenClaw to run `gym workout scheduled-begin` at `0 12 * * 1,4` with timezone
`America/Los_Angeles`. Delivery is a separate conversational action based on the returned JSON.

The core loop is **RECOMMEND → PERFORM → LOG → ADJUST → REPEAT**.

**LINE GOES UP.**
