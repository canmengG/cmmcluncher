"""Windows DPAPI storage for the Microsoft account refresh token.

The refresh token is a long-lived credential.  It is protected with the current
Windows user's DPAPI key (``CryptProtectData``) and written atomically to
``%APPDATA%\\MinecraftLauncher\\account.dat``.  There is intentionally no
plaintext fallback: if DPAPI is unavailable, the caller keeps the session in
memory only.
"""

from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path

from .errors import LauncherError
from .storage import app_data_dir

ACCOUNT_FILE = "account.dat"
_ENTROPY = b"minecraft-launcher/msa/v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class SecureStoreError(LauncherError):
    """The protected account file could not be read or written."""


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def account_path() -> Path:
    return app_data_dir() / ACCOUNT_FILE


def _blob(data: bytes) -> tuple[_DATA_BLOB, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _DATA_BLOB(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    return blob, buffer


def _protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise SecureStoreError("当前系统不支持 Windows DPAPI，无法保存微软登录令牌。")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    in_blob, in_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(_ENTROPY)
    out_blob = _DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "MinecraftLauncher",
        ctypes.byref(entropy_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    # Keep the input buffers alive until the call returns.
    _ = (in_buffer, entropy_buffer)
    if not ok:
        raise SecureStoreError(f"DPAPI 加密失败：{ctypes.WinError()}")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise SecureStoreError("当前系统不支持 Windows DPAPI，无法读取微软登录令牌。")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    in_blob, in_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(_ENTROPY)
    out_blob = _DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    _ = (in_buffer, entropy_buffer)
    if not ok:
        raise SecureStoreError(f"DPAPI 解密失败：{ctypes.WinError()}")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def save_account(payload: dict) -> None:
    """Encrypt and atomically write the account payload."""
    try:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        raise SecureStoreError(f"账户数据无法序列化：{exc}") from exc
    encrypted = _protect(data)
    path = account_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(encrypted)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise SecureStoreError(f"无法保存微软登录令牌：{exc}") from exc


def load_account() -> dict | None:
    path = account_path()
    try:
        encrypted = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SecureStoreError(f"无法读取微软登录令牌：{exc}") from exc
    if not encrypted:
        return None
    try:
        payload = json.loads(_unprotect(encrypted).decode("utf-8"))
    except (SecureStoreError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecureStoreError(f"微软登录令牌已损坏或不能解密：{exc}") from exc
    return payload if isinstance(payload, dict) else None


def delete_account() -> None:
    try:
        account_path().unlink(missing_ok=True)
    except OSError as exc:
        raise SecureStoreError(f"无法删除微软登录令牌：{exc}") from exc
