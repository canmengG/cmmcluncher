"""Mojang Java runtime catalog, user-consented installers, and manual links.

The launcher must never download a Java runtime behind the user's back.  This
module therefore splits "looking" from "installing":

* :func:`fetch_catalog` lists the official Mojang components and their sizes
  are only fetched when the user asks for a specific component;
* :func:`build_install_plan` resolves every file, target path and URL;
* :func:`install_java_runtime` **requires** ``confirmed=True``.  The UI passes
  that flag only after the user ticks the consent box and confirms the dialog;
  the CLI only after ``--yes`` or an interactive ``y`` answer.

Everything else (download URLs, install directory, source, whether to download
at all, or downloading from a vendor manually) stays a user choice.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .errors import LauncherError, VersionError
from .java import probe_java
from .manifest import fetch_bytes
from .mirrors import DownloadSource
from .models import JavaRuntime
from .net import (
    CancelledError,
    DownloadError,
    DownloadSpec,
    HttpClient,
    PauseToken,
    human_size,
    sha1_file,
)
from .safety import (
    is_within,
    normalise_sha1,
    posix_relpath,
    safe_component,
    safe_relative_path,
)
from .storage import app_data_dir, cache_dir

# The official "all platforms" Java runtime manifest used by the vanilla
# launcher.  BMCLAPI mirrors this exact path (verified 2026-10-31).
JAVA_RUNTIME_MANIFEST_URL = (
    "https://launchermeta.mojang.com/v1/products/java-runtime/"
    "2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json"
)

PLATFORM_WINDOWS_X64 = "windows-x64"
PLATFORM_WINDOWS_ARM64 = "windows-arm64"
PLATFORM_WINDOWS_X86 = "windows-x86"

CATALOG_TTL_SECONDS = 6 * 60 * 60
MAX_CATALOG_BYTES = 16 * 1024 * 1024
MAX_COMPONENT_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_RUNTIME_FILES = 6000
MAX_RUNTIME_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_INSTALL_PATH_LENGTH = 220
MARKER_NAME = ".mc-launcher-runtime.json"
MARKER_SCHEMA = 1

# Preferred Mojang component for a requested Java major version.  The catalog
# is still the source of truth; this map only avoids downloading every
# component manifest just to find the right one.
COMPONENT_FOR_MAJOR = {
    8: "jre-legacy",
    16: "java-runtime-alpha",
    17: "java-runtime-gamma",
    21: "java-runtime-delta",
    25: "java-runtime-epsilon",
}

# Vendor pages a user can visit to download a Java runtime themselves.
VENDOR_LINKS = (
    (
        "Eclipse Adoptium（Temurin，推荐）",
        "https://adoptium.net/temurin/releases/?os=windows&arch=x64&version={major}",
        "下载 Windows x64 的 JDK 或 JRE zip，解压后用“导入已有 Java…”选择 bin\\java.exe。",
    ),
    (
        "Microsoft Build of OpenJDK",
        "https://learn.microsoft.com/java/openjdk/download",
        "微软维护的 OpenJDK；选择 Windows x64 zip。",
    ),
    (
        "Azul Zulu",
        "https://www.azul.com/downloads/?version=java-{major}-lts&os=windows&architecture=x86-64-bit&package=jdk",
        "适合需要长期支持版本的场景；选择 Windows x64 zip。",
    ),
    (
        "Amazon Corretto",
        "https://aws.amazon.com/corretto/",
        "免费长期支持发行版；选择 Windows x64 zip。",
    ),
    (
        "BellSoft Liberica",
        "https://bell-sw.com/pages/downloads/",
        "提供 JRE/JDK 的 Windows x64 包。",
    ),
    (
        "Oracle JDK",
        "https://www.oracle.com/java/technologies/downloads/",
        "Oracle 许可与 OpenJDK 不同，商用前请阅读其条款。",
    ),
)


@dataclass(frozen=True)
class JavaRuntimeComponent:
    """One installable Java runtime published by Mojang."""

    id: str
    version: str
    platform: str
    manifest_url: str
    manifest_sha1: str
    manifest_size: int | None = None
    java_major: int | None = None
    experimental: bool = False

    @property
    def label(self) -> str:
        major = f"Java {self.java_major}" if self.java_major else "Java"
        return f"{self.id} · {major} {self.version}"

    def matches_major(self, major: int | None) -> bool:
        return major is None or self.java_major == major


@dataclass
class JavaRuntimeCatalog:
    source_id: str
    platforms: dict[str, dict[str, list[JavaRuntimeComponent]]]
    fetched_at: float = 0.0
    from_cache: bool = False

    def components(self, platform: str = PLATFORM_WINDOWS_X64) -> list[JavaRuntimeComponent]:
        items = []
        for entries in self.platforms.get(platform, {}).values():
            if entries:
                items.append(max(entries, key=_component_sort_key))
        items.sort(key=_component_sort_key, reverse=True)
        return items

    def find(
        self, component_id: str, platform: str = PLATFORM_WINDOWS_X64
    ) -> JavaRuntimeComponent | None:
        entries = self.platforms.get(platform, {}).get(component_id) or []
        return max(entries, key=_component_sort_key) if entries else None

    def platforms_with_runtime(self) -> list[str]:
        return [
            name
            for name, entries in self.platforms.items()
            if any(entries.values())
        ]


@dataclass(frozen=True)
class RuntimeFile:
    """One file inside a component manifest (always the uncompressed ``raw``)."""

    relative: Path
    url: str
    sha1: str
    size: int
    executable: bool = False


@dataclass
class ComponentManifest:
    component: JavaRuntimeComponent
    files: list[RuntimeFile]
    directories: list[Path]
    total_bytes: int
    manifest_sha1: str

    @property
    def file_count(self) -> int:
        return len(self.files)


@dataclass(frozen=True)
class PlannedRuntimeFile:
    file: RuntimeFile
    urls: tuple[str, ...]

    @property
    def relative(self) -> Path:
        return self.file.relative


@dataclass
class JavaRuntimePlan:
    component: JavaRuntimeComponent
    source: DownloadSource
    install_root: Path
    install_dir: Path
    files: list[PlannedRuntimeFile]
    directories: list[Path]
    manifest_sha1: str

    @property
    def java_path(self) -> Path:
        return self.install_dir / "bin" / "java.exe"

    @property
    def marker_path(self) -> Path:
        return self.install_dir / MARKER_NAME

    @property
    def total_bytes(self) -> int:
        return sum(item.file.size for item in self.files)

    def destination(self, item: PlannedRuntimeFile) -> Path:
        return self.install_dir / item.relative


@dataclass
class RuntimeProgress:
    phase: str
    done_bytes: int = 0
    total_bytes: int = 0
    done_files: int = 0
    total_files: int = 0
    label: str = ""
    current: str = ""


@dataclass
class JavaRuntimeOutcome:
    plan: JavaRuntimePlan
    java_path: Path
    runtime: JavaRuntime | None
    downloaded_files: int = 0
    downloaded_bytes: int = 0
    skipped_files: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.runtime is not None


@dataclass
class RuntimeFileStatus:
    path: Path
    status: str  # ok | missing | size | hash | unreadable
    expected_size: int | None = None
    actual_size: int | None = None
    detail: str = ""


@dataclass
class RuntimeVerifyReport:
    install_dir: Path
    component_id: str = ""
    version: str = ""
    managed: bool = False
    statuses: list[RuntimeFileStatus] = field(default_factory=list)
    marker_error: str = ""
    elapsed: float = 0.0

    @property
    def total(self) -> int:
        return len(self.statuses)

    @property
    def ok(self) -> int:
        return sum(1 for item in self.statuses if item.status == "ok")

    @property
    def missing(self) -> list[RuntimeFileStatus]:
        return [item for item in self.statuses if item.status == "missing"]

    @property
    def corrupt(self) -> list[RuntimeFileStatus]:
        return [
            item
            for item in self.statuses
            if item.status in ("size", "hash", "unreadable")
        ]

    @property
    def problematic(self) -> list[RuntimeFileStatus]:
        return [item for item in self.statuses if item.status != "ok"]

    @property
    def bad_bytes(self) -> int:
        return sum(item.expected_size or 0 for item in self.problematic)

    @property
    def healthy(self) -> bool:
        if not self.managed:
            return (self.install_dir / "bin" / "java.exe").is_file()
        return bool(self.statuses) and not self.problematic


@dataclass
class InstalledRuntime:
    install_dir: Path
    component_id: str
    java_path: Path
    version: str = ""
    java_major: int | None = None
    managed: bool = False
    size_bytes: int = 0
    installed_at: str = ""

    @property
    def label(self) -> str:
        version = self.version or "未知版本"
        return f"{self.component_id} · {version}"


@dataclass(frozen=True)
class ManualJavaLink:
    label: str
    url: str
    note: str = ""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _version_key(value: str) -> tuple:
    parts = re.findall(r"\d+", value or "")
    numbers = tuple(int(part) for part in parts[:4])
    return numbers + (0,) * (4 - len(numbers))


def _component_sort_key(component: JavaRuntimeComponent) -> tuple:
    return (
        0 if component.experimental else 1,
        component.java_major or 0,
        _version_key(component.version),
        component.id,
    )


def _major_from_text(value: str) -> int | None:
    text = (value or "").strip()
    match = re.match(r"(\d+)", text)
    return int(match.group(1)) if match else None


def default_platform() -> str:
    machine = (os.environ.get("PROCESSOR_ARCHITECTURE") or "").upper()
    if os.name == "nt" and machine in ("ARM64", "AARCH64"):
        return PLATFORM_WINDOWS_ARM64
    return PLATFORM_WINDOWS_X64


def default_install_root(game_root: Path | None) -> Path:
    """The launcher-managed runtime folder shown as the default choice."""
    if game_root is not None:
        return (game_root / "runtime").resolve()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return (Path(local) / "MinecraftLauncher" / "runtime").resolve()
    return (app_data_dir() / "runtime").resolve()


def validate_install_root(value: str | Path) -> Path:
    """Validate a user-chosen install root without creating it yet."""
    text = str(value).strip().strip('"')
    if not text:
        raise LauncherError("请选择 Java 安装位置。")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        path = path.resolve()
    except OSError:
        path = path.absolute()
    if path.exists() and not path.is_dir():
        raise LauncherError(f"Java 安装位置不是文件夹：{path}")
    if path.parent == path:
        raise LauncherError("不能把 Java 安装到磁盘根目录，请选择子文件夹。")
    windows_dir = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if windows_dir:
        try:
            system_root = Path(windows_dir).resolve()
        except OSError:
            system_root = None
        if system_root is not None:
            try:
                path.relative_to(system_root)
                raise LauncherError("不能把 Java 安装到 Windows 系统目录。")
            except ValueError:
                pass
    if len(str(path)) > MAX_INSTALL_PATH_LENGTH:
        raise LauncherError(
            f"安装路径过长（{len(str(path))} 字符），请选择更短的文件夹。"
        )
    return path


def _cache_root(source: DownloadSource, cache_root: Path | None) -> Path:
    return (cache_root or cache_dir()) / source.id


def _load_cached_catalog(path: Path) -> tuple[bytes | None, float]:
    if not path.is_file():
        return None, 0.0
    try:
        wrapper = json.loads(path.read_bytes())
    except (OSError, ValueError, json.JSONDecodeError):
        return None, 0.0
    if not isinstance(wrapper, dict) or wrapper.get("schema") != 1:
        return None, 0.0
    raw = wrapper.get("manifest")
    if not isinstance(raw, dict):
        return None, 0.0
    try:
        saved_at = float(wrapper.get("saved_at") or 0)
    except (TypeError, ValueError):
        saved_at = 0.0
    return json.dumps(raw, ensure_ascii=False).encode("utf-8"), saved_at


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _free_space(path: Path) -> int | None:
    candidate = path
    while True:
        try:
            if candidate.exists():
                return shutil.disk_usage(candidate).free
        except OSError:
            return None
        if candidate.parent == candidate:
            return None
        candidate = candidate.parent


def _matches_file(path: Path, sha1: str | None, size: int | None, *, deep: bool = True) -> bool:
    try:
        if not path.is_file():
            return False
        if size is not None and path.stat().st_size != size:
            return False
        if deep and sha1:
            return sha1_file(path).lower() == sha1.lower()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def _parse_catalog(source: DownloadSource, payload: bytes) -> JavaRuntimeCatalog:
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VersionError(f"Java 运行时清单不是有效 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise VersionError("Java 运行时清单的根节点不是对象。")

    platforms: dict[str, dict[str, list[JavaRuntimeComponent]]] = {}
    for platform_name, entries in data.items():
        if not isinstance(platform_name, str) or not isinstance(entries, dict):
            continue
        try:
            safe_component(platform_name, what="平台名称", max_length=64)
        except VersionError:
            continue
        bucket: dict[str, list[JavaRuntimeComponent]] = {}
        for component_id, versions in entries.items():
            if not isinstance(versions, list) or not versions:
                continue
            if not (
                component_id.startswith("java-runtime")
                or component_id.startswith("jre-")
            ):
                # "minecraft-java-exe" and other launcher helpers are not JREs.
                continue
            try:
                checked_id = safe_component(
                    component_id, what="Java 组件名称", max_length=64
                )
            except VersionError:
                continue
            for entry in versions:
                if not isinstance(entry, dict):
                    continue
                manifest = entry.get("manifest")
                version = entry.get("version")
                if not isinstance(manifest, dict) or not isinstance(version, dict):
                    continue
                manifest_url = manifest.get("url")
                if not isinstance(manifest_url, str) or not manifest_url.startswith(
                    "https://"
                ):
                    continue
                try:
                    manifest_sha1 = normalise_sha1(
                        manifest.get("sha1"), what="Java 组件清单哈希"
                    )
                except VersionError:
                    continue
                raw_size = manifest.get("size")
                manifest_size = raw_size if isinstance(raw_size, int) and raw_size > 0 else None
                version_name = str(version.get("name") or "").strip()
                major = _major_from_text(version_name)
                experimental = "snapshot" in checked_id or "snapshot" in version_name.lower()
                component = JavaRuntimeComponent(
                    id=checked_id,
                    version=version_name or "未知",
                    platform=platform_name,
                    manifest_url=manifest_url,
                    manifest_sha1=manifest_sha1,
                    manifest_size=manifest_size,
                    java_major=major,
                    experimental=experimental,
                )
                bucket.setdefault(checked_id, []).append(component)
        if bucket:
            platforms[platform_name] = {
                key: sorted(values, key=_component_sort_key)
                for key, values in bucket.items()
            }

    if not platforms:
        raise VersionError("Java 运行时清单中没有可用的组件。")
    return JavaRuntimeCatalog(source_id=source.id, platforms=platforms)


def fetch_catalog(
    source: DownloadSource,
    *,
    client: HttpClient | None = None,
    force: bool = False,
    ttl: int = CATALOG_TTL_SECONDS,
    cache_root: Path | None = None,
    logger: Callable[[str], None] | None = None,
) -> JavaRuntimeCatalog:
    """Load the Java runtime catalog, with a cache and offline fallback."""
    log = logger or (lambda _message: None)
    path = _cache_root(source, cache_root) / "java-runtime" / "all.json"
    cached_payload, cached_at = _load_cached_catalog(path)

    if cached_payload is not None and not force and time.time() - cached_at < ttl:
        try:
            catalog = _parse_catalog(source, cached_payload)
            catalog.fetched_at = cached_at
            catalog.from_cache = True
            return catalog
        except VersionError:
            cached_payload = None

    http = client or HttpClient(
        source.allowed_hosts,
        strict_redirects=source.strict_redirects,
        logger=log,
    )
    urls = tuple(dict.fromkeys((source.rewrite(JAVA_RUNTIME_MANIFEST_URL), JAVA_RUNTIME_MANIFEST_URL)))
    try:
        payload = fetch_bytes(http, urls, max_size=MAX_CATALOG_BYTES, logger=log)
        catalog = _parse_catalog(source, payload)
        catalog.fetched_at = time.time()
        wrapper = {
            "schema": 1,
            "source": source.id,
            "saved_at": catalog.fetched_at,
            "manifest": json.loads(payload),
        }
        try:
            _write_atomic(path, json.dumps(wrapper, ensure_ascii=False).encode("utf-8"))
        except OSError as exc:
            log(f"无法写入 Java 运行时清单缓存：{exc}")
        return catalog
    except (LauncherError, OSError) as exc:
        error = str(exc)

    if cached_payload is not None:
        try:
            catalog = _parse_catalog(source, cached_payload)
            catalog.fetched_at = cached_at
            catalog.from_cache = True
            log("网络不可用，使用缓存的 Java 运行时清单。")
            return catalog
        except VersionError:
            pass
    raise LauncherError(
        "无法获取 Mojang Java 运行时清单。\n"
        f"  - {error}\n"
        f"你也可以手动访问：{JAVA_RUNTIME_MANIFEST_URL}\n"
        "或在“手动下载网址”中选择 Adoptium / Microsoft 等发行版。"
    )


def choose_component(
    components: Iterable[JavaRuntimeComponent], required_major: int | None
) -> JavaRuntimeComponent | None:
    """Pick the best default component for a required major version.

    Exact matches win; otherwise the closest newer runtime is offered (Minecraft
    usually starts on a newer major), and only then a lower one.
    """
    items = list(components)
    if not items:
        return None
    if required_major is None:
        stable = [item for item in items if not item.experimental] or items
        return max(stable, key=_component_sort_key)
    exact = [
        item
        for item in items
        if item.java_major == required_major
    ]
    if exact:
        stable = [item for item in exact if not item.experimental]
        return max(stable or exact, key=_component_sort_key)
    newer = [item for item in items if (item.java_major or 0) > required_major]
    if newer:
        return min(
            newer,
            key=lambda item: (
                0 if not item.experimental else 1,
                item.java_major or 0,
                _version_key(item.version),
            ),
        )
    lower = [item for item in items if (item.java_major or 0) < required_major]
    if lower:
        return max(lower, key=_component_sort_key)
    return max(items, key=_component_sort_key)


def resolve_component(
    catalog: JavaRuntimeCatalog,
    selector: str | None,
    *,
    platform: str | None = None,
    required_major: int | None = None,
) -> JavaRuntimeComponent:
    """Resolve a component id, a Java major number, or the default choice."""
    platform = platform or default_platform()
    components = catalog.components(platform)
    if not components:
        raise LauncherError(
            f"Java 运行时清单中没有 {platform} 平台的组件，请改用官方源或手动下载。"
        )
    text = (selector or "").strip()
    if text:
        exact = catalog.find(text, platform)
        if exact is not None:
            return exact
        if text.isdigit():
            chosen = choose_component(components, int(text))
            if chosen is not None:
                return chosen
        raise LauncherError(
            f"找不到 Java 组件：{selector}。可用组件："
            + "、".join(item.id for item in components)
        )
    chosen = choose_component(components, required_major)
    if chosen is None:
        raise LauncherError("没有可用的 Java 运行时组件。")
    return chosen


# ---------------------------------------------------------------------------
# Component manifest
# ---------------------------------------------------------------------------


def _parse_component_manifest(
    component: JavaRuntimeComponent, payload: bytes, manifest_sha1: str
) -> ComponentManifest:
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VersionError(f"Java 组件清单不是有效 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise VersionError("Java 组件清单的根节点不是对象。")
    raw_files = data.get("files")
    if not isinstance(raw_files, dict):
        raise VersionError("Java 组件清单缺少 files 对象。")
    if len(raw_files) > MAX_RUNTIME_FILES:
        raise VersionError("Java 组件包含的文件过多，已拒绝解析。")

    files: list[RuntimeFile] = []
    directories: list[Path] = []
    seen_paths: dict[str, str] = {}
    total = 0
    for name, entry in raw_files.items():
        try:
            relative = safe_relative_path(name, what="Java 运行时文件路径")
        except VersionError as exc:
            raise VersionError(str(exc)) from exc
        if len(relative.parts) > 16:
            raise VersionError(f"Java 运行时文件层级过深：{name[:120]}")
        key = posix_relpath(relative).lower()
        previous = seen_paths.get(key)
        if previous is not None:
            raise VersionError(
                f"Java 组件清单包含重复路径（大小写不敏感）：{name} 与 {previous}"
            )
        seen_paths[key] = name
        if not isinstance(entry, dict):
            continue
        kind = entry.get("type")
        if kind == "directory":
            directories.append(relative)
            continue
        if kind == "link":
            raise VersionError(
                f"Java 组件清单包含链接条目（已拒绝）：{name}"
            )
        if kind != "file":
            raise VersionError(f"Java 组件清单包含未知条目类型：{kind!r}")
        downloads = entry.get("downloads")
        if not isinstance(downloads, dict):
            raise VersionError(f"Java 运行时文件缺少 downloads：{name}")
        raw = downloads.get("raw")
        if not isinstance(raw, dict):
            raise VersionError(
                f"Java 运行时文件缺少未压缩下载信息（仅支持 raw）：{name}"
            )
        url = raw.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise VersionError(f"Java 运行时文件下载地址无效：{name}")
        try:
            sha1 = normalise_sha1(raw.get("sha1"), what="Java 运行时文件哈希")
        except VersionError as exc:
            raise VersionError(f"{name}：{exc}") from exc
        size = raw.get("size")
        if not isinstance(size, int) or size < 0 or size > 1024 * 1024 * 1024:
            raise VersionError(f"Java 运行时文件大小无效：{name}")
        total += size
        if total > MAX_RUNTIME_TOTAL_BYTES:
            raise VersionError("Java 运行时总大小超过限制，已拒绝安装。")
        files.append(
            RuntimeFile(
                relative=relative,
                url=url,
                sha1=sha1,
                size=size,
                executable=bool(entry.get("executable")),
            )
        )

    if not files:
        raise VersionError("Java 组件清单中没有文件。")

    # A path cannot be both a file and the parent of another file.
    file_paths = {posix_relpath(item.relative).lower() for item in files}
    for directory in directories:
        if posix_relpath(directory).lower() in file_paths:
            raise VersionError(
                f"Java 组件清单中目录与文件同名：{posix_relpath(directory)}"
            )
    for item in files:
        parents = [posix_relpath(parent).lower() for parent in item.relative.parents if parent.parts]
        for parent in parents:
            if parent in file_paths:
                raise VersionError(
                    f"Java 组件清单中文件与目录冲突：{posix_relpath(item.relative)}"
                )
    return ComponentManifest(
        component=component,
        files=files,
        directories=directories,
        total_bytes=total,
        manifest_sha1=manifest_sha1,
    )


def fetch_component_manifest(
    component: JavaRuntimeComponent,
    source: DownloadSource,
    *,
    client: HttpClient | None = None,
    cache_root: Path | None = None,
    logger: Callable[[str], None] | None = None,
) -> ComponentManifest:
    """Fetch (or reuse) a component manifest and verify its SHA-1."""
    log = logger or (lambda _message: None)
    cache_path = (
        _cache_root(source, cache_root)
        / "java-runtime"
        / "components"
        / f"{component.manifest_sha1}.json"
    )
    if cache_path.is_file():
        try:
            payload = cache_path.read_bytes()
            if sha1_file(cache_path).lower() == component.manifest_sha1:
                return _parse_component_manifest(
                    component, payload, component.manifest_sha1
                )
        except (OSError, VersionError):
            pass

    http = client or HttpClient(
        source.allowed_hosts,
        strict_redirects=source.strict_redirects,
        logger=log,
    )
    urls = source.urls_for(component.manifest_url)
    payload = fetch_bytes(
        http, urls, max_size=MAX_COMPONENT_MANIFEST_BYTES, logger=log
    )
    actual = sha1_file_bytes(payload)
    if actual != component.manifest_sha1:
        raise VersionError(
            f"Java 组件清单 SHA-1 不匹配：期望 {component.manifest_sha1}，实际 {actual}"
        )
    if component.manifest_size is not None and len(payload) != component.manifest_size:
        raise VersionError(
            f"Java 组件清单大小不匹配：期望 {component.manifest_size}，实际 {len(payload)}"
        )
    try:
        _write_atomic(cache_path, payload)
    except OSError as exc:
        log(f"无法写入 Java 组件清单缓存：{exc}")
    return _parse_component_manifest(component, payload, actual)


def sha1_file_bytes(payload: bytes) -> str:
    import hashlib

    return hashlib.sha1(payload).hexdigest()


# ---------------------------------------------------------------------------
# Install plan
# ---------------------------------------------------------------------------


def build_install_plan(
    component: JavaRuntimeComponent,
    manifest: ComponentManifest,
    install_root: str | Path,
    source: DownloadSource,
) -> JavaRuntimePlan:
    root = validate_install_root(install_root)
    try:
        folder_name = safe_component(component.id, what="Java 组件名称", max_length=64)
    except VersionError as exc:
        raise LauncherError(str(exc)) from exc
    install_dir = root / folder_name
    if install_dir.exists() and not install_dir.is_dir():
        raise LauncherError(f"安装目标已存在且不是文件夹：{install_dir}")

    planned: list[PlannedRuntimeFile] = []
    for item in manifest.files:
        destination = install_dir / item.relative
        if not is_within(install_dir, destination):
            raise LauncherError(
                f"Java 运行时文件会写出安装目录，已拒绝：{posix_relpath(item.relative)}"
            )
        urls: list[str] = []
        for url in source.urls_for(item.url):
            if url and url not in urls:
                urls.append(url)
        if not urls:
            raise LauncherError(
                f"Java 运行时文件没有可用的下载地址：{posix_relpath(item.relative)}"
            )
        planned.append(PlannedRuntimeFile(file=item, urls=tuple(urls)))

    # Only directories are created explicitly; a file path must never be
    # treated as a directory, and every file's parents are included.
    file_paths = {posix_relpath(item.relative).lower() for item in manifest.files}
    directory_paths: set[Path] = set()
    for item in manifest.directories:
        if posix_relpath(item).lower() not in file_paths:
            directory_paths.add(item)
    for item in manifest.files:
        for parent in item.relative.parents:
            if not parent.parts:
                continue
            if posix_relpath(parent).lower() in file_paths:
                raise VersionError(
                    f"Java 组件清单中文件与目录冲突：{posix_relpath(item.relative)}"
                )
            directory_paths.add(parent)
    directories = sorted(directory_paths, key=lambda item: len(item.parts))
    return JavaRuntimePlan(
        component=component,
        source=source,
        install_root=root,
        install_dir=install_dir,
        files=planned,
        directories=directories,
        manifest_sha1=manifest.manifest_sha1,
    )


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def _prepare_install_dir(plan: JavaRuntimePlan) -> None:
    try:
        plan.install_root.mkdir(parents=True, exist_ok=True)
        plan.install_dir.mkdir(parents=True, exist_ok=True)
        for relative in plan.directories:
            directory = plan.install_dir / relative
            if not is_within(plan.install_dir, directory):
                raise LauncherError(
                    f"Java 运行时目录会写出安装目录，已拒绝：{posix_relpath(relative)}"
                )
            directory.mkdir(parents=True, exist_ok=True)
        probe = plan.install_root / f".mc-launcher-write-test-{os.getpid()}"
        try:
            probe.write_bytes(b"ok")
        finally:
            probe.unlink(missing_ok=True)
    except OSError as exc:
        raise LauncherError(
            f"Java 安装位置没有写入权限：\n{plan.install_root}\n"
            f"系统错误：{exc.strerror or exc}\n"
            "请选择其他文件夹（例如游戏目录下的 runtime）。"
        ) from exc


def _write_marker(plan: JavaRuntimePlan) -> None:
    files = [
        {
            "path": posix_relpath(item.relative),
            "sha1": item.file.sha1,
            "size": item.file.size,
            "executable": item.file.executable,
        }
        for item in plan.files
    ]
    marker = {
        "schema": MARKER_SCHEMA,
        "component": plan.component.id,
        "version": plan.component.version,
        "platform": plan.component.platform,
        "java_major": plan.component.java_major,
        "source": plan.source.id,
        "manifest_sha1": plan.manifest_sha1,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "install_root": str(plan.install_root),
        "java_major_command": str(plan.java_path),
        "total_bytes": plan.total_bytes,
        "files": files,
    }
    _write_atomic(
        plan.marker_path,
        json.dumps(marker, ensure_ascii=False, indent=2).encode("utf-8"),
    )


def install_java_runtime(
    plan: JavaRuntimePlan,
    *,
    client: HttpClient,
    confirmed: bool,
    workers: int = 4,
    progress: Callable[[RuntimeProgress], None] | None = None,
    log: Callable[[str, str], None] | None = None,
    cancel: threading.Event | None = None,
    pause: PauseToken | None = None,
) -> JavaRuntimeOutcome:
    """Download and verify a Java runtime.

    ``confirmed`` is mandatory and intentional: this function refuses to make
    any network request unless the caller records the user's consent.
    """
    if not confirmed:
        raise LauncherError("尚未获得用户确认，已取消 Java 下载。")
    log = log or (lambda _message, _level="info": None)
    _prepare_install_dir(plan)

    total_bytes = plan.total_bytes
    total_files = len(plan.files)
    log(
        f"准备安装 {plan.component.id}（{plan.component.version}）"
        f"：{total_files} 个文件 / {human_size(total_bytes)}，"
        f"目标 {plan.install_dir}",
        "info",
    )

    pending: list[PlannedRuntimeFile] = []
    skipped_bytes = 0
    skipped_files = 0
    for item in plan.files:
        destination = plan.destination(item)
        if _matches_file(destination, item.file.sha1, item.file.size):
            skipped_files += 1
            skipped_bytes += item.file.size
        else:
            pending.append(item)

    if skipped_files:
        log(
            f"已存在 {skipped_files} 个完整文件（{human_size(skipped_bytes)}），"
            f"只需下载 {len(pending)} 个。",
            "muted",
        )

    if pending:
        free = _free_space(plan.install_root)
        required = sum(item.file.size for item in pending)
        if free is not None and free < required + 64 * 1024 * 1024:
            raise DownloadError(
                f"磁盘空间不足：需要约 {human_size(required)}，"
                f"剩余 {human_size(free)}。"
            )

    pending_files = len(pending)
    pending_bytes = sum(item.file.size for item in pending)
    state_lock = threading.Lock()
    emit_lock = threading.Lock()
    file_bytes: dict[str, int] = {}
    done_bytes = skipped_bytes
    done_files = skipped_files
    current = ""
    errors: list[str] = []
    downloaded_paths: list[Path] = []
    last_emit = 0.0
    speed_samples: deque[tuple[float, int]] = deque()

    def emit(force: bool = False, label: str | None = None) -> None:
        nonlocal last_emit, current
        with emit_lock:
            now = time.monotonic()
            if label is not None:
                current = label
            if not force and now - last_emit < 0.12:
                return
            last_emit = now
            speed_samples.append((now, done_bytes))
            while len(speed_samples) > 2 and now - speed_samples[0][0] > 5.0:
                speed_samples.popleft()
            speed = 0.0
            if len(speed_samples) >= 2:
                first_time, first_bytes = speed_samples[0]
                span = now - first_time
                if span > 0.2:
                    speed = max(0.0, (done_bytes - first_bytes) / span)
            if progress is not None:
                progress(
                    RuntimeProgress(
                        phase="install",
                        done_bytes=min(done_bytes, total_bytes),
                        total_bytes=total_bytes,
                        done_files=min(done_files, total_files),
                        total_files=total_files,
                        label=f"{human_size(speed)}/s",
                        current=current,
                    )
                )

    def task(item: PlannedRuntimeFile) -> tuple[PlannedRuntimeFile, int, str | None]:
        key = posix_relpath(item.relative).lower()
        spec = DownloadSpec(
            dest=plan.destination(item),
            url=item.urls[0],
            fallback_urls=tuple(item.urls[1:]),
            sha1=item.file.sha1,
            size=item.file.size,
            label=posix_relpath(item.relative),
        )

        def on_progress(absolute: int) -> None:
            nonlocal done_bytes
            with state_lock:
                previous = file_bytes.get(key, 0)
                done_bytes = max(0, done_bytes + absolute - previous)
                file_bytes[key] = absolute
            emit(label=spec.label)

        try:
            written = client.download_file(
                spec, progress=on_progress, cancel=cancel, pause=pause
            )
            return item, written, None
        except CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - collected per file
            return item, 0, str(exc)

    if pending:
        log(
            f"开始下载 {pending_files} 个文件，共 {human_size(pending_bytes)}。",
            "info",
        )
        emit(force=True, label="准备下载…")

        def cleanup_cancelled() -> None:
            for path in downloaded_paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            for planned in pending:
                dest = plan.destination(planned)
                for candidate in (
                    dest,
                    dest.with_name(dest.name + ".part"),
                ):
                    try:
                        candidate.unlink(missing_ok=True)
                    except OSError:
                        pass
                try:
                    for segment in dest.parent.glob(dest.name + ".part.seg*"):
                        try:
                            segment.unlink(missing_ok=True)
                        except OSError:
                            pass
                except OSError:
                    pass

        try:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=max(1, min(8, workers))
            ) as executor:
                futures = {executor.submit(task, item): item for item in pending}
                try:
                    for future in concurrent.futures.as_completed(futures):
                        if cancel is not None and cancel.is_set():
                            for pending_future in futures:
                                pending_future.cancel()
                            raise CancelledError("Java 下载已取消。")
                        try:
                            item, written, error = future.result()
                        except CancelledError:
                            raise
                        except Exception as exc:  # noqa: BLE001
                            item = futures[future]
                            written, error = 0, str(exc)
                        with state_lock:
                            done_files += 1
                        if error:
                            errors.append(f"{posix_relpath(item.relative)}：{error}")
                            log(
                                f"失败：{posix_relpath(item.relative)} - {error}",
                                "error",
                            )
                        elif written:
                            downloaded_paths.append(plan.destination(item))
                            log(f"完成：{posix_relpath(item.relative)}", "muted")
                        emit(force=True)
                finally:
                    if cancel is not None and cancel.is_set():
                        for pending_future in futures:
                            pending_future.cancel()
        except CancelledError:
            cleanup_cancelled()
            raise
    if cancel is not None and cancel.is_set():
        if pending:
            cleanup_cancelled()
        raise CancelledError("Java 下载已取消。")
    if errors:
        raise DownloadError(
            f"有 {len(errors)} 个 Java 运行时文件下载失败：\n"
            + "\n".join(f"  - {item}" for item in errors[:12])
        )

    _write_marker(plan)
    emit(force=True, label="校验安装结果…")
    runtime = probe_java(plan.java_path, source="mojang-runtime")
    warnings: list[str] = []
    if runtime is None:
        warnings.append(
            "Java 文件已安装，但无法运行 java.exe。请检查安全软件拦截，"
            "或改用其他目录后重试。"
        )
        log(warnings[-1], "error")
    else:
        log(
            f"Java 安装完成：{runtime.version}（{plan.java_path}）",
            "success",
        )

    return JavaRuntimeOutcome(
        plan=plan,
        java_path=plan.java_path,
        runtime=runtime,
        downloaded_files=pending_files,
        downloaded_bytes=pending_bytes,
        skipped_files=skipped_files,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Verification / discovery
# ---------------------------------------------------------------------------


def _read_marker(install_dir: Path) -> dict | None:
    path = install_dir / MARKER_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_bytes())
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema") != MARKER_SCHEMA:
        return None
    return data


def verify_java_runtime(install_dir: str | Path, *, deep: bool = True) -> RuntimeVerifyReport:
    """Verify a managed runtime against its local marker (works offline)."""
    directory = Path(install_dir).expanduser()
    try:
        directory = directory.resolve()
    except OSError:
        directory = directory.absolute()
    started = time.monotonic()
    report = RuntimeVerifyReport(install_dir=directory)
    marker = _read_marker(directory)
    if marker is None:
        report.managed = False
        report.marker_error = "没有找到启动器安装记录（.mc-launcher-runtime.json）"
        report.elapsed = time.monotonic() - started
        return report

    report.managed = True
    report.component_id = str(marker.get("component") or directory.name)
    report.version = str(marker.get("version") or "")
    files = marker.get("files")
    if not isinstance(files, list) or not files:
        report.managed = False
        report.marker_error = "安装记录中没有文件列表"
        report.elapsed = time.monotonic() - started
        return report

    for entry in files:
        if not isinstance(entry, dict):
            continue
        try:
            relative = safe_relative_path(entry.get("path"), what="安装记录文件路径")
            sha1 = normalise_sha1(entry.get("sha1"), what="安装记录文件哈希")
        except VersionError:
            continue
        expected_size = entry.get("size")
        expected_size = expected_size if isinstance(expected_size, int) else None
        destination = directory / relative
        if not is_within(directory, destination):
            report.statuses.append(
                RuntimeFileStatus(
                    path=destination,
                    status="unreadable",
                    expected_size=expected_size,
                    detail="路径越界",
                )
            )
            continue

        status = RuntimeFileStatus(
            path=destination, status="ok", expected_size=expected_size
        )
        try:
            if not destination.is_file():
                status.status = "missing"
            else:
                actual_size = destination.stat().st_size
                status.actual_size = actual_size
                if expected_size is not None and actual_size != expected_size:
                    status.status = "size"
                    status.detail = f"大小 {actual_size} != {expected_size}"
                elif deep:
                    actual = sha1_file(destination).lower()
                    if actual != sha1:
                        status.status = "hash"
                        status.detail = "SHA-1 不匹配"
        except OSError as exc:
            status.status = "unreadable"
            status.detail = str(exc)
        report.statuses.append(status)

    report.elapsed = time.monotonic() - started
    return report


def scan_installed_runtimes(roots: Iterable[Path | str]) -> list[InstalledRuntime]:
    """Return managed/unmanaged runtimes found directly below each root."""
    seen: set[Path] = set()
    found: list[InstalledRuntime] = []
    for root in roots:
        try:
            directory = Path(root).expanduser().resolve()
        except OSError:
            continue
        if directory in seen or not directory.is_dir():
            continue
        seen.add(directory)
        for child in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
            if not child.is_dir():
                continue
            java_path = child / "bin" / "java.exe"
            if not java_path.is_file():
                continue
            marker = _read_marker(child) or {}
            version = str(marker.get("version") or "")
            major = marker.get("java_major")
            total = marker.get("total_bytes")
            size_bytes = total if isinstance(total, int) else _folder_size(child)
            found.append(
                InstalledRuntime(
                    install_dir=child,
                    component_id=str(marker.get("component") or child.name),
                    java_path=java_path,
                    version=version,
                    java_major=major if isinstance(major, int) else None,
                    managed=bool(marker),
                    size_bytes=size_bytes,
                    installed_at=str(marker.get("installed_at") or ""),
                )
            )
    return found


def _folder_size(root: Path) -> int:
    total = 0
    try:
        for path in root.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


def find_installed_runtime(
    root: Path | str, component_id: str
) -> InstalledRuntime | None:
    try:
        folder = safe_component(component_id, what="Java 组件名称", max_length=64)
        directory = (Path(root).expanduser() / folder).resolve()
    except (OSError, VersionError):
        return None
    if not (directory / "bin" / "java.exe").is_file():
        return None
    marker = _read_marker(directory) or {}
    total = marker.get("total_bytes")
    return InstalledRuntime(
        install_dir=directory,
        component_id=str(marker.get("component") or component_id),
        java_path=directory / "bin" / "java.exe",
        version=str(marker.get("version") or ""),
        java_major=marker.get("java_major")
        if isinstance(marker.get("java_major"), int)
        else None,
        managed=bool(marker),
        size_bytes=total if isinstance(total, int) else _folder_size(directory),
        installed_at=str(marker.get("installed_at") or ""),
    )


# ---------------------------------------------------------------------------
# Manual download links / formatting
# ---------------------------------------------------------------------------


def manual_java_links(
    java_major: int | None = None,
    component: JavaRuntimeComponent | None = None,
) -> list[ManualJavaLink]:
    major = java_major or (component.java_major if component else None) or 17
    links = [
        ManualJavaLink(
            "Mojang Java 运行时清单（官方，JSON）",
            JAVA_RUNTIME_MANIFEST_URL,
            "官方启动器使用的清单；可按组件和平台查看/下载单个文件。",
        )
    ]
    if component is not None:
        links.append(
            ManualJavaLink(
                f"{component.id} 组件清单（{component.platform}）",
                component.manifest_url,
                "该组件的文件列表与 SHA-1；适合手工核对或脚本下载。",
            )
        )
    for label, template, note in VENDOR_LINKS:
        links.append(ManualJavaLink(label, template.format(major=major), note))
    return links


def format_component_table(
    components: Iterable[JavaRuntimeComponent], required_major: int | None = None
) -> str:
    items = list(components)
    lines = [
        f"{'组件':<26} {'Java':<6} {'版本':<24} {'平台':<16} 说明",
        "-" * 96,
    ]
    for item in items:
        mark = ""
        if required_major is not None and item.java_major == required_major:
            mark = "匹配当前版本"
        elif item.experimental:
            mark = "实验/快照"
        lines.append(
            f"{item.id:<26} {str(item.java_major or '?'):<6} "
            f"{item.version:<24} {item.platform:<16} {mark}"
        )
    return "\n".join(lines)


def format_runtime_report(report: RuntimeVerifyReport) -> str:
    lines = [
        f"安装目录：{report.install_dir}",
        f"组件：{report.component_id or '未知'}"
        + (f"（{report.version}）" if report.version else ""),
    ]
    if not report.managed:
        lines.append("管理状态：不是由本启动器安装，或无安装记录")
        lines.append(
            "java.exe：" + ("存在" if (report.install_dir / "bin" / "java.exe").is_file() else "不存在")
        )
        if report.marker_error:
            lines.append(f"说明：{report.marker_error}")
        return "\n".join(lines)
    lines.append(
        f"文件：{report.total} 个，通过 {report.ok} 个，"
        f"缺失 {len(report.missing)} 个，损坏 {len(report.corrupt)} 个"
    )
    if report.problematic:
        lines.append(
            f"需要补全约 {human_size(report.bad_bytes)}；"
            "使用“开始下载并安装”会只补全缺失/损坏文件。"
        )
        for item in report.problematic[:12]:
            try:
                relative = posix_relpath(item.path.relative_to(report.install_dir))
            except ValueError:
                relative = str(item.path)
            lines.append(f"  - {relative}：{item.status} {item.detail}".rstrip())
    else:
        lines.append("结果：全部文件校验通过。")
    lines.append(f"耗时：{report.elapsed:.2f} 秒")
    return "\n".join(lines)
