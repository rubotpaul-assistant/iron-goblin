# Strength history data-integrity defects

## Problem statement

The canonical SQLite database projects prescribed exercises as history rows, uses workout
planning dates for exercises performed later, stores inconsistent canonical-name casing,
collapses per-set weights, exposes lifecycle IDs as workout numbers, and leaves a deprecated
Google Sheets tab in place. Partially performed workouts are labeled wholly skipped. The strength
logging boundary also accepts missing or out-of-range final-set RIR.

## Reproduction

Run `gym sheets sync` against the current database and inspect `Strength History`. Empty rows
appear for skipped prescriptions, workout 4 is dated 2026-08-24 despite its results being
performed on 2026-09-08, lower-case exercise names coexist with Title Case names, and ramped
weights cannot be reconstructed.

## Root cause

`SheetsService._history_rows` left-joins all prescriptions and groups missing sets into empty
results. `workout_sessions.local_date` represents lifecycle creation, not exercise performance.
Exercise names are inserted verbatim, set weights are reduced with `MAX`, and sync has no legacy
projection retirement. `skip_workout` always emits `skipped`, regardless of recorded sets. RIR
validation is optional and has no maximum.

## Impact

The sheet overstates performed work, misdates real work, loses set-level evidence, and presents
inconsistent identifiers and names. Missing exertion data also weakens future progression logic.

## Contributing factors

Tests asserted happy-path row counts but did not include skipped prescriptions, carried-forward
sessions, mixed casing, ramped weights, or the full Google tab lifecycle.

## Fix strategy

Persist the local performance date at logging time; provide a governed CLI correction for legacy
records; export only completed exercises with performed-workout ordinals, lifecycle status, and
per-set loads; normalize all-lowercase canonical names; require RIR 0–3; and retire only the exact
deprecated tab during sync.

## Prevention

Keep regression tests at service, migration, CLI, and projection boundaries. Treat lifecycle
records and performed results as separate concepts in every query.
