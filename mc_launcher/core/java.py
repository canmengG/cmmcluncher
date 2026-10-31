"""Find and identify Java runtimes available on the machine."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

from .models import JavaRuntime


def _java_name() -> str:
    return "java.exe" if os.name == "nt" else "java"


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def probe_java(path: str | Path, source: str = "") -> JavaRuntime | None:
    """Run ``java -version`` and parse its major version."""
    candidate = Path(path)
    if candidate.is_dir():
        candidate = candidate / "bin" / _java_name()
    try:
        candidate = candidate.resolve()
    except OSError:
        pass
    if not candidate.is_file():
        return None

    try:
        completed = subprocess.run(
            [str(candidate), "-version"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=8,
            creationflags=_creation_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    output = f"{completed.stdout}\n{completed.stderr}"
    match = re.search(r'version\s+"([^"]+)"', output)
    if not match:
        return None
    version = match.group(1)
    major_text = version.split(".", 1)[1] if version.startswith("1.") else version
    major_match = re.match(r"(\d+)", major_text)
    if not major_match:
        return None
    return JavaRuntime(path=candidate, version=version, major=int(major_match.group(1)), source=source)


def _add_candidate(result: list[Path], path: Path | None) -> None:
    if path is None:
        return
    try:
        resolved = path.resolve()
    except OSError:
        return
    if resolved not in result:
        result.append(resolved)


def _runtime_candidates(root: Path) -> Iterable[Path]:
    runtime_root = root / "runtime"
    if not runtime_root.is_dir():
        return []
    found: list[Path] = []
    for pattern in ("*/windows-x64/*/bin/java.exe", "*/windows-x86/*/bin/java.exe", "**/bin/java.exe"):
        for path in runtime_root.glob(pattern):
            if path.is_file():
                _add_candidate(found, path)
    return found


def _installed_candidates() -> Iterable[Path]:
    bases: list[Path] = []
    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        value = os.environ.get(env_name)
        if value:
            bases.append(Path(value))
    bases.extend([Path.home() / ".jdks", Path.home() / "scoop" / "apps"])

    vendor_names = (
        "Java",
        "Eclipse Adoptium",
        "Microsoft",
        "Zulu",
        "BellSoft",
        "Amazon Corretto",
        "Semeru",
        "OpenJDK",
        "jdk",
    )
    found: list[Path] = []
    for base in bases:
        if not base.is_dir():
            continue
        if (base / "bin" / "java.exe").is_file():
            _add_candidate(found, base / "bin" / "java.exe")
        for vendor in vendor_names:
            vendor_path = base / vendor
            if not vendor_path.is_dir():
                continue
            for child in vendor_path.iterdir():
                if child.is_dir() and (child / "bin" / "java.exe").is_file():
                    _add_candidate(found, child / "bin" / "java.exe")
    return found


def discover_java(root: Path) -> list[JavaRuntime]:
    """Return all distinct Java installations, probing each one."""
    candidates: list[Path] = []

    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        _add_candidate(candidates, Path(java_home) / "bin" / _java_name())
    which_java = shutil.which("java")
    if which_java:
        _add_candidate(candidates, Path(which_java))
    for path in _runtime_candidates(root):
        _add_candidate(candidates, path)
    for path in _installed_candidates():
        _add_candidate(candidates, path)

    runtimes: list[JavaRuntime] = []
    seen_versions: set[tuple[Path, str]] = set()
    for path in candidates[:20]:
        runtime = probe_java(path, source="discovered")
        if runtime is None:
            continue
        key = (runtime.path, runtime.version)
        if key in seen_versions:
            continue
        seen_versions.add(key)
        runtimes.append(runtime)
    runtimes.sort(key=lambda item: (item.major, item.version), reverse=True)
    return runtimes


def score_java(runtime: JavaRuntime, required_major: int | None, game_root: Path) -> int:
    score = 0
    if required_major is not None:
        if runtime.major == required_major:
            score += 100
        elif runtime.major > required_major:
            score -= 2 * (runtime.major - required_major)
        else:
            score -= 20 * (required_major - runtime.major)
    try:
        runtime.path.resolve().relative_to((game_root / "runtime").resolve())
        score += 5
    except (ValueError, OSError):
        pass
    return score


def pick_java(
    runtimes: Iterable[JavaRuntime],
    required_major: int | None = None,
    game_root: Path | None = None,
) -> JavaRuntime | None:
    runtimes = list(runtimes)
    if not runtimes:
        return None
    root = game_root or Path.cwd()
    return max(runtimes, key=lambda item: score_java(item, required_major, root))


def required_java_major(version_data: dict) -> int | None:
    java_version = version_data.get("javaVersion")
    if isinstance(java_version, dict):
        major = java_version.get("majorVersion")
        if isinstance(major, int):
            return major

    version_id = str(version_data.get("id") or "")
    match = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", version_id)
    if not match:
        return None
    major, minor = int(match.group(1)), int(match.group(2))
    patch = int(match.group(3) or 0)
    if major >= 26:
        return 21
    if major == 1:
        if minor >= 21:
            return 21
        if minor == 20 and patch >= 5:
            return 21
        if minor >= 18:
            return 17
        if minor == 17:
            return 16
        return 8
    return None


def java_for_version(
    runtimes: Iterable[JavaRuntime],
    version_data: dict,
    game_root: Path,
) -> JavaRuntime | None:
    return pick_java(runtimes, required_java_major(version_data), game_root)
