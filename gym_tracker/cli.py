"""Structured command-line interface owned by the deterministic layer."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

import typer

from gym_tracker.config import Settings
from gym_tracker.db import Database
from gym_tracker.domain import Category, Orientation
from gym_tracker.service import GymError, GymService
from gym_tracker.sheets import GoogleSheetsWriter, SheetsService

app = typer.Typer(no_args_is_help=True)
workout_app = typer.Typer(no_args_is_help=True)
log_app = typer.Typer(no_args_is_help=True)
exercise_app = typer.Typer(no_args_is_help=True)
equipment_app = typer.Typer(no_args_is_help=True)
history_app = typer.Typer(no_args_is_help=True)
sheets_app = typer.Typer(no_args_is_help=True)
user_app = typer.Typer(no_args_is_help=True)
profile_app = typer.Typer(no_args_is_help=True)
identity_app = typer.Typer(no_args_is_help=True)
cardio_app = typer.Typer(no_args_is_help=True)
calories_app = typer.Typer(no_args_is_help=True)
app.add_typer(workout_app, name="workout")
app.add_typer(log_app, name="log")
app.add_typer(exercise_app, name="exercise")
app.add_typer(equipment_app, name="equipment")
app.add_typer(history_app, name="history")
app.add_typer(sheets_app, name="sheets")
app.add_typer(user_app, name="user")
user_app.add_typer(profile_app, name="profile")
app.add_typer(identity_app, name="identity")
app.add_typer(cardio_app, name="cardio")
app.add_typer(calories_app, name="calories")


def _service() -> GymService:
    return GymService(Database(Settings.from_environment().database_path))


def _emit(payload: dict[str, Any]) -> None:
    typer.echo(json.dumps(payload, sort_keys=True))


def _run(operation: Any, *args: Any, **kwargs: Any) -> None:
    try:
        _emit(operation(*args, **kwargs))
    except (GymError, ValueError, InvalidOperation) as error:
        _emit({"ok": False, "error": str(error), "error_type": type(error).__name__})
        raise typer.Exit(code=1) from error


@workout_app.command("begin")
def workout_begin() -> None:
    """Begin a manual workout."""
    _run(_service().begin_workout)


@workout_app.command("scheduled-begin")
def workout_scheduled_begin() -> None:
    """Idempotently begin today's scheduled workout in Pacific time."""
    _run(_service().begin_workout, scheduled=True)


@workout_app.command("current")
def workout_current() -> None:
    """Show the active workout."""
    _run(_service().current_workout)


@workout_app.command("substitute")
def workout_substitute(exercise: Annotated[str, typer.Option("--exercise")]) -> None:
    """Replace an unlogged prescription with a compatible exercise."""
    _run(_service().substitute_exercise, exercise)


@workout_app.command("complete")
def workout_complete() -> None:
    """Complete a workout after all categories are logged."""
    _run(_service().complete_workout)


@workout_app.command("skip")
def workout_skip() -> None:
    """Skip the active workout."""
    _run(_service().skip_workout)


@workout_app.command("log-bodyweight")
def workout_log_bodyweight(
    user: Annotated[str, typer.Option("--user")],
    activity_date: Annotated[str, typer.Option("--date")],
    exercise: Annotated[list[str], typer.Option("--exercise")],
    reps: Annotated[list[str], typer.Option("--reps")],
    final_set_rir: Annotated[list[int], typer.Option("--final-set-rir")],
    notes: Annotated[str, typer.Option("--notes")] = "",
) -> None:
    """Log one completed backdated bodyweight workout atomically."""
    if not exercise or len(exercise) != len(reps) or len(exercise) != len(final_set_rir):
        _run(
            lambda: (_ for _ in ()).throw(
                GymError("provide one --reps and --final-set-rir for every --exercise")
            )
        )
        return
    parsed_reps: list[tuple[int, ...]] = []
    try:
        for value in reps:
            parsed = tuple(int(item.strip()) for item in value.split(","))
            if not parsed or len(parsed) > 20:
                raise GymError("reps must contain between 1 and 20 sets per exercise")
            parsed_reps.append(parsed)
    except ValueError as error:
        _run(lambda: (_ for _ in ()).throw(GymError("reps must be comma-separated integers")))
        raise AssertionError from error
    results = tuple(zip(exercise, parsed_reps, final_set_rir, strict=True))
    _run(_service().log_bodyweight_workout, user, activity_date, results, notes=notes)


