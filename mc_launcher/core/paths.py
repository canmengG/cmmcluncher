"""Game-directory discovery and version-isolation helpers."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from .errors import LauncherError
from .models import GameRoot, VersionSummary

# A version folder containing any of these is treated as an isolated game
# directory.  These are the files/folders Minecraft itself creates.
ISOLATION_MARKERS = (
    ".minecraft",
    ".mc-launcher-modpack.json",
    "PCL",
    "HMCL",
    ".hmcl",
    "mods",
    "saves",
    "config",
    "options.txt",
    "resourcepacks",
    "shaderpacks",
    "logs",
    "crash-reports",
)


def normalize_game_root(selected: str | Path) -> GameRoot:
    """Find the standard ``versions``/``libraries`` root from a selection.

    Users commonly select either the real game root, its parent folder, the
    ``versions`` folder, or a single version folder.  All of those are accepted
    as long as a standard root can be found.
    """
    path = Path(selected).expanduser()
    if not path.exists():
        raise LauncherError(f"游戏目录不存在：{path}")
    if path.is_file():
        path = path.parent
    path = path.resolve()

    candidates: list[Path] = [path]

    if path.name.lower() == "versions":
        candidates.append(path.parent)
    if (path / ".minecraft").is_dir():
        candidates.insert(0, path / ".minecraft")
    if path.parent.name.lower() == "versions":
        candidates.append(path.parent.parent)
    if (path / ".minecraft" / "versions").is_dir():
        candidates.insert(0, path / ".minecraft")

    seen: set[Path] = set()
    fallback: Path | None = None
    for candidate in candidates:
        try:
            candidate = candidate.resolve()
        except OSError:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        if (candidate / "versions").is_dir():
            fallback = fallback or candidate
            if (candidate / "libraries").is_dir():
                return GameRoot(
                    root=candidate,
                    versions_dir=candidate / "versions",
                    libraries_dir=candidate / "libraries",
                    assets_dir=candidate / "assets",
                )

    if fallback is not None:
        raise LauncherError(
            "找到了 versions 文件夹，但缺少 libraries 文件夹。\n"
            f"请选择完整的游戏根目录：{fallback}"
        )
    raise LauncherError(
        "没有找到 Minecraft 游戏目录。\n"
        "请选择包含 versions、libraries、assets 的文件夹，"
        "例如 .minecraft。"
    )


def game_root_path(selected: str | Path) -> Path:
    """Resolve the standard game root without creating any directory."""
    path = Path(selected).expanduser()
    if path.exists() and path.is_file():
        raise LauncherError(f"游戏目录不能是文件：{path}")
    try:
        path = path.resolve()
    except OSError:
        path = path.absolute()
    if path.name.lower() == ".minecraft":
        return path
    if (path / ".minecraft").is_dir():
        return path / ".minecraft"
    if path.name.lower() == "versions":
        return path.parent
    if path.parent.name.lower() == "versions":
        return path.parent.parent
    return path


def ensure_game_root(selected: str | Path) -> GameRoot:
    """Create or accept a game root for a fresh download.

    Unlike :func:`normalize_game_root`, this does not require ``versions`` to
    already exist; it creates ``versions``, ``libraries`` and ``assets``.
    """
    root = game_root_path(selected)
    try:
        root.mkdir(parents=True, exist_ok=True)
        for name in ("versions", "libraries", "assets"):
            (root / name).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise LauncherError(
            f"无法创建游戏目录：{root}\n{exc}\n"
            "请选择有写入权限的位置，并避免从受限沙箱启动。"
        ) from exc
    return GameRoot(
        root=root,
        versions_dir=root / "versions",
        libraries_dir=root / "libraries",
        assets_dir=root / "assets",
    )


def version_game_dir(summary: VersionSummary) -> Path:
    """Return the game directory used when version isolation is enabled."""
    nested = summary.folder / ".minecraft"
    if nested.is_dir():
        return nested
    return summary.folder


def has_isolated_game_data(summary: VersionSummary) -> bool:
    """Heuristically detect whether a version should use its own game dir.

    A folder containing game data (``mods/``, ``saves/``, ``options.txt`` ...)
    is obviously isolated.  Loader/modpack profiles also inherit from another
    version and are treated as isolated by default even before they have
    created any game data: otherwise a freshly installed NeoForge/Quilt version
    would silently read the shared ``.minecraft/mods`` folder.  Users can still
    uncheck "版本隔离" to deliberately share the root game directory.
    """
    if any((summary.folder / marker).exists() for marker in ISOLATION_MARKERS):
        return True
    try:
        with summary.json_path.open("r", encoding="utf-8-sig") as file:
            data = json.load(file)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and bool(str(data.get("inheritsFrom") or "").strip())


def resolve_game_dir(
    root: GameRoot, summary: VersionSummary, isolated: bool
) -> Path:
    if isolated:
        game_dir = version_game_dir(summary)
    else:
        game_dir = root.root
    game_dir.mkdir(parents=True, exist_ok=True)
    return game_dir


def ensure_directory_writable(directory: Path) -> None:
    """Fail early when Minecraft will not be able to write logs/saves.

    A sandbox (or a read-only folder) can allow reading the client jar and
    libraries while denying the game's own writes.  Minecraft then crashes much
    later with a confusing ``logs/latest.log: AccessDenied`` message, so probe
    the directory before starting Java.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".mc-launcher-write-test-{uuid.uuid4().hex}"
        try:
            with probe.open("xb") as file:
                file.write(b"ok")
                file.flush()
        finally:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError as exc:
        raise LauncherError(
            f"游戏目录没有写入权限：\n{directory}\n\n"
            "Minecraft 启动时必须在这里创建 logs、downloads、crash-reports。\n"
            "如果启动器是从 DSH 工作区或受限沙箱中打开的，"
            "请改为在 Windows 资源管理器中直接双击 exe，"
            "或把启动器放到游戏目录所在位置后再运行。\n"
            "如果仍失败，请检查文件夹的 Windows 权限或安全软件拦截。\n"
            f"系统错误：{exc.strerror or exc}"
        ) from exc

