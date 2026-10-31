"""Command-line entry point and headless diagnostics for the launcher."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

from . import APP_NAME, APP_VERSION
from .core.cache import (
    CATEGORY_CHOICES,
    clean_cache,
    format_cache_usage,
    format_clean_report,
    scan_cache,
)
from .core.content import (
    CONTENT_TYPES,
    SOURCE_CURSEFORGE,
    SOURCE_MODRINTH,
    ContentError,
    build_content_plan,
    content_type_label,
    default_target_dir,
    format_search_result,
    format_versions,
    get_content_source,
    install_content,
    read_mrpack,
    select_best_version,
    validate_world_dir,
)
from .core.errors import LauncherError
from .core.install import (
    build_local_plan,
    format_verify_report,
    verify_plan,
)
from .core.java import discover_java, pick_java, required_java_major
from .core.java_runtime import (
    build_install_plan,
    default_install_root,
    default_platform,
    fetch_catalog,
    fetch_component_manifest,
    format_component_table,
    format_runtime_report,
    install_java_runtime,
    manual_java_links,
    resolve_component,
    scan_installed_runtimes,
    validate_install_root,
    verify_java_runtime,
)
from .core.launch import build_launch_plan, cleanup_plan
from .core.manifest import KIND_LABELS, filter_versions, load_manifest
from .core.mirrors import SOURCES, get_source, loader_allowed_hosts, source_by_label
from .core.models import LaunchOptions, MicrosoftAccount, VersionSummary
from .core.modpack import (
    build_instance_plan,
    download_and_install_launchable_modpack,
    format_instance_plan,
    install_modpack_archive,
    resolve_modpack_dependencies,
)
from .core.msauth import (
    AuthError,
    HttpAuthTransport,
    clear_saved_account,
    load_saved_account,
    login_device_code,
    save_account,
    validate_client_id,
)
from .core.net import CancelledError, HttpClient, human_size
from .core.paths import (
    ensure_game_root,
    game_root_path,
    has_isolated_game_data,
    normalize_game_root,
)
from .core.settings import resolve_client_id, save_client_id
from .core.storage import app_data_dir
from .core.version import list_versions, load_merged_version
from .core.version_admin import (
    format_version_table,
    list_version_details,
    remove_version,
    version_details,
)


def _default_username() -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "", os.environ.get("USERNAME", ""))[:16]
    return name or "Steve"


def _choose_version(
    versions: list[VersionSummary], requested: str | None
) -> VersionSummary:
    if not versions:
        raise LauncherError("versions 文件夹中没有可用版本。")
    if requested:
        for version in versions:
            if requested in (version.version_id, version.folder_name):
                return version
        raise LauncherError(f"找不到版本：{requested}")
    return next(
        (version for version in versions if version.version_type == "release"),
        versions[0],
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="MinecraftLauncher",
        description=(
            "Minecraft launcher: local launch, version download, "
            "Fabric/Forge install and integrity repair."
        ),
    )
    parser.add_argument("--game-dir", help="Minecraft root containing versions/libraries/assets")
    parser.add_argument("--game-version", help="Version id or folder name")
    parser.add_argument("--username", default=_default_username(), help="Offline player name")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Ignore a saved Microsoft account and launch with the offline username",
    )
    parser.add_argument(
        "--ms-client-id",
        help="Azure Application (client) ID for --login-microsoft/--account-info",
    )
    parser.add_argument(
        "--login-microsoft",
        action="store_true",
        help="Sign in with a Microsoft account using the device-code flow",
    )
    parser.add_argument(
        "--logout-microsoft",
        action="store_true",
        help="Delete the saved Microsoft login token",
    )
    parser.add_argument(
        "--account-info",
        action="store_true",
        help="Show the saved Microsoft account (never prints tokens)",
    )
    parser.add_argument("--java", help="Path to java.exe")
    parser.add_argument("--memory", type=int, default=2048, help="Maximum heap in MB")
    parser.add_argument(
        "--isolated",
        dest="isolated",
        action="store_true",
        help="Use versions/<version> as the game directory",
    )
    parser.add_argument(
        "--no-isolated",
        dest="isolated",
        action="store_false",
        help="Use the game root as the game directory",
    )
    parser.set_defaults(isolated=None)
    parser.add_argument("--natives-dir", help="Use this native directory instead of a temp one")
    parser.add_argument("--list-versions", action="store_true", help="List installed versions")
    parser.add_argument(
        "--details",
        action="store_true",
        help="With --list-versions: show type, loader, size, modified time and isolation",
    )
    parser.add_argument(
        "--remove-version",
        metavar="VERSION",
        help="Delete versions/<VERSION> (never shared libraries/assets/saves)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve files and print the command without starting Java",
    )
    parser.add_argument("--version", action="version", version=APP_VERSION)

    # Downloader / installer
    parser.add_argument(
        "--source",
        choices=[item.id for item in SOURCES.values()],
        default="bmclapi",
        help="Download provider (default: bmclapi, official Mojang also available)",
    )
    parser.add_argument(
        "--list-remote",
        action="store_true",
        help="List versions available from the selected download source",
    )
    parser.add_argument(
        "--remote-type",
        choices=list(KIND_LABELS),
        default="release",
        help="Filter remote versions by type",
    )
    parser.add_argument(
        "--install",
        metavar="VERSION",
        help="Install or repair one game version, then optionally a loader",
    )
    parser.add_argument(
        "--loader",
        choices=["vanilla", "fabric", "forge", "neoforge", "quilt"],
        default="vanilla",
        help="Loader to install with --install",
    )
    parser.add_argument(
        "--loader-version",
        help="Exact Fabric/Forge/NeoForge/Quilt build; default uses --loader-channel",
    )
    parser.add_argument(
        "--loader-channel",
        choices=["stable", "latest", "all"],
        default="stable",
        help="Loader release channel",
    )
    parser.add_argument(
        "--verify",
        metavar="VERSION",
        help="Deep-verify an installed version and print a report",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Only check file existence/size (skip SHA-1) for --verify/--install",
    )
    parser.add_argument(
        "--workers",
        "--threads",
        dest="workers",
        type=int,
        default=6,
        help="Parallel file downloads (default: 6)",
    )
    parser.add_argument(
        "--segments",
        type=int,
        default=4,
        help="Connections per large file (>= 8 MB); 0 or 1 disables (default: 4)",
    )
    parser.add_argument(
        "--cache-info",
        action="store_true",
        help="Show launcher cache sizes (manifests, installers, natives, partial files)",
    )
    parser.add_argument(
        "--clean-cache",
        nargs="?",
        const="all",
        choices=list(CATEGORY_CHOICES),
        help="Delete one cache category (or all) after confirmation",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Skip interactive confirmation for --remove-version/--clean-cache/"
            "--install-java/--content-install/--modpack-archive (and allow "
            "overwriting modpack instance files)"
        ),
    )

    # Java runtime acquisition (0.4.0)
    parser.add_argument(
        "--java-info",
        action="store_true",
        help="Show required Java, detected Java and launcher-managed runtimes",
    )
    parser.add_argument(
        "--list-java",
        action="store_true",
        help="List Mojang Java runtime components (does not download)",
    )
    parser.add_argument(
        "--install-java",
        metavar="COMPONENT|MAJOR",
        help=(
            "Install a Mojang Java runtime (for example 17 or java-runtime-gamma); "
            "asks for confirmation unless --yes is given"
        ),
    )
    parser.add_argument(
        "--verify-java",
        metavar="COMPONENT",
        help="Verify an installed launcher-managed Java runtime against its marker",
    )
    parser.add_argument(
        "--java-dir",
        help=(
            "Java install root (default: <game-dir>/runtime, or "
            "LOCALAPPDATA/MinecraftLauncher/runtime without --game-dir)"
        ),
    )
    parser.add_argument(
        "--required-java",
        type=int,
        help="Override the required Java major for --java-info/--java-urls/--install-java",
    )
    parser.add_argument(
        "--java-urls",
        action="store_true",
        help="Print Mojang and vendor Java download URLs for manual installation",
    )

    # Content download center (0.5.0)
    parser.add_argument(
        "--content-search",
        metavar="QUERY",
        help="Search Modrinth/CurseForge content (empty query lists popular items)",
    )
    parser.add_argument(
        "--content-type",
        choices=list(CONTENT_TYPES),
        default="mod",
        help="Content type for --content-search/--content-install (default: mod)",
    )
    parser.add_argument(
        "--content-source",
        choices=[SOURCE_MODRINTH, SOURCE_CURSEFORGE],
        default=SOURCE_MODRINTH,
        help="Content provider (default: modrinth)",
    )
    parser.add_argument(
        "--content-game-version",
        help="Minecraft version filter for content search/versions",
    )
    parser.add_argument(
        "--content-loader",
        help="Loader filter: fabric/forge/neoforge/quilt/datapack",
    )
    parser.add_argument(
        "--content-category",
        help="Category name filter for content search",
    )
    parser.add_argument(
        "--content-sort",
        choices=["relevance", "downloads", "follows", "updated"],
        default="relevance",
        help="Search order (default: relevance)",
    )
    parser.add_argument(
        "--content-limit",
        type=int,
        default=20,
        help="Search result count, 1-50 (default: 20)",
    )
    parser.add_argument(
        "--content-versions",
        metavar="PROJECT",
        help="List versions for a project slug/id (does not download)",
    )
    parser.add_argument(
        "--content-install",
        metavar="PROJECT",
        help="Download and install a project after confirmation (needs --game-dir)",
    )
    parser.add_argument(
        "--content-version",
        help="Exact version id or version number for --content-install",
    )
    parser.add_argument(
        "--content-channel",
        choices=["release", "latest"],
        default="release",
        help="Version channel preference when --content-version is omitted",
    )
    parser.add_argument(
        "--content-dir",
        help="Override the install directory (mods/resourcepacks/... by default)",
    )
    parser.add_argument(
        "--content-world",
        help="World folder for datapack installs (must contain level.dat)",
    )
    parser.add_argument(
        "--modpack-archive",
        metavar="PATH",
        help=(
            "Install a local .mrpack/ZIP containing modrinth.index.json as an "
            "isolated, launchable version"
        ),
    )
    parser.add_argument(
        "--modpack-name",
        help="Override the instance name for --modpack-archive",
    )
    parser.add_argument(
        "--modpack-no-launch",
        action="store_true",
        help=(
            "Only unpack modpack content; do not install the game/loader or "
            "register a launchable version"
        ),
    )
    parser.add_argument(
        "--curseforge-key",
        help=(
            "CurseForge API key; prefer saving it in the UI/config.json. "
            "The value is not logged or included in the release."
        ),
    )
    return parser


def _redact_command(command: list[str]) -> list[str]:
    result: list[str] = []
    hide = False
    for part in command:
        if hide:
            result.append("<redacted>")
            hide = False
            continue
        if part == "--accessToken":
            result.append(part)
            hide = True
            continue
        if part.startswith("--accessToken="):
            result.append("--accessToken=<redacted>")
            continue
        result.append(part)
    return result


def _redact_command_line(text: str) -> str:
    return re.sub(r"(--accessToken(?:=|\s+))(\S+)", r"\1<redacted>", text)


def _cli_account(args: argparse.Namespace) -> MicrosoftAccount | None:
    if getattr(args, "offline", False):
        return None
    try:
        return load_saved_account()
    except LauncherError as exc:
        print(f"警告：{exc}；将使用离线账户。", file=sys.stderr)
        return None


def _doctor(args: argparse.Namespace) -> int:
    if not args.game_dir:
        print("ERROR: --game-dir is required with --list-versions or --dry-run", file=sys.stderr)
        return 2
    try:
        root = normalize_game_root(args.game_dir)
        versions = list_versions(root)
        if args.list_versions:
            if not versions:
                print("No installed versions found.")
                return 1
            if args.details:
                print(format_version_table(list_version_details(root)))
                return 0
            for version in versions:
                jar = "jar" if version.jar_path else "inherited"
                print(
                    f"{version.version_id}\t{version.version_type or '-'}\t"
                    f"{version.folder_name}\t{jar}"
                )
            return 0

        summary = _choose_version(versions, args.game_version)
        loaded = load_merged_version(root, summary)
        required = required_java_major(loaded.data)
        java_path = Path(args.java) if args.java else None
        if java_path is None:
            runtime = pick_java(discover_java(root.root), required, root.root)
            java_path = runtime.path if runtime else Path("java")

        account = _cli_account(args)
        if account is not None and account.expires_at <= time.time():
            print(
                "警告：保存的微软登录已过期；图形界面启动时会尝试刷新，"
                "dry-run 仅使用当前令牌。",
                file=sys.stderr,
            )
        isolated = args.isolated
        if isolated is None:
            isolated = has_isolated_game_data(summary)
        uses_owned_natives = args.natives_dir is None
        natives_dir = Path(args.natives_dir) if args.natives_dir else Path.cwd() / "build" / "dry-run-natives"
        plan = build_launch_plan(
            root,
            summary,
            LaunchOptions(
                username=args.username,
                java_path=java_path,
                memory_mb=args.memory,
                isolated=isolated,
                natives_dir=natives_dir,
                check_writable=False,
                account=account,
            ),
        )
        payload = {
            "version": summary.version_id,
            "version_type": summary.version_type,
            "game_dir": str(plan.game_dir),
            "assets_dir": str(plan.assets_dir),
            "java": str(plan.java_path),
            "required_java": required,
            "isolated": isolated,
            "account": "microsoft" if account is not None else "offline",
            "warnings": plan.warnings,
            "command": _redact_command(plan.command),
            "command_line": _redact_command_line(plan.command_line),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if uses_owned_natives:
            shutil.rmtree(plan.natives_dir, ignore_errors=True)
        else:
            cleanup_plan(plan)
        return 0
    except LauncherError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _confirm(prompt: str, assume_yes: bool) -> bool | None:
    """Return True/False for a yes/no answer, or None when --yes is required."""
    if assume_yes:
        return True
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return None
    except (AttributeError, ValueError):
        return None
    try:
        answer = input(f"{prompt} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _remove_installed_version(args: argparse.Namespace) -> int:
    if not args.game_dir:
        print("ERROR: --game-dir and --remove-version are required", file=sys.stderr)
        return 2
    try:
        root = normalize_game_root(args.game_dir)
        summary = next(
            (
                item
                for item in list_versions(root)
                if args.remove_version in (item.version_id, item.folder_name)
            ),
            None,
        )
        if summary is not None:
            details = version_details(root, summary)
            description = (
                f"{details.folder_name}（{details.summary.version_type or '未知类型'}，"
                f"加载器 {details.loader}，{human_size(details.size_bytes)}，"
                f"{details.file_count} 个文件）"
            )
            children = [
                item.folder_name
                for item in list_version_details(root)
                if item.folder_name != details.folder_name
                and item.inherits_from in (details.version_id, details.folder_name)
            ]
            if children:
                print(
                    "警告：以下版本继承自它，删除后可能无法启动：\n  "
                    + "、".join(children[:10])
                )
        else:
            description = args.remove_version
        answer = _confirm(f"确定删除版本 {description}？", args.yes)
        if answer is None:
            print(
                "ERROR: 非交互环境请在确认后重新执行并加 --yes。",
                file=sys.stderr,
            )
            return 2
        if not answer:
            print("已取消。")
            return 130
        result = remove_version(root, args.remove_version)
        print(
            f"已删除 versions/{result.folder_name}，"
            f"释放 {human_size(result.freed_bytes)}（{result.file_count} 个文件）。"
        )
        print("共享的 libraries/assets/resources 和存档未受影响。")
        return 0
    except LauncherError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _cache_info(args: argparse.Namespace) -> int:
    root = None
    if args.game_dir:
        try:
            root = normalize_game_root(args.game_dir)
        except LauncherError as exc:
            print(f"提示：游戏目录不可用，跳过 *.part 统计：{exc}", file=sys.stderr)
    usages = scan_cache(root)
    print(format_cache_usage(usages))
    if root is None:
        print("提示：未提供有效的 --game-dir，未统计游戏目录内的 *.part 分片。")
    return 0


def _clean_cache_category(args: argparse.Namespace) -> int:
    root = None
    if args.game_dir:
        try:
            root = normalize_game_root(args.game_dir)
        except LauncherError as exc:
            if "partial" in (args.clean_cache or ""):
                print(f"ERROR: 清理分片需要有效的游戏目录：{exc}", file=sys.stderr)
                return 1
            print(f"提示：游戏目录不可用，跳过 *.part 统计：{exc}", file=sys.stderr)
    label = args.clean_cache or "all"
    answer = _confirm(f"确定清理缓存类别 {label}？", args.yes)
    if answer is None:
        print("ERROR: 非交互环境请在确认后重新执行并加 --yes。", file=sys.stderr)
        return 2
    if not answer:
        print("已取消。")
        return 130
    try:
        report = clean_cache(label, root, logger=_log)
    except LauncherError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(format_clean_report(report))
    return 0 if report.ok else 1


def _print_progress(progress) -> None:
    if progress.total_bytes:
        percent = min(100.0, progress.done_bytes * 100.0 / progress.total_bytes)
        prefix = f"\r{progress.phase:12s} {percent:5.1f}%"
    else:
        prefix = f"\r{progress.phase:12s}"
    text = (
        f"{prefix}  {human_size(progress.done_bytes)} / "
        f"{human_size(progress.total_bytes)}  "
        f"{progress.done_files}/{progress.total_files}  "
        f"{progress.label}  {progress.current[:50]}"
    )
    print(text[:160].ljust(150), end="", flush=True)


def _log(message: str, level: str = "info") -> None:
    if level == "muted":
        return
    print(f"[{level}] {message}", flush=True)


def _list_remote(args: argparse.Namespace) -> int:
    source = get_source(args.source)
    client = HttpClient(
        source.allowed_hosts,
        strict_redirects=source.strict_redirects,
        logger=lambda message: None,
    )
    try:
        manifest = load_manifest(source, client=client, force=True)
    except LauncherError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"下载源：{source.name}")
    print(
        f"最新正式版：{manifest.latest_release or '-'}    "
        f"最新快照：{manifest.latest_snapshot or '-'}    "
        f"共 {len(manifest.versions)} 个版本"
    )
    print(f"{'版本':<28} {'类型':<10} {'发布日期':<12}")
    for version in filter_versions(manifest.versions, args.remote_type):
        print(f"{version.id:<28} {version.type_label:<10} {version.date_text:<12}")
    return 0


def _verify_installed(args: argparse.Namespace) -> int:
    if not args.game_dir or not args.verify:
        print("ERROR: --game-dir and --verify are required", file=sys.stderr)
        return 2
    try:
        root = normalize_game_root(args.game_dir)
        source = get_source(args.source)
        plan = build_local_plan(root, args.verify, source, logger=_log)
        report = verify_plan(plan, deep=not args.fast)
        print(format_verify_report(report))
        if plan.warnings:
            print("提示：")
            for item in plan.warnings:
                print(f"  - {item}")
        return 0 if report.healthy else 1
    except LauncherError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _install(args: argparse.Namespace) -> int:
    if not args.game_dir or not args.install:
        print("ERROR: --game-dir and --install are required", file=sys.stderr)
        return 2
    from .core.loaders import install_selected

    try:
        root = ensure_game_root(args.game_dir)
        source = get_source(args.source)
        client = HttpClient(
            loader_allowed_hosts(source),
            strict_redirects=False,
            logger=lambda message: None,
            segments=max(0, min(16, args.segments)),
        )
        java_path = Path(args.java) if args.java else None
        print(
            f"开始安装：Minecraft {args.install} / {args.loader} "
            f"/ 源 {source.name}（文件并发 {max(1, min(12, args.workers))}，"
            f"大文件分段 {max(0, min(16, args.segments))}）",
            flush=True,
        )
        outcome = install_selected(
            root,
            source,
            args.install,
            loader=args.loader,
            loader_version=args.loader_version,
            loader_channel=args.loader_channel,
            java_path=java_path,
            client=client,
            logger=_log,
            progress=_print_progress,
            deep=not args.fast,
            workers=max(1, min(12, args.workers)),
        )
        print()
        print(format_verify_report(outcome.verify))
        print(
            f"本次下载 {outcome.downloaded_files} 个文件 / "
            f"{human_size(outcome.downloaded_bytes)}，"
            f"跳过 {outcome.skipped_files} 个已完整文件。"
        )
        if outcome.warnings:
            print("提示：")
            for item in outcome.warnings:
                print(f"  - {item}")
        return 0 if outcome.healthy else 1
    except CancelledError:
        print("\n已取消。", file=sys.stderr)
        return 130
    except LauncherError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1


def _java_root_for_args(args: argparse.Namespace) -> Path:
    if args.java_dir:
        return validate_install_root(args.java_dir)
    game_root: Path | None = None
    if args.game_dir:
        try:
            game_root = normalize_game_root(args.game_dir).root
        except LauncherError:
            game_root = Path(args.game_dir).expanduser()
    return validate_install_root(default_install_root(game_root))


def _required_java_for_args(args: argparse.Namespace) -> int | None:
    if args.required_java is not None:
        return args.required_java
    if not args.game_dir:
        return None
    try:
        root = normalize_game_root(args.game_dir)
        summary = _choose_version(list_versions(root), args.game_version)
        return required_java_major(load_merged_version(root, summary).data)
    except LauncherError:
        return None


def _java_info(args: argparse.Namespace) -> int:
    required = _required_java_for_args(args)
    print(f"平台：{default_platform()}")
    if required is None:
        print("需要的 Java：未指定（可用 --game-version 或 --required-java 指定）")
    else:
        print(f"需要的 Java：Java {required}")

    game_root = None
    if args.game_dir:
        try:
            game_root = normalize_game_root(args.game_dir)
        except LauncherError as exc:
            print(f"提示：游戏目录不可用，只检查系统 Java：{exc}", file=sys.stderr)
    runtimes = discover_java(game_root.root if game_root is not None else Path.cwd())
    if not runtimes:
        print("检测到的 Java：未检测到")
    else:
        print(f"检测到的 Java：{len(runtimes)} 个")
        for item in runtimes:
            print(f"  - Java {item.major}（{item.version}）：{item.path}")
        if required is not None and not any(item.major == required for item in runtimes):
            print(f"  ! 没有与 Java {required} 匹配的系统 Java")

    try:
        java_root = _java_root_for_args(args)
        installed = scan_installed_runtimes([java_root])
    except LauncherError as exc:
        print(f"安装位置不可用：{exc}", file=sys.stderr)
        return 1
    print(f"启动器 Java 安装位置：{java_root}")
    if installed:
        print(f"已安装的启动器运行时：{len(installed)} 个")
        for item in installed:
            tag = "已记录" if item.managed else "未记录"
            print(
                f"  - {item.component_id}：{item.version or '未知版本'}，"
                f"{human_size(item.size_bytes)}，{tag}，{item.java_path}"
            )
    else:
        print("已安装的启动器运行时：无")

    if required is not None and not any(
        item.major == required for item in runtimes
    ) and not any(item.java_major == required for item in installed):
        print(
            "\n没有可用的 Java。可以：\n"
            f"  - 运行：MinecraftLauncher --install-java {required} "
            f"--java-dir \"{java_root}\"（会先询问确认，脚本里加 --yes）\n"
            "  - 或运行：MinecraftLauncher --java-urls 查看自行下载的官方/发行版网址\n"
            "  - 或在图形界面点击 Java 一栏的“获取 Java…”"
        )
    return 0


def _list_java(args: argparse.Namespace) -> int:
    source = get_source(args.source)
    client = HttpClient(
        source.allowed_hosts,
        strict_redirects=source.strict_redirects,
        logger=lambda message: None,
    )
    try:
        catalog = fetch_catalog(source, client=client, logger=_log)
    except LauncherError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(f"也可以手动访问：{manual_java_links()[0].url}", file=sys.stderr)
        return 1
    components = catalog.components(default_platform())
    required = _required_java_for_args(args)
    print(f"下载源：{source.name}    平台：{default_platform()}")
    print(format_component_table(components, required))
    print(
        "\n以上只是组件列表，尚未下载任何文件。"
        "\n使用 --install-java <组件或 Java 大版本> 安装；会先显示文件数和大小并请求确认。"
    )
    return 0


def _install_java(args: argparse.Namespace) -> int:
    source = get_source(args.source)
    required = _required_java_for_args(args)
    try:
        java_root = _java_root_for_args(args)
    except LauncherError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    client = HttpClient(
        source.allowed_hosts,
        strict_redirects=source.strict_redirects,
        logger=lambda message: None,
        segments=max(0, min(8, args.segments)),
    )
    try:
        catalog = fetch_catalog(source, client=client, logger=_log)
        component = resolve_component(
            catalog, args.install_java, required_major=required
        )
        print(
            f"Java 组件：{component.id}（Java {component.java_major}，"
            f"{component.version}，{component.platform}）"
        )
        manifest = fetch_component_manifest(component, source, client=client, logger=_log)
        plan = build_install_plan(component, manifest, java_root, source)
        print(
            f"文件：{manifest.file_count} 个，下载约 {human_size(manifest.total_bytes)}"
        )
        print(f"来源：{source.name}")
        print(f"安装到：{plan.install_dir}")
        if args.dry_run:
            print("--dry-run：未下载，也没有写入任何文件。")
            return 0
        answer = _confirm(
            f"确认下载并安装到 {plan.install_dir}（约 {human_size(manifest.total_bytes)}）？",
            args.yes,
        )
        if answer is None:
            print(
                "ERROR: 非交互环境请在确认后重新执行并加 --yes。",
                file=sys.stderr,
            )
            return 2
        if not answer:
            print("已取消，没有下载。")
            return 130
        outcome = install_java_runtime(
            plan,
            client=client,
            confirmed=True,
            workers=max(1, min(8, args.workers)),
            progress=_print_progress,
            log=_log,
        )
        print()
        print(
            f"安装完成：{plan.install_dir}\n"
            f"下载 {outcome.downloaded_files} 个文件 / "
            f"{human_size(outcome.downloaded_bytes)}，"
            f"跳过 {outcome.skipped_files} 个完整文件。"
        )
        if outcome.runtime is None:
            print(
                "ERROR: 文件已安装，但 java.exe 无法运行。请检查安全软件或改用其他目录。",
                file=sys.stderr,
            )
            return 1
        print(f"Java：{outcome.runtime.version}")
        print(f"java.exe：{outcome.java_path}")
        print(f"启动时可以加：--java \"{outcome.java_path}\"")
        return 0
    except CancelledError:
        print("\n已取消；已完成的文件与未完成分片会保留。", file=sys.stderr)
        return 130
    except LauncherError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1


def _verify_java(args: argparse.Namespace) -> int:
    try:
        java_root = _java_root_for_args(args)
    except LauncherError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    installed = scan_installed_runtimes([java_root])
    target = next(
        (
            item
            for item in installed
            if args.verify_java
            in (item.component_id, item.install_dir.name)
        ),
        None,
    )
    if target is None:
        print(
            f"ERROR: 在 {java_root} 找不到组件 {args.verify_java}。",
            file=sys.stderr,
        )
        if installed:
            print(
                "已安装：" + "、".join(item.component_id for item in installed),
                file=sys.stderr,
            )
        return 1
    report = verify_java_runtime(target.install_dir)
    print(format_runtime_report(report))
    return 0 if report.healthy else 1


def _java_urls(args: argparse.Namespace) -> int:
    required = _required_java_for_args(args)
    major = required or args.required_java or 17
    print(f"Java 大版本：{major}（仅打印网址，不下载、不修改任何文件）\n")
    for link in manual_java_links(major):
        print(f"{link.label}\n  {link.url}")
        if link.note:
            print(f"  说明：{link.note}")
        print()
    print(
        "提示：下载 Windows x64 的 JDK/JRE zip 后解压，在启动器 Java 一栏"
        "点击“浏览…”选择 bin\\java.exe；"
        "或使用 --java \"<解压目录>\\bin\\java.exe\"。"
    )
    return 0


def _content_key(args: argparse.Namespace) -> str:
    if getattr(args, "curseforge_key", None):
        return str(args.curseforge_key).strip()
    path = app_data_dir() / "config.json"
    try:
        with path.open("r", encoding="utf-8-sig") as file:
            data = json.load(file)
        if isinstance(data, dict):
            return str(data.get("curseforge_api_key", "")).strip()
    except (OSError, json.JSONDecodeError):
        pass
    return ""


def _content_source(args: argparse.Namespace):
    return get_content_source(
        args.content_source,
        api_key=_content_key(args),
        logger=lambda message: print(f"  {message}", file=sys.stderr),
        segments=max(0, min(8, args.segments)),
    )


def _content_project(source, name: str, content_type: str):
    try:
        return source.project(name)
    except LauncherError:
        result = source.search(name, content_type, limit=10)
        for project in result.projects:
            if project.id == name or project.slug == name:
                return project
        if result.projects:
            return result.projects[0]
        raise ContentError(f"找不到项目：{name}")


def _content_search(args: argparse.Namespace) -> int:
    try:
        source = _content_source(args)
        result = source.search(
            args.content_search or "",
            args.content_type,
            game_version=args.content_game_version or "",
            loader=args.content_loader or "",
            category=args.content_category or "",
            sort=args.content_sort,
            limit=max(1, min(50, args.content_limit)),
        )
    except LauncherError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(format_search_result(result))
    return 0


def _content_versions(args: argparse.Namespace) -> int:
    try:
        source = _content_source(args)
        project = _content_project(source, args.content_versions, args.content_type)
        versions = source.versions(
            project.id,
            game_version=args.content_game_version or "",
            loader=args.content_loader or "",
        )
    except LauncherError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(format_versions(project, versions))
    if versions:
        best = select_best_version(
            versions,
            game_version=args.content_game_version or "",
            loader=args.content_loader or "",
            channel=args.content_channel,
        )
        if best is not None:
            print(f"\n默认选择：{best.version_number} [{best.type_label}]")
    return 0


def _progress_line(done: int, total: int, current: str = "") -> None:
    if total > 0:
        percent = min(100.0, done * 100.0 / total)
        suffix = f" {current}" if current else ""
        print(
            f"\r  {percent:5.1f}%  {human_size(done)} / {human_size(total)}{suffix}",
            end="",
            flush=True,
        )
    else:
        print(f"\r  {human_size(done)}", end="", flush=True)


def _install_modpack_archive(args: argparse.Namespace) -> int:
    if not args.game_dir:
        print("ERROR: --game-dir is required with --modpack-archive", file=sys.stderr)
        return 2
    archive = Path(args.modpack_archive)
    if not archive.is_file():
        print(f"ERROR: 压缩包不存在：{archive}", file=sys.stderr)
        return 2
    try:
        index = read_mrpack(archive)
        dependencies = resolve_modpack_dependencies(index)
        game_root = game_root_path(args.game_dir)
        plan = build_instance_plan(
            index,
            game_root,
            slug=args.modpack_name or "",
            overwrite=bool(args.yes),
            launch=not args.modpack_no_launch,
        )
    except LauncherError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(format_instance_plan(plan))
    print(
        f"压缩包：{archive}\n"
        f"内容文件：{len(index.files)} 个，约 "
        f"{human_size(sum(item.size or 0 for item in index.files))}"
    )
    if args.dry_run:
        print("--dry-run：只打印计划，不读取/解压内容，也不下载。")
        return 0
    answer = _confirm("确认安装这个整合包压缩包？", args.yes)
    if answer is None:
        print(
            "非交互环境需要 --yes 才会安装（这是有意的确认要求）。",
            file=sys.stderr,
        )
        return 2
    if not answer:
        print("已取消。")
        return 1
    try:
        content_source = get_content_source(
            SOURCE_MODRINTH, segments=max(0, min(8, args.segments))
        )
        outcome = install_modpack_archive(
            archive,
            game_root,
            slug=args.modpack_name or "",
            overwrite=bool(args.yes),
            launch=not args.modpack_no_launch,
            confirmed=True,
            client=content_source.client,  # type: ignore[attr-defined]
            source=get_source(args.source),
            java_path=Path(args.java) if args.java else None,
            progress=lambda item: _progress_line(
                item.downloaded, item.total, item.current
            ),
            logger=lambda message: print(f"  {message}", file=sys.stderr),
        )
    except (LauncherError, OSError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    print(f"\n{outcome.message}")
    for warning in outcome.warnings:
        print(f"注意：{warning}", file=sys.stderr)
    if outcome.launchable:
        print(
            f"已创建隔离版本 {outcome.version_id}；启动命令："
            f'launcher.py --game-dir "{game_root}" --game-version {outcome.version_id}'
        )
    return 0


def _content_install(args: argparse.Namespace) -> int:
    if not args.game_dir:
        print("ERROR: --game-dir is required with --content-install", file=sys.stderr)
        return 2
    content_type = args.content_type
    try:
        source = _content_source(args)
        project = _content_project(source, args.content_install, content_type)
        versions = source.versions(
            project.id,
            game_version=args.content_game_version or "",
            loader=args.content_loader or "",
        )
        if not versions:
            raise ContentError(
                f"{project.title} 没有匹配的版本"
                f"（游戏 {args.content_game_version or '全部'} / "
                f"加载器 {args.content_loader or '全部'}）。"
            )
        version = None
        if args.content_version:
            version = next(
                (
                    item
                    for item in versions
                    if args.content_version in (item.id, item.version_number)
                ),
                None,
            )
            if version is None:
                raise ContentError(
                    f"找不到版本 {args.content_version}；可用："
                    + "、".join(item.version_number for item in versions[:10])
                )
        else:
            version = select_best_version(
                versions,
                game_version=args.content_game_version or "",
                loader=args.content_loader or "",
                channel=args.content_channel,
            )
        if version is None:
            raise ContentError("没有可安装的版本。")
        file = version.primary_file
        if file is None:
            raise ContentError("该版本没有可下载文件。")
        root = ensure_game_root(args.game_dir)
        world_dir = None
        if content_type == "datapack":
            if not args.content_world:
                raise ContentError(
                    "安装数据包需要 --content-world <世界文件夹>（包含 level.dat）。"
                )
            world_dir = validate_world_dir(Path(args.content_world))
    except LauncherError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(
        f"项目：{project.title}（{content_type_label(content_type)}）\n"
        f"版本：{version.version_number} [{version.type_label}]\n"
        f"文件：{file.filename}（{human_size(file.size)}）\n"
        f"来源：{args.content_source}\n"
        f"游戏目录：{root.root}"
    )
    if content_type == "modpack":
        print(
            f"目标：{root.root / 'versions' / '<实例>'}（版本目录即隔离游戏目录）\n"
            f"加载器：以 .mrpack 的 dependencies 为准；NeoForge/Quilt 仅整理内容"
        )
        if args.dry_run:
            print("--dry-run：只打印计划，不下载。")
            return 0
        answer = _confirm("确认下载并安装整合包（含游戏和加载器）？", args.yes)
        if answer is None:
            print(
                "非交互环境需要 --yes 才会下载（这是有意的确认要求）。",
                file=sys.stderr,
            )
            return 2
        if not answer:
            print("已取消。")
            return 1
        try:
            outcome = download_and_install_launchable_modpack(
                project,
                version,
                file,
                root.root,
                slug=project.slug,
                launch=not args.modpack_no_launch,
                source=get_source(args.source),
                client=source.client,  # type: ignore[attr-defined]
                java_path=Path(args.java) if args.java else None,
                confirmed=True,
                progress=lambda item: _progress_line(
                    item.downloaded, item.total, item.current
                ),
                logger=lambda message: print(f"  {message}", file=sys.stderr),
            )
        except (LauncherError, OSError) as exc:
            print(f"\nERROR: {exc}", file=sys.stderr)
            return 1
        print(f"\n{outcome.message}")
        for warning in outcome.warnings:
            print(f"注意：{warning}", file=sys.stderr)
        if outcome.launchable:
            print(
                f"已创建隔离版本 {outcome.version_id}；启动命令："
                f'launcher.py --game-dir "{root.root}" --game-version {outcome.version_id}'
            )
        return 0

    try:
        target = (
            Path(args.content_dir)
            if args.content_dir
            else default_target_dir(root.root, content_type, world_dir)
        )
        plan = build_content_plan(
            project,
            version,
            file,
            root.root,
            content_type,
            target_dir=target,
            world_dir=world_dir,
        )
        if args.dry_run:
            print(
                f"目标：{plan.dest}\n"
                f"SHA-1：{plan.file.sha1}\n"
                f"大小：{human_size(plan.size)}\n"
                "--dry-run：只打印计划，不下载。"
            )
            return 0
        if plan.dest.exists():
            answer = _confirm(
                f"目标文件已存在，是否覆盖？\n  {plan.dest}", args.yes
            )
            if answer is None:
                print(
                    "非交互环境需要 --yes 才会覆盖已有文件。", file=sys.stderr
                )
                return 2
            if not answer:
                print("已取消。")
                return 1
            plan = build_content_plan(
                project,
                version,
                file,
                root.root,
                content_type,
                target_dir=target,
                world_dir=world_dir,
                overwrite=True,
            )
    except LauncherError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(
        f"目标：{plan.dest}\n"
        f"SHA-1：{plan.file.sha1}\n"
        f"大小：{human_size(plan.size)}"
    )
    answer = _confirm("确认下载并安装？", args.yes)
    if answer is None:
        print(
            "非交互环境需要 --yes 才会下载（这是有意的确认要求）。",
            file=sys.stderr,
        )
        return 2
    if not answer:
        print("已取消。")
        return 1
    try:
        outcome = install_content(
            plan,
            client=source.client,  # type: ignore[attr-defined]
            confirmed=True,
            progress=lambda value: _progress_line(value, plan.size, plan.filename),
            logger=lambda message: print(f"  {message}", file=sys.stderr),
        )
    except (LauncherError, OSError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    print()
    print(outcome.message)
    return 0


def _login_microsoft(args: argparse.Namespace) -> int:
    client_id = resolve_client_id(getattr(args, "ms_client_id", None))
    if not client_id:
        print(
            "ERROR: 缺少 Azure client ID。请先注册 Entra ID 应用，"
            "然后传入 --ms-client-id <Application (client) ID>。",
            file=sys.stderr,
        )
        return 2
    try:
        client_id = validate_client_id(client_id)
    except AuthError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    def on_code(code) -> None:
        print("请在浏览器打开：", code.verification_uri)
        print("输入代码：", code.user_code)
        if code.message:
            print(code.message)
        print("等待微软登录完成…（Ctrl+C 可取消）\n")

    try:
        account = login_device_code(
            client_id,
            HttpAuthTransport(),
            on_code=on_code,
        )
    except KeyboardInterrupt:
        print("\n已取消微软登录。", file=sys.stderr)
        return 1
    except AuthError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except LauncherError as exc:
        print(f"ERROR: 微软登录失败：{exc}", file=sys.stderr)
        return 1

    try:
        save_account(account)
        save_client_id(client_id)
    except (LauncherError, OSError) as exc:
        print(f"警告：登录成功，但无法保存令牌：{exc}", file=sys.stderr)
        print(
            "本次运行仍可使用该账户；下次启动需要重新登录。",
            file=sys.stderr,
        )
    print(f"登录成功：{account.name}（{account.uuid}）")
    print("令牌已使用 Windows DPAPI 加密保存；不会写入日志。")
    return 0


def _logout_microsoft(_args: argparse.Namespace) -> int:
    try:
        clear_saved_account()
    except LauncherError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("已删除保存的微软登录令牌。")
    return 0


def _account_info(_args: argparse.Namespace) -> int:
    try:
        account = load_saved_account()
    except LauncherError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if account is None:
        print("当前未登录微软账户（使用离线账户）。")
        return 1
    remaining = int(account.expires_at - time.time())
    status = "有效" if remaining > 0 else "已过期"
    print(f"账户：{account.name}")
    print(f"UUID：{account.uuid}")
    print(f"令牌状态：{status}")
    if remaining > 0:
        print(f"剩余时间：约 {remaining // 3600} 小时 {remaining % 3600 // 60} 分钟")
    print(f"Azure client ID：{account.client_id or '（未记录）'}")
    print("令牌内容不会显示。")
    return 0


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    if args.login_microsoft:
        return _login_microsoft(args)
    if args.logout_microsoft:
        return _logout_microsoft(args)
    if args.account_info:
        return _account_info(args)
    if args.content_search is not None:
        return _content_search(args)
    if args.content_versions:
        return _content_versions(args)
    if args.modpack_archive:
        return _install_modpack_archive(args)
    if args.content_install:
        return _content_install(args)
    if args.java_urls:
        return _java_urls(args)
    if args.java_info:
        return _java_info(args)
    if args.list_java:
        return _list_java(args)
    if args.install_java:
        return _install_java(args)
    if args.verify_java:
        return _verify_java(args)
    if args.cache_info:
        return _cache_info(args)
    if args.clean_cache:
        return _clean_cache_category(args)
    if args.remove_version:
        return _remove_installed_version(args)
    if args.list_remote:
        return _list_remote(args)
    if args.install:
        return _install(args)
    if args.verify:
        return _verify_installed(args)
    if args.list_versions or args.dry_run:
        return _doctor(args)
    if args.game_dir:
        print(
            "Notice: --game-dir is only used with --list-versions, --dry-run, "
            "--install or --verify. Opening the UI.",
            file=sys.stderr,
        )
    try:
        from .app import main as ui_main
    except ImportError as exc:
        print(f"ERROR: Tkinter is not available: {exc}", file=sys.stderr)
        return 1
    ui_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
