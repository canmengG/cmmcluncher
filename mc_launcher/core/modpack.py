"""Launchable modpack instances (0.5.x).

A modpack is installed as a normal Minecraft version folder, not as a loose
``modpacks/`` directory::

    versions/<instance-id>/
        <instance-id>.json          # inheritsFrom the installed loader profile
        mods/ config/ saves/ ...    # this folder *is* the isolated game dir
        .mc-launcher-modpack.json

The instance JSON inherits from the exact Fabric/Forge profile that was just
installed, so the existing launcher code resolves libraries, the client jar and
arguments through the normal ``inheritsFrom`` chain.  The version folder itself
is used as the game directory, matching PCL/HMCL-style isolated versions;
``version_game_dir()`` already falls back to the version folder when there is
no nested ``.minecraft``.  Deleting the version removes its mods/saves/config.

0.5.1 instances that already contain a nested ``.minecraft`` are detected and
kept in that layout for repair, so upgrading does not orphan existing content.

0.6.0 installs NeoForge and Quilt as well: NeoForge reuses the Forge-style
installer pipeline, while Quilt uses its profile JSON like Fabric.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .content import (
    ContentError,
    ContentFile,
    ContentProgress,
    ContentProject,
    ContentVersion,
    ModpackIndex,
    ModpackOutcome,
    ModpackPlan,
    content_instance_name,
    download_modpack_archive,
    install_modpack,
    read_modpack_marker,
    read_mrpack,
    write_modpack_marker,
)
from .errors import VersionError
from .loaders import install_selected
from .mirrors import DownloadSource, get_source
from .models import GameRoot
from .net import HttpClient, PauseToken
from .paths import ensure_game_root
from .safety import is_within, safe_component

# `dependencies` key -> (internal loader id, display name)
LOADER_KEYS: tuple[tuple[str, str, str], ...] = (
    ("fabric-loader", "fabric", "Fabric"),
    ("forge", "forge", "Forge"),
    ("neoforge", "neoforge", "NeoForge"),
    ("quilt-loader", "quilt", "Quilt"),
)
LAUNCHABLE_LOADERS = {"vanilla", "fabric", "forge", "neoforge", "quilt"}
CONTENT_ONLY_LOADERS: set[str] = set()


@dataclass(frozen=True)
class ModpackDependencies:
    minecraft: str
    loader: str = "vanilla"
    loader_version: str = ""
    launchable: bool = True
    reason: str = ""

    @property
    def loader_label(self) -> str:
        if self.loader == "vanilla":
            return "原版"
        for _key, loader, label in LOADER_KEYS:
            if loader == self.loader:
                return f"{label} {self.loader_version}".strip()
        return self.loader

    @property
    def summary(self) -> str:
        return f"Minecraft {self.minecraft} / {self.loader_label}"


@dataclass(frozen=True)
class ModpackInstancePlan:
    index: ModpackIndex
    game_root: Path
    instance_id: str
    version_dir: Path
    game_dir: Path
    dependencies: ModpackDependencies
    overwrite: bool = False
    launch: bool = True

    @property
    def version_json_path(self) -> Path:
        return self.version_dir / f"{self.instance_id}.json"

    @property
    def marker_path(self) -> Path:
        return self.game_dir / ".mc-launcher-modpack.json"

    @property
    def launchable(self) -> bool:
        return bool(self.launch and self.dependencies.launchable)


@dataclass
class LoaderInstallResult:
    version_id: str
    warnings: list[str] = field(default_factory=list)


def resolve_modpack_dependencies(index: ModpackIndex) -> ModpackDependencies:
    """Turn ``modrinth.index.json`` dependencies into an install decision."""
    raw_minecraft = index.dependencies.get("minecraft", "")
    try:
        minecraft = safe_component(
            raw_minecraft, what="整合包 Minecraft 版本", max_length=120
        )
    except VersionError as exc:
        raise ContentError(
            f"整合包没有声明有效的 Minecraft 版本：{raw_minecraft!r}"
        ) from exc

    declared = [
        (key, loader, label)
        for key, loader, label in LOADER_KEYS
        if index.dependencies.get(key)
    ]
    if len(declared) > 1:
        names = " / ".join(item[2] for item in declared)
        raise ContentError(
            f"整合包同时声明了多个加载器（{names}），无法确定要安装哪一个。"
        )
    if not declared:
        return ModpackDependencies(minecraft=minecraft)

    key, loader, label = declared[0]
    raw_version = index.dependencies.get(key, "")
    try:
        loader_version = safe_component(
            raw_version, what=f"{label} 版本", max_length=120
        )
    except VersionError as exc:
        raise ContentError(f"整合包声明的 {label} 版本无效：{raw_version!r}") from exc

    if loader in CONTENT_ONLY_LOADERS:
        reason = (
            f"整合包使用 {label} {loader_version}；当前版本只整理整合包内容，"
            "没有创建可启动版本。"
        )
        return ModpackDependencies(
            minecraft=minecraft,
            loader=loader,
            loader_version=loader_version,
            launchable=False,
            reason=reason,
        )
    if loader not in LAUNCHABLE_LOADERS:
        return ModpackDependencies(
            minecraft=minecraft,
            loader=loader,
            loader_version=loader_version,
            launchable=False,
            reason=f"暂不支持自动安装加载器 {label}。",
        )
    return ModpackDependencies(
        minecraft=minecraft,
        loader=loader,
        loader_version=loader_version,
    )


def _base_instance_name(index: ModpackIndex, slug: str = "") -> str:
    base = (slug or index.name or index.archive.stem or "modpack").strip()
    name = content_instance_name(base, index.version_id)
    try:
        return safe_component(name, what="整合包实例名", max_length=120)
    except VersionError:
        # `safe_component` rejects path separators/reserved names; fall back to a
        # conservative name instead of failing before the user can install.
        cleaned = "".join(
            char if char.isalnum() or char in "._-" else "-" for char in name
        ).strip(".-")
        return safe_component(
            cleaned[:110] or "modpack", what="整合包实例名", max_length=120
        )


def _existing_instance_game_dir(
    version_dir: Path, index: ModpackIndex
) -> Path | None:
    """Return the game directory of an existing instance of *this* pack.

    New instances use ``versions/<id>`` itself.  0.5.1 created a nested
    ``.minecraft`` folder; keep using it when repairing such an instance.
    """
    for game_dir in (version_dir, version_dir / ".minecraft"):
        marker = read_modpack_marker(game_dir)
        if marker is None:
            continue
        if (
            str(marker.get("name") or "") == index.name
            and str(marker.get("version_id") or "") == index.version_id
        ):
            return game_dir
    return None


def _existing_instance_matches(version_dir: Path, index: ModpackIndex) -> bool:
    return _existing_instance_game_dir(version_dir, index) is not None


def build_instance_plan(
    index: ModpackIndex,
    game_root: Path | GameRoot,
    *,
    slug: str = "",
    overwrite: bool = False,
    launch: bool = True,
) -> ModpackInstancePlan:
    """Choose an isolated ``versions/<id>`` folder for this modpack."""
    root_path = (
        game_root.root if isinstance(game_root, GameRoot) else Path(game_root)
    ).resolve()
    dependencies = resolve_modpack_dependencies(index)
    versions_dir = root_path / "versions"
    base_id = _base_instance_name(index, slug)

    instance_id = base_id
    counter = 2
    while True:
        version_dir = versions_dir / instance_id
        if not version_dir.exists() or _existing_instance_matches(version_dir, index):
            break
        if version_dir.is_dir() and not any(version_dir.iterdir()):
            break
        instance_id = f"{base_id}-{counter}"
        counter += 1
        if counter > 100:
            raise ContentError("无法为整合包选择不冲突的版本目录，请清理 versions/。")

    try:
        instance_id = safe_component(
            instance_id, what="整合包版本 ID", max_length=120
        )
    except VersionError as exc:
        raise ContentError(str(exc)) from exc

    version_dir = versions_dir / instance_id
    existing_game_dir = _existing_instance_game_dir(version_dir, index)
    # New instances use the version folder itself as the game directory.
    # Repairing a 0.5.1 instance keeps its nested `.minecraft` layout.
    game_dir = existing_game_dir or version_dir
    if not is_within(versions_dir, game_dir):
        raise ContentError("整合包实例目录越界，已拒绝。")
    if len(str(game_dir)) > 230:
        raise ContentError("整合包实例路径过长，请缩短游戏目录。")

    if dependencies.launchable and launch and version_dir.exists():
        marker = read_modpack_marker(game_dir)
        if not marker and any(version_dir.iterdir()) and not overwrite:
            raise ContentError(
                f"版本目录已存在且不是本启动器创建的整合包实例：\n{version_dir}\n"
                "请换一个实例名，或确认覆盖后重试。"
            )

    return ModpackInstancePlan(
        index=index,
        game_root=root_path,
        instance_id=instance_id,
        version_dir=version_dir,
        game_dir=game_dir,
        dependencies=dependencies,
        overwrite=bool(overwrite),
        launch=bool(launch),
    )


def _content_plan(plan: ModpackInstancePlan, *, launchable: bool) -> ModpackPlan:
    return ModpackPlan(
        index=plan.index,
        game_root=plan.game_root,
        instance_dir=plan.game_dir,
        overwrite=plan.overwrite,
        version_dir=plan.version_dir,
        version_id=plan.instance_id,
        launchable=launchable,
        loader_label=plan.dependencies.loader_label,
    )


def _default_loader_installer(
    *,
    root: GameRoot,
    source: DownloadSource,
    game_version: str,
    loader: str,
    loader_version: str | None,
    java_path: Path | None,
    progress: Callable[[object], None] | None,
    cancel: threading.Event | None,
    pause: PauseToken | None,
    logger: Callable[[str, str], None] | None,
    deep: bool,
    workers: int,
) -> LoaderInstallResult:
    outcome = install_selected(
        root,
        source,
        game_version,
        loader=loader,
        loader_version=loader_version,
        java_path=java_path,
        progress=progress,
        cancel=cancel,
        pause=pause,
        logger=logger,
        deep=deep,
        workers=workers,
    )
    return LoaderInstallResult(
        version_id=outcome.plan.version_id,
        warnings=list(outcome.warnings),
    )


def _write_instance_version(
    plan: ModpackInstancePlan,
    loader_version_id: str,
) -> None:
    payload = {
        "id": plan.instance_id,
        "inheritsFrom": loader_version_id,
        "type": "release",
        "releaseTime": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mcLauncher": {
            "schema": 1,
            "instance": True,
            "modpack": plan.index.name,
            "modpackVersion": plan.index.version_id,
            "archive": str(plan.index.archive),
            "gameVersion": plan.dependencies.minecraft,
            "loader": plan.dependencies.loader,
            "loaderVersion": plan.dependencies.loader_version,
            "loaderLabel": plan.dependencies.loader_label,
        },
    }
    target = plan.version_json_path
    if not is_within(plan.game_root / "versions", target):
        raise ContentError("整合包版本 JSON 越界，已拒绝。")
    if target.is_file() and not plan.overwrite:
        try:
            existing = json.loads(target.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            existing = {}
        metadata = existing.get("mcLauncher") if isinstance(existing, dict) else None
        if not (isinstance(metadata, dict) and metadata.get("instance")):
            raise ContentError(
                f"版本 JSON 已存在且不是整合包实例：\n{target}\n"
                "请换一个实例名，或确认覆盖后重试。"
            )
    try:
        plan.version_dir.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, target)
    except OSError as exc:
        raise ContentError(f"无法写入整合包版本 JSON：{target}\n{exc}") from exc


def install_instance(
    plan: ModpackInstancePlan,
    *,
    confirmed: bool = False,
    source: DownloadSource | None = None,
    client: HttpClient | None = None,
    progress: Callable[[ContentProgress], None] | None = None,
    cancel: threading.Event | None = None,
    pause: PauseToken | None = None,
    logger: Callable[[str], None] | None = None,
    loader_installer: Callable[..., LoaderInstallResult] | None = None,
    java_path: Path | None = None,
    deep: bool = True,
    workers: int = 6,
) -> ModpackOutcome:
    """Install a modpack as a launchable, isolated version.

    ``loader_installer`` is injectable so unit tests can exercise the version
    registration/content layout without downloading a real game.  Production
    callers should leave it as ``None``.
    """
    if not confirmed:
        raise ContentError("尚未确认安装整合包。")
    log = logger or (lambda _message: None)
    root = ensure_game_root(plan.game_root)
    dependencies = plan.dependencies
    content_only = not plan.launchable

    # Refuse foreign directories *before* a potentially large game download.
    if plan.game_dir.exists() and any(plan.game_dir.iterdir()) and not plan.overwrite:
        if read_modpack_marker(plan.game_dir) is None:
            raise ContentError(
                f"实例目录已存在且非本启动器创建：\n{plan.game_dir}\n"
                "请选择覆盖或删除该目录后重试。"
            )
    if plan.version_json_path.is_file() and not plan.overwrite:
        try:
            existing = json.loads(
                plan.version_json_path.read_text(encoding="utf-8-sig")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            existing = {}
        metadata = existing.get("mcLauncher") if isinstance(existing, dict) else None
        if not (isinstance(metadata, dict) and metadata.get("instance")):
            raise ContentError(
                f"版本 JSON 已存在且不是整合包实例：\n{plan.version_json_path}\n"
                "请换一个实例名，或确认覆盖后重试。"
            )

    if content_only:
        outcome = install_modpack(
            _content_plan(plan, launchable=False),
            client=client,
            progress=progress,
            cancel=cancel,
            pause=pause,
            confirmed=True,
            logger=log,
        )
        outcome.launchable = False
        outcome.version_id = ""
        outcome.version_dir = None
        if dependencies.reason:
            outcome.warnings.append(dependencies.reason)
            log(dependencies.reason)
        return outcome

    if source is None:
        source = get_source("bmclapi")
    installer = loader_installer or _default_loader_installer
    log(
        f"准备安装 Minecraft {dependencies.minecraft} + "
        f"{dependencies.loader_label}。"
    )

    def loader_progress(item) -> None:
        if progress is None:
            return
        try:
            progress(
                ContentProgress(
                    phase="安装游戏与加载器",
                    current=str(getattr(item, "current", "") or getattr(item, "label", "")),
                    downloaded=int(getattr(item, "done_bytes", 0) or 0),
                    total=int(getattr(item, "total_bytes", 0) or 0),
                    files_done=int(getattr(item, "done_files", 0) or 0),
                    files_total=int(getattr(item, "total_files", 0) or 0),
                )
            )
        except Exception:
            pass

    def loader_log(message: str, _level: str = "info") -> None:
        log(message)

    result = installer(
        root=root,
        source=source,
        game_version=dependencies.minecraft,
        loader=dependencies.loader,
        loader_version=dependencies.loader_version or None,
        java_path=java_path,
        progress=loader_progress if progress is not None else None,
        cancel=cancel,
        pause=pause,
        logger=loader_log,
        deep=deep,
        workers=workers,
    )
    version_id = str(result.version_id or "").strip()
    try:
        version_id = safe_component(
            version_id, what="加载器版本 ID", max_length=120
        )
    except VersionError as exc:
        raise ContentError(str(exc)) from exc
    profile_json = root.versions_dir / version_id / f"{version_id}.json"
    if not profile_json.is_file():
        raise ContentError(
            f"加载器安装完成，但找不到版本 JSON：\n{profile_json}\n"
            "请检查下载源或重新安装加载器。"
        )

    outcome = install_modpack(
        _content_plan(plan, launchable=True),
        client=client,
        progress=progress,
        cancel=cancel,
        pause=pause,
        confirmed=True,
        logger=log,
        write_marker=False,
    )
    _write_instance_version(plan, version_id)
    outcome.launchable = True
    outcome.version_id = plan.instance_id
    outcome.version_dir = plan.version_dir
    outcome.loader_label = dependencies.loader_label
    outcome.warnings.extend(result.warnings)
    final_plan = _content_plan(plan, launchable=True)
    write_modpack_marker(final_plan, outcome)
    return outcome


def install_modpack_archive(
    archive_path: Path,
    game_root: Path | GameRoot,
    *,
    slug: str = "",
    overwrite: bool = False,
    launch: bool = True,
    confirmed: bool = False,
    source: DownloadSource | None = None,
    client: HttpClient | None = None,
    progress: Callable[[ContentProgress], None] | None = None,
    cancel: threading.Event | None = None,
    pause: PauseToken | None = None,
    logger: Callable[[str], None] | None = None,
    loader_installer: Callable[..., LoaderInstallResult] | None = None,
    java_path: Path | None = None,
    deep: bool = True,
    workers: int = 6,
) -> ModpackOutcome:
    """Install a ``.mrpack``/ZIP that contains ``modrinth.index.json``."""
    index = read_mrpack(Path(archive_path))
    plan = build_instance_plan(
        index, game_root, slug=slug, overwrite=overwrite, launch=launch
    )
    return install_instance(
        plan,
        confirmed=confirmed,
        source=source,
        client=client,
        progress=progress,
        cancel=cancel,
        pause=pause,
        logger=logger,
        loader_installer=loader_installer,
        java_path=java_path,
        deep=deep,
        workers=workers,
    )


def download_and_install_launchable_modpack(
    project: ContentProject,
    version: ContentVersion,
    file: ContentFile | None,
    game_root: Path | GameRoot,
    *,
    slug: str = "",
    overwrite: bool = False,
    launch: bool = True,
    confirmed: bool = False,
    source: DownloadSource | None = None,
    client: HttpClient | None = None,
    progress: Callable[[ContentProgress], None] | None = None,
    cancel: threading.Event | None = None,
    pause: PauseToken | None = None,
    logger: Callable[[str], None] | None = None,
    cache_root: Path | None = None,
    java_path: Path | None = None,
    deep: bool = True,
    workers: int = 6,
    loader_installer: Callable[..., LoaderInstallResult] | None = None,
) -> ModpackOutcome:
    """Download a Modrinth modpack file, then install a launchable instance."""
    archive_path, index = download_modpack_archive(
        project,
        version,
        file,
        client=client,
        progress=progress,
        cancel=cancel,
        pause=pause,
        confirmed=confirmed,
        logger=logger,
        cache_root=cache_root,
    )
    root_path = (
        game_root.root if isinstance(game_root, GameRoot) else Path(game_root)
    )
    plan = build_instance_plan(
        index,
        root_path,
        slug=slug or project.slug,
        overwrite=overwrite,
        launch=launch,
    )
    return install_instance(
        plan,
        confirmed=True,
        source=source,
        client=client,
        progress=progress,
        cancel=cancel,
        pause=pause,
        logger=logger,
        java_path=java_path,
        deep=deep,
        workers=workers,
        loader_installer=loader_installer,
    )


def format_instance_plan(plan: ModpackInstancePlan) -> str:
    lines = [
        f"整合包：{plan.index.name} {plan.index.version_id}",
        f"依赖：{plan.dependencies.summary}",
        f"版本目录：{plan.version_dir}",
        f"隔离游戏目录：{plan.game_dir}",
        (
            f"目标版本 ID：{plan.instance_id}"
            if plan.launchable
            else "可启动版本：不会创建（仅整理内容）"
        ),
    ]
    if plan.dependencies.reason:
        lines.append(f"说明：{plan.dependencies.reason}")
    return "\n".join(lines)
