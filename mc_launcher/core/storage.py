"""Local application data locations."""

from __future__ import annotations

import os
from pathlib import Path


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / "MinecraftLauncher"
    return Path.home() / ".minecraft_launcher"


def cache_dir(source_id: str | None = None) -> Path:
    base = app_data_dir() / "cache"
    if source_id:
        base = base / source_id
    return base


def installer_cache_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "MinecraftLauncher" / "installers"
    return app_data_dir() / "installers"


def log_dir() -> Path:
    return app_data_dir() / "logs"


def log_file() -> Path:
    return log_dir() / "launcher.log"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
