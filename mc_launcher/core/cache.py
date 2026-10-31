"""Known launcher cache locations, size accounting and safe clean-up.

Only data that the launcher itself creates outside the game installation is
removed here:

* ``%APPDATA%\\MinecraftLauncher\\cache\\<source>`` — manifests, version JSON
  and asset indexes;
* ``%LOCALAPPDATA%\\MinecraftLauncher\\installers`` — downloaded Forge
  installers;
* ``%LOCALAPPDATA%\\MinecraftLauncher\\natives\\mc-launcher-natives-*`` (and
  the Temp fallback) — extracted native libraries from interrupted runs;
* residual ``*.part*`` files inside the selected game directory.

The game installation itself (``versions``, ``libraries``, ``assets``,
``resources``, saves and configuration) is never deleted.  Every target is
resolved and checked against its allowed base before deletion, and symbolic
links/reparse points are unlinked instead of followed.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .errors import LauncherError
from .mirrors import SOURCES
from .models import GameRoot
from .net import human_size
from .safety import is_within
from .storage import cache_dir, installer_cache_dir

CATEGORY_MANIFESTS = "manifests"
CATEGORY_INSTALLERS = "installers"
CATEGORY_NATIVES = "natives"
CATEGORY_PARTIAL = "partial"

CATEGORY_ORDER = (
    CATEGORY_MANIFESTS,
    CATEGORY_INSTALLERS,
    CATEGORY_NATIVES,
    CATEGORY_PARTIAL,
)

CATEGORY_LABELS = {
    CATEGORY_MANIFESTS: "元数据缓存",
    CATEGORY_INSTALLERS: "加载器安装器",
    CATEGORY_NATIVES: "临时 natives",
    CATEGORY_PARTIAL: "未完成下载分片",
}

CATEGORY_DESCRIPTIONS = {
    CATEGORY_MANIFESTS: "版本清单、版本 JSON 与资源索引缓存；删除后下次安装会重新拉取。",
    CATEGORY_INSTALLERS: "已下载的 Forge 安装器；删除后下次安装 Forge 会重新下载并校验。",
    CATEGORY_NATIVES: "启动时解压 native 库的临时目录；正常退出会自动清理，这里清理残留。",
    CATEGORY_PARTIAL: "游戏目录内中断下载留下的 *.part 文件；不会删除已安装文件。",
}

CATEGORY_CHOICES = (*CATEGORY_ORDER, "all")

NATIVES_PREFIX = "mc-launcher-natives-"
PARTIAL_GLOBS = ("*.part", "*.part.seg*")


@dataclass(frozen=True)
class CacheTarget:
    """One concrete path (file or directory) that may be deleted."""

    category: str
    path: Path


@dataclass
class CacheUsage:
    category: str
    label: str
    description: str
    bytes: int = 0
    files: int = 0
    paths: tuple[Path, ...] = ()
    errors: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return self.bytes == 0 and self.files == 0


@dataclass
class CleanReport:
    requested: tuple[str, ...] = ()
    freed_bytes: int = 0
    removed_files: int = 0
    removed_paths: int = 0
    categories: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------------------
# Locating caches
# ---------------------------------------------------------------------------


def natives_bases() -> list[Path]:
    """Directories that may contain ``mc-launcher-natives-*`` folders."""
    bases: list[Path] = []
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        bases.append(Path(local_appdata) / "MinecraftLauncher" / "natives")
    bases.append(Path(tempfile.gettempdir()) / "MinecraftLauncher" / "natives")
    unique: list[Path] = []
    for base in bases:
        if base not in unique:
            unique.append(base)
    return unique


def _manifest_paths() -> list[Path]:
    base = cache_dir()
    if not base.is_dir():
        return []
    paths: list[Path] = []
    for source_id in SOURCES:
        candidate = base / source_id
        if candidate.is_dir():
            paths.append(candidate)
    return paths


def _installer_paths() -> list[Path]:
    base = installer_cache_dir()
    if not base.is_dir():
        return []
    try:
        return [item for item in base.iterdir() if item.name]
    except OSError:
        return []


def _natives_paths() -> list[Path]:
    paths: list[Path] = []
    for base in natives_bases():
        if not base.is_dir():
            continue
        try:
            children = list(base.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir() and child.name.startswith(NATIVES_PREFIX):
                paths.append(child)
    return paths


def _partial_paths(game_root: GameRoot | None) -> list[Path]:
    if game_root is None or not game_root.root.is_dir():
        return []
    paths: list[Path] = []
    for pattern in PARTIAL_GLOBS:
        try:
            matches = list(game_root.root.rglob(pattern))
        except OSError:
            continue
        for match in matches:
            try:
                if match.is_file():
                    paths.append(match)
            except OSError:
                continue
    # rglob can return the same path through different patterns; deduplicate.
    unique: dict[str, Path] = {}
    for path in paths:
        unique[str(path).lower()] = path
    return list(unique.values())


def cache_targets(
    game_root: GameRoot | None = None,
    categories: Iterable[str] | None = None,
) -> list[CacheTarget]:
    wanted = set(_normalise_categories(categories)) if categories is not None else set(CATEGORY_ORDER)
    targets: list[CacheTarget] = []
    if CATEGORY_MANIFESTS in wanted:
        targets.extend(CacheTarget(CATEGORY_MANIFESTS, path) for path in _manifest_paths())
    if CATEGORY_INSTALLERS in wanted:
        targets.extend(CacheTarget(CATEGORY_INSTALLERS, path) for path in _installer_paths())
    if CATEGORY_NATIVES in wanted:
        targets.extend(CacheTarget(CATEGORY_NATIVES, path) for path in _natives_paths())
    if CATEGORY_PARTIAL in wanted:
        targets.extend(CacheTarget(CATEGORY_PARTIAL, path) for path in _partial_paths(game_root))
    return targets


def _normalise_categories(categories: Iterable[str] | None) -> tuple[str, ...]:
    if categories is None:
        return CATEGORY_ORDER
    if isinstance(categories, str):
        categories = (categories,)
    result: list[str] = []
    for item in categories:
        value = str(item).strip().lower()
        if value in ("all", "*", ""):
            for category in CATEGORY_ORDER:
                if category not in result:
                    result.append(category)
            continue
        if value not in CATEGORY_LABELS:
            raise LauncherError(
                f"未知缓存类别：{item}（可选：{', '.join(CATEGORY_CHOICES)}）"
            )
        if value not in result:
            result.append(value)
    return tuple(result)


# ---------------------------------------------------------------------------
# Size accounting
# ---------------------------------------------------------------------------


def _measure_path(path: Path) -> tuple[int, int, list[str]]:
    total = 0
    files = 0
    errors: list[str] = []
    try:
        if _is_link_like(path):
            try:
                return path.lstat().st_size, 1, errors
            except OSError as exc:
                return 0, 0, [f"{path}：{exc}"]
        if path.is_file():
            try:
                return path.stat().st_size, 1, errors
            except OSError as exc:
                return 0, 0, [f"{path}：{exc}"]
        if not path.is_dir():
            return 0, 0, errors
        for root, dirs, names in os.walk(path, followlinks=False):
            root_path = Path(root)
            dirs[:] = [name for name in dirs if not _is_link_like(root_path / name)]
            for name in names:
                candidate = root_path / name
                if _is_link_like(candidate):
                    continue
                try:
                    total += candidate.stat().st_size
                    files += 1
                except OSError as exc:
                    if len(errors) < 5:
                        errors.append(f"{candidate}：{exc}")
    except OSError as exc:
        if len(errors) < 5:
            errors.append(f"{path}：{exc}")
    return total, files, errors


def scan_cache(game_root: GameRoot | None = None) -> list[CacheUsage]:
    """Measure every known cache category without modifying anything."""
    targets = cache_targets(game_root)
    grouped: dict[str, list[CacheTarget]] = {category: [] for category in CATEGORY_ORDER}
    for target in targets:
        grouped.setdefault(target.category, []).append(target)

    usages: list[CacheUsage] = []
    for category in CATEGORY_ORDER:
        usage = CacheUsage(
            category=category,
            label=CATEGORY_LABELS[category],
            description=CATEGORY_DESCRIPTIONS[category],
        )
        paths = [target.path for target in grouped.get(category, [])]
        for path in paths:
            total, files, errors = _measure_path(path)
            usage.bytes += total
            usage.files += files
            usage.errors.extend(errors)
        usage.paths = tuple(paths)
        usages.append(usage)
    return usages


def total_cache_bytes(usages: Iterable[CacheUsage]) -> int:
    return sum(item.bytes for item in usages)


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------


def _allowed_base(category: str, game_root: GameRoot | None) -> Path | None:
    if category == CATEGORY_MANIFESTS:
        return cache_dir()
    if category == CATEGORY_INSTALLERS:
        return installer_cache_dir()
    if category == CATEGORY_NATIVES:
        # Each native folder lives below a base; validation is repeated against
        # every known base in ``_target_is_safe``.
        return None
    if category == CATEGORY_PARTIAL:
        return game_root.root if game_root is not None else None
    return None


def _target_is_safe(target: CacheTarget, game_root: GameRoot | None) -> tuple[bool, str]:
    path = target.path
    try:
        if not path.exists() and not path.is_symlink():
            return False, f"{path}：路径不存在"
    except OSError as exc:
        return False, f"{path}：{exc}"

    category = target.category
    if category == CATEGORY_NATIVES:
        bases = [base for base in natives_bases() if base.is_dir()]
        if not any(
            is_within(base, path) and _resolved(path) != _resolved(base)
            for base in bases
        ):
            return False, f"{path}：不在允许的 natives 缓存目录内"
        return True, ""

    base = _allowed_base(category, game_root)
    if base is None:
        return False, f"{path}：当前类别没有可用的缓存目录"
    if not is_within(base, path) or _resolved(path) == _resolved(base):
        return False, f"{path}：不在允许的缓存目录内"
    return True, ""


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


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


def _remove_tree(path: Path) -> None:
    def on_error(function, target, exc_info):  # type: ignore[no-untyped-def]
        try:
            os.chmod(target, stat.S_IWRITE)
            function(target)
        except OSError:
            raise exc_info[1] if isinstance(exc_info, tuple) else exc_info

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=on_error)
    else:  # pragma: no cover - Python 3.10/3.11 compatibility
        shutil.rmtree(path, onerror=on_error)


def _delete_path(path: Path) -> tuple[int, int]:
    """Delete one file or directory; return (bytes, files)."""
    if _is_link_like(path):
        size = 0
        try:
            size = path.lstat().st_size
        except OSError:
            pass
        path.unlink()
        return size, 1
    if path.is_dir():
        size, files, _errors = _measure_path(path)
        _remove_tree(path)
        return size, files
    size = path.stat().st_size
    try:
        path.unlink()
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)
        path.unlink()
    return size, 1


def clean_cache(
    categories: Iterable[str] | None = None,
    game_root: GameRoot | None = None,
    *,
    targets: list[CacheTarget] | None = None,
    logger: Callable[[str], None] | None = None,
) -> CleanReport:
    """Delete selected cache categories.

    ``targets`` exists for tests; normal callers pass ``categories`` so the
    paths are re-discovered immediately before deletion.
    """
    log = logger or (lambda _message: None)
    wanted = _normalise_categories(categories)
    report = CleanReport(requested=wanted)
    selected = targets if targets is not None else cache_targets(game_root, wanted)
    for target in selected:
        if target.category not in wanted:
            continue
        safe, reason = _target_is_safe(target, game_root)
        if not safe:
            report.errors.append(reason)
            log(f"跳过：{reason}")
            continue
        try:
            freed, files = _delete_path(target.path)
        except OSError as exc:
            report.errors.append(f"{target.path}：{exc}")
            log(f"删除失败：{target.path}：{exc}")
            continue
        report.freed_bytes += freed
        report.removed_files += files
        report.removed_paths += 1
        if target.category not in report.categories:
            report.categories.append(target.category)
        log(
            f"已清理 {CATEGORY_LABELS[target.category]}：{target.path}"
            f"（{human_size(freed)}）"
        )
    return report


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_cache_usage(usages: Iterable[CacheUsage]) -> str:
    usages = list(usages)
    lines = [f"{'类别':<12}{'文件':>8}{'大小':>12}  说明"]
    for usage in usages:
        lines.append(
            f"{usage.label:<12}{usage.files:>8}{human_size(usage.bytes):>12}  "
            f"{usage.description}"
        )
        for path in usage.paths[:4]:
            lines.append(f"{'':<12}{'':>8}{'':>12}  - {path}")
        if len(usage.paths) > 4:
            lines.append(
                f"{'':<12}{'':>8}{'':>12}  ... 另有 {len(usage.paths) - 4} 个路径"
            )
        if usage.errors:
            for error in usage.errors[:3]:
                lines.append(f"{'':<12}{'':>8}{'':>12}  ! {error}")
    lines.append(f"{'合计':<12}{sum(u.files for u in usages):>8}"
                 f"{human_size(total_cache_bytes(usages)):>12}")
    return "\n".join(lines)


def format_clean_report(report: CleanReport) -> str:
    if report.removed_paths == 0 and not report.errors:
        return "没有需要清理的缓存。"
    lines = [
        f"已释放空间：{human_size(report.freed_bytes)}"
        f"（删除 {report.removed_paths} 个路径 / {report.removed_files} 个文件）"
    ]
    labels = [CATEGORY_LABELS.get(item, item) for item in report.categories]
    if labels:
        lines.append("清理类别：" + "、".join(labels))
    if report.errors:
        lines.append("以下项目未清理：")
        lines.extend(f"  - {item}" for item in report.errors[:10])
        if len(report.errors) > 10:
            lines.append(f"  ... 另有 {len(report.errors) - 10} 条")
    return "\n".join(lines)
