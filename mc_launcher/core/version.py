"""Minecraft version discovery, inheritance merging and file resolution."""

from __future__ import annotations

import copy
import json
import os
import platform
import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import LauncherError, VersionError
from .models import GameRoot, VersionSummary


# ---------------------------------------------------------------------------
# Version discovery
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8-sig") as file:
            data = json.load(file)
    except OSError as exc:
        raise VersionError(f"无法读取文件：{path}\n{exc}") from exc
    except json.JSONDecodeError as exc:
        raise VersionError(f"JSON 格式错误：{path}\n{exc}") from exc
    if not isinstance(data, dict):
        raise VersionError(f"版本 JSON 根节点不是对象：{path}")
    return data


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _version_json_for_folder(folder: Path) -> Path | None:
    preferred = folder / f"{folder.name}.json"
    if preferred.is_file():
        return preferred
    candidates = sorted(folder.glob("*.json"))
    return candidates[0] if candidates else None


def _find_jar_candidates(
    root: GameRoot,
    folder: Path,
    version_id: str,
    info: dict[str, Any],
) -> Path | None:
    jar_id = info.get("jar")
    names: list[str] = []
    if isinstance(jar_id, str) and jar_id:
        names.append(jar_id)
    names.extend([folder.name, version_id])

    for name in names:
        candidate = root.versions_dir / name / f"{name}.jar"
        if candidate.is_file():
            return candidate
    for name in names:
        candidate = folder / f"{name}.jar"
        if candidate.is_file():
            return candidate
    return None


