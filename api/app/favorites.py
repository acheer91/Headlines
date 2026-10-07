"""Favorites: config/favorites.json, e.g. {"nfl": ["NE"], "ncaaf": ["TEX"]} (team abbreviations).

Read on every call, so an edit takes effect without a rebuild. Shared by the API and the Temporal worker.
The path is worked out inside load(), never at import: workflows.py imports the activities module, and
Temporal's workflow sandbox refuses filesystem calls such as Path.resolve() during that import.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def default_file() -> Path:
    return Path(os.environ.get("FAVORITES_FILE",
                               Path(__file__).resolve().parents[2] / "config" / "favorites.json"))


def load(path: Path | None = None) -> dict[str, list[str]]:
    try:
        data = json.loads((path or default_file()).read_text())
        return {k: [a.upper() for a in v] for k, v in data.items() if isinstance(v, list)}
    except (OSError, ValueError, AttributeError):   # AttributeError: a non-string entry or a non-object file
        return {}
