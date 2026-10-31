"""Filesystem name and hash validation.

Remote metadata is untrusted input.  Version ids, library paths, asset names
and hashes all become local paths, so they must never be able to escape the
selected game root or create invalid Windows file names.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from .errors import VersionError

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_INVALID_CHARS = set('<>:"/\\|?*')


def normalise_sha1(value: object, *, what: str = "SHA-1") -> str:
    if not isinstance(value, str):
        raise VersionError(f"{what} 缺失或格式错误。")
    text = value.strip().lower()
    if not _SHA1_RE.fullmatch(text):
        raise VersionError(f"{what} 格式错误：{value!r}")
    return text


def safe_component(value: object, *, what: str = "名称", max_length: int = 128) -> str:
    """Return a single safe path component, allowing spaces and dots.

    Minecraft snapshot ids contain spaces (for example ``1.14 Pre-Release 1``),
    so spaces must be allowed.  Separators, drive letters, control characters,
    reserved DOS device names and trailing dots/spaces are rejected.
    """
    if not isinstance(value, str):
        raise VersionError(f"{what} 必须是字符串。")
    text = value.strip()
    if not text or len(text) > max_length:
        raise VersionError(f"{what} 长度无效：{value!r}")
    if text in (".", ".."):
        raise VersionError(f"{what} 不能是 . 或 ..")
    if any(char in _INVALID_CHARS or ord(char) < 32 for char in text):
        raise VersionError(f"{what} 含有非法字符：{value!r}")
    if text[-1] in (" ", "."):
        raise VersionError(f"{what} 不能以空格或点结尾：{value!r}")
    if text.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        raise VersionError(f"{what} 使用了 Windows 保留名称：{value!r}")
    return text


def safe_relative_path(value: object, *, what: str = "文件路径") -> Path:
    """Validate a remote relative path and return a ``Path`` below a base."""
    if not isinstance(value, str) or not value.strip():
        raise VersionError(f"{what} 为空。")
    text = value.replace("\\", "/").strip()
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise VersionError(f"{what} 不能是绝对路径：{value!r}")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts:
        raise VersionError(f"{what} 无效：{value!r}")
    checked: list[str] = []
    for part in parts:
        if part == "..":
            raise VersionError(f"{what} 不能包含 ..：{value!r}")
        checked.append(safe_component(part, what=what, max_length=255))
    return Path(*checked)


def check_asset_hash(value: object) -> str:
    return normalise_sha1(value, what="资源哈希")


def check_asset_name(value: object) -> Path:
    """Validate an asset key used when materialising virtual resources."""
    if not isinstance(value, str):
        raise VersionError("资源名称无效。")
    text = value.replace("\\", "/")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise VersionError(f"资源名称不能是绝对路径：{value!r}")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise VersionError(f"资源名称包含路径穿越：{value!r}")
    for part in parts:
        if any(char in _INVALID_CHARS or ord(char) < 32 for char in part):
            raise VersionError(f"资源名称含有非法字符：{value!r}")
        if part[-1] in (" ", "."):
            raise VersionError(f"资源名称不能以空格或点结尾：{value!r}")
    return Path(*parts)


def is_within(base: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(base.resolve())
        return True
    except (ValueError, OSError):
        return False


def posix_relpath(path: Path) -> str:
    return PurePosixPath(*path.parts).as_posix()