@log_app.command("exercise")
def log_exercise(
    exercise: Annotated[str, typer.Option("--exercise")],
    reps: Annotated[str, typer.Option("--reps")],
    final_set_rir: Annotated[int, typer.Option("--final-set-rir")],
    weight: Annotated[str | None, typer.Option("--weight")] = None,
    weights: Annotated[str | None, typer.Option("--weights")] = None,
    unit: Annotated[str, typer.Option("--unit")] = "lb",
    technical_failure: Annotated[bool, typer.Option("--technical-failure")] = False,
) -> None:
    """Log one three-set exercise result."""
    try:
        parsed = tuple(int(value.strip()) for value in reps.split(","))
    except ValueError as error:
        _run(lambda: (_ for _ in ()).throw(GymError("reps must be three comma-separated integers")))
        raise AssertionError from error
    if len(parsed) != 3:
        _run(lambda: (_ for _ in ()).throw(GymError("reps must contain exactly three sets")))
        return
    if (weight is None) == (weights is None):
        _run(
            lambda: (_ for _ in ()).throw(GymError("provide exactly one of --weight or --weights"))
        )
        return
    try:
        parsed_weight: Decimal | tuple[Decimal, Decimal, Decimal]
        if weights is None:
            parsed_weight = Decimal(weight or "")
        else:
            weight_values = tuple(Decimal(value.strip()) for value in weights.split(","))
            if len(weight_values) != 3:
                raise GymError("weights must contain exactly three values")
            parsed_weight = weight_values
    except InvalidOperation as error:
        _run(lambda: (_ for _ in ()).throw(GymError("weights must be numeric")))
        raise AssertionError from error
    _run(
        _service().log_exercise,
        exercise,
        parsed_weight,
        parsed,
        unit,
        final_set_rir,
        technical_failure,
    )


@history_app.command("correct-date")
def history_correct_date(
    user: Annotated[str, typer.Option("--user")],
    workout_number: Annotated[int, typer.Option("--workout-number")],
    activity_date: Annotated[str, typer.Option("--date")],
) -> None:
    """Correct the local performance date for one recorded workout."""
    _run(_service().correct_workout_date, user, workout_number, activity_date)


@exercise_app.command("add")
def exercise_add(
    name: str,
    category: Annotated[Category, typer.Option("--category")],
    orientation: Annotated[Orientation, typer.Option("--orientation")],
    equipment_type: Annotated[str, typer.Option("--equipment-type")],
    unit: Annotated[str, typer.Option("--unit")] = "lb",
    equipment: Annotated[str | None, typer.Option("--equipment")] = None,
) -> None:
    """Add a canonical classified exercise."""
    _run(_service().add_exercise, name, category, orientation, equipment_type, unit, equipment)


@exercise_app.command("search")
def exercise_search(query: str) -> None:
    """Search names and learned aliases."""
    _run(_service().search_exercises, query)


@exercise_app.command("alias-add")
def exercise_alias_add(alias: str, exercise: str) -> None:
    """Attach a phrase to a canonical exercise."""
    _run(_service().add_alias, alias, exercise)


@equipment_app.command("add")
def equipment_add(
    name: str,
    equipment_type: Annotated[str, typer.Option("--type")],
    minimum: Annotated[str, typer.Option("--min")],
    maximum: Annotated[str, typer.Option("--max")],
    increment: Annotated[str, typer.Option("--increment")],
    unit: Annotated[str, typer.Option("--unit")] = "lb",
) -> None:
    """Add equipment and its valid load grid."""
    _run(
        _service().add_equipment,
        name,
        equipment_type,
        Decimal(minimum),
        Decimal(maximum),
        Decimal(increment),
        unit,
    )


@equipment_app.command("list")
def equipment_list() -> None:
    """List equipment."""
    _run(_service().list_equipment)


@equipment_app.command("remove")
def equipment_remove(name: str) -> None:
    """Mark equipment unavailable."""
    _run(_service().remove_equipment, name)


@history_app.command("recent")
def history_recent(limit: Annotated[int, typer.Option("--limit")] = 10) -> None:
    """Show recent workout history."""
    _run(_service().recent_history, limit)


