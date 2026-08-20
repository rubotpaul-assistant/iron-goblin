"""Runtime configuration tests."""

import pytest

from gym_tracker.config import Settings


def test_spreadsheet_id_uses_shared_runtime_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Iron Goblin honors the workspace-standard spreadsheet reference."""
    monkeypatch.setenv("SPREADSHEET_ID", "shared-sheet-id")
    monkeypatch.delenv("GYM_SPREADSHEET_ID", raising=False)

    assert Settings.from_environment().spreadsheet_id == "shared-sheet-id"


def test_legacy_spreadsheet_id_remains_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing deployments using the old app-specific name keep working."""
    monkeypatch.delenv("SPREADSHEET_ID", raising=False)
    monkeypatch.setenv("GYM_SPREADSHEET_ID", "legacy-sheet-id")

    assert Settings.from_environment().spreadsheet_id == "legacy-sheet-id"


def test_shared_spreadsheet_id_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented shared reference wins when both names are present."""
    monkeypatch.setenv("SPREADSHEET_ID", "shared-sheet-id")
    monkeypatch.setenv("GYM_SPREADSHEET_ID", "legacy-sheet-id")

    assert Settings.from_environment().spreadsheet_id == "shared-sheet-id"
