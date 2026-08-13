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
app.add_typer(workout_app, name="workout")
app.add_typer(log_app, name="log")
app.add_typer(exercise_app, name="exercise")
app.add_typer(equipment_app, name="equipment")
app.add_typer(history_app, name="history")
app.add_typer(sheets_app, name="sheets")


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


@workout_app.command("complete")
def workout_complete() -> None:
    """Complete a workout after all categories are logged."""
    _run(_service().complete_workout)


@workout_app.command("skip")
def workout_skip() -> None:
    """Skip the active workout."""
    _run(_service().skip_workout)


@log_app.command("exercise")
def log_exercise(
    exercise: Annotated[str, typer.Option("--exercise")],
    weight: Annotated[str, typer.Option("--weight")],
    reps: Annotated[str, typer.Option("--reps")],
    unit: Annotated[str, typer.Option("--unit")] = "lb",
    final_set_rir: Annotated[int | None, typer.Option("--final-set-rir")] = None,
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
    _run(
        _service().log_exercise,
        exercise,
        Decimal(weight),
        parsed,
        unit,
        final_set_rir,
        technical_failure,
    )


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
