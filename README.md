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
Use repeated result options to atomically record a completed, backdated bodyweight session—even
when its exercises span both orientations and a scheduled workout is active:

```bash
gym workout log-bodyweight --user geoff --date 2026-10-13 \
  --exercise "Push-Up" --reps 6,6,6 --final-set-rir 3 \
  --exercise "Bodyweight Squat" --reps 10,10,10 --final-set-rir 3 \
  --notes "Low grit bodyweight workout"
```

Every exercise must already exist with equipment type `bodyweight`. Exact retries return the
existing workout with `duplicate: true` instead of writing a second session. Mixed sessions do not
alter horizontal/vertical workout alternation.

Use `gym history correct-date --user <slug> --workout-number <n> --date YYYY-MM-DD` to repair
a legacy performance date through the governed CLI. Strength logging requires final-set RIR from
0 through 3 (`3+` is recorded as 3), and partially performed workouts close as `partial` rather
than `skipped`.

`Strength History` is a projection of performed exercises only. It includes performed-workout
numbers, lifecycle status, Pacific performance dates, and each set's actual load. Sync retires the
deprecated `Workout History` tab while preserving unrelated spreadsheet tabs.

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
ownership and user/date indexes. It also adds exercise performance dates, normalizes all-lowercase
canonical exercise names to Title Case, and distinguishes partial from wholly skipped workouts.
Back up a production database before running a new build against it and retain that backup after
verification.

## Scheduling

Configure OpenClaw to run `gym workout scheduled-begin` at `0 12 * * 1,4` with timezone
`America/Los_Angeles`. Delivery is a separate conversational action based on the returned JSON.
When an older workout is still active, scheduled begin carries it forward and returns
`carried_forward: true` instead of failing or creating a second workout.

The core loop is **RECOMMEND → PERFORM → LOG → ADJUST → REPEAT**.

**LINE GOES UP.**
