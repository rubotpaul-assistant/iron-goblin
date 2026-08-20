"""Runtime configuration without credential material."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Non-secret settings and references to runtime-managed secrets."""

    database_path: Path
    spreadsheet_id: str | None

    @classmethod
    def from_environment(cls) -> Settings:
        """Load safe settings; authentication libraries read credentials themselves."""
        return cls(
            database_path=Path(os.getenv("GYM_DATABASE_PATH", "data/gym.sqlite3")),
            spreadsheet_id=os.getenv("SPREADSHEET_ID") or os.getenv("GYM_SPREADSHEET_ID"),
        )