def list_versions(root: GameRoot) -> list[VersionSummary]:
    """Scan ``versions/`` and return usable version folders, newest first."""
    if not root.versions_dir.is_dir():
        raise LauncherError(f"找不到 versions 文件夹：{root.versions_dir}")

    versions: list[VersionSummary] = []
    for folder in sorted(root.versions_dir.iterdir(), key=lambda p: p.name.lower()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        json_path = _version_json_for_folder(folder)
        if json_path is None:
            continue
        try:
            info = _read_json(json_path)
        except VersionError:
            # A broken/unrelated JSON in a version folder should not hide all
            # other versions.  It will fail with a detailed error when opened.
            continue
        if not any(key in info for key in ("id", "mainClass", "inheritsFrom", "libraries")):
            continue

        version_id = str(info.get("id") or json_path.stem)
        release_time = _parse_time(info.get("releaseTime"))
        if release_time is None:
            try:
                release_time = datetime.fromtimestamp(
                    json_path.stat().st_mtime, tz=timezone.utc
                )
            except OSError:
                release_time = None

        versions.append(
            VersionSummary(
                folder=folder,
                version_id=version_id,
                version_type=str(info.get("type") or ""),
                release_time=release_time,
                json_path=json_path,
                jar_path=_find_jar_candidates(root, folder, version_id, info),
            )
        )

    versions.sort(
        key=lambda item: (
            item.release_time.timestamp() if item.release_time else 0.0,
            item.version_id.lower(),
        ),
        reverse=True,
    )
    return versions


# ---------------------------------------------------------------------------
# Inheritance merging
# ---------------------------------------------------------------------------


def _library_key(library: dict[str, Any]) -> str:
    name = library.get("name")
    if isinstance(name, str) and name:
        return name
    return json.dumps(library, sort_keys=True, ensure_ascii=False)


def _merge_arguments(
    parent: dict[str, Any], child: dict[str, Any]
) -> dict[str, list[Any]]:
    merged: dict[str, list[Any]] = {}
    for key in ("game", "jvm"):
        value = []
        for source in (parent, child):
            arguments = source.get("arguments")
            if isinstance(arguments, dict) and isinstance(arguments.get(key), list):
                value.extend(copy.deepcopy(arguments[key]))
        if value:
            merged[key] = value
    return merged


def _merge_version(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(parent)

    for key, value in child.items():
        if key in ("libraries", "arguments", "downloads", "logging", "inheritsFrom"):
            continue
        result[key] = copy.deepcopy(value)

    # Libraries are inherited in order.  A child library with the same Maven
    # coordinate replaces the parent entry in place.
    merged_libraries: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for library in list(parent.get("libraries") or []) + list(child.get("libraries") or []):
        if not isinstance(library, dict):
            continue
        key = _library_key(library)
        if key in positions:
            merged_libraries[positions[key]] = copy.deepcopy(library)
        else:
            positions[key] = len(merged_libraries)
            merged_libraries.append(copy.deepcopy(library))
    if merged_libraries:
        result["libraries"] = merged_libraries
    else:
        result.pop("libraries", None)

    merged_arguments = _merge_arguments(parent, child)
    if merged_arguments:
        result["arguments"] = merged_arguments
    else:
        result.pop("arguments", None)

    # A child that explicitly uses the legacy argument string replaces the
    # modern argument structure from its parent.
    if isinstance(child.get("minecraftArguments"), str):
        result["minecraftArguments"] = child["minecraftArguments"]
        if "arguments" not in child:
            result.pop("arguments", None)
    elif "arguments" not in result and isinstance(parent.get("minecraftArguments"), str):
        result["minecraftArguments"] = parent["minecraftArguments"]

    if isinstance(child.get("downloads"), dict):
        downloads = copy.deepcopy(parent.get("downloads") or {})
        downloads.update(copy.deepcopy(child["downloads"]))
        result["downloads"] = downloads

    if "logging" in child:
        result["logging"] = copy.deepcopy(child["logging"])

    result["id"] = child.get("id") or parent.get("id")
    result.pop("inheritsFrom", None)
    return result


@dataclass
class LoadedVersion:
    data: dict[str, Any]
    summary: VersionSummary
    parent_ids: list[str] = field(default_factory=list)
    source_paths: list[Path] = field(default_factory=list)


def _version_index(root: GameRoot) -> dict[str, Path]:
    index: dict[str, Path] = {}
    if not root.versions_dir.is_dir():
        return index
    for folder in sorted(root.versions_dir.iterdir(), key=lambda p: p.name.lower()):
        if not folder.is_dir():
            continue
        json_path = _version_json_for_folder(folder)
        if json_path is None:
            continue
        try:
            info = _read_json(json_path)
        except VersionError:
            continue
        version_id = str(info.get("id") or json_path.stem)
        index.setdefault(version_id, json_path)
        index.setdefault(folder.name, json_path)
    return index


def load_merged_version(root: GameRoot, summary: VersionSummary) -> LoadedVersion:
    """Load a version JSON and merge every ``inheritsFrom`` parent into it."""
    index = _version_index(root)
    chain: list[tuple[Path, dict[str, Any]]] = []
    seen: set[Path] = set()
    current = summary.json_path
    parent_ids: list[str] = []

    while True:
        resolved = current.resolve()
        if resolved in seen:
            raise VersionError(f"版本继承出现循环：{current}")
        seen.add(resolved)
        info = _read_json(current)
        chain.append((current, info))
        parent_id = info.get("inheritsFrom")
        if not parent_id:
            break
        parent_id = str(parent_id)
        parent_ids.append(parent_id)
        parent_path = index.get(parent_id)
        if parent_path is None:
            raise VersionError(
                f"版本 {summary.version_id} 继承自 {parent_id}，"
                f"但找不到对应的版本 JSON。\n"
                f"请确认 versions/{parent_id}/{parent_id}.json 存在。"
            )
        current = parent_path

    merged = copy.deepcopy(chain[-1][1])
    for _, child in reversed(chain[:-1]):
        merged = _merge_version(merged, child)

    # The folder is the source of truth for which instance the user selected.
    # Keep the JSON id (modded versions often differ), but make it available.
    merged.setdefault("id", summary.version_id)
    return LoadedVersion(
        data=merged,
        summary=summary,
        parent_ids=parent_ids,
        source_paths=[path for path, _ in chain],
    )


def base_game_version(root: GameRoot, summary: VersionSummary) -> str:
    """Return the vanilla Minecraft version a loader/modpack instance targets.

    ``versions/<folder>`` often carries a loader or modpack id (for example
    ``fabulously-optimized-14.1.1``), which is not a game version.  Following
    the ``inheritsFrom`` chain to its last parent yields ``26.2`` for such an
    instance; a plain vanilla version has no parent and returns its own id.
    """
    try:
        loaded = load_merged_version(root, summary)
    except (LauncherError, OSError):
        return summary.version_id
    if loaded.parent_ids:
        return loaded.parent_ids[-1]
    return summary.version_id


# ---------------------------------------------------------------------------
# Rules and libraries
# ---------------------------------------------------------------------------


def _current_os_name() -> str:
    if os.name == "nt":
        return "windows"
    if platform.system() == "Darwin":
        return "osx"
    return "linux"


def _current_arch() -> str:
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64", "x64"):
        return "x86_64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine in ("x86", "i386", "i686"):
        return "x86"
    return machine


def _rule_matches(rule: dict[str, Any], features: dict[str, bool]) -> bool:
    rule_features = rule.get("features")
    if isinstance(rule_features, dict):
        for key, expected in rule_features.items():
            if bool(features.get(key, False)) != bool(expected):
                return False

    rule_os = rule.get("os")
    if isinstance(rule_os, dict):
        if "name" in rule_os and rule_os["name"] != _current_os_name():
            return False
        if "arch" in rule_os and rule_os["arch"] != _current_arch():
            return False
        if "version" in rule_os:
            try:
                if not re.search(str(rule_os["version"]), platform.version()):
                    return False
            except re.error:
                return False
    return True


def is_allowed(item: dict[str, Any], features: dict[str, bool] | None = None) -> bool:
    """Evaluate Minecraft's allow/disallow rule list for an argument/library."""
    rules = item.get("rules")
    if not isinstance(rules, list) or not rules:
        return True
    features = features or {}
    allowed = False
    for rule in rules:
        if isinstance(rule, dict) and _rule_matches(rule, features):
            allowed = rule.get("action") == "allow"
    return allowed


def maven_path(name: str) -> Path:
    """Convert ``group:artifact:version[:classifier][@ext]`` to a local path."""
    coordinate, _, extension = name.partition("@")
    extension = extension or "jar"
    parts = coordinate.split(":")
    if len(parts) < 3:
        raise VersionError(f"无法识别的 Maven 坐标：{name}")
    group, artifact, version = parts[0], parts[1], parts[2]
    classifier = parts[3] if len(parts) > 3 and parts[3] else None
    filename = f"{artifact}-{version}"
    if classifier:
        filename += f"-{classifier}"
    filename += f".{extension}"
    return Path(*group.split("."), artifact, version, filename)


def _artifact_path(root: GameRoot, artifact: dict[str, Any], name: str | None) -> Path:
    relative = artifact.get("path")
    if isinstance(relative, str) and relative:
        relative_path = Path(relative.replace("/", os.sep))
        if relative_path.is_absolute():
            raise VersionError(f"库文件路径不应为绝对路径：{relative}")
        return root.libraries_dir / relative_path
    if name:
        return root.libraries_dir / maven_path(name)
    raise VersionError("库条目缺少 downloads.artifact.path 和 name。")


def _native_classifier(library: dict[str, Any]) -> str | None:
    natives = library.get("natives")
    if not isinstance(natives, dict):
        return None
    arch = "64" if struct.calcsize("P") * 8 == 64 else "32"
    template = natives.get("windows")
    if not isinstance(template, str) or not template:
        return None
    candidates = [
        template.replace("${arch}", arch),
        template,
        "natives-windows",
        f"natives-windows-{arch}",
    ]
    classifiers = library.get("downloads", {}).get("classifiers")
    if isinstance(classifiers, dict):
        for candidate in candidates:
            if candidate in classifiers:
                return candidate
    return candidates[0]


@dataclass
class LibrarySet:
    classpath: list[Path] = field(default_factory=list)
    native_archives: list[tuple[Path, list[str]]] = field(default_factory=list)
    missing: list[Path] = field(default_factory=list)


def resolve_libraries(
    root: GameRoot,
    version_data: dict[str, Any],
    features: dict[str, bool] | None = None,
) -> LibrarySet:
    """Resolve every allowed library to a local file without downloading."""
    result = LibrarySet()
    seen_classpath: set[Path] = set()
    seen_missing: set[Path] = set()

    for library in version_data.get("libraries") or []:
        if not isinstance(library, dict) or not is_allowed(library, features):
            continue

        name = library.get("name")
        downloads = library.get("downloads")
        if not isinstance(downloads, dict):
            downloads = {}

        native_classifier = _native_classifier(library)
        artifact = downloads.get("artifact")
        artifact_path: Path | None = None

        if isinstance(artifact, dict):
            artifact_path = _artifact_path(root, artifact, str(name) if name else None)
        elif isinstance(name, str) and name:
            artifact_path = root.libraries_dir / maven_path(name)

        # Native-only libraries often have no ordinary artifact; their jar is
        # selected through downloads.classifiers.
        if native_classifier:
            classifiers = downloads.get("classifiers")
            native_artifact = (
                classifiers.get(native_classifier)
                if isinstance(classifiers, dict)
                else None
            )
            if isinstance(native_artifact, dict):
                native_path = _artifact_path(
                    root, native_artifact, str(name) if name else None
                )
            elif isinstance(name, str) and name:
                native_path = root.libraries_dir / maven_path(
                    f"{name}:{native_classifier}"
                )
            else:
                native_path = None

            if native_path is not None:
                if native_path.is_file():
                    excludes = []
                    extract = library.get("extract")
                    if isinstance(extract, dict) and isinstance(extract.get("exclude"), list):
                        excludes = [str(value) for value in extract["exclude"]]
                    result.native_archives.append((native_path, excludes))
                elif native_path not in seen_missing:
                    result.missing.append(native_path)
                    seen_missing.add(native_path)

        # A library with a natives map and no artifact should not be placed on
        # the classpath.  A modern native-classified library does have an
        # artifact and must be on the classpath (LWJGL extracts it itself).
        if artifact_path is not None and not (native_classifier and artifact is None):
            if artifact_path.is_file():
                if artifact_path not in seen_classpath:
                    result.classpath.append(artifact_path)
                    seen_classpath.add(artifact_path)
            elif artifact_path not in seen_missing:
                result.missing.append(artifact_path)
                seen_missing.add(artifact_path)

    return result


# ---------------------------------------------------------------------------
# Client jar, assets and log config
# ---------------------------------------------------------------------------


def find_client_jar(
    root: GameRoot, loaded: LoadedVersion
) -> Path | None:
    merged = loaded.data
    names: list[str] = []
    jar_hint = merged.get("jar")
    if isinstance(jar_hint, str) and jar_hint:
        names.append(jar_hint)
    for version_id in (
        [str(merged.get("id") or ""), loaded.summary.version_id, loaded.summary.folder_name]
        + loaded.parent_ids
    ):
        if version_id and version_id not in names:
            names.append(version_id)

    for name in names:
        candidate = root.versions_dir / name / f"{name}.jar"
        if candidate.is_file():
            return candidate
    for name in names:
        candidate = loaded.summary.folder / f"{name}.jar"
        if candidate.is_file():
            return candidate
    if loaded.summary.jar_path and loaded.summary.jar_path.is_file():
        return loaded.summary.jar_path
    return None


@dataclass
class AssetInfo:
    index_id: str | None
    index_path: Path | None
    assets_dir: Path
    virtual: bool = False
    map_to_resources: bool = False


def resolve_assets(
    root: GameRoot,
    version_data: dict[str, Any],
    game_dir: Path | None = None,
) -> AssetInfo:
    index_id = version_data.get("assetIndex", {}).get("id") or version_data.get("assets")
    if not isinstance(index_id, str) or not index_id:
        return AssetInfo(index_id=None, index_path=None, assets_dir=root.assets_dir)

    index_path = root.assets_dir / "indexes" / f"{index_id}.json"
    if not index_path.is_file():
        raise VersionError(
            f"缺少资源索引文件：{index_path}\n"
            "请先用官方启动器或启动器补全该游戏目录的资源文件。"
        )
    try:
        index_data = _read_json(index_path)
    except VersionError:
        raise

    assets_dir = root.assets_dir
    virtual = bool(index_data.get("virtual"))
    map_to_resources = bool(index_data.get("map_to_resources"))
    if virtual:
        virtual_dir = root.assets_dir / "virtual" / index_id
        if virtual_dir.is_dir():
            assets_dir = virtual_dir
    if map_to_resources:
        candidates = []
        if game_dir is not None:
            candidates.append(game_dir / "resources")
        candidates.append(root.root / "resources")
        for candidate in candidates:
            if candidate.is_dir():
                assets_dir = candidate
                break
    return AssetInfo(
        index_id=index_id,
        index_path=index_path,
        assets_dir=assets_dir,
        virtual=virtual,
        map_to_resources=map_to_resources,
    )


def find_log_config(
    root: GameRoot,
    version_data: dict[str, Any],
    instance_dir: Path | None = None,
) -> Path | None:
    logging = version_data.get("logging")
    if not isinstance(logging, dict):
        return None
    client = logging.get("client")
    if not isinstance(client, dict):
        return None
    file_info = client.get("file")
    if not isinstance(file_info, dict):
        return None
    file_id = file_info.get("id")
    if not isinstance(file_id, str) or not file_id:
        return None

    candidates = [
        root.assets_dir / "log_configs" / file_id,
        root.root / "log_configs" / file_id,
    ]
    if instance_dir is not None:
        candidates.append(instance_dir / "log_configs" / file_id)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None