@history_app.command("summary")
def history_summary(
    user: Annotated[str, typer.Option("--user")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Summarize user history from recorded workout results."""
    del json_output
    _run(_service().history_summary, user)


@user_app.command("list")
def user_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List active users."""
    del json_output
    _run(_service().list_users)


@user_app.command("show")
def user_show(user: str, json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show one user without sensitive profile fields."""
    del json_output
    _run(_service().show_user, user)


@profile_app.command("set")
def profile_set(
    user: str,
    height: Annotated[str, typer.Option("--height")],
    weight: Annotated[str, typer.Option("--weight")],
    birth_date: Annotated[str, typer.Option("--birth-date")],
    bmr_sex: Annotated[str, typer.Option("--bmr-sex")],
    baseline_factor: Annotated[str, typer.Option("--baseline-factor")] = "1.20",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Set a normalized metabolic profile."""
    del json_output
    _run(
        _service().set_profile, user, height, weight, birth_date, bmr_sex, Decimal(baseline_factor)
    )


@profile_app.command("show")
def profile_show(user: str, json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show an explicitly requested metabolic profile."""
    del json_output
    _run(_service().profile, user)


@identity_app.command("map")
def identity_map(
    platform: Annotated[str, typer.Option("--platform")],
    sender_id: Annotated[str, typer.Option("--sender-id")],
    user: Annotated[str, typer.Option("--user")],
    display_hint: Annotated[str | None, typer.Option("--display-hint")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Bind a platform identity to a user."""
    del json_output
    _run(_service().map_identity, platform, sender_id, user, display_hint)


@identity_app.command("resolve")
def identity_resolve(
    platform: Annotated[str, typer.Option("--platform")],
    sender_id: Annotated[str, typer.Option("--sender-id")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Resolve a platform identity deterministically."""
    del json_output
    _run(_service().resolve_identity, platform, sender_id)


@cardio_app.command("log")
def cardio_log(
    user: Annotated[str, typer.Option("--user")],
    activity: Annotated[str, typer.Option("--activity")],
    duration: Annotated[str, typer.Option("--duration")],
    rpe: Annotated[int, typer.Option("--rpe")],
    activity_date: Annotated[str, typer.Option("--date")],
    distance: Annotated[str | None, typer.Option("--distance")] = None,
    elevation_gain: Annotated[str | None, typer.Option("--elevation-gain")] = None,
    source: Annotated[str, typer.Option("--source")] = "manual",
    source_external_id: Annotated[str | None, typer.Option("--source-external-id")] = None,
    source_message_id: Annotated[str | None, typer.Option("--source-message-id")] = None,
    notes: Annotated[str, typer.Option("--notes")] = "",
    submitted_by: Annotated[str | None, typer.Option("--submitted-by")] = None,
    imported_calories: Annotated[str | None, typer.Option("--imported-calories")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Log one validated cardio activity."""
    del json_output
    _run(
        _service().log_cardio,
        user=user,
        activity=activity,
        duration=duration,
        rpe=rpe,
        activity_date=activity_date,
        distance=distance,
        elevation_gain=elevation_gain,
        source=source,
        source_external_id=source_external_id,
        source_message_id=source_message_id,
        notes=notes,
        submitted_by=submitted_by,
        imported_calories=None if imported_calories is None else Decimal(imported_calories),
    )


@cardio_app.command("recent")
def cardio_recent(
    user: Annotated[str, typer.Option("--user")],
    limit: Annotated[int, typer.Option("--limit")] = 10,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show user-scoped cardio history."""
    del json_output
    _run(_service().recent_cardio, user, limit)


@calories_app.command("bmr")
def calories_bmr(
    user: str,
    activity_date: Annotated[str, typer.Option("--date")] = "today",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Calculate one user's BMR for a date."""
    del json_output
    _run(_service().bmr, user, activity_date)


@calories_app.command("day")
def calories_day(
    user: Annotated[str, typer.Option("--user")],
    activity_date: Annotated[str, typer.Option("--date")] = "today",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Return estimated daily burn components."""
    del json_output
    _run(_service().daily_calories, user, activity_date)


@sheets_app.command("status")
def sheets_status() -> None:
    """Show safe synchronization metadata."""
    settings = Settings.from_environment()
    _run(
        SheetsService(
            Database(settings.database_path), settings.spreadsheet_id, GoogleSheetsWriter()
        ).status
    )


@sheets_app.command("sync")
def sheets_sync() -> None:
    """Explicitly project SQLite history to Google Sheets."""
    settings = Settings.from_environment()
    _run(
        SheetsService(
            Database(settings.database_path), settings.spreadsheet_id, GoogleSheetsWriter()
        ).sync
    )


if __name__ == "__main__":
    app()
