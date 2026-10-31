"""Fabric and Forge loader metadata and installation.

Fabric is installed by writing the loader profile JSON returned by
``meta.fabricmc.net`` and downloading its Maven libraries.  No downloaded code
is executed.

Forge needs its official installer because modern versions run binary
patch/processor steps.  The installer is downloaded over HTTPS from a
configured provider, its published SHA-1 is checked, and it is started with a
fixed argument list (``--installClient <game root>``), never through a shell.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from xml.etree import ElementTree

from .errors import LauncherError, VersionError
from .java import discover_java, pick_java, probe_java, required_java_major
from .manifest import (
    RemoteVersion,
    VersionDetail,
    fetch_bytes,
    fetch_version_detail,
    load_manifest,
    parse_time,
)
from .mirrors import DownloadSource, loader_allowed_hosts
from .net import (
    CancelledError,
    DownloadError,
    DownloadSpec,
    HttpClient,
    PauseToken,
    PROVIDER_HOSTS,
    human_size,
)
from .install import (
    InstallOutcome,
    InstallPlan,
    ProgressCallback,
    _add_file,
    _library_files,
    build_plan_from_detail,
    build_local_plan,
    build_vanilla_plan,
    install_plan,
    sort_planned_files,
    verify_plan,
)
from .models import GameRoot
from .paths import normalize_game_root
from .safety import normalise_sha1, safe_component, safe_relative_path
from .storage import ensure_dir, installer_cache_dir

LogCallback = Callable[[str, str], None]
_MAX_FABRIC_PROFILE_BYTES = 4 * 1024 * 1024
_MAX_FORGE_METADATA_BYTES = 16 * 1024 * 1024
_FORGE_CACHE_TTL = 600.0
_FORGE_CACHE: dict[str, tuple[float, "ForgeCatalog"]] = {}
_FORGE_CACHE_LOCK = threading.Lock()
_FABRIC_GAME_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_FABRIC_LOADER_CACHE: dict[tuple[str, str], tuple[float, list["LoaderBuild"]]] = {}
_QUILT_LOADER_CACHE: dict[tuple[str, str], tuple[float, list["LoaderBuild"]]] = {}
_NEOFORGE_CACHE: dict[tuple[str, str], tuple[float, list["LoaderBuild"]]] = {}
_QUILT_META = "https://meta.quiltmc.org"
_NEOFORGE_MAVEN = "https://maven.neoforged.net/releases"
_QUILT_PROFILE_MAX_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class LoaderBuild:
    loader: str
    game_version: str
    version: str
    stable: bool = False
    recommended: bool = False

    @property
    def version_id(self) -> str:
        if self.loader == "fabric":
            return f"fabric-loader-{self.version}-{self.game_version}"
        if self.loader == "quilt":
            return f"quilt-loader-{self.version}-{self.game_version}"
        if self.loader == "forge":
            return f"{self.game_version}-forge-{self.version}"
        if self.loader == "neoforge":
            if self.game_version == "1.20.1":
                return f"{self.game_version}-forge-{self.version}"
            return f"neoforge-{self.version}"
        return self.game_version

    @property
    def label(self) -> str:
        if self.loader == "fabric":
            channel = "稳定" if self.stable else "快照/实验"
            return f"Fabric {self.version}  [{channel}]"
        if self.loader == "quilt":
            channel = "稳定" if self.stable else "测试版"
            return f"Quilt {self.version}  [{channel}]"
        if self.loader == "neoforge":
            channel = "稳定" if self.stable else "测试版"
            return f"NeoForge {self.version}  [{channel}]"
        channel = "推荐" if self.recommended else "最新/实验"
        return f"Forge {self.version}  [{channel}]"


def _client(source: DownloadSource, logger: LogCallback | None) -> HttpClient:
    return HttpClient(
        loader_allowed_hosts(source),
        # BMCLAPI answers with CDN redirects even for loader metadata/libraries;
        # the redirect target is still required to be public HTTPS.
        strict_redirects=False,
        logger=lambda message: logger(message, "muted") if logger else None,
    )


def _fabric_http(
    source: DownloadSource,
    client: HttpClient | None,
    logger: LogCallback | None,
) -> HttpClient:
    """A loader metadata client that can reach both official Meta and BMCLAPI.

    Loader profiles deliberately fall back to BMCLAPI even when the user picked
    the official source.  Always build a permissive metadata client so a mirror
    URL is not rejected by a stricter source client; game files still use the
    caller's client and the selected source policy.
    """
    return HttpClient(
        PROVIDER_HOSTS,
        strict_redirects=False,
        timeout=10.0,
        retries=0,
        logger=lambda message: logger(message, "muted") if logger else None,
    )


# ---------------------------------------------------------------------------
# Fabric
# ---------------------------------------------------------------------------


def _fabric_urls(source: DownloadSource, path: str) -> tuple[str, ...]:
    official = f"https://meta.fabricmc.net/{path.lstrip('/')}"
    mirror = f"https://bmclapi2.bangbang93.com/fabric-meta/{path.lstrip('/')}"
    # Always keep both the selected source and the BMCLAPI mirror.  Fabric's
    # official meta endpoint is frequently unreachable from mainland China;
    # racing it against the mirror avoids a 45-second TLS timeout before the
    # user sees an error (or before the mirror response is used).
    candidates = [
        source.fabric_meta.rstrip("/") + "/" + path.lstrip("/"),
        official,
        mirror,
    ]
    result: list[str] = []
    for url in candidates:
        if url not in result:
            result.append(url)
    return tuple(result)


def fetch_fabric_games(
    source: DownloadSource,
    *,
    client: HttpClient | None = None,
    logger: LogCallback | None = None,
    force: bool = False,
) -> list[dict[str, Any]]:
    cache_key = source.id
    now = time.monotonic()
    if not force:
        cached = _FABRIC_GAME_CACHE.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]
    http = _fabric_http(source, client, logger)
    payload = fetch_bytes(
        http,
        _fabric_urls(source, "v2/versions/game"),
        max_size=8 * 1024 * 1024,
        logger=lambda message: logger(message, "muted") if logger else None,
    )
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VersionError(f"Fabric 游戏版本列表无效：{exc}") from exc
    if not isinstance(data, list):
        raise VersionError("Fabric 游戏版本列表格式错误。")
    games: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        version = item.get("version")
        if not isinstance(version, str) or not version:
            continue
        try:
            version = safe_component(version, what="Fabric 游戏版本", max_length=96)
        except VersionError:
            continue
        games.append({"version": version, "stable": bool(item.get("stable"))})
    _FABRIC_GAME_CACHE[cache_key] = (now + 600.0, games)
    return games


def fetch_fabric_loaders(
    source: DownloadSource,
    game_version: str,
    *,
    client: HttpClient | None = None,
    logger: LogCallback | None = None,
    force: bool = False,
) -> list[LoaderBuild]:
    cache_key = (source.id, game_version)
    now = time.monotonic()
    if not force:
        cached = _FABRIC_LOADER_CACHE.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]
    http = _fabric_http(source, client, logger)
    payload = fetch_bytes(
        http,
        _fabric_urls(source, f"v2/versions/loader/{urllib.parse.quote(game_version, safe='')}"),
        max_size=16 * 1024 * 1024,
        logger=lambda message: logger(message, "muted") if logger else None,
    )
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VersionError(f"Fabric Loader 版本列表无效：{exc}") from exc
    if not isinstance(data, list):
        raise VersionError("Fabric Loader 版本列表格式错误。")
    builds: list[LoaderBuild] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        loader = item.get("loader")
        if isinstance(loader, dict):
            version = loader.get("version")
            stable = bool(loader.get("stable"))
        else:
            version = item.get("version")
            stable = bool(item.get("stable"))
        if not isinstance(version, str) or not version:
            continue
        try:
            version = safe_component(version, what="Fabric Loader 版本", max_length=64)
        except VersionError:
            continue
        builds.append(
            LoaderBuild(
                loader="fabric",
                game_version=game_version,
                version=version,
                stable=stable,
                recommended=stable,
            )
        )
    builds.sort(key=lambda item: _version_key(item.version), reverse=True)
    _FABRIC_LOADER_CACHE[cache_key] = (now + 600.0, builds)
    return builds


def select_loader_builds(
    builds: Iterable[LoaderBuild],
    channel: str,
) -> list[LoaderBuild]:
    builds = list(builds)
    if channel in ("stable", "recommended"):
        stable = [item for item in builds if item.stable]
        return stable or builds[:1]
    if channel in ("latest",):
        return builds[:1]
    return builds


def fetch_fabric_profile(
    source: DownloadSource,
    game_version: str,
    loader_version: str,
    *,
    client: HttpClient | None = None,
    logger: LogCallback | None = None,
) -> VersionDetail:
    http = _fabric_http(source, client, logger)
    path = (
        f"v2/versions/loader/{urllib.parse.quote(game_version, safe='')}/"
        f"{urllib.parse.quote(loader_version, safe='')}/profile/json"
    )
    payload = fetch_bytes(
        http,
        _fabric_urls(source, path),
        max_size=_MAX_FABRIC_PROFILE_BYTES,
        logger=lambda message: logger(message, "muted") if logger else None,
    )
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VersionError(f"Fabric 安装配置无效：{exc}") from exc
    if not isinstance(data, dict):
        raise VersionError("Fabric 安装配置根节点不是对象。")
    profile_id = data.get("id")
    if not isinstance(profile_id, str) or not profile_id:
        raise VersionError("Fabric 安装配置缺少 id。")
    profile_id = safe_component(profile_id, what="Fabric 版本 ID", max_length=128)
    inherits = data.get("inheritsFrom")
    if inherits != game_version:
        raise VersionError(
            f"Fabric 安装配置针对 {inherits}，与所选游戏版本 {game_version} 不符。"
        )
    remote = RemoteVersion(
        id=profile_id,
        type="release",
        url=_fabric_urls(source, path)[0],
        sha1="",
        release_time=parse_time(data.get("releaseTime"))
        or datetime.now(timezone.utc),
    )
    return VersionDetail(remote=remote, data=data, raw=payload, sha1=_sha1_bytes(payload))


def build_fabric_plan(
    root: GameRoot,
    source: DownloadSource,
    game_detail: VersionDetail,
    build: LoaderBuild,
    *,
    client: HttpClient | None = None,
    logger: LogCallback | None = None,
) -> InstallPlan:
    http = client or _client(source, logger)
    profile = fetch_fabric_profile(
        source, build.game_version, build.version, client=http, logger=logger
    )
    return _build_profile_plan(
        root,
        source,
        game_detail,
        build,
        profile,
        loader="fabric",
        client=http,
        logger=logger,
    )


def _build_profile_plan(
    root: GameRoot,
    source: DownloadSource,
    game_detail: VersionDetail,
    build: LoaderBuild,
    profile: VersionDetail,
    *,
    loader: str,
    client: HttpClient | None = None,
    logger: LogCallback | None = None,
) -> InstallPlan:
    """Build the install plan for a loader that is defined by a profile JSON.

    Fabric and Quilt both publish a profile that inherits the vanilla version
    and lists Maven libraries, so the same plan builder is used for both.
    """
    http = client or _client(source, logger)
    base_plan = build_plan_from_detail(root, source, game_detail, client=http, logger=logger)
    warnings = list(base_plan.warnings)
    files = {item.relative.as_posix().lower(): item for item in base_plan.files}
    for planned in _library_files(profile.data, source, logger, warnings):
        _add_file(files, planned)
    profile_id = profile.remote.id
    relative = Path("versions") / profile_id / f"{profile_id}.json"
    return InstallPlan(
        root=root,
        source=source,
        version_id=profile_id,
        version_type="release",
        loader=loader,
        game_version=build.game_version,
        version_json_relative=relative,
        version_json_payload=profile.raw,
        version_json_sha1=profile.sha1,
        files=sort_planned_files(files.values()),
        asset_index=base_plan.asset_index,
        warnings=warnings,
        prerequisites=[game_detail],
    )


# ---------------------------------------------------------------------------
# Quilt
# ---------------------------------------------------------------------------


def _quilt_urls(path: str) -> tuple[str, ...]:
    """Quilt Meta has no BMCLAPI mirror; only the official endpoint is used."""
    return (f"{_QUILT_META}/{path.lstrip('/')}",)


def fetch_quilt_loaders(
    source: DownloadSource,
    game_version: str,
    *,
    client: HttpClient | None = None,
    logger: LogCallback | None = None,
    force: bool = False,
) -> list[LoaderBuild]:
    cache_key = (source.id, game_version)
    now = time.monotonic()
    if not force:
        cached = _QUILT_LOADER_CACHE.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]
    http = _fabric_http(source, client, logger)
    payload = fetch_bytes(
        http,
        _quilt_urls(
            f"v3/versions/loader/{urllib.parse.quote(game_version, safe='')}"
        ),
        max_size=16 * 1024 * 1024,
        logger=lambda message: logger(message, "muted") if logger else None,
    )
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VersionError(f"Quilt Loader 版本列表无效：{exc}") from exc
    if not isinstance(data, list):
        raise VersionError("Quilt Loader 版本列表格式错误。")
    raw_versions: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        loader = item.get("loader")
        version = loader.get("version") if isinstance(loader, dict) else item.get("version")
        if not isinstance(version, str) or not version:
            continue
        try:
            raw_versions.append(
                safe_component(version, what="Quilt Loader 版本", max_length=64)
            )
        except VersionError:
            continue
    raw_versions = sorted(set(raw_versions), key=_version_key, reverse=True)
    recommended = next((item for item in raw_versions if "-beta" not in item.lower()), "")
    builds = [
        LoaderBuild(
            loader="quilt",
            game_version=game_version,
            version=version,
            stable=version == recommended,
            recommended=version == recommended,
        )
        for version in raw_versions
    ]
    _QUILT_LOADER_CACHE[cache_key] = (now + 600.0, builds)
    return builds


def fetch_quilt_profile(
    source: DownloadSource,
    game_version: str,
    loader_version: str,
    *,
    client: HttpClient | None = None,
    logger: LogCallback | None = None,
) -> VersionDetail:
    http = _fabric_http(source, client, logger)
    path = (
        f"v3/versions/loader/{urllib.parse.quote(game_version, safe='')}/"
        f"{urllib.parse.quote(loader_version, safe='')}/profile/json"
    )
    payload = fetch_bytes(
        http,
        _quilt_urls(path),
        max_size=_QUILT_PROFILE_MAX_BYTES,
        logger=lambda message: logger(message, "muted") if logger else None,
    )
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VersionError(f"Quilt 安装配置无效：{exc}") from exc
    if not isinstance(data, dict):
        raise VersionError("Quilt 安装配置根节点不是对象。")
    profile_id = data.get("id")
    if not isinstance(profile_id, str) or not profile_id:
        raise VersionError("Quilt 安装配置缺少 id。")
    profile_id = safe_component(profile_id, what="Quilt 版本 ID", max_length=128)
    inherits = data.get("inheritsFrom")
    if inherits != game_version:
        raise VersionError(
            f"Quilt 安装配置针对 {inherits}，与所选游戏版本 {game_version} 不符。"
        )
    remote = RemoteVersion(
        id=profile_id,
        type="release",
        url=_quilt_urls(path)[0],
        sha1="",
        release_time=parse_time(data.get("releaseTime"))
        or datetime.now(timezone.utc),
    )
    return VersionDetail(remote=remote, data=data, raw=payload, sha1=_sha1_bytes(payload))


def build_quilt_plan(
    root: GameRoot,
    source: DownloadSource,
    game_detail: VersionDetail,
    build: LoaderBuild,
    *,
    client: HttpClient | None = None,
    logger: LogCallback | None = None,
) -> InstallPlan:
    http = client or _client(source, logger)
    profile = fetch_quilt_profile(
        source, build.game_version, build.version, client=http, logger=logger
    )
    return _build_profile_plan(
        root,
        source,
        game_detail,
        build,
        profile,
        loader="quilt",
        client=http,
        logger=logger,
    )


def install_quilt(
    root: GameRoot,
    source: DownloadSource,
    game_detail: VersionDetail,
    build: LoaderBuild,
    *,
    client: HttpClient,
    logger: LogCallback | None = None,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
    pause: PauseToken | None = None,
    deep: bool = True,
    workers: int = 6,
) -> InstallOutcome:
    log = logger or (lambda _message, _level="info": None)
    log(f"准备安装 Quilt Loader {build.version}（Minecraft {build.game_version}）。", "info")
    base_plan = build_plan_from_detail(root, source, game_detail, client=client, logger=logger)
    install_plan(
        base_plan,
        client=client,
        deep=False,
        workers=workers,
        progress=progress,
        log=logger,
        cancel=cancel,
        pause=pause,
    )
    quilt_plan = build_quilt_plan(
        root, source, game_detail, build, client=client, logger=logger
    )
    return install_plan(
        quilt_plan,
        client=client,
        deep=deep,
        workers=workers,
        progress=progress,
        log=logger,
        cancel=cancel,
        pause=pause,
    )


# ---------------------------------------------------------------------------
# Forge
# ---------------------------------------------------------------------------


@dataclass
class ForgeCatalog:
    versions: dict[str, list[str]] = field(default_factory=dict)
    promotions: dict[str, str] = field(default_factory=dict)
    fetched_at: float = 0.0


def _version_key(value: str) -> tuple[Any, ...]:
    parts: list[Any] = []
    for piece in re.split(r"[.\-+]", value):
        if piece.isdigit():
            parts.append((0, int(piece)))
        else:
            parts.append((1, piece.lower()))
    return tuple(parts)


def load_forge_catalog(
    source: DownloadSource,
    *,
    client: HttpClient | None = None,
    logger: LogCallback | None = None,
    force: bool = False,
) -> ForgeCatalog:
    now = time.monotonic()
    if not force:
        with _FORGE_CACHE_LOCK:
            cached = _FORGE_CACHE.get(source.id)
        if cached and cached[0] > now:
            return cached[1]

    http = client or _client(source, logger)
    official_xml = (
        "https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml"
    )
    xml_urls = tuple(
        dict.fromkeys(
            (
                source.forge_maven.rstrip("/")
                + "/net/minecraftforge/forge/maven-metadata.xml",
                official_xml,
            )
        )
    )
    payload = fetch_bytes(
        http,
        xml_urls,
        max_size=_MAX_FORGE_METADATA_BYTES,
        logger=lambda message: logger(message, "muted") if logger else None,
    )
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise VersionError(f"Forge 版本元数据无效：{exc}") from exc
    versions: dict[str, list[str]] = {}
    for node in root.findall(".//version"):
        text = (node.text or "").strip()
        if not text or "-" not in text:
            continue
        game, _, forge = text.rpartition("-")
        if not game or not forge:
            continue
        versions.setdefault(game, []).append(forge)
    for builds in versions.values():
        builds.sort(key=_version_key, reverse=True)

    promotions: dict[str, str] = {}
    try:
        promo_payload = fetch_bytes(
            http,
            (source.forge_promotions,),
            max_size=2 * 1024 * 1024,
            logger=lambda message: logger(message, "muted") if logger else None,
        )
        promo_data = json.loads(promo_payload)
        raw_promos = promo_data.get("promos") if isinstance(promo_data, dict) else None
        if isinstance(raw_promos, dict):
            promotions = {
                str(key): str(value)
                for key, value in raw_promos.items()
                if isinstance(key, str) and isinstance(value, str)
            }
    except (LauncherError, OSError, ValueError, json.JSONDecodeError) as exc:
        if logger:
            logger(f"无法读取 Forge 推荐版本：{exc}", "warning")

    catalog = ForgeCatalog(versions=versions, promotions=promotions, fetched_at=time.time())
    with _FORGE_CACHE_LOCK:
        _FORGE_CACHE[source.id] = (now + _FORGE_CACHE_TTL, catalog)
    return catalog


def forge_builds(
    catalog: ForgeCatalog,
    game_version: str,
    channel: str,
) -> list[LoaderBuild]:
    builds = list(catalog.versions.get(game_version, []))
    if not builds:
        return []
    latest = catalog.promotions.get(f"{game_version}-latest")
    recommended = catalog.promotions.get(f"{game_version}-recommended")
    if latest not in builds:
        latest = builds[0]
    if recommended not in builds:
        recommended = latest
    selected: list[str]
    if channel in ("stable", "recommended"):
        selected = [recommended]
    elif channel == "latest":
        selected = [latest]
    else:
        selected = builds
    return [
        LoaderBuild(
            loader="forge",
            game_version=game_version,
            version=build,
            stable=build == recommended,
            recommended=build == recommended,
        )
        for build in selected
    ]


def forge_installer_official_url(game_version: str, forge_version: str) -> str:
    coordinate = f"{game_version}-{forge_version}"
    return (
        "https://maven.minecraftforge.net/net/minecraftforge/forge/"
        f"{coordinate}/forge-{coordinate}-installer.jar"
    )


# ---------------------------------------------------------------------------
# NeoForge
# ---------------------------------------------------------------------------

# NeoForge 1.20.1 still publishes the legacy ``net.neoforged:forge`` artifact;
# 1.20.2 and newer use ``net.neoforged:neoforge`` with a Minecraft-derived
# version prefix (1.21.1 -> 21.1.x, 26.3 -> 26.3.0.x).
_NEOFORGE_LEGACY_GAME = "1.20.1"


def neoforge_artifact(game_version: str) -> str:
    return "forge" if game_version == _NEOFORGE_LEGACY_GAME else "neoforge"


def neoforge_version_prefixes(game_version: str) -> tuple[str, ...]:
    """Accepted NeoForge version prefixes for a Minecraft version."""
    if game_version == _NEOFORGE_LEGACY_GAME:
        return ()
    if game_version.startswith("1."):
        rest = game_version[2:]
        if rest.count(".") == 0:
            return (f"{rest}.0.",)
        return (f"{rest}.",)
    parts = game_version.split(".")
    if len(parts) == 2:
        return (f"{game_version}.0.",)
    return (f"{game_version}.",)


def _neoforge_version_matches(version: str, prefixes: tuple[str, ...]) -> bool:
    for prefix in prefixes:
        if not version.startswith(prefix):
            continue
        remainder = version[len(prefix) :]
        if remainder and remainder.split("-", 1)[0].isdigit():
            return True
    return False


def fetch_neoforge_builds(
    source: DownloadSource,
    game_version: str,
    *,
    client: HttpClient | None = None,
    logger: LogCallback | None = None,
    force: bool = False,
) -> list[LoaderBuild]:
    """List NeoForge builds for one Minecraft version.

    Uses the BMCLAPI Maven mirror first because ``maven.neoforged.net`` is
    frequently unreachable (and can be DNS-poisoned) outside its home network.
    """
    cache_key = (source.id, game_version)
    now = time.monotonic()
    if not force:
        cached = _NEOFORGE_CACHE.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]
    http = _fabric_http(source, client, logger)
    artifact = neoforge_artifact(game_version)
    official = (
        f"{_NEOFORGE_MAVEN}/net/neoforged/{artifact}/maven-metadata.xml"
    )
    from .install import _fabric_library_urls

    payload = fetch_bytes(
        http,
        _fabric_library_urls(source, official),
        max_size=_MAX_FORGE_METADATA_BYTES,
        logger=lambda message: logger(message, "muted") if logger else None,
    )
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise VersionError(f"NeoForge 版本元数据无效：{exc}") from exc
    raw_versions = [
        (node.text or "").strip()
        for node in root.findall(".//version")
        if (node.text or "").strip()
    ]
    if artifact == "forge":
        prefix = f"{game_version}-"
        builds = sorted(
            (
                version[len(prefix) :]
                for version in raw_versions
                if version.startswith(prefix)
            ),
            key=_version_key,
            reverse=True,
        )
    else:
        prefixes = neoforge_version_prefixes(game_version)
        builds = sorted(
            (
                version
                for version in raw_versions
                if _neoforge_version_matches(version, prefixes)
                and len(version) <= 64
            ),
            key=_version_key,
            reverse=True,
        )
    stable = next((item for item in builds if "-beta" not in item.lower()), "")
    result = [
        LoaderBuild(
            loader="neoforge",
            game_version=game_version,
            version=version,
            stable=version == stable,
            recommended=version == stable,
        )
        for version in builds
    ]
    _NEOFORGE_CACHE[cache_key] = (now + 600.0, result)
    return result


def neoforge_installer_official_url(game_version: str, neoforge_version: str) -> str:
    if neoforge_artifact(game_version) == "forge":
        coordinate = f"{game_version}-{neoforge_version}"
        return (
            f"{_NEOFORGE_MAVEN}/net/neoforged/forge/{coordinate}/"
            f"forge-{coordinate}-installer.jar"
        )
    return (
        f"{_NEOFORGE_MAVEN}/net/neoforged/neoforge/{neoforge_version}/"
        f"neoforge-{neoforge_version}-installer.jar"
    )


def _forge_sha1(
    source: DownloadSource,
    official_url: str,
    client: HttpClient,
    logger: LogCallback | None,
) -> str:
    sha_urls = tuple(url + ".sha1" for url in source.urls_for(official_url))
    payload = fetch_bytes(
        client,
        sha_urls,
        max_size=2048,
        logger=lambda message: logger(message, "muted") if logger else None,
    )
    match = re.search(rb"([0-9a-fA-F]{40})", payload)
    if not match:
        raise VersionError("Forge 安装器的 SHA-1 文件格式错误。")
    return normalise_sha1(match.group(1).decode("ascii"))


def _read_forge_profile_id(installer: Path, label: str = "Forge") -> tuple[str, str]:
    try:
        with zipfile.ZipFile(installer) as archive:
            info = archive.getinfo("install_profile.json")
            if info.file_size > 8 * 1024 * 1024:
                raise VersionError(f"{label} install_profile.json 过大。")
            data = json.loads(archive.read(info))
    except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise VersionError(f"{label} 安装器损坏：{exc}") from exc
    if not isinstance(data, dict):
        raise VersionError(f"{label} install_profile.json 格式错误。")
    version_id = data.get("version") or data.get("path")
    minecraft = data.get("minecraft")
    if not isinstance(version_id, str) or not version_id:
        # Very old installers store the profile under "install".
        install = data.get("install")
        if isinstance(install, dict):
            version_id = install.get("version") or install.get("path")
    if not isinstance(version_id, str) or not version_id:
        raise VersionError(f"{label} 安装器缺少版本 ID。")
    version_id = safe_component(version_id, what=f"{label} 版本 ID", max_length=128)
    if not isinstance(minecraft, str) or not minecraft:
        raise VersionError(f"{label} 安装器缺少 Minecraft 版本。")
    return version_id, minecraft


def _read_zip_json(installer: Path, name: str, max_size: int = 8 * 1024 * 1024) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(installer) as archive:
            info = archive.getinfo(name)
            if info.file_size > max_size:
                raise VersionError(f"Forge 安装器中的 {name} 过大。")
            data = json.loads(archive.read(info))
    except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise VersionError(f"读取 Forge 安装器 {name} 失败：{exc}") from exc
    if not isinstance(data, dict):
        raise VersionError(f"Forge 安装器 {name} 格式错误。")
    return data


def prefetch_forge_libraries(
    root: GameRoot,
    source: DownloadSource,
    installer: Path,
    *,
    client: HttpClient,
    logger: LogCallback | None = None,
    workers: int = 6,
    cancel: threading.Event | None = None,
    pause: PauseToken | None = None,
    fallback_maven: str = "https://maven.minecraftforge.net",
    url_resolver: Callable[[str], tuple[str, ...]] | None = None,
    label: str = "Forge",
) -> tuple[int, int, list[str]]:
    """Download Forge/NeoForge installer libraries through the selected provider.

    The installers hard-code their official Maven host and have no provider
    setting.  In mainland China the Forge host is often unreachable and the
    NeoForge host can even be DNS-poisoned, while BMCLAPI mirrors both trees.
    Pre-populating ``libraries/`` lets the installer run without those network
    requests (it validates the existing files' checksums and skips them).
    """
    log = logger or (lambda _message, _level="info": None)
    profile = _read_zip_json(installer, "install_profile.json")
    version_data = _read_zip_json(installer, "version.json")

    def resolve(url: str) -> tuple[str, ...]:
        if url_resolver is not None:
            return url_resolver(url)
        return source.urls_for(url)

    specs: dict[str, DownloadSpec] = {}

    def add_artifact(artifact: Any, label: str, official_fallback_base: str) -> None:
        if not isinstance(artifact, dict):
            return
        path = artifact.get("path")
        if not isinstance(path, str) or not path:
            return
        try:
            relative = safe_relative_path(path, what=f"{label} 库路径")
        except VersionError:
            return
        key = relative.as_posix().lower()
        if key in specs:
            return
        official = artifact.get("url")
        if not isinstance(official, str) or not official:
            official = official_fallback_base.rstrip("/") + "/" + relative.as_posix()
        sha1 = artifact.get("sha1")
        try:
            sha1 = normalise_sha1(sha1) if sha1 else None
        except VersionError:
            sha1 = None
        size = artifact.get("size")
        urls = resolve(official)
        specs[key] = DownloadSpec(
            dest=root.libraries_dir / relative,
            url=urls[0],
            fallback_urls=tuple(urls[1:]),
            sha1=sha1,
            size=size if isinstance(size, int) and size >= 0 else None,
            label=label,
        )

    for library in profile.get("libraries") or []:
        if not isinstance(library, dict):
            continue
        downloads = library.get("downloads")
        artifact = downloads.get("artifact") if isinstance(downloads, dict) else None
        add_artifact(
            artifact,
            str(library.get("name") or f"{label} 库"),
            fallback_maven,
        )
    for library in version_data.get("libraries") or []:
        if not isinstance(library, dict):
            continue
        downloads = library.get("downloads")
        artifact = downloads.get("artifact") if isinstance(downloads, dict) else None
        add_artifact(
            artifact,
            str(library.get("name") or f"{label} 库"),
            fallback_maven,
        )

    coordinates: set[str] = set()
    for processor in profile.get("processors") or []:
        if not isinstance(processor, dict):
            continue
        jar = processor.get("jar")
        if isinstance(jar, str):
            coordinates.add(jar)
        for item in processor.get("classpath") or []:
            if isinstance(item, str):
                coordinates.add(item)
    for coordinate in sorted(coordinates):
        try:
            from .version import maven_path

            relative = safe_relative_path(
                maven_path(coordinate).as_posix(), what=f"{label} 处理器路径"
            )
        except VersionError:
            continue
        key = relative.as_posix().lower()
        if key in specs or (root.libraries_dir / relative).is_file():
            continue
        official = fallback_maven.rstrip("/") + "/" + relative.as_posix()
        urls = resolve(official)
        sha1 = None
        try:
            checksum_payload = fetch_bytes(
                client,
                tuple(url + ".sha1" for url in urls),
                max_size=2048,
                logger=lambda message: logger(message, "muted") if logger else None,
            )
            import re as _re

            match = _re.search(rb"([0-9a-fA-F]{40})", checksum_payload)
            if match:
                sha1 = normalise_sha1(match.group(1).decode("ascii"))
        except LauncherError:
            pass
        specs[key] = DownloadSpec(
            dest=root.libraries_dir / relative,
            url=urls[0],
            fallback_urls=tuple(urls[1:]),
            sha1=sha1,
            size=None,
            label=coordinate,
        )

    pending = list(specs.values())
    if not pending:
        log(f"{label} 安装器没有需要预取的依赖。", "muted")
        return 0, 0, []
    log(f"检查/预取 {label} 安装器依赖：{len(pending)} 个文件。", "info")
    downloaded_bytes = 0
    lock = threading.Lock()

    def task(spec: DownloadSpec) -> tuple[DownloadSpec, int, str | None]:
        try:
            return spec, client.download_file(spec, cancel=cancel, pause=pause), None
        except CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return spec, 0, str(exc)

    def run_batch(
        batch: list[DownloadSpec],
    ) -> dict[str, tuple[DownloadSpec, str]]:
        nonlocal downloaded_bytes
        failures: dict[str, tuple[DownloadSpec, str]] = {}
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, workers)
        ) as executor:
            future_map = {
                executor.submit(task, spec): spec for spec in batch
            }
            for future in concurrent.futures.as_completed(future_map):
                if cancel is not None and cancel.is_set():
                    for item in future_map:
                        item.cancel()
                    raise CancelledError(f"{label} 预下载已取消。")
                fallback_spec = future_map[future]
                try:
                    spec, written, error = future.result()
                except CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    key = fallback_spec.dest.as_posix().lower()
                    failures[key] = (fallback_spec, str(exc))
                    continue
                if error:
                    key = spec.dest.as_posix().lower()
                    failures[key] = (spec, f"{spec.label}：{error}")
                    log(f"{label} 依赖下载失败：{spec.label}", "warning")
                else:
                    with lock:
                        downloaded_bytes += written
        return failures

    failures = run_batch(pending)
    if failures:
        # The mirror occasionally times out on a single small artifact.  The
        # installer cannot fall back to BMCLAPI itself, so retry once here.
        retry = [item[0] for item in failures.values()]
        log(f"重试 {len(retry)} 个未下载成功的 {label} 依赖…", "info")
        failures = run_batch(retry)
    return (
        len(pending) - len(failures),
        downloaded_bytes,
        [item[1] for item in failures.values()],
    )


def _ensure_launcher_profiles(root: GameRoot) -> None:
    path = root.root / "launcher_profiles.json"
    if path.exists():
        try:
            with path.open("r", encoding="utf-8-sig") as file:
                data = json.load(file)
            if isinstance(data, dict):
                return
        except (OSError, json.JSONDecodeError):
            pass
        # Never silently destroy a user file.  A malformed file is usually
        # recoverable, but the installer itself will also fail without it.
        backup = path.with_name("launcher_profiles.json.bak")
        try:
            if not backup.exists():
                path.replace(backup)
        except OSError:
            pass
    payload = {"profiles": {}, "settings": {}, "version": 3}
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _terminate_tree(process: subprocess.Popen[str]) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            process.terminate()
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
        except OSError:
            pass


def _installer_sha1_optional(
    urls: tuple[str, ...],
    client: HttpClient,
    logger: LogCallback | None,
    label: str,
) -> str | None:
    """Best-effort published installer hash.

    NeoForge's Maven mirror does not publish ``.sha1`` sidecars and the official
    host may be unreachable, so the caller falls back to structural validation
    (ZIP + install_profile.json + version.json) when this returns ``None``.
    """
    log = logger or (lambda _message, _level="info": None)
    try:
        payload = fetch_bytes(
            client,
            tuple(url + ".sha1" for url in urls),
            max_size=2048,
            logger=lambda message: logger(message, "muted") if logger else None,
        )
    except LauncherError as exc:
        log(f"{label} 安装器没有可用的官方 SHA-1：{exc}", "warning")
        return None
    match = re.search(rb"([0-9a-fA-F]{40})", payload)
    if not match:
        log(f"{label} 安装器的 SHA-1 文件格式异常，改用结构校验。", "warning")
        return None
    return normalise_sha1(match.group(1).decode("ascii"))


def install_forge(
    root: GameRoot,
    source: DownloadSource,
    game_detail: VersionDetail,
    build: LoaderBuild,
    *,
    java_path: Path,
    client: HttpClient,
    logger: LogCallback | None = None,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
    pause: PauseToken | None = None,
    deep: bool = True,
    workers: int = 6,
) -> InstallOutcome:
    game_version = build.game_version
    official_installer = forge_installer_official_url(game_version, build.version)
    return _install_with_installer(
        root,
        source,
        game_detail,
        build,
        java_path=java_path,
        client=client,
        logger=logger,
        progress=progress,
        cancel=cancel,
        pause=pause,
        deep=deep,
        workers=workers,
        loader="forge",
        label="Forge",
        official_installer=official_installer,
        installer_name=f"forge-{game_version}-{build.version}-installer.jar",
        require_sha1=True,
        fallback_maven="https://maven.minecraftforge.net",
        url_resolver=None,
    )


def install_neoforge(
    root: GameRoot,
    source: DownloadSource,
    game_detail: VersionDetail,
    build: LoaderBuild,
    *,
    java_path: Path,
    client: HttpClient,
    logger: LogCallback | None = None,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
    pause: PauseToken | None = None,
    deep: bool = True,
    workers: int = 6,
) -> InstallOutcome:
    game_version = build.game_version
    official_installer = neoforge_installer_official_url(game_version, build.version)
    from .install import _fabric_library_urls

    return _install_with_installer(
        root,
        source,
        game_detail,
        build,
        java_path=java_path,
        client=client,
        logger=logger,
        progress=progress,
        cancel=cancel,
        pause=pause,
        deep=deep,
        workers=workers,
        loader="neoforge",
        label="NeoForge",
        official_installer=official_installer,
        installer_name=(
            f"neoforge-{build.version}-installer.jar"
            if neoforge_artifact(game_version) == "neoforge"
            else f"forge-{game_version}-{build.version}-installer.jar"
        ),
        require_sha1=False,
        fallback_maven=_NEOFORGE_MAVEN,
        url_resolver=lambda url: _fabric_library_urls(source, url),
    )


def _install_with_installer(
    root: GameRoot,
    source: DownloadSource,
    game_detail: VersionDetail,
    build: LoaderBuild,
    *,
    java_path: Path,
    client: HttpClient,
    logger: LogCallback | None,
    progress: ProgressCallback | None,
    cancel: threading.Event | None,
    pause: PauseToken | None,
    deep: bool,
    workers: int,
    loader: str,
    label: str,
    official_installer: str,
    installer_name: str,
    require_sha1: bool,
    fallback_maven: str,
    url_resolver: Callable[[str], tuple[str, ...]] | None,
) -> InstallOutcome:
    """Download and run a Forge-style installer, then verify the result.

    Forge and NeoForge share the same installer format and the same
    ``--installClient`` command line; only the Maven host, artifact names and
    publisher checksum policy differ.
    """
    log = logger or (lambda _message, _level="info": None)
    game_version = build.game_version
    resolve = url_resolver or source.urls_for
    installer_urls = resolve(official_installer)
    log(f"准备安装 {label} {build.version}（Minecraft {game_version}）。", "info")

    # 1. Vanilla base first.  The installer needs a valid client installation.
    base_plan = build_vanilla_plan(
        root, source, game_detail.remote, client=client, logger=logger
    )
    install_plan(
        base_plan,
        client=client,
        deep=False,
        workers=workers,
        progress=progress,
        log=logger,
        cancel=cancel,
        pause=pause,
    )

    # 2. Download and validate the installer before executing it.
    if require_sha1:
        sha1 = _forge_sha1(source, official_installer, client, logger)
    else:
        sha1 = _installer_sha1_optional(
            installer_urls[:1], client, logger, label
        )
    installer_path = ensure_dir(installer_cache_dir()) / installer_name
    spec = DownloadSpec(
        dest=installer_path,
        url=installer_urls[0],
        fallback_urls=tuple(installer_urls[1:]),
        sha1=sha1,
        size=None,
        label=f"{label} {build.version} 安装器",
    )
    client.download_file(spec, cancel=cancel, pause=pause)
    if sha1:
        log(f"{label} 安装器 SHA-1 校验通过：{sha1}", "success")

    version_id, installer_game = _read_forge_profile_id(installer_path, label)
    if installer_game != game_version:
        raise VersionError(
            f"{label} 安装器针对 {installer_game}，与所选 {game_version} 不符。"
        )
    version_data = _read_zip_json(installer_path, "version.json")
    version_json_id = version_data.get("id")
    if isinstance(version_json_id, str) and version_json_id:
        try:
            checked_id = safe_component(
                version_json_id, what=f"{label} 版本 ID", max_length=128
            )
        except VersionError as exc:
            raise VersionError(str(exc)) from exc
        if checked_id != version_id:
            raise VersionError(
                f"{label} 安装器内部版本不一致：{version_id} / {checked_id}"
            )
    if not sha1:
        log(
            f"{label} 安装器未取得官方 SHA-1；已通过 HTTPS、ZIP 结构与 "
            "install_profile/version JSON 一致性校验。",
            "warning",
        )

    # 3. Pick a Java version suited to the target game, not just the UI choice.
    required = required_java_major(game_detail.data)
    runtime = pick_java(discover_java(root.root), required, root.root)
    installer_java = runtime.path if runtime is not None else Path(java_path)
    if not installer_java.is_file():
        raise LauncherError(f"找不到用于运行 {label} 安装器的 Java：{installer_java}")
    probed = probe_java(installer_java)
    if probed is None:
        raise LauncherError(f"无法运行 Java：{installer_java}")
    if required is not None and probed.major != required:
        log(
            f"警告：{label} 安装器将使用 Java {probed.major}，"
            f"目标版本建议 Java {required}。",
            "warning",
        )

    prefetched, prefetched_bytes, prefetch_errors = prefetch_forge_libraries(
        root,
        source,
        installer_path,
        client=client,
        logger=logger,
        workers=workers,
        cancel=cancel,
        pause=pause,
        fallback_maven=fallback_maven,
        url_resolver=url_resolver,
        label=label,
    )
    if prefetched:
        log(
            f"已通过下载源预取 {prefetched} 个 {label} 依赖"
            f"（{human_size(prefetched_bytes)}）。",
            "success",
        )
    if prefetch_errors:
        log(
            f"有 {len(prefetch_errors)} 个 {label} 依赖未能预取，"
            "安装器可能会尝试官方源。",
            "warning",
        )

    _ensure_launcher_profiles(root)
    command = [
        str(installer_java),
        "-jar",
        str(installer_path),
        "--installClient",
        str(root.root),
    ]
    log(
        f"运行 {label} 官方安装器（会执行安装器内的官方代码）：\n  "
        + " ".join(command),
        "command",
    )
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        cwd=str(root.root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creationflags,
    )
    last_lines: list[str] = []
    started = time.monotonic()
    timed_out = False

    def reader() -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            text = line.rstrip("\r\n")
            if not text:
                continue
            last_lines.append(text)
            if len(last_lines) > 200:
                del last_lines[:100]
            lowered = text.lower()
            is_error = (
                lowered.startswith(("error", "failed", "exception"))
                or " failed" in lowered
                or "exception:" in lowered
            )
            log(text, "error" if is_error else "game")

    reader_thread = threading.Thread(
        target=reader, daemon=True, name=f"{loader}-installer"
    )
    reader_thread.start()
    try:
        while process.poll() is None:
            if cancel is not None and cancel.is_set():
                _terminate_tree(process)
                raise CancelledError(f"{label} 安装已取消。")
            if time.monotonic() - started > 1800:
                timed_out = True
                _terminate_tree(process)
                break
            time.sleep(0.2)
    finally:
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            _terminate_tree(process)
            process.wait(timeout=10)
        reader_thread.join(timeout=5)

    if timed_out:
        raise LauncherError(f"{label} 安装器运行超过 30 分钟，已终止。")
    if process.returncode != 0:
        tail = "\n".join(last_lines[-12:])
        raise LauncherError(
            f"{label} 安装器退出代码 {process.returncode}。\n{tail}"
        )

    version_json = root.versions_dir / version_id / f"{version_id}.json"
    if not version_json.is_file():
        raise LauncherError(
            f"{label} 安装器未生成版本 JSON：{version_json}\n"
            f"请查看安装器日志，或确认 {label} 版本与 Minecraft 版本匹配。"
        )
    log(f"{label} 安装完成，版本 ID：{version_id}", "success")

    # 4. Verify the resulting installation and repair anything the installer
    #    skipped (some versions leave optional libraries out).
    local_plan = build_local_plan(root, version_id, source, logger=logger)
    local_plan.loader = loader
    local_plan.game_version = game_version
    outcome = install_plan(
        local_plan,
        client=client,
        deep=deep,
        workers=workers,
        progress=progress,
        log=logger,
        cancel=cancel,
    )
    outcome.warnings.append(f"{label} 安装器：{installer_path}")
    return outcome


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _sha1_bytes(payload: bytes) -> str:
    import hashlib

    return hashlib.sha1(payload).hexdigest()


def selected_game_detail(
    source: DownloadSource,
    game_version: str,
    *,
    client: HttpClient | None = None,
    logger: LogCallback | None = None,
) -> VersionDetail:
    manifest = load_manifest(source, client=client, logger=lambda message: logger(message, "muted") if logger else None)
    remote = manifest.find(game_version)
    if remote is None:
        raise VersionError(f"版本清单中没有 {game_version}。")
    http = client or _client(source, logger)
    return fetch_version_detail(source, remote, client=http, logger=lambda message: logger(message, "muted") if logger else None)


def install_fabric(
    root: GameRoot,
    source: DownloadSource,
    game_detail: VersionDetail,
    build: LoaderBuild,
    *,
    client: HttpClient,
    logger: LogCallback | None = None,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
    pause: PauseToken | None = None,
    deep: bool = True,
    workers: int = 6,
) -> InstallOutcome:
    log = logger or (lambda _message, _level="info": None)
    log(f"准备安装 Fabric Loader {build.version}（Minecraft {build.game_version}）。", "info")
    base_plan = build_plan_from_detail(
        root, source, game_detail, client=client, logger=logger
    )
    install_plan(
        base_plan,
        client=client,
        deep=False,
        workers=workers,
        progress=progress,
        log=logger,
        cancel=cancel,
        pause=pause,
    )
    fabric_plan = build_fabric_plan(
        root, source, game_detail, build, client=client, logger=logger
    )
    outcome = install_plan(
        fabric_plan,
        client=client,
        deep=deep,
        workers=workers,
        progress=progress,
        log=logger,
        cancel=cancel,
        pause=pause,
    )
    return outcome


def install_selected(
    root: GameRoot,
    source: DownloadSource,
    game_version: str,
    *,
    loader: str = "vanilla",
    loader_version: str | None = None,
    loader_channel: str = "stable",
    java_path: Path | None = None,
    client: HttpClient | None = None,
    logger: LogCallback | None = None,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
    pause: PauseToken | None = None,
    deep: bool = True,
    workers: int = 6,
) -> InstallOutcome:
    """Install vanilla, Fabric, Forge, NeoForge or Quilt for one version."""
    log = logger or (lambda _message, _level="info": None)
    http = client or _client(source, logger)
    manifest = load_manifest(
        source,
        client=http,
        logger=lambda message: logger(message, "muted") if logger else None,
    )
    remote = manifest.find(game_version)
    if remote is None:
        raise VersionError(f"版本清单中没有 {game_version}。")
    game_detail = fetch_version_detail(
        source,
        remote,
        client=http,
        logger=lambda message: logger(message, "muted") if logger else None,
    )

    if loader == "vanilla":
        plan = build_plan_from_detail(
            root, source, game_detail, client=http, logger=logger
        )
        return install_plan(
            plan,
            client=http,
            deep=deep,
            workers=workers,
            progress=progress,
            log=logger,
            cancel=cancel,
            pause=pause,
        )

    if loader == "fabric":
        builds = fetch_fabric_loaders(
            source, game_version, client=http, logger=logger
        )
        build = _choose_build(
            builds, loader_version, loader_channel, "Fabric"
        )
        return install_fabric(
            root,
            source,
            game_detail,
            build,
            client=http,
            logger=logger,
            progress=progress,
            cancel=cancel,
            pause=pause,
            deep=deep,
            workers=workers,
        )

    if loader == "forge":
        catalog = load_forge_catalog(source, client=http, logger=logger)
        builds = forge_builds(catalog, game_version, loader_channel)
        if loader_version:
            all_builds = forge_builds(catalog, game_version, "all")
            build = next(
                (item for item in all_builds if item.version == loader_version), None
            )
            if build is None:
                raise VersionError(
                    f"Forge 没有适用于 Minecraft {game_version} 的版本 {loader_version}。"
                )
        else:
            build = _choose_build(builds, None, loader_channel, "Forge")
        installer_java = _installer_java_path(
            root, game_detail, java_path, "Forge"
        )
        return install_forge(
            root,
            source,
            game_detail,
            build,
            java_path=installer_java,
            client=http,
            logger=logger,
            progress=progress,
            cancel=cancel,
            pause=pause,
            deep=deep,
            workers=workers,
        )

    if loader == "quilt":
        builds = fetch_quilt_loaders(
            source, game_version, client=http, logger=logger
        )
        build = _choose_build(builds, loader_version, loader_channel, "Quilt")
        return install_quilt(
            root,
            source,
            game_detail,
            build,
            client=http,
            logger=logger,
            progress=progress,
            cancel=cancel,
            pause=pause,
            deep=deep,
            workers=workers,
        )

    if loader == "neoforge":
        builds = fetch_neoforge_builds(
            source, game_version, client=http, logger=logger
        )
        build = _choose_build(builds, loader_version, loader_channel, "NeoForge")
        installer_java = _installer_java_path(
            root, game_detail, java_path, "NeoForge"
        )
        return install_neoforge(
            root,
            source,
            game_detail,
            build,
            java_path=installer_java,
            client=http,
            logger=logger,
            progress=progress,
            cancel=cancel,
            pause=pause,
            deep=deep,
            workers=workers,
        )

    raise VersionError(f"未知的加载器：{loader}")


def _installer_java_path(
    root: GameRoot,
    game_detail: VersionDetail,
    java_path: Path | None,
    label: str,
) -> Path:
    if java_path is not None:
        return java_path
    runtime = pick_java(
        discover_java(root.root), required_java_major(game_detail.data), root.root
    )
    if runtime is None:
        raise LauncherError(
            f"没有找到可用的 Java，无法运行 {label} 安装器。"
        )
    return runtime.path


def _choose_build(
    builds: list[LoaderBuild],
    loader_version: str | None,
    channel: str,
    name: str,
) -> LoaderBuild:
    if not builds:
        raise VersionError(f"没有找到可用的 {name} 版本。")
    if loader_version:
        matched = next(
            (item for item in builds if item.version == loader_version), None
        )
        if matched is None:
            raise VersionError(f"没有找到 {name} 版本 {loader_version}。")
        return matched
    selected = select_loader_builds(builds, channel)
    return selected[0] if selected else builds[0]

