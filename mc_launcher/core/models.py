"""Small data objects shared by the launcher core and UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class GameRoot:
    """The standard layout of a Minecraft installation."""

    root: Path
    versions_dir: Path
    libraries_dir: Path
    assets_dir: Path

    @property
    def runtime_dir(self) -> Path:
        return self.root / "runtime"


@dataclass(frozen=True)
class VersionSummary:
    """A version folder discovered below ``versions/``."""

    folder: Path
    version_id: str
    version_type: str
    release_time: datetime | None
    json_path: Path
    jar_path: Path | None

    @property
    def folder_name(self) -> str:
        return self.folder.name

    @property
    def label(self) -> str:
        suffix = {
            "release": "正式版",
            "snapshot": "快照",
            "old_beta": "旧 Beta",
            "old_alpha": "旧 Alpha",
        }.get(self.version_type, self.version_type or "未知")
        if self.version_id != self.folder_name:
            return f"{self.version_id}  [{suffix}]  ({self.folder_name})"
        return f"{self.version_id}  [{suffix}]"


@dataclass(frozen=True)
class JavaRuntime:
    """A Java executable discovered on the machine."""

    path: Path
    version: str
    major: int
    source: str = ""

    @property
    def display(self) -> str:
        return f"Java {self.version} · {self.path}"


@dataclass(frozen=True)
class MicrosoftAccount:
    """A signed-in Microsoft/Xbox Live account.

    ``access_token`` and ``refresh_token`` are credentials.  They are never
    written to ``config.json`` or the log; persistent storage goes through
    :mod:`mc_launcher.core.secure_store` (DPAPI).
    """

    name: str
    uuid: str
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: float
    xuid: str = ""
    client_id: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "uuid": self.uuid,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "xuid": self.xuid,
            "client_id": self.client_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MicrosoftAccount | None":
        try:
            name = str(data["name"])
            uuid_value = str(data["uuid"])
            access = str(data["access_token"])
            refresh = str(data["refresh_token"])
            expires = float(data["expires_at"])
        except (KeyError, TypeError, ValueError):
            return None
        if not name or not uuid_value or not access or not refresh:
            return None
        return cls(
            name=name,
            uuid=uuid_value,
            access_token=access,
            refresh_token=refresh,
            expires_at=expires,
            xuid=str(data.get("xuid", "") or ""),
            client_id=str(data.get("client_id", "") or ""),
        )


@dataclass(frozen=True)
class LaunchOptions:
    """Everything needed to turn a version into a process command."""

    username: str
    java_path: Path
    memory_mb: int = 2048
    isolated: bool = True
    natives_dir: Path | None = None
    window_width: int = 854
    window_height: int = 480
    demo: bool = False
    check_writable: bool = True
    account: MicrosoftAccount | None = None


@dataclass
class LaunchPlan:
    """A fully resolved command ready to be started."""

    command: list[str]
    cwd: Path
    version_id: str
    game_dir: Path
    assets_dir: Path
    natives_dir: Path
    java_path: Path
    log_config: Path | None = None
    warnings: list[str] = field(default_factory=list)
    temporary_natives: bool = False

    @property
    def command_line(self) -> str:
        return " ".join(_quote(part) for part in self.command)


def _quote(value: str) -> str:
    if not value:
        return '""'
    if any(char in value for char in ' \t"'):
        return '"' + value.replace('"', '\\"') + '"'
    return value
