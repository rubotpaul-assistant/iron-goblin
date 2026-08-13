"""Machine-readable CLI contract tests."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gym_tracker.cli import app


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


@pytest.mark.parametrize("reps", ["5,wat,5", "5,5"])
def test_cli_rejects_invalid_reps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reps: str
) -> None:
    monkeypatch.setenv("GYM_DATABASE_PATH", str(tmp_path / "gym.sqlite3"))
    result = CliRunner().invoke(
        app, ["log", "exercise", "--exercise", "press", "--weight", "50", "--reps", reps]
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
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["set_weights"] == [45.0, 45.0, 65.0]
