"""Manual, network-dependent checks for the download/repair pipeline.

This is not collected by ``unittest discover`` (the file name does not start
with ``test_``).  It exercises the real Mojang/BMCLAPI endpoints against a
throw-away game directory:

    py tests/manual_download_check.py --game-dir F:\\tmp\\mc --version 1.6.4 install
    py tests/manual_download_check.py --game-dir F:\\tmp\\mc --version 26.3 resume --cancel-mb 30
    py tests/manual_download_check.py --game-dir F:\\tmp\\mc --version 1.6.4 repair
    py tests/manual_download_check.py --game-dir F:\\game\\minecraft\\mc\\.minecraft --version 1.20.1 verify

Exit code 0 means the scenario passed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import threading
import time
from pathlib import Path

# Allow running the file directly from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mc_launcher.core.install import (  # noqa: E402
    build_local_plan,
    format_verify_report,
    verify_plan,
)
from mc_launcher.core.loaders import install_selected  # noqa: E402
from mc_launcher.core.mirrors import get_source  # noqa: E402
from mc_launcher.core.net import CancelledError, HttpClient, human_size  # noqa: E402
from mc_launcher.core.paths import ensure_game_root  # noqa: E402


def _client(source, timeout: float = 20.0, logger=None) -> HttpClient:
    return HttpClient(
        source.allowed_hosts,
        strict_redirects=source.strict_redirects,
        timeout=timeout,
        retries=1,
        logger=logger or (lambda message: None),
    )


def _log(message: str, level: str = "info") -> None:
    if level == "muted":
        return
    print(f"[{level}] {message}", flush=True)


def _progress_reporter(phase_state: dict):
    last = {"emit": 0.0}

    def report(progress) -> None:
        phase_state["phase"] = progress.phase
        if progress.phase == "download":
            phase_state["downloaded"] = progress.done_bytes
        now = time.monotonic()
        if now - last["emit"] < 0.5:
            return
        last["emit"] = now
        if progress.total_bytes:
            percent = progress.done_bytes * 100.0 / progress.total_bytes
            text = (
                f"{progress.phase:12s} {percent:5.1f}%  "
                f"{progress.done_files}/{progress.total_files}  "
                f"{human_size(progress.done_bytes)}/{human_size(progress.total_bytes)}  "
                f"{progress.label}  {progress.current[:60]}"
            )
        else:
            text = f"{progress.phase:12s} {progress.done_files}/{progress.total_files}"
        print(f"\r{text[:150]:<150}", end="", flush=True)

    return report


def _install(args, root, source, cancel: threading.Event | None = None):
    logs: list[str] = []
    phase_state: dict = {}

    def logger(message: str, level: str = "info") -> None:
        logs.append(f"{level}: {message}")
        if level in ("warning", "error", "success", "command"):
            _log(message, level)

    http = _client(source, logger=lambda message: logs.append(f"network: {message}"))

    started = time.monotonic()
    outcome = install_selected(
        root,
        source,
        args.version,
        loader="vanilla",
        client=http,
        logger=logger,
        progress=_progress_reporter(phase_state),
        cancel=cancel,
        deep=True,
        workers=max(1, args.workers),
    )
    print()
    elapsed = time.monotonic() - started
    print(format_verify_report(outcome.verify))
    print(
        f"耗时 {elapsed:.1f}s；下载 {outcome.downloaded_files} 个文件 / "
        f"{human_size(outcome.downloaded_bytes)}；跳过 {outcome.skipped_files} 个完整文件。"
    )
    return outcome, logs


def _parts(root) -> list[Path]:
    return list(root.root.rglob("*.part"))


def _fresh(game_dir: Path) -> Path:
    target = Path(game_dir)
    if target.exists():
        print(f"删除旧测试目录：{target}", flush=True)
        shutil.rmtree(target)
    return ensure_game_root(target)


def scenario_install(args) -> int:
    source = get_source(args.source)
    root = _fresh(args.game_dir)
    outcome, _logs = _install(args, root, source)
    return 0 if outcome.healthy else 1


def scenario_verify(args) -> int:
    source = get_source(args.source)
    root = ensure_game_root(args.game_dir)
    plan = build_local_plan(root, args.version, source, logger=_log)
    report = verify_plan(plan, deep=True)
    print(format_verify_report(report))
    return 0 if report.healthy else 1


def _choose_victims(plan):
    assets = [item for item in plan.files if item.category == "asset" and item.sha1]
    library = next(
        (item for item in plan.files if item.category == "library" and item.sha1),
        None,
    )
    client = next(
        (item for item in plan.files if item.category == "client" and item.sha1),
        None,
    )
    victims = assets[:2]
    if library is not None:
        victims.append(library)
    return victims, client


def scenario_repair(args) -> int:
    source = get_source(args.source)
    root = ensure_game_root(args.game_dir)
    plan = build_local_plan(root, args.version, source, logger=_log)
    victims, client = _choose_victims(plan)
    if len(victims) < 2 or client is None:
        print("没有足够的文件用于修复测试。", file=sys.stderr)
        return 2
    for item in victims:
        path = item.destination(root)
        if path.exists():
            path.unlink()
            print(f"已删除：{item.relative.as_posix()}")
    client_path = client.destination(root)
    if client_path.is_file():
        size = client_path.stat().st_size
        # Same size, different content: only a deep hash check finds this.
        with client_path.open("r+b") as handle:
            handle.seek(size // 2)
            handle.write(b"\x00" * 1024)
        print(f"已损坏（大小不变）：{client.relative.as_posix()}")
    expected = len(victims) + 1
    outcome, logs = _install(args, root, source)
    resumed = sum(1 for line in logs if "继续下载未完成的文件" in line)
    print(f"重新下载文件数：{outcome.downloaded_files}（期望 {expected}）；续传提示：{resumed}")
    return 0 if outcome.healthy and outcome.downloaded_files == expected else 1


def scenario_resume(args) -> int:
    source = get_source(args.source)
    root = _fresh(args.game_dir)
    threshold = max(1, args.cancel_mb) * 1024 * 1024
    cancel = threading.Event()
    phase_state: dict = {}

    def progress(progress_obj) -> None:
        state_phase = phase_state.get("phase")
        if (
            progress_obj.phase == "download"
            and progress_obj.done_bytes >= threshold
            and state_phase != "cancel"
            and not cancel.is_set()
        ):
            state_phase = "cancel"
            phase_state["phase"] = state_phase
            cancel.set()
            print(
                f"\n下载到 {human_size(progress_obj.done_bytes)}，模拟中断。",
                flush=True,
            )

    http = _client(source)
    logs: list[str] = []

    def logger(message: str, level: str = "info") -> None:
        logs.append(f"{level}: {message}")
        if level in ("warning", "error", "success"):
            _log(message, level)

    try:
        install_selected(
            root,
            source,
            args.version,
            client=http,
            logger=logger,
            progress=progress,
            cancel=cancel,
            deep=True,
            workers=max(1, args.workers),
        )
    except CancelledError:
        pass
    parts = _parts(root)
    partial_bytes = sum(item.stat().st_size for item in parts)
    print(f"\n中断后剩余分片：{len(parts)} 个，共 {human_size(partial_bytes)}")
    if not parts:
        print("警告：没有保留任何分片，续传场景未覆盖。", file=sys.stderr)

    cancel2 = threading.Event()
    outcome, logs2 = _install(args, root, source, cancel=cancel2)
    resumed = sum(1 for line in logs2 if "继续下载未完成的文件" in line)
    print(f"续传提示：{resumed} 个文件；最终下载 {outcome.downloaded_files} 个文件。")
    return 0 if outcome.healthy and resumed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["install", "verify", "repair", "resume"])
    parser.add_argument("--game-dir", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source", choices=["bmclapi", "mojang"], default="bmclapi")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--cancel-mb", type=int, default=30)
    args = parser.parse_args()
    if args.mode == "install":
        return scenario_install(args)
    if args.mode == "verify":
        return scenario_verify(args)
    if args.mode == "repair":
        return scenario_repair(args)
    return scenario_resume(args)


if __name__ == "__main__":
    raise SystemExit(main())
