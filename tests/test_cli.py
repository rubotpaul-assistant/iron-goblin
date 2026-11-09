"""Machine-readable CLI contract tests."""

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gym_tracker.cli import app

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def test_cli_returns_json_and_never_environment_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GYM_DATABASE_PATH", str(tmp_path / "gym.sqlite3"))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "do-not-leak.json")
    result = CliRunner().invoke(app, ["equipment", "list"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {"equipment": [], "ok": True}
    assert "do-not-leak" not in result.stdout


def test_cli_reports_structured_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GYM_DATABASE_PATH", str(tmp_path / "gym.sqlite3"))
    result = CliRunner().invoke(app, ["workout", "current"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "no active workout"


def test_history_summary_is_user_scoped_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GYM_DATABASE_PATH", str(tmp_path / "gym.sqlite3"))

    result = CliRunner().invoke(app, ["history", "summary", "--user", "geoff", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["user"] == "geoff"


def test_workout_substitute_command_is_exposed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GYM_DATABASE_PATH", str(tmp_path / "gym.sqlite3"))
    runner = CliRunner()
    runner.invoke(
        app,
        [
            "exercise",
            "add",
            "original push",
            "--category",
            "push",
            "--orientation",
            "vertical",
            "--equipment-type",
            "machine",
        ],
    )
    runner.invoke(app, ["workout", "begin"])
    runner.invoke(
        app,
        [
            "exercise",
            "add",
            "replacement push",
            "--category",
            "push",
            "--orientation",
            "vertical",
            "--equipment-type",
            "dumbbell",
        ],
    )

    result = runner.invoke(app, ["workout", "substitute", "--exercise", "replacement push"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["exercise"] == "Replacement Push"


def test_cli_corrects_recorded_workout_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GYM_DATABASE_PATH", str(tmp_path / "gym.sqlite3"))
    runner = CliRunner()
    assert (
        runner.invoke(
            app,
            [
                "exercise",
                "add",
                "Chest Press",
                "--category",
                "push",
                "--orientation",
                "vertical",
                "--equipment-type",
                "machine",
            ],
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["workout", "begin"]).exit_code == 0
    assert (
        runner.invoke(
            app,
            [
                "log",
                "exercise",
                "--exercise",
                "Chest Press",
                "--reps",
                "5,5,3",
                "--weights",
                "100,100,100",
                "--final-set-rir",
                "0",
                "--technical-failure",
            ],
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["workout", "skip"]).exit_code == 0

    result = runner.invoke(
        app,
        [
            "history",
            "correct-result",
            "--user",
            "geoff",
            "--workout-number",
            "1",
            "--exercise",
            "Chest Press",
            "--reps",
            "5,5,3",
            "--weights",
            "100,100,115",
            "--final-set-rir",
            "0",
            "--technical-failure",
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["weights"] == [100.0, 100.0, 115.0]
    assert payload["technical_failure"] is True


@pytest.mark.parametrize(
    ("option", "value", "message"),
    [
        ("--reps", "five,5,3", "comma-separated integers"),
        ("--weights", "100,heavy,115", "weights must be numeric"),
    ],
)
def test_cli_result_correction_rejects_malformed_sets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    option: str,
    value: str,
    message: str,
) -> None:
    monkeypatch.setenv("GYM_DATABASE_PATH", str(tmp_path / "gym.sqlite3"))
    command = [
        "history",
        "correct-result",
        "--user",
        "geoff",
        "--workout-number",
        "1",
        "--exercise",
        "Chest Press",
        "--reps",
        "5,5,3",
        "--weights",
        "100,100,115",
        "--final-set-rir",
        "0",
    ]
    command[command.index(option) + 1] = value

    result = CliRunner().invoke(app, command)

    assert result.exit_code == 1
    assert message in json.loads(result.stdout)["error"]


def test_cli_end_to_end_workout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GYM_DATABASE_PATH", str(tmp_path / "gym.sqlite3"))
    runner = CliRunner()
    commands = [
        [
            "equipment",
            "add",
            "press",
            "--type",
            "machine",
            "--min",
            "10",
            "--max",
            "100",
            "--increment",
            "10",
        ],
        [
            "exercise",
            "add",
            "overhead press",
            "--category",
            "push",
            "--orientation",
            "vertical",
            "--equipment-type",
            "machine",
            "--equipment",
            "press",
        ],
        ["exercise", "alias-add", "press thing", "overhead press"],
        ["exercise", "search", "press thing"],
        ["workout", "begin"],
        [
            "log",
            "exercise",
            "--exercise",
            "press thing",
            "--weight",
            "50",
            "--reps",
            "5,5,5",
            "--final-set-rir",
            "1",
        ],
        ["workout", "skip"],
        ["history", "recent", "--limit", "1"],
        ["equipment", "remove", "press"],
    ]
    for command in commands:
        result = runner.invoke(app, command)
        assert result.exit_code == 0, result.stdout
        assert json.loads(result.stdout)["ok"] is True


def test_cli_adds_irregular_loads_and_assigns_existing_exercise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GYM_DATABASE_PATH", str(tmp_path / "gym.sqlite3"))
    runner = CliRunner()
    exercise = runner.invoke(
        app,
        [
            "exercise",
            "add",
            "assisted dip",
            "--category",
            "push",
            "--orientation",
            "vertical",
            "--equipment-type",
            "machine",
        ],
    )
    equipment = runner.invoke(
        app,
        [
            "equipment",
            "add",
            "assisted dip stack",
            "--type",
            "machine",
            "--min",
            "30",
            "--max",
            "70",
            "--increment",
            "5",
            "--loads",
            "30,45,55,70",
            "--direction",
            "decrease",
        ],
    )
    assigned = runner.invoke(
        app,
        [
            "equipment",
            "assign",
            "--exercise",
            "assisted dip",
            "--equipment",
            "assisted dip stack",
        ],
    )

    assert exercise.exit_code == equipment.exit_code == assigned.exit_code == 0
    listed = runner.invoke(app, ["equipment", "list"])
    assert json.loads(listed.stdout)["equipment"][0]["supported_weights"] == [30, 45, 55, 70]
    assert json.loads(listed.stdout)["equipment"][0]["progression_direction"] == "decrease"


def test_cli_rejects_non_numeric_equipment_loads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GYM_DATABASE_PATH", str(tmp_path / "gym.sqlite3"))

    result = CliRunner().invoke(
        app,
        [
            "equipment",
            "add",
            "broken stack",
            "--type",
            "machine",
            "--min",
            "10",
            "--max",
            "100",
            "--increment",
            "5",
            "--loads",
            "10,nope,20",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == "loads must be comma-separated numbers"


@pytest.mark.parametrize("reps", ["5,wat,5", "5,5"])
def test_cli_rejects_invalid_reps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reps: str
) -> None:
    monkeypatch.setenv("GYM_DATABASE_PATH", str(tmp_path / "gym.sqlite3"))
    result = CliRunner().invoke(
        app,
        [
            "log",
            "exercise",
            "--exercise",
            "press",
            "--weight",
            "50",
            "--reps",
            reps,
            "--final-set-rir",
            "1",
        ],
    )
    assert result.exit_code == 1
    assert "three" in json.loads(result.stdout)["error"]


def test_cli_accepts_three_set_weights(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The JSON CLI accepts a distinct load for every set."""
    monkeypatch.setenv("GYM_DATABASE_PATH", str(tmp_path / "gym.sqlite3"))
    runner = CliRunner()
    assert (
        runner.invoke(
            app,
            [
                "exercise",
                "add",
                "squat",
                "--category",
                "legs",
                "--orientation",
                "vertical",
                "--equipment-type",
                "barbell",
            ],
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["workout", "begin"]).exit_code == 0
    result = runner.invoke(
        app,
        [
            "log",
            "exercise",
            "--exercise",
            "squat",
            "--weights",
            "45,45,65",
            "--reps",
            "5,5,5",
            "--final-set-rir",
            "1",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["set_weights"] == [45.0, 45.0, 65.0]


def test_cli_requires_final_set_rir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("GYM_DATABASE_PATH", str(tmp_path / "gym.sqlite3"))

    result = CliRunner().invoke(
        app,
        ["log", "exercise", "--exercise", "press", "--weight", "50", "--reps", "5,5,5"],
        color=True,
    )

    assert result.exit_code == 2
    assert "--final-set-rir" in ANSI_ESCAPE.sub("", result.output)


def test_cli_corrects_performed_workout_date(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GYM_DATABASE_PATH", str(tmp_path / "gym.sqlite3"))
    runner = CliRunner()
    runner.invoke(
        app,
        [
            "exercise",
            "add",
            "press",
            "--category",
            "push",
            "--orientation",
            "vertical",
            "--equipment-type",
            "dumbbell",
        ],
    )
    runner.invoke(app, ["workout", "begin"])
    runner.invoke(
        app,
        [
            "log",
            "exercise",
            "--exercise",
            "press",
            "--weight",
            "10",
            "--reps",
            "5,5,5",
            "--final-set-rir",
            "1",
        ],
    )
    runner.invoke(app, ["workout", "skip"])

    result = runner.invoke(
        app,
        [
            "history",
            "correct-date",
            "--user",
            "geoff",
            "--workout-number",
            "1",
            "--date",
            "2026-09-08",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["exercises_updated"] == 1


def test_cli_logs_backdated_mixed_bodyweight_workout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GYM_DATABASE_PATH", str(tmp_path / "gym.sqlite3"))
    runner = CliRunner()
    for command in (
        [
            "exercise",
            "add",
            "Push-Up",
            "--category",
            "push",
            "--orientation",
            "horizontal",
            "--equipment-type",
            "bodyweight",
        ],
        [
            "exercise",
            "add",
            "Bodyweight Squat",
            "--category",
            "legs",
            "--orientation",
            "vertical",
            "--equipment-type",
            "bodyweight",
        ],
    ):
        assert runner.invoke(app, command).exit_code == 0

    result = runner.invoke(
        app,
        [
            "workout",
            "log-bodyweight",
            "--user",
            "geoff",
            "--date",
            "2026-10-13",
            "--exercise",
            "Push-Up",
            "--reps",
            "6,6,6",
            "--final-set-rir",
            "3",
            "--exercise",
            "Bodyweight Squat",
            "--reps",
            "8,8,8,8",
            "--final-set-rir",
            "3",
            "--notes",
            "Low grit bodyweight workout",
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["date"] == "2026-10-13"
    assert payload["orientation"] == "mixed"
    assert [item["exercise"] for item in payload["exercises"]] == [
        "Push-Up",
        "Bodyweight Squat",
    ]
    assert payload["exercises"][1]["reps"] == [8, 8, 8, 8]


def test_cli_rejects_unpaired_bodyweight_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GYM_DATABASE_PATH", str(tmp_path / "gym.sqlite3"))

    result = CliRunner().invoke(
        app,
        [
            "workout",
            "log-bodyweight",
            "--user",
            "geoff",
            "--date",
            "2026-10-13",
            "--exercise",
            "Push-Up",
            "--exercise",
            "Bodyweight Squat",
            "--reps",
            "6,6,6",
            "--final-set-rir",
            "3",
        ],
    )

    assert result.exit_code == 1
    assert "one --reps and --final-set-rir" in json.loads(result.stdout)["error"]


def test_cli_multiuser_cardio_energy_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GYM_DATABASE_PATH", str(tmp_path / "gym.sqlite3"))
    runner = CliRunner()
    commands = [
        ["user", "list", "--json"],
        [
            "identity",
            "map",
            "--platform",
            "discord",
            "--sender-id",
            "579",
            "--user",
            "geoff",
            "--json",
        ],
        ["identity", "resolve", "--platform", "discord", "--sender-id", "579", "--json"],
        [
            "user",
            "profile",
            "set",
            "freedom",
            "--height",
            "5ft 6in",
            "--weight",
            "150 lb",
            "--birth-date",
            "1987-04-12",
            "--bmr-sex",
            "female",
            "--json",
        ],
        [
            "cardio",
            "log",
            "--user",
            "freedom",
            "--activity",
            "hiking",
            "--duration",
            "41m",
            "--distance",
            "1.41 mi",
            "--elevation-gain",
            "240 ft",
            "--rpe",
            "7",
            "--date",
            "2026-08-18",
            "--source",
            "alltrails_screenshot",
            "--json",
        ],
        ["cardio", "recent", "--user", "freedom", "--json"],
        ["calories", "bmr", "freedom", "--date", "2026-08-18", "--json"],
        ["calories", "day", "--user", "freedom", "--date", "2026-08-18", "--json"],
    ]
    payloads = []
    for command in commands:
        result = runner.invoke(app, command)
        assert result.exit_code == 0, result.stdout
        payloads.append(json.loads(result.stdout))
    assert payloads[2]["user"] == "geoff"
    assert payloads[4]["canonical_activity"] == "hiking"
    assert payloads[-1]["label"] == "Estimated daily burn"
