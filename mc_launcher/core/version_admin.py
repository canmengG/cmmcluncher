"""Local version inventory and safe version-folder removal.

Version management deliberately stays inside ``versions/<folder>``: shared
``libraries``, ``assets``, ``resources`` and saves stored in the game root are
never touched.  The folder is first renamed to a hidden trash name (which fails
while the game is running and holds files open), then deleted.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .errors import LauncherError, VersionError
from .models import GameRoot, VersionSummary
from .paths import has_isolated_game_data
from .safety import safe_component
from .version import list_versions

TRASH_PREFIX = ".mc-launcher-delete-"
UNKNOWN_LOADER = "原版"


@dataclass(frozen=True)
class VersionDetails:
    summary: VersionSummary
    size_bytes: int
    file_count: int
    modified: datetime | None
    loader: str
    isolated: bool
    inherits_from: str

    @property
    def folder_name(self) -> str:
        return self.summary.folder_name

    @property
    def version_id(self) -> str:
        return self.summary.version_id

    @property
    def label(self) -> str:
        return self.summary.label

    @property
    def modified_text(self) -> str:
        if self.modified is None:
            return "未知"
        return self.modified.astimezone().strftime("%Y-%m-%d %H:%M")

    @property
    def isolation_text(self) -> str:
        return "是" if self.isolated else "否"


@dataclass
class RemovalResult:
    folder: Path
    version_id: str
    freed_bytes: int
    file_count: int

    @property
    def folder_name(self) -> str:
        return self.folder.name


def _read_version_json(summary: VersionSummary) -> dict:
    try:
        with summary.json_path.open("r", encoding="utf-8-sig") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def detect_loader(summary: VersionSummary) -> str:
    """Best-effort loader name for the version list.

    Folder/id names are the most reliable signal for most installs
    (``fabric-loader-...``, ``quilt-loader-...``, ``neoforge-...``).  NeoForge
    1.20.1 uses the legacy ``1.20.1-forge-<version>`` folder name, so the
    version JSON is consulted before falling back to "Forge".
    """
    name = f"{summary.folder_name} {summary.version_id}".lower()
    if "neoforge" in name:
        return "NeoForge"
    if "quilt" in name:
        return "Quilt"
    if "fabric" in name:
        return "Fabric"
    data = _read_version_json(summary)
    metadata = data.get("mcLauncher")
    if isinstance(metadata, dict):
        label = metadata.get("loaderLabel")
        if isinstance(label, str) and label.strip():
            return label.strip()
    main_class = str(data.get("mainClass") or "").lower()
    libraries = " ".join(
        str(item.get("name") or "")
        for item in data.get("libraries") or []
        if isinstance(item, dict)
    ).lower()
    if "neoforge" in main_class or "neoforged" in libraries:
        return "NeoForge"
    if "quilt" in main_class or "quilt-loader" in libraries:
        return "Quilt"
    if "fabric" in main_class or "fabric-loader" in libraries:
        return "Fabric"
    if "forge" in name or "forge" in main_class or "forge" in libraries:
        return "Forge"
    return UNKNOWN_LOADER


def _folder_usage(folder: Path) -> tuple[int, int, datetime | None]:
    total = 0
    files = 0
    newest: float | None = None
    try:
        if _is_link_like(folder):
            return 0, 0, None
        for root, dirs, names in os.walk(folder, followlinks=False):
            root_path = Path(root)
            dirs[:] = [name for name in dirs if not _is_link_like(root_path / name)]
            for name in names:
                candidate = root_path / name
                try:
                    info = candidate.lstat()
                except OSError:
                    continue
                total += info.st_size
                files += 1
                if newest is None or info.st_mtime > newest:
                    newest = info.st_mtime
    except OSError:
        pass
    modified = (
        datetime.fromtimestamp(newest, tz=timezone.utc) if newest is not None else None
    )
    return total, files, modified


def version_details(root: GameRoot, summary: VersionSummary) -> VersionDetails:
    data = _read_version_json(summary)
    size, files, modified = _folder_usage(summary.folder)
    inherits = data.get("inheritsFrom")
    return VersionDetails(
        summary=summary,
        size_bytes=size,
        file_count=files,
        modified=modified,
        loader=detect_loader(summary),
        isolated=has_isolated_game_data(summary),
        inherits_from=str(inherits) if isinstance(inherits, str) else "",
    )


def list_version_details(root: GameRoot) -> list[VersionDetails]:
    return [version_details(root, summary) for summary in list_versions(root)]


def _is_link_like(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        if hasattr(os.path, "isjunction") and os.path.isjunction(path):  # type: ignore[attr-defined]
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    except OSError:
        return False


def _resolve_folder(root: GameRoot, identifier: str) -> Path:
    text = str(identifier or "").strip()
    if not text:
        raise VersionError("版本 ID 不能为空。")
    if "/" in text or "\\" in text:
        raise VersionError(f"版本 ID 不能包含路径分隔符：{identifier!r}")
    try:
        safe_component(text, what="版本 ID", max_length=128)
    except VersionError:
        # Snapshot ids can contain spaces; safe_component already allows those.
        # The extra check above only rejects separators/traversal.
        raise
    if text in (".", ".."):
        raise VersionError("版本 ID 不能是 . 或 ..")

    versions_root = root.versions_dir
    if not versions_root.is_dir():
        raise LauncherError(f"找不到 versions 文件夹：{versions_root}")

    direct = versions_root / text
    if direct.is_dir():
        return direct
    # Accept a version JSON id that differs from its folder name.
    for summary in list_versions(root):
        if summary.version_id == text:
            return summary.folder
        if summary.folder_name == text:
            return summary.folder
    raise VersionError(f"找不到要删除的版本：{identifier}")


def remove_version(
    root: GameRoot,
    identifier: str,
    *,
    protected_folders: Iterable[Path] = (),
) -> RemovalResult:
    """Delete ``versions/<folder>`` after strict containment checks.

    ``protected_folders`` lets the UI refuse to delete the version whose game
    process is currently running.  A running game also makes the rename fail on
    Windows; that error is reported without deleting anything.
    """
    folder = _resolve_folder(root, identifier)
    versions_root = _resolved(root.versions_dir)
    resolved = _resolved(folder)

    if resolved.parent != versions_root:
        raise VersionError(
            f"拒绝删除：{folder} 不在 versions 目录的直接子目录中。"
        )
    if _is_link_like(folder):
        raise VersionError(
            f"拒绝删除：{folder} 是符号链接或目录联接，"
            "请手动确认它指向的位置。"
        )
    if folder.name.startswith("."):
        raise VersionError(f"拒绝删除隐藏目录：{folder.name}")

    for candidate in protected_folders:
        if candidate is None:
            continue
        if _resolved(Path(candidate)) == resolved:
            raise VersionError(
                f"版本 {folder.name} 正在运行，无法删除。请先关闭游戏。"
            )

    size, files, _modified = _folder_usage(folder)
    trash = root.versions_dir / f"{TRASH_PREFIX}{uuid.uuid4().hex}"
    try:
        os.replace(folder, trash)
    except OSError as exc:
        raise VersionError(
            f"无法删除版本 {folder.name}：目录正被占用或没有权限。\n"
            "请关闭正在运行的游戏后重试。\n"
            f"{exc}"
        ) from exc

    try:
        _remove_tree(trash)
    except OSError as exc:
        restored = False
        if not folder.exists():
            try:
                os.replace(trash, folder)
                restored = True
            except OSError:
                restored = False
        if restored:
            raise VersionError(
                f"删除版本 {folder.name} 失败，目录已恢复：{exc}"
            ) from exc
        raise VersionError(
            f"删除版本 {folder.name} 未完成：{exc}\n"
            f"部分文件可能保留在隐藏目录：{trash}"
        ) from exc

    return RemovalResult(
        folder=folder,
        version_id=folder.name,
        freed_bytes=size,
        file_count=files,
    )


def _remove_tree(path: Path) -> None:
    def on_error(function, target, exc_info):  # type: ignore[no-untyped-def]
        try:
            os.chmod(target, stat.S_IWRITE)
            function(target)
        except OSError:
            raise exc_info[1] if isinstance(exc_info, tuple) else exc_info

    import sys

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=on_error)
    else:  # pragma: no cover - Python 3.10/3.11 compatibility
        shutil.rmtree(path, onerror=on_error)


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def format_version_table(details: Iterable[VersionDetails]) -> str:
    details = list(details)
    if not details:
        return "versions 文件夹中没有可用版本。"
    from .net import human_size

    name_width = max(28, min(48, max(len(item.folder_name) for item in details) + 2))
    header = (
        f"{'版本目录':<{name_width}}{'类型':<10}{'加载器':<10}"
        f"{'大小':>10}  {'修改时间':<17}{'隔离'}"
    )
    lines = [header]
    for item in details:
        lines.append(
            f"{item.folder_name:<{name_width}}{item.summary.version_type or '-':<10}"
            f"{item.loader:<10}{human_size(item.size_bytes):>10}  "
            f"{item.modified_text:<17}{item.isolation_text}"
        )
    lines.append(
        f"共 {len(details)} 个版本，合计 "
        f"{human_size(sum(item.size_bytes for item in details))}。"
    )
    return "\n".join(lines)
