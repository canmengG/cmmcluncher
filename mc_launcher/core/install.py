"""Build install/verification plans for vanilla and modded versions.

A plan is a deterministic list of files with expected sizes and SHA-1 hashes.
The same structure is used for:

* downloading a fresh version;
* checking an existing installation (deep or fast);
* repairing only missing/corrupt files;
* writing the loader profile and materialising legacy virtual assets.

Nothing here executes downloaded code except the official Forge installer,
which is handled separately in :mod:`mc_launcher.core.loaders`.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import posixpath
import shutil
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .errors import LauncherError, VersionError
from .manifest import RemoteVersion, VersionDetail, fetch_bytes
from .mirrors import DownloadSource
from .models import GameRoot
from .net import (
    CancelledError,
    DownloadError,
    DownloadSpec,
    HttpClient,
    PauseToken,
    PROVIDER_HOSTS,
    human_size,
    sha1_file,
    validate_url,
)
from .paths import version_game_dir
from .safety import (
    check_asset_hash,
    check_asset_name,
    normalise_sha1,
    safe_component,
    safe_relative_path,
)
from .version import (
    _native_classifier,
    find_client_jar,
    is_allowed,
    list_versions,
    load_merged_version,
    maven_path,
)

ProgressCallback = Callable[["Progress"], None]
LogCallback = Callable[[str, str], None]


@dataclass(frozen=True)
class PlannedFile:
    relative: Path
    urls: tuple[str, ...] = ()
    sha1: str | None = None
    size: int | None = None
    category: str = "file"
    label: str = ""

    def destination(self, root: GameRoot) -> Path:
        return root.root / self.relative

    @property
    def display(self) -> str:
        return self.label or self.relative.name


@dataclass(frozen=True)
class AssetIndexPlan:
    id: str
    relative: Path
    virtual: bool
    map_to_resources: bool
    objects: dict[str, str] = field(default_factory=dict)


@dataclass
class InstallPlan:
    root: GameRoot
    source: DownloadSource
    version_id: str
    version_type: str
    loader: str
    game_version: str
    version_json_relative: Path
    version_json_payload: bytes
    version_json_sha1: str
    files: list[PlannedFile]
    asset_index: AssetIndexPlan | None = None
    warnings: list[str] = field(default_factory=list)
    prerequisites: list[VersionDetail] = field(default_factory=list)

    @property
    def version_json_path(self) -> Path:
        return self.root.root / self.version_json_relative

    @property
    def total_size(self) -> int:
        return sum(item.size or 0 for item in self.files)

    @property
    def sidecar_path(self) -> Path:
        return self.root.versions_dir / self.version_id / ".mc-launcher-files.json"


@dataclass
class Progress:
    phase: str
    done_bytes: int = 0
    total_bytes: int = 0
    done_files: int = 0
    total_files: int = 0
    label: str = ""
    # The file currently being transferred/checked; kept separate from
    # ``label`` so the UI can show it without parsing formatted text.
    current: str = ""


@dataclass
class FileStatus:
    file: PlannedFile
    status: str  # ok | missing | size | hash | unreadable
    actual_size: int | None = None
    actual_sha1: str | None = None
    detail: str = ""


@dataclass
class VerifyReport:
    statuses: list[FileStatus] = field(default_factory=list)
    version_json_status: str = "ok"
    version_json_detail: str = ""
    unverified: int = 0
    elapsed: float = 0.0

    @property
    def total(self) -> int:
        return len(self.statuses)

    @property
    def ok(self) -> int:
        return sum(1 for item in self.statuses if item.status == "ok")

    @property
    def missing(self) -> list[FileStatus]:
        return [item for item in self.statuses if item.status == "missing"]

    @property
    def corrupt(self) -> list[FileStatus]:
        return [item for item in self.statuses if item.status in ("size", "hash", "unreadable")]

    @property
    def problematic(self) -> list[FileStatus]:
        return [item for item in self.statuses if item.status != "ok"]

    @property
    def healthy(self) -> bool:
        return not self.problematic and self.version_json_status not in (
            "missing",
            "unreadable",
        )

    @property
    def bad_bytes(self) -> int:
        return sum(item.file.size or 0 for item in self.problematic)


@dataclass
class InstallOutcome:
    plan: InstallPlan
    verify: VerifyReport
    downloaded_files: int = 0
    downloaded_bytes: int = 0
    skipped_files: int = 0
    materialized_files: int = 0
    warnings: list[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def healthy(self) -> bool:
        return self.verify.healthy


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def _check_urls(source: DownloadSource | None, official_url: str) -> tuple[str, ...]:
    if source is None:
        return ()
    candidates: list[str] = []
    for url in source.urls_for(official_url):
        try:
            validate_url(url, source.allowed_hosts, check_dns=False)
        except LauncherError:
            continue
        if url not in candidates:
            candidates.append(url)
    if not candidates:
        raise VersionError(f"下载地址不在允许的源中：{official_url[:160]}")
    return tuple(candidates)


def _asset_urls(source: DownloadSource | None, official_url: str) -> tuple[str, ...]:
    if source is None:
        return ()
    candidates: list[str] = []
    if source.asset_official_first:
        preferred = (official_url, source.rewrite(official_url))
    else:
        preferred = source.urls_for(official_url)
    for url in preferred:
        if not url:
            continue
        try:
            validate_url(url, source.allowed_hosts, check_dns=False)
        except LauncherError:
            continue
        if url not in candidates:
            candidates.append(url)
    if not candidates:
        raise VersionError(f"资源下载地址不在允许的源中：{official_url[:160]}")
    return tuple(candidates)


def _maven_url(base: str, relative: Path) -> str:
    # Keep '+' literal in Maven paths.  Fabric's sponge-mixin uses versions
    # such as ``0.12.5+mixin.0.8.5``; percent-encoding the plus (``%2B``) is
    # valid for the official Maven host but BMCLAPI's Maven proxy answers 404,
    # which used to make Fabric 0.14.22 impossible to install.
    path = urllib.parse.quote(relative.as_posix(), safe="/+")
    if base.endswith("/"):
        return base + path
    parsed = urllib.parse.urlsplit(base)
    if parsed.path.lower().endswith((".jar", ".zip")):
        return base
    return base.rstrip("/") + "/" + path


def _libraries_url(relative: Path) -> str:
    return "https://libraries.minecraft.net/" + urllib.parse.quote(
        relative.as_posix(), safe="/"
    )


def _planned(
    relative: Path,
    *,
    urls: tuple[str, ...],
    sha1: str | None,
    size: int | None,
    category: str,
    label: str = "",
) -> PlannedFile:
    return PlannedFile(
        relative=relative,
        urls=urls,
        sha1=sha1,
        size=size,
        category=category,
        label=label,
    )


_CATEGORY_ORDER = {
    "client": 0,
    "asset_index": 1,
    "log_config": 2,
    "library": 3,
    "loader_library": 4,
    "native": 5,
    "asset": 6,
}


def sort_planned_files(files: Iterable[PlannedFile]) -> list[PlannedFile]:
    """Order files so the base installation lands before asset objects.

    The client jar, the asset index and libraries are what make a version
    runnable; a user who cancels after those are present has a usable (if
    incomplete) installation.  Asset objects are the long tail.
    """
    return sorted(
        files,
        key=lambda item: (
            _CATEGORY_ORDER.get(item.category, 50),
            item.relative.as_posix().lower(),
        ),
    )


def _add_file(bucket: dict[str, PlannedFile], planned: PlannedFile) -> None:
    key = planned.relative.as_posix().lower()
    existing = bucket.get(key)
    if existing is None:
        bucket[key] = planned
        return
    if (existing.sha1 or "") != (planned.sha1 or "") or (
        existing.size is not None
        and planned.size is not None
        and existing.size != planned.size
    ):
        raise VersionError(f"同一路径出现不同文件：{planned.relative}")
    if planned.urls:
        merged = list(existing.urls)
        for url in planned.urls:
            if url not in merged:
                merged.append(url)
        bucket[key] = PlannedFile(
            relative=existing.relative,
            urls=tuple(merged),
            sha1=existing.sha1 or planned.sha1,
            size=existing.size if existing.size is not None else planned.size,
            category=existing.category,
            label=existing.label or planned.label,
        )


# ---------------------------------------------------------------------------
# Library/asset parsing
# ---------------------------------------------------------------------------


def _parse_size(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _parse_sha1(value: Any, logger: LogCallback | None, label: str) -> str | None:
    if value in (None, ""):
        return None
    try:
        return normalise_sha1(value)
    except VersionError:
        if logger:
            logger(f"{label} 的 SHA-1 格式错误，已跳过校验。", "warning")
        return None


def _bmcl_maven_mirror(url: str) -> str | None:
    """BMCLAPI Maven URL for a Fabric/NeoForge artifact, if one exists.

    Fabric publishes under ``maven.fabricmc.net/`` and NeoForge under
    ``maven.neoforged.net/releases/``.  BMCLAPI mirrors both trees at
    ``/maven/`` (without NeoForge's ``/releases`` prefix).
    """
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host == "maven.fabricmc.net":
        mirror_path = "/maven" + parsed.path
    elif host == "maven.neoforged.net" and parsed.path.startswith("/releases/"):
        mirror_path = "/maven" + parsed.path[len("/releases") :]
    else:
        return None
    return urllib.parse.urlunsplit(
        ("https", "bmclapi2.bangbang93.com", mirror_path, parsed.query, "")
    )


def _fabric_library_urls(source: DownloadSource | None, url: str) -> tuple[str, ...]:
    """Fabric/NeoForge Maven URLs with the BMCLAPI mirror tried first.

    Fabric and NeoForge profiles hard-code their official Maven hosts.  Those
    hosts can time out or be unreachable in some networks, while BMCLAPI
    mirrors the same Maven trees.  Keep the official URL as a fallback; SHA-1
    still validates whichever copy is used.
    """
    urls = list(_check_urls(source, url)) if source is not None else [url]
    mirror = _bmcl_maven_mirror(url)
    if mirror is None:
        return tuple(urls)
    try:
        validate_url(mirror, PROVIDER_HOSTS, check_dns=False)
    except LauncherError:
        return tuple(urls)
    ordered = [mirror] + [item for item in urls if item != mirror]
    return tuple(dict.fromkeys(ordered))


def _library_files(
    data: dict[str, Any],
    source: DownloadSource | None,
    logger: LogCallback | None,
    warnings: list[str],
) -> list[PlannedFile]:
    files: dict[str, PlannedFile] = {}
    for library in data.get("libraries") or []:
        if not isinstance(library, dict) or not is_allowed(library):
            continue
        name = library.get("name")
        if not isinstance(name, str) or not name:
            continue
        downloads = library.get("downloads")
        if not isinstance(downloads, dict):
            downloads = {}
        artifact = downloads.get("artifact")
        artifact_added = False

        if isinstance(artifact, dict):
            maven_relative = _artifact_relative(artifact, name)
            relative = Path("libraries") / maven_relative
            url = artifact.get("url")
            if not isinstance(url, str) or not url:
                url = _libraries_url(maven_relative)
            urls = _fabric_library_urls(source, url)
            sha1 = _parse_sha1(artifact.get("sha1"), logger, name)
            size = _parse_size(artifact.get("size"))
            _add_file(
                files,
                _planned(
                    relative,
                    urls=urls,
                    sha1=sha1,
                    size=size,
                    category="library",
                    label=name,
                ),
            )
            artifact_added = True
        elif isinstance(library.get("url"), str) and library.get("url"):
            # Fabric/Quilt loader profiles provide a Maven base URL rather
            # than downloads.artifact.
            maven_relative = _maven_relative(name)
            relative = Path("libraries") / maven_relative
            base = str(library["url"])
            url = _maven_url(base, maven_relative)
            urls = _fabric_library_urls(source, url)
            sha1 = _parse_sha1(library.get("sha1"), logger, name)
            size = _parse_size(library.get("size"))
            _add_file(
                files,
                _planned(
                    relative,
                    urls=urls,
                    sha1=sha1,
                    size=size,
                    category="loader_library",
                    label=name,
                ),
            )
            artifact_added = True

        classifier = _native_classifier(library)
        if classifier:
            classifiers = downloads.get("classifiers")
            native_artifact = (
                classifiers.get(classifier) if isinstance(classifiers, dict) else None
            )
            if isinstance(native_artifact, dict):
                maven_relative = _artifact_relative(
                    native_artifact, f"{name}:{classifier}"
                )
                relative = Path("libraries") / maven_relative
                url = native_artifact.get("url")
                if not isinstance(url, str) or not url:
                    url = _libraries_url(maven_relative)
                urls = _fabric_library_urls(source, url)
                sha1 = _parse_sha1(native_artifact.get("sha1"), logger, name)
                size = _parse_size(native_artifact.get("size"))
                _add_file(
                    files,
                    _planned(
                        relative,
                        urls=urls,
                        sha1=sha1,
                        size=size,
                        category="native",
                        label=f"{name}:{classifier}",
                    ),
                )
                artifact_added = True

        if not artifact_added:
            # A few old loader profiles carry a library with only a name.  It
            # cannot be downloaded, but it also usually means the library is
            # already part of the client jar.
            warnings.append(f"库 {name} 没有下载地址，已跳过。")
            if logger:
                logger(f"库 {name} 没有下载地址，已跳过。", "warning")
    return list(files.values())


def _artifact_relative(artifact: dict[str, Any], name: str) -> Path:
    relative = artifact.get("path")
    if isinstance(relative, str) and relative:
        return safe_relative_path(relative, what="库路径")
    return _maven_relative(name)


def _maven_relative(name: str) -> Path:
    path = maven_path(name)
    return safe_relative_path(path.as_posix(), what="Maven 路径")


def _asset_index_files(
    root: GameRoot,
    data: dict[str, Any],
    source: DownloadSource | None,
    client: HttpClient | None,
    logger: LogCallback | None,
    warnings: list[str],
    *,
    trust_local_index: bool = False,
) -> tuple[list[PlannedFile], AssetIndexPlan | None]:
    files: dict[str, PlannedFile] = {}
    raw_index = data.get("assetIndex")
    if not isinstance(raw_index, dict):
        raw_index = {}
    index_id = raw_index.get("id") or data.get("assets")
    if not isinstance(index_id, str) or not index_id:
        warnings.append("该版本没有资源索引，跳过资源下载。")
        return [], None
    try:
        index_id = safe_component(index_id, what="资源索引 ID", max_length=96)
    except VersionError as exc:
        warnings.append(str(exc))
        return [], None

    relative = Path("assets") / "indexes" / f"{index_id}.json"
    target = root.root / relative
    expected_sha1 = _parse_sha1(raw_index.get("sha1"), logger, f"资源索引 {index_id}")
    expected_size = _parse_size(raw_index.get("size"))

    payload: bytes | None = None
    local_is_valid = False
    if target.is_file():
        try:
            local = target.read_bytes()
            if trust_local_index:
                # Local verification trusts the installed index; third-party
                # launchers may rewrite index metadata in the version JSON.
                payload = local
                local_is_valid = True
                expected_sha1 = _bytes_sha1(local)
                expected_size = len(local)
            elif (
                (expected_size is None or len(local) == expected_size)
                and (expected_sha1 is None or _bytes_sha1(local) == expected_sha1)
            ):
                payload = local
                local_is_valid = True
            else:
                warnings.append(f"本地资源索引 {index_id} 校验失败，将重新下载。")
        except OSError:
            payload = None

    if payload is None:
        cache_file = None
        if source is not None and expected_sha1:
            from .storage import cache_dir

            cache_file = (
                cache_dir(source.id)
                / "indexes"
                / f"{index_id}-{expected_sha1[:12]}.json"
            )
            if cache_file.is_file():
                try:
                    cached = cache_file.read_bytes()
                    if _bytes_sha1(cached) == expected_sha1:
                        payload = cached
                except OSError:
                    pass
    if payload is None:
        url = raw_index.get("url")
        if isinstance(url, str) and url and client is not None and source is not None:
            try:
                payload = fetch_bytes(
                    client,
                    source.urls_for(url),
                    max_size=16 * 1024 * 1024,
                    logger=lambda message: logger(message, "muted") if logger else None,
                )
                if expected_size is not None and len(payload) != expected_size:
                    raise VersionError("资源索引大小不匹配。")
                if expected_sha1 is not None and _bytes_sha1(payload) != expected_sha1:
                    raise VersionError("资源索引 SHA-1 不匹配。")
                if cache_file is not None:
                    try:
                        _write_bytes_atomic(cache_file, payload)
                    except OSError:
                        pass
            except LauncherError as exc:
                if local_is_valid:
                    warnings.append(f"无法下载资源索引，使用本地文件：{exc}")
                    payload = target.read_bytes()
                else:
                    raise VersionError(
                        f"资源索引 {index_id} 校验失败，且无法重新下载：{exc}"
                    ) from exc
        elif target.is_file() and local_is_valid:
            payload = target.read_bytes()
            warnings.append("资源索引缺少下载地址，使用本地文件。")
        elif client is None:
            # Local verification without a remote plan: no index means the
            # asset list cannot be reconstructed.  Report it as a warning and
            # keep checking the files we do know about.
            warnings.append(f"缺少资源索引 {index_id}，无法检查资源对象。")
            return [], None
        else:
            raise VersionError(f"缺少资源索引 {index_id}，且没有可用的下载地址。")

    try:
        index_data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VersionError(f"资源索引 {index_id} 不是有效 JSON：{exc}") from exc
    objects = index_data.get("objects")
    if not isinstance(objects, dict):
        raise VersionError(f"资源索引 {index_id} 缺少 objects 列表。")
    if len(objects) > 200_000:
        raise VersionError("资源索引条目过多，已拒绝解析。")

    if raw_index.get("url"):
        urls = _check_urls(source, str(raw_index["url"]))
    else:
        urls = ()
    _add_file(
        files,
        _planned(
            relative,
            urls=urls,
            sha1=expected_sha1,
            size=expected_size,
            category="asset_index",
            label=f"资源索引 {index_id}",
        ),
    )

    names: dict[str, str] = {}
    seen_hashes: set[str] = set()
    for name, obj in objects.items():
        if not isinstance(name, str) or not isinstance(obj, dict):
            continue
        try:
            asset_hash = check_asset_hash(obj.get("hash"))
        except VersionError:
            warnings.append(f"资源 {name} 的哈希无效，已跳过。")
            continue
        size = _parse_size(obj.get("size"))
        if size is None:
            warnings.append(f"资源 {name} 缺少大小，已跳过。")
            continue
        names[name] = asset_hash
        if asset_hash in seen_hashes:
            continue
        seen_hashes.add(asset_hash)
        object_relative = Path("assets") / "objects" / asset_hash[:2] / asset_hash
        official = f"https://resources.download.minecraft.net/{asset_hash[:2]}/{asset_hash}"
        _add_file(
            files,
            _planned(
                object_relative,
                urls=_asset_urls(source, official),
                sha1=asset_hash,
                size=size,
                category="asset",
                label=name,
            ),
        )

    asset_plan = AssetIndexPlan(
        id=index_id,
        relative=relative,
        virtual=bool(index_data.get("virtual")),
        map_to_resources=bool(index_data.get("map_to_resources")),
        objects=names,
    )
    return list(files.values()), asset_plan


def _logging_files(
    data: dict[str, Any],
    source: DownloadSource | None,
    logger: LogCallback | None,
) -> list[PlannedFile]:
    logging = data.get("logging")
    if not isinstance(logging, dict):
        return []
    client = logging.get("client")
    if not isinstance(client, dict):
        return []
    file_info = client.get("file")
    if not isinstance(file_info, dict):
        return []
    file_id = file_info.get("id")
    if not isinstance(file_id, str) or not file_id:
        return []
    try:
        file_id = safe_component(file_id, what="日志配置 ID", max_length=128)
    except VersionError:
        return []
    url = file_info.get("url")
    if not isinstance(url, str) or not url:
        sha1 = file_info.get("sha1")
        if isinstance(sha1, str) and len(sha1) == 40:
            url = f"https://launchermeta.mojang.com/v1/objects/{sha1}"
        else:
            return []
    try:
        urls = _check_urls(source, url)
    except VersionError:
        return []
    return [
        _planned(
            Path("assets") / "log_configs" / file_id,
            urls=urls,
            sha1=_parse_sha1(file_info.get("sha1"), logger, "日志配置"),
            size=_parse_size(file_info.get("size")),
            category="log_config",
            label=f"日志配置 {file_id}",
        )
    ]


def _client_file(
    root: GameRoot,
    version_id: str,
    data: dict[str, Any],
    source: DownloadSource | None,
) -> PlannedFile:
    downloads = data.get("downloads")
    client = downloads.get("client") if isinstance(downloads, dict) else None
    if not isinstance(client, dict):
        raise VersionError(f"版本 {version_id} 缺少客户端 jar 下载信息。")
    relative = Path("versions") / version_id / f"{version_id}.jar"
    url = client.get("url")
    if not isinstance(url, str) or not url:
        raise VersionError(f"版本 {version_id} 的客户端 jar 缺少下载地址。")
    return _planned(
        relative,
        urls=_check_urls(source, url),
        sha1=_parse_sha1(client.get("sha1"), None, "客户端 jar"),
        size=_parse_size(client.get("size")),
        category="client",
        label="客户端 jar",
    )


# ---------------------------------------------------------------------------
# Plan construction
# ---------------------------------------------------------------------------


def build_vanilla_plan(
    root: GameRoot,
    source: DownloadSource,
    remote: RemoteVersion,
    *,
    client: HttpClient | None = None,
    logger: LogCallback | None = None,
) -> InstallPlan:
    from .manifest import fetch_version_detail

    http = client or HttpClient(
        source.allowed_hosts,
        strict_redirects=source.strict_redirects,
        logger=lambda message: logger(message, "muted") if logger else None,
    )
    detail = fetch_version_detail(source, remote, client=http, logger=lambda message: logger(message, "muted") if logger else None)
    return build_plan_from_detail(root, source, detail, client=http, logger=logger)


def build_plan_from_detail(
    root: GameRoot,
    source: DownloadSource,
    detail: VersionDetail,
    *,
    client: HttpClient | None = None,
    logger: LogCallback | None = None,
) -> InstallPlan:
    data = detail.data
    version_id = safe_component(
        detail.remote.id, what="版本 ID", max_length=96
    )
    if client is None:
        client = HttpClient(
            source.allowed_hosts,
            strict_redirects=source.strict_redirects,
            logger=lambda message: logger(message, "muted") if logger else None,
        )
    warnings: list[str] = []
    files: dict[str, PlannedFile] = {}
    _add_file(files, _client_file(root, version_id, data, source))
    for planned in _library_files(data, source, logger, warnings):
        _add_file(files, planned)
    asset_files, asset_plan = _asset_index_files(
        root, data, source, client, logger, warnings
    )
    for planned in asset_files:
        _add_file(files, planned)
    for planned in _logging_files(data, source, logger):
        _add_file(files, planned)

    payload = detail.raw
    version_json_relative = Path("versions") / version_id / f"{version_id}.json"
    return InstallPlan(
        root=root,
        source=source,
        version_id=version_id,
        version_type=str(data.get("type") or detail.remote.type or "release"),
        loader="vanilla",
        game_version=version_id,
        version_json_relative=version_json_relative,
        version_json_payload=payload,
        version_json_sha1=_bytes_sha1(payload),
        files=sort_planned_files(files.values()),
        asset_index=asset_plan,
        warnings=warnings,
    )


def build_local_plan(
    root: GameRoot,
    version_id: str,
    source: DownloadSource | None = None,
    *,
    logger: LogCallback | None = None,
) -> InstallPlan:
    """Build a verification plan from an already installed version."""
    summaries = list_versions(root)
    summary = next(
        (
            item
            for item in summaries
            if version_id in (item.version_id, item.folder_name)
        ),
        None,
    )
    if summary is None:
        raise VersionError(f"找不到已安装版本：{version_id}")
    loaded = load_merged_version(root, summary)
    data = loaded.data
    warnings: list[str] = []
    files: dict[str, PlannedFile] = {}

    client_jar = find_client_jar(root, loaded)
    if client_jar is not None:
        try:
            relative = client_jar.resolve().relative_to(root.root.resolve())
        except (ValueError, OSError):
            relative = Path("versions") / summary.folder_name / client_jar.name
        downloads = data.get("downloads")
        client_info = downloads.get("client") if isinstance(downloads, dict) else None
        _add_file(
            files,
            _planned(
                relative,
                urls=(),
                sha1=_parse_sha1(
                    client_info.get("sha1") if isinstance(client_info, dict) else None,
                    logger,
                    "客户端 jar",
                ),
                size=_parse_size(
                    client_info.get("size") if isinstance(client_info, dict) else None
                ),
                category="client",
                label="客户端 jar",
            ),
        )
    else:
        warnings.append("本地版本缺少客户端 jar。")

    for planned in _library_files(data, source, logger, warnings):
        _add_file(files, planned)

    asset_files, asset_plan = _asset_index_files(
        root, data, source, None, logger, warnings, trust_local_index=True
    )
    for planned in asset_files:
        _add_file(files, planned)
    for planned in _logging_files(data, source, logger):
        _add_file(files, planned)

    json_path = summary.json_path
    try:
        payload = json_path.read_bytes()
    except OSError as exc:
        raise VersionError(f"无法读取版本 JSON：{exc}") from exc
    try:
        relative_json = json_path.resolve().relative_to(root.root.resolve())
    except (ValueError, OSError):
        relative_json = Path("versions") / summary.folder_name / json_path.name

    return InstallPlan(
        root=root,
        source=source or _fallback_source(),
        version_id=summary.version_id,
        version_type=summary.version_type or "release",
        loader=_detect_loader(data),
        game_version=str(data.get("inheritsFrom") or summary.version_id),
        version_json_relative=relative_json,
        version_json_payload=payload,
        version_json_sha1=_bytes_sha1(payload),
        files=sort_planned_files(files.values()),
        asset_index=asset_plan,
        warnings=warnings,
    )


def _fallback_source() -> DownloadSource:
    from .mirrors import get_source

    return get_source("mojang")


def _detect_loader(data: dict[str, Any]) -> str:
    main_class = str(data.get("mainClass") or "")
    libraries = " ".join(
        str(item.get("name") or "") for item in data.get("libraries") or [] if isinstance(item, dict)
    )
    main_lower = main_class.lower()
    lib_lower = libraries.lower()
    # NeoForge's legacy installer keeps the Forge bootstrap main class, so the
    # library group (net.neoforged) is the reliable signal.  Check it first.
    if "neoforge" in main_lower or "neoforged" in lib_lower:
        return "neoforge"
    if "quilt" in main_lower or "quilt-loader" in lib_lower:
        return "quilt"
    if "fabric" in main_lower or "fabric-loader" in lib_lower:
        return "fabric"
    if "forge" in main_lower or "minecraftforge" in lib_lower:
        return "forge"
    return "vanilla"


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_plan(
    plan: InstallPlan,
    *,
    deep: bool = True,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> VerifyReport:
    start = time.monotonic()
    statuses: list[FileStatus] = []
    total = len(plan.files)
    total_bytes = sum(item.size or 0 for item in plan.files)
    done_bytes = 0
    last_emit = 0.0
    for index, planned in enumerate(plan.files, 1):
        if cancel is not None and cancel.is_set():
            raise CancelledError("操作已取消。")
        path = planned.destination(plan.root)
        status = _verify_file(path, planned.sha1, planned.size, deep=deep)
        statuses.append(FileStatus(file=planned, **status))
        done_bytes += planned.size or 0
        now = time.monotonic()
        if progress is not None and (index == total or now - last_emit >= 0.1):
            last_emit = now
            progress(
                Progress(
                    phase="verify",
                    done_bytes=min(done_bytes, total_bytes),
                    total_bytes=total_bytes,
                    done_files=index,
                    total_files=total,
                    label=planned.display,
                )
            )
    version_json_status = "ok"
    version_detail = ""
    json_path = plan.version_json_path
    if not json_path.is_file():
        version_json_status = "missing"
        version_detail = "版本 JSON 缺失"
    else:
        try:
            payload = json_path.read_bytes()
            if deep and _bytes_sha1(payload) != plan.version_json_sha1:
                version_json_status = "modified"
                version_detail = "版本 JSON 与安装时不同（可能被启动器或用户修改）"
        except OSError as exc:
            version_json_status = "unreadable"
            version_detail = str(exc)
    unverified = sum(
        1
        for status in statuses
        if status.status == "ok" and status.file.sha1 is None and status.file.size is None
    )
    report = VerifyReport(
        statuses=statuses,
        version_json_status=version_json_status,
        version_json_detail=version_detail,
        unverified=unverified,
        elapsed=time.monotonic() - start,
    )
    if progress is not None:
        progress(
            Progress(
                phase="verify_done",
                done_bytes=total_bytes,
                total_bytes=total_bytes,
                done_files=total,
                total_files=total,
                label="检查完成",
            )
        )
    return report


def _verify_file(
    path: Path, sha1: str | None, size: int | None, *, deep: bool
) -> dict[str, Any]:
    try:
        if not path.is_file():
            return {"status": "missing", "detail": "文件不存在"}
        actual_size = path.stat().st_size
        if size is not None and actual_size != size:
            return {
                "status": "size",
                "actual_size": actual_size,
                "detail": f"大小不符：期望 {size}，实际 {actual_size}",
            }
        actual_sha1 = None
        if deep and sha1:
            actual_sha1 = sha1_file(path)
            if actual_sha1.lower() != sha1.lower():
                return {
                    "status": "hash",
                    "actual_size": actual_size,
                    "actual_sha1": actual_sha1,
                    "detail": f"SHA-1 不符：期望 {sha1}，实际 {actual_sha1}",
                }
        return {
            "status": "ok",
            "actual_size": actual_size,
            "actual_sha1": actual_sha1,
            "detail": "",
        }
    except OSError as exc:
        return {"status": "unreadable", "detail": str(exc)}


# ---------------------------------------------------------------------------
# Download/install execution
# ---------------------------------------------------------------------------


def _pending_files(report: VerifyReport) -> list[PlannedFile]:
    return [item.file for item in report.problematic]


def _cleanup_cancelled_downloads(
    downloaded: list[Path], pending: list[PlannedFile], root: GameRoot
) -> None:
    """Delete files downloaded by a cancelled operation and all partials."""
    for path in downloaded:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    for planned in pending:
        dest = planned.destination(root)
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


def _download_tasks(
    plan: InstallPlan,
    pending: list[PlannedFile],
    client: HttpClient,
    *,
    workers: int,
    progress: ProgressCallback | None,
    log: LogCallback | None,
    cancel: threading.Event | None,
    pause: PauseToken | None = None,
) -> tuple[int, int, list[str]]:
    log = log or (lambda _message, _level="info": None)
    total_bytes = sum(item.size or 0 for item in pending)
    total_files = len(pending)
    state_lock = threading.Lock()
    emit_lock = threading.Lock()
    # Bytes currently present per file.  Progress callbacks are absolute, so
    # retries and resumed partial files never double-count.
    file_bytes: dict[str, int] = {}
    done_bytes = 0
    done_files = 0
    current_label = ""
    errors: list[str] = []
    downloaded_paths: list[Path] = []
    last_emit = 0.0
    speed_samples: deque[tuple[float, int]] = deque()

    def emit(force: bool = False, current: str | None = None) -> None:
        nonlocal last_emit, current_label
        with emit_lock:
            now = time.monotonic()
            if current is not None:
                current_label = current
            if not force and now - last_emit < 0.12:
                return
            last_emit = now
            # Sliding five-second window so a stall is visible quickly instead
            # of being hidden by the average of a long download.
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
                    Progress(
                        phase="download",
                        done_bytes=min(done_bytes, total_bytes),
                        total_bytes=total_bytes,
                        done_files=done_files,
                        total_files=total_files,
                        label=f"{human_size(speed)}/s",
                        current=current_label,
                    )
                )

    def task(planned: PlannedFile) -> tuple[PlannedFile, int, str | None]:
        key = planned.relative.as_posix().lower()
        spec = DownloadSpec(
            dest=planned.destination(plan.root),
            url=planned.urls[0],
            fallback_urls=tuple(planned.urls[1:]),
            sha1=planned.sha1,
            size=planned.size,
            label=planned.label or planned.relative.as_posix(),
        )

        def on_progress(absolute: int) -> None:
            nonlocal done_bytes
            with state_lock:
                previous = file_bytes.get(key, 0)
                done_bytes = max(0, done_bytes + absolute - previous)
                file_bytes[key] = absolute
            emit(current=planned.display)

        try:
            written = client.download_file(
                spec, progress=on_progress, cancel=cancel, pause=pause
            )
            return planned, written, None
        except CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - report per-file failures
            return planned, 0, str(exc)

    log(f"开始下载 {total_files} 个文件，共 {human_size(total_bytes)}。", "info")
    downloaded_bytes = 0
    try:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, workers)
        ) as executor:
            futures = {executor.submit(task, item): item for item in pending}
            try:
                for future in concurrent.futures.as_completed(futures):
                    if cancel is not None and cancel.is_set():
                        for pending_future in futures:
                            pending_future.cancel()
                        raise CancelledError("下载已取消。")
                    try:
                        planned, written, error = future.result()
                    except CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        planned = futures[future]
                        written, error = 0, str(exc)
                    with state_lock:
                        done_files += 1
                    if error:
                        errors.append(f"{planned.relative.as_posix()}：{error}")
                        log(f"失败：{planned.display} - {error}", "error")
                    else:
                        downloaded_bytes += written
                        if written:
                            downloaded_paths.append(planned.destination(plan.root))
                            log(f"完成：{planned.display}", "muted")
                    emit(force=True)
            finally:
                if cancel is not None and cancel.is_set():
                    for pending_future in futures:
                        pending_future.cancel()
    except CancelledError:
        _cleanup_cancelled_downloads(downloaded_paths, pending, plan.root)
        raise
    if cancel is not None and cancel.is_set():
        _cleanup_cancelled_downloads(downloaded_paths, pending, plan.root)
        raise CancelledError("下载已取消。")
    emit(force=True)
    return done_files, downloaded_bytes, errors


def install_plan(
    plan: InstallPlan,
    *,
    client: HttpClient,
    deep: bool = True,
    workers: int = 6,
    progress: ProgressCallback | None = None,
    log: LogCallback | None = None,
    cancel: threading.Event | None = None,
    pause: PauseToken | None = None,
) -> InstallOutcome:
    log = log or (lambda _message, _level="info": None)
    report = verify_plan(plan, deep=deep, progress=progress, cancel=cancel)
    if report.problematic:
        log(
            f"检查完成：{len(report.problematic)} 个文件需要补全"
            f"（缺失 {len(report.missing)}，损坏 {len(report.corrupt)}）。",
            "warning",
        )
    else:
        log("现有文件完整。", "success")

    pending = _pending_files(report)
    if pending:
        missing_urls = [item for item in pending if not item.urls]
        if missing_urls:
            raise DownloadError(
                "以下文件缺少下载地址，无法自动补全：\n"
                + "\n".join(f"  - {item.relative.as_posix()}" for item in missing_urls[:10])
            )
        required = sum(item.size or 0 for item in pending)
        try:
            free = shutil.disk_usage(plan.root.root).free
        except OSError:
            free = None
        if free is not None and required and free < required + 64 * 1024 * 1024:
            raise DownloadError(
                f"磁盘空间不足：需要约 {human_size(required)}，剩余 {human_size(free)}。"
            )
        downloaded_files, downloaded_bytes, errors = _download_tasks(
            plan,
            pending,
            client,
            workers=workers,
            progress=progress,
            log=log,
            cancel=cancel,
            pause=pause,
        )
    else:
        downloaded_files, downloaded_bytes, errors = 0, 0, []

    # The version JSON is kept when it is valid and belongs to this version:
    # launchers and users legitimately add fields (memory, JVM args).  A
    # missing or structurally broken JSON is (re)written so the version stays
    # launchable after an interrupted third-party modification.
    _ensure_version_json(plan, log)

    # A few loader libraries publish no SHA-1 in their profile.  Record the
    # hash we actually received so future checks can detect corruption.
    enriched: list[PlannedFile] = []
    for planned in plan.files:
        if planned.sha1 is None:
            path = planned.destination(plan.root)
            if path.is_file():
                try:
                    planned = PlannedFile(
                        relative=planned.relative,
                        urls=planned.urls,
                        sha1=sha1_file(path),
                        size=planned.size if planned.size is not None else path.stat().st_size,
                        category=planned.category,
                        label=planned.label,
                    )
                except OSError:
                    pass
        enriched.append(planned)
    plan.files = enriched

    materialized = materialize_assets(plan, logger=log)

    if errors:
        raise DownloadError(
            f"有 {len(errors)} 个文件下载失败：\n"
            + "\n".join(f"  - {item}" for item in errors[:12])
        )

    final = verify_plan(plan, deep=deep, progress=progress, cancel=cancel)
    outcome = InstallOutcome(
        plan=plan,
        verify=final,
        downloaded_files=downloaded_files,
        downloaded_bytes=downloaded_bytes,
        skipped_files=max(0, len(plan.files) - len(pending)),
        materialized_files=materialized,
        warnings=list(plan.warnings),
    )
    if final.healthy:
        write_sidecar(plan, final)
        log("安装完成，所有文件校验通过。", "success")
    else:
        log(
            f"安装后仍有 {len(final.problematic)} 个文件异常，请查看日志。",
            "error",
        )
    return outcome


def _ensure_version_json(plan: InstallPlan, log: LogCallback) -> str:
    """Write the version JSON when missing or structurally broken.

    Returns ``written``, ``repaired`` or ``kept``.  A valid JSON with the
    expected ``id`` is never replaced: other launchers may have added fields
    that must survive a repair.
    """
    path = plan.version_json_path
    if not path.is_file():
        _write_bytes_atomic(path, plan.version_json_payload)
        log(f"已写入版本 JSON：{plan.version_json_relative}", "muted")
        return "written"
    try:
        existing = path.read_bytes()
    except OSError as exc:
        log(f"无法读取现有版本 JSON，将重写：{exc}", "warning")
        _write_bytes_atomic(path, plan.version_json_payload)
        plan.warnings.append("版本 JSON 无法读取，已重写。")
        return "repaired"
    try:
        data = json.loads(existing)
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = None
    broken = not isinstance(data, dict) or not isinstance(data.get("id"), str)
    wrong_id = isinstance(data, dict) and not broken and data.get("id") != plan.version_id
    if broken or wrong_id:
        reason = "内容损坏" if broken else f"id 为 {data.get('id')!r}，与目标 {plan.version_id!r} 不一致"
        log(f"版本 JSON {reason}，已按下载源内容重写。", "warning")
        plan.warnings.append(f"版本 JSON 已重写（{reason}）。")
        _write_bytes_atomic(path, plan.version_json_payload)
        return "repaired"
    return "kept"


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def materialize_assets(
    plan: InstallPlan, *, logger: LogCallback | None = None
) -> int:
    """Create ``assets/virtual/<id>`` and ``resources/`` for old versions."""
    log = logger or (lambda _message, _level="info": None)
    asset_plan = plan.asset_index
    if asset_plan is None or not (asset_plan.virtual or asset_plan.map_to_resources):
        return 0
    objects_dir = plan.root.assets_dir / "objects"
    destinations: list[Path] = []
    if asset_plan.virtual:
        destinations.append(plan.root.assets_dir / "virtual" / asset_plan.id)
    if asset_plan.map_to_resources:
        destinations.append(plan.root.root / "resources")
    created = 0
    skipped = 0
    for name, asset_hash in asset_plan.objects.items():
        source = objects_dir / asset_hash[:2] / asset_hash
        if not source.is_file():
            continue
        try:
            relative_name = check_asset_name(name)
        except VersionError:
            skipped += 1
            continue
        for destination_root in destinations:
            target = destination_root / relative_name
            if target.is_file() and target.stat().st_size == source.stat().st_size:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + f".tmp{os.getpid()}")
            try:
                try:
                    os.link(source, temporary)
                except OSError:
                    shutil.copy2(source, temporary)
                os.replace(temporary, target)
                created += 1
            except OSError as exc:
                _unlink_quietly(temporary)
                skipped += 1
                if skipped <= 5:
                    log(f"无法写入虚拟资源 {name}：{exc}", "warning")
    if created:
        log(f"已整理 {created} 个旧版虚拟资源文件。", "muted")
    if skipped:
        log(f"有 {skipped} 个旧版资源文件被跳过（名称或权限问题）。", "warning")
    return created


def write_sidecar(plan: InstallPlan, report: VerifyReport) -> None:
    entries = []
    for status in report.statuses:
        planned = status.file
        entries.append(
            {
                "path": planned.relative.as_posix(),
                "sha1": planned.sha1,
                "size": planned.size,
                "category": planned.category,
            }
        )
    payload = {
        "schema": 1,
        "created": int(time.time()),
        "version_id": plan.version_id,
        "loader": plan.loader,
        "game_version": plan.game_version,
        "source": plan.source.id,
        "version_json_sha1": _bytes_sha1(plan.version_json_payload),
        "virtual": bool(plan.asset_index and plan.asset_index.virtual),
        "map_to_resources": bool(plan.asset_index and plan.asset_index.map_to_resources),
        "files": entries,
    }
    try:
        _write_bytes_atomic(
            plan.sidecar_path,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bytes_sha1(payload: bytes) -> str:
    import hashlib

    return hashlib.sha1(payload).hexdigest()


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def format_verify_report(report: VerifyReport) -> str:
    json_labels = {
        "ok": "正常",
        "modified": "已修改（保留本地文件）",
        "missing": "缺失",
        "unreadable": "无法读取",
    }
    lines = [
        f"检查文件：{report.total} 个",
        f"完整：{report.ok} 个",
        f"缺失：{len(report.missing)} 个",
        f"损坏：{len(report.corrupt)} 个",
        f"版本 JSON：{json_labels.get(report.version_json_status, report.version_json_status)}",
    ]
    if report.version_json_detail:
        lines.append(f"  {report.version_json_detail}")
    if report.problematic:
        lines.append("需要补全：")
        for status in report.problematic[:30]:
            lines.append(f"  - {status.file.relative.as_posix()}（{status.detail}）")
        if len(report.problematic) > 30:
            lines.append(f"  ... 另有 {len(report.problematic) - 30} 个")
    return "\n".join(lines)
