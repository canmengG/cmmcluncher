"""Build and start a Minecraft process from a resolved version."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Iterable

from .. import APP_NAME, APP_VERSION
from .auth import offline_session, offline_uuid_hex, validate_username
from .errors import LauncherError, VersionError
from .models import GameRoot, LaunchOptions, LaunchPlan, VersionSummary
from .paths import ensure_directory_writable, resolve_game_dir
from .version import (
    AssetInfo,
    find_client_jar,
    find_log_config,
    is_allowed,
    load_merged_version,
    resolve_assets,
    resolve_libraries,
)

MAX_COMMAND_LENGTH = 30_000


def _substitute(template: str, values: dict[str, str]) -> str:
    result = template
    for key, value in values.items():
        result = result.replace("${" + key + "}", value)
    if "${" in result:
        start = result.find("${")
        end = result.find("}", start)
        unknown = result[start : end + 1] if end >= 0 else result[start:]
        raise VersionError(f"不支持的启动参数占位符：{unknown}")
    return result


def _argument_values(
    entries: Iterable[Any], values: dict[str, str], features: dict[str, bool]
) -> list[str]:
    result: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            result.append(_substitute(entry, values))
            continue
        if not isinstance(entry, dict) or not is_allowed(entry, features):
            continue
        value = entry.get("value")
        raw_values = value if isinstance(value, list) else [value]
        for item in raw_values:
            if isinstance(item, str):
                result.append(_substitute(item, values))
    return result


def _has_flag(arguments: list[str], flag: str) -> bool:
    return any(argument == flag or argument.startswith(flag + "=") for argument in arguments)


def _ensure_ignored_jar(arguments: list[str], filename: str) -> None:
    """Add the resolved game jar to Forge's ``-DignoreList``.

    Forge's profile ignores ``${version_name}.jar``, which is the modded
    version's own jar.  With ``inheritsFrom`` installations the client jar
    actually lives in the parent version folder (for example ``1.20.1.jar``),
    so it would otherwise be registered twice: once by Forge as the
    ``minecraft`` module and once by BootstrapLauncher as an automatic module.
    Java then aborts with a ``ResolutionException`` about duplicate packages.
    """
    if not filename:
        return
    for index, argument in enumerate(arguments):
        if not argument.startswith("-DignoreList="):
            continue
        entries = [
            item for item in argument.split("=", 1)[1].split(",") if item
        ]
        # Entries are filename prefixes (e.g. "forge-", "client-extra").
        if any(filename == entry or filename.startswith(entry) for entry in entries):
            return
        entries.append(filename)
        arguments[index] = "-DignoreList=" + ",".join(entries)
        return


def _append_required(
    arguments: list[str], flag: str, value: str | None = None
) -> None:
    if _has_flag(arguments, flag):
        return
    arguments.append(flag)
    if value is not None:
        arguments.append(value)


def _extract_natives(
    archives: Iterable[tuple[Path, list[str]]], destination: Path
) -> list[str]:
    warnings: list[str] = []
    destination.mkdir(parents=True, exist_ok=True)
    for archive, excludes in archives:
        try:
            with zipfile.ZipFile(archive) as zipped:
                for info in zipped.infolist():
                    if info.is_dir():
                        continue
                    name = info.filename.replace("\\", "/")
                    if name.startswith("META-INF/"):
                        continue
                    if any(name.startswith(prefix) for prefix in excludes):
                        continue
                    # Flatten native libraries into one directory.  Old LWJGL
                    # jars store the DLLs at the archive root; flattening also
                    # keeps data from escaping the temp directory.
                    target = destination / Path(name).name
                    with zipped.open(info) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
        except (OSError, zipfile.BadZipFile) as exc:
            warnings.append(f"无法解压本地库 {archive.name}：{exc}")
    return warnings


def _classpath_string(paths: Iterable[Path]) -> str:
    return os.pathsep.join(str(path) for path in paths)


def _create_natives_dir() -> Path:
    """Create a temp directory outside the (possibly read-only) app folder."""
    candidates: list[Path] = []
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidates.append(Path(local_appdata) / "MinecraftLauncher" / "natives")
    candidates.append(Path(tempfile.gettempdir()) / "MinecraftLauncher" / "natives")
    candidates.append(Path.cwd())
    for base in candidates:
        try:
            base.mkdir(parents=True, exist_ok=True)
            return Path(tempfile.mkdtemp(prefix="mc-launcher-natives-", dir=base))
        except OSError:
            continue
    raise LauncherError("无法创建临时 native 目录，请检查系统临时文件夹权限。")


def build_launch_plan(
    root: GameRoot,
    summary: VersionSummary,
    options: LaunchOptions,
) -> LaunchPlan:
    """Resolve files and construct the Java command without downloading."""
    account = options.account
    if account is not None:
        username = validate_username(account.name)
    else:
        username = validate_username(options.username)
    loaded = load_merged_version(root, summary)
    version_data = loaded.data
    java_path = options.java_path.expanduser()
    try:
        java_path = java_path.resolve()
    except OSError:
        pass

    main_class = version_data.get("mainClass")
    if not isinstance(main_class, str) or not main_class:
        raise VersionError(f"版本 {summary.version_id} 缺少 mainClass。")

    game_dir = resolve_game_dir(root, summary, options.isolated)
    if options.check_writable:
        ensure_directory_writable(game_dir)
    assets = resolve_assets(root, version_data, game_dir)
    libraries = resolve_libraries(root, version_data)
    client_jar = find_client_jar(root, loaded)
    if client_jar is None:
        raise VersionError(
            f"找不到客户端 jar：{summary.version_id}\n"
            f"通常应位于 versions/{summary.folder_name}/{summary.folder_name}.jar。"
        )

    if libraries.missing:
        preview = "\n".join(f"  - {path}" for path in libraries.missing[:8])
        more = "" if len(libraries.missing) <= 8 else f"\n  ... 另有 {len(libraries.missing) - 8} 个"
        raise VersionError(
            f"缺少 {len(libraries.missing)} 个本地库文件，无法启动：\n{preview}{more}\n"
            "请先用官方启动器或整合包安装器补全 libraries 文件夹。"
        )

    classpath: list[Path] = []
    seen: set[Path] = set()
    for path in [*libraries.classpath, client_jar]:
        if path not in seen:
            seen.add(path)
            classpath.append(path)

    temporary_natives = options.natives_dir is None
    if options.natives_dir is not None:
        natives_dir = options.natives_dir
        natives_dir.mkdir(parents=True, exist_ok=True)
    else:
        natives_dir = _create_natives_dir()

    # Modern version JSON points JNA/LWJGL/Netty temporary paths below the
    # natives directory.  Create them up front so those libraries do not race
    # or fail on a missing directory.
    for subdirectory in ("java", "jna", "lwjgl", "netty"):
        (natives_dir / subdirectory).mkdir(parents=True, exist_ok=True)

    warnings_extra: list[str] = []
    try:
        warnings_extra = _extract_natives(libraries.native_archives, natives_dir)

        log_config = find_log_config(root, version_data, summary.folder)
        version_id = str(version_data.get("id") or summary.version_id)
        is_legacy = not isinstance(version_data.get("arguments"), dict) and isinstance(
            version_data.get("minecraftArguments"), str
        )
        if account is not None:
            uuid_value = account.uuid
            access_token = account.access_token
            user_type = "msa"
            auth_session = ""
            xuid = account.xuid
            client_id = account.client_id
        else:
            uuid_value = offline_uuid_hex(username)
            access_token = "0"
            user_type = "legacy" if is_legacy else "msa"
            auth_session = offline_session(username)
            xuid = ""
            client_id = ""
        assets_index_id = assets.index_id or "legacy"

        values = {
            "auth_player_name": username,
            "profile_name": username,
            "version_name": version_id,
            "game_directory": str(game_dir),
            "assets_root": str(assets.assets_dir),
            "game_assets": str(assets.assets_dir),
            "assets_index_name": assets_index_id,
            "auth_uuid": uuid_value,
            "uuid": uuid_value,
            "auth_access_token": access_token,
            "accessToken": access_token,
            "auth_session": auth_session,
            "user_type": user_type,
            "version_type": str(version_data.get("type") or "release"),
            "natives_directory": str(natives_dir),
            "launcher_name": APP_NAME,
            "launcher_version": APP_VERSION,
            "classpath": _classpath_string(classpath),
            "classpath_separator": os.pathsep,
            "library_directory": str(root.libraries_dir),
            "resolution_width": str(options.window_width),
            "resolution_height": str(options.window_height),
            "user_properties": "{}",
            "userProperties": "{}",
            "auth_xuid": xuid,
            "clientid": client_id,
            "path": str(log_config) if log_config else "",
            "arch": "64" if os.environ.get("PROCESSOR_ARCHITECTURE", "").endswith("64") else "32",
            "primary_jar": str(client_jar),
        }

        features = {
            "is_demo_user": options.demo,
            "has_custom_resolution": False,
            "has_quick_plays_support": False,
            "is_quick_play_singleplayer": False,
            "is_quick_play_multiplayer": False,
            "is_quick_play_realms": False,
        }

        memory = max(512, min(int(options.memory_mb), 65536))
        jvm_args: list[str] = [f"-Xms{min(512, memory)}M", f"-Xmx{memory}M"]
        arguments = version_data.get("arguments")
        if isinstance(arguments, dict):
            jvm_args.extend(
                _argument_values(arguments.get("jvm") or [], values, features)
            )
        # Forge/NeoForge bootstrap: the parent's client jar must be ignored by
        # the module finder, otherwise its packages are registered twice.
        _ensure_ignored_jar(jvm_args, client_jar.name)
        jvm_args = [
            argument
            for argument in jvm_args
            if argument != "-Dlog4j.configurationFile="
        ]

        # The modern version JSON usually already contains -cp, but modded
        # versions do not always inherit it.  Add it only when absent.
        if not _has_flag(jvm_args, "-cp") and not _has_flag(jvm_args, "-classpath"):
            jvm_args.extend(["-cp", values["classpath"]])

        if not any(argument.startswith("-Djava.library.path=") for argument in jvm_args):
            jvm_args.append(f"-Djava.library.path={natives_dir}")
        if not any(argument.startswith("-Dminecraft.launcher.brand=") for argument in jvm_args):
            jvm_args.append(f"-Dminecraft.launcher.brand={APP_NAME}")
        if not any(argument.startswith("-Dminecraft.launcher.version=") for argument in jvm_args):
            jvm_args.append(f"-Dminecraft.launcher.version={APP_VERSION}")
        if log_config and not any(
            argument.startswith("-Dlog4j.configurationFile=") for argument in jvm_args
        ):
            jvm_args.append(f"-Dlog4j.configurationFile={log_config}")
        if not any(
            argument.startswith("-Dlog4j2.formatMsgNoLookups=") for argument in jvm_args
        ):
            jvm_args.append("-Dlog4j2.formatMsgNoLookups=true")

        if isinstance(arguments, dict):
            game_args = _argument_values(arguments.get("game") or [], values, features)
        elif isinstance(version_data.get("minecraftArguments"), str):
            try:
                game_args = [
                    _substitute(part, values)
                    for part in shlex.split(str(version_data["minecraftArguments"]))
                ]
            except ValueError as exc:
                raise VersionError(f"无法解析 minecraftArguments：{exc}") from exc
        else:
            game_args = []

        _append_required(game_args, "--username", username)
        _append_required(game_args, "--version", version_id)
        _append_required(game_args, "--gameDir", str(game_dir))
        if assets.index_id:
            _append_required(game_args, "--assetsDir", str(assets.assets_dir))
            _append_required(game_args, "--assetIndex", assets.index_id)
        _append_required(game_args, "--uuid", uuid_value)
        _append_required(game_args, "--accessToken", access_token)
        _append_required(game_args, "--userType", user_type)
        _append_required(game_args, "--versionType", str(version_data.get("type") or "release"))
        if is_legacy:
            _append_required(game_args, "--userProperties", "{}")

        command = [str(java_path), *jvm_args, main_class, *game_args]

        command_length = sum(len(part) + 1 for part in command)
        if command_length > MAX_COMMAND_LENGTH:
            raise LauncherError(
                "生成的启动命令过长，可能超出 Windows 命令行限制。\n"
                f"当前长度约 {command_length} 字符。"
            )

        if assets.virtual and assets.assets_dir == root.assets_dir:
            warnings_extra.append(
                "该版本使用旧版虚拟资源索引，但 assets/virtual/<索引> 尚未生成；"
                "请在下载页执行“下载 / 补全”以整理旧版资源。"
            )

        return LaunchPlan(
            command=command,
            cwd=game_dir,
            version_id=version_id,
            game_dir=game_dir,
            assets_dir=assets.assets_dir,
            natives_dir=natives_dir,
            java_path=java_path,
            log_config=log_config,
            warnings=warnings_extra,
            temporary_natives=temporary_natives,
        )
    except Exception:
        if temporary_natives:
            shutil.rmtree(natives_dir, ignore_errors=True)
        raise


def cleanup_plan(plan: LaunchPlan) -> None:
    """Remove the temporary native directory after the game exits."""
    if plan.temporary_natives:
        shutil.rmtree(plan.natives_dir, ignore_errors=True)


def start_game(plan: LaunchPlan) -> subprocess.Popen[str]:
    """Start the Java process with captured output for the UI log pane."""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    return subprocess.Popen(
        plan.command,
        cwd=str(plan.cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creationflags,
    )
