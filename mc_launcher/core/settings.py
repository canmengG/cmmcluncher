"""Per-user configuration helpers shared by the UI and the CLI.

The file lives next to the existing launcher config (``config.json``).  It is
deliberately tiny and dependency-free; secrets such as the Microsoft refresh
token are *not* stored here (see :mod:`mc_launcher.core.secure_store`).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .storage import app_data_dir

ENV_CLIENT_ID = "MC_LAUNCHER_MS_CLIENT_ID"
CONFIG_NAME = "config.json"


def config_path() -> Path:
    return app_data_dir() / CONFIG_NAME


def load_config() -> dict:
    try:
        with config_path().open("r", encoding="utf-8-sig") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(data: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def saved_client_id() -> str:
    value = load_config().get("ms_client_id", "")
    return value.strip() if isinstance(value, str) else ""


def save_client_id(client_id: str) -> None:
    data = load_config()
    data["ms_client_id"] = client_id.strip()
    save_config(data)


def resolve_client_id(explicit: str | None = None) -> str:
    """Explicit argument > environment variable > saved config."""
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get(ENV_CLIENT_ID, "").strip()
    if env:
        return env
    return saved_client_id()
