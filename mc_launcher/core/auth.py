"""Offline account helpers.

The launcher does not talk to Mojang's authentication servers.  It creates the
same deterministic offline UUID that Minecraft itself uses for offline players.
"""

from __future__ import annotations

import hashlib
import re
import uuid

from .errors import LauncherError

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")


def validate_username(username: str) -> str:
    username = username.strip()
    if not USERNAME_RE.fullmatch(username):
        raise LauncherError("玩家名需为 1-16 个字母、数字或下划线。")
    return username


def offline_uuid(username: str) -> uuid.UUID:
    """Java's UUID.nameUUIDFromBytes(("OfflinePlayer:" + name).getBytes())."""
    digest = bytearray(hashlib.md5(f"OfflinePlayer:{username}".encode("utf-8")).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    return uuid.UUID(bytes=bytes(digest))


def offline_uuid_hex(username: str) -> str:
    return offline_uuid(username).hex


def offline_session(username: str) -> str:
    """Legacy ``token:accessToken:uuid`` session string."""
    return f"0:0:{offline_uuid_hex(username)}"
