#!/usr/bin/env python3
"""下载 Minecraft 版本清单，并打印所有正式版的版本号。"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
MANIFEST_FILE = Path(__file__).with_name("version_manifest_v2.json")


def download_manifest() -> dict:
    """下载 version_manifest_v2.json，保存到磁盘并用 json 解析。"""
    request = urllib.request.Request(
        MANIFEST_URL,
        headers={"User-Agent": "minecraft-release-list/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()

    MANIFEST_FILE.write_bytes(raw)
    return json.loads(raw.decode("utf-8"))


def main() -> int:
    try:
        manifest = download_manifest()
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"Download or parse failed: {exc}", file=sys.stderr)
        return 1

    releases = [
        version["id"]
        for version in manifest.get("versions", [])
        if version.get("type") == "release"
    ]

    print(f"Total: {len(releases)} releases (newest first):")
    for index, version in enumerate(releases, start=1):
        print(f"{index}. {version}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
