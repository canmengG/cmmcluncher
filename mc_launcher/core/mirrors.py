"""Download providers and their URL rewrite rules.

Two providers are supported:

* ``mojang`` — the official Mojang endpoints, with strict redirects;
* ``bmclapi`` — the BMCLAPI mirror used by HMCL/PCL in mainland China.  It
  redirects large files to rotating CDN mirrors, so redirects are allowed to
  other public HTTPS hosts while initial URLs remain allow-listed.

Every provider keeps the original official URL as a fallback, so a broken
mirror never leaves the user without a download path.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Iterable

from .net import LOADER_HOSTS, MIRROR_HOSTS, OFFICIAL_HOSTS, PROVIDER_HOSTS

_BMCL = "https://bmclapi2.bangbang93.com"

# (source host, replacement base URL, path prefix to strip).  The remaining
# path/query is appended to the replacement base.  NeoForge publishes under
# ``maven.neoforged.net/releases/...`` while BMCLAPI mirrors the same tree at
# ``/maven/...`` (without the ``/releases`` prefix).
_BMCL_REWRITES: tuple[tuple[str, str, str], ...] = (
    ("piston-meta.mojang.com", _BMCL, ""),
    ("launchermeta.mojang.com", _BMCL, ""),
    ("launcher.mojang.com", _BMCL, ""),
    ("piston-data.mojang.com", _BMCL, ""),
    ("libraries.minecraft.net", f"{_BMCL}/maven", ""),
    ("resources.download.minecraft.net", f"{_BMCL}/assets", ""),
    ("meta.fabricmc.net", f"{_BMCL}/fabric-meta", ""),
    ("maven.fabricmc.net", f"{_BMCL}/maven", ""),
    ("maven.minecraftforge.net", f"{_BMCL}/maven", ""),
    ("maven.neoforged.net", f"{_BMCL}/maven", "/releases"),
)


@dataclass(frozen=True)
class DownloadSource:
    id: str
    name: str
    description: str
    manifest_url: str
    fallback_manifest_url: str
    fabric_meta: str
    forge_maven: str
    forge_promotions: str
    strict_redirects: bool
    rewrites: tuple[tuple[str, str, str], ...] = ()
    allowed_hosts: frozenset[str] = PROVIDER_HOSTS
    # Large asset indexes contain thousands of tiny objects.  BMCLAPI's
    # per-request rate limit makes it a poor primary for those, while Mojang's
    # object CDN is usually fast everywhere.  Use the official object host
    # first and the mirror as a fallback in that case.
    asset_official_first: bool = False

    def rewrite(self, url: str) -> str:
        if not self.rewrites:
            return url
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
        for source_host, base, strip_prefix in self.rewrites:
            if host != source_host:
                continue
            path = parsed.path
            if strip_prefix:
                if not path.startswith(strip_prefix):
                    continue
                path = path[len(strip_prefix) :]
            base_parts = urllib.parse.urlsplit(base)
            path = base_parts.path.rstrip("/") + path
            return urllib.parse.urlunsplit(
                ("https", base_parts.netloc, path, parsed.query, "")
            )
        return url

    def urls_for(self, official_url: str) -> tuple[str, ...]:
        """Primary provider URL plus the official fallback when they differ."""
        primary = self.rewrite(official_url)
        if primary == official_url:
            return (official_url,)
        return (primary, official_url)


def _build_sources() -> dict[str, DownloadSource]:
    mojang = DownloadSource(
        id="mojang",
        name="Mojang 官方源",
        description="官方 launchermeta / piston-data / libraries.minecraft.net，直连或走系统代理。",
        manifest_url="https://piston-meta.mojang.com/mc/game/version_manifest_v2.json",
        fallback_manifest_url="https://launchermeta.mojang.com/mc/game/version_manifest_v2.json",
        fabric_meta="https://meta.fabricmc.net",
        forge_maven="https://maven.minecraftforge.net",
        forge_promotions="https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json",
        strict_redirects=True,
        rewrites=(),
        allowed_hosts=frozenset(OFFICIAL_HOSTS | LOADER_HOSTS),
    )
    bmclapi = DownloadSource(
        id="bmclapi",
        name="BMCLAPI 镜像（国内加速）",
        description="BMCLAPI 镜像；文件会跳转到其公开 CDN，失败时自动回退官方源。",
        manifest_url=f"{_BMCL}/mc/game/version_manifest_v2.json",
        fallback_manifest_url="https://piston-meta.mojang.com/mc/game/version_manifest_v2.json",
        fabric_meta=f"{_BMCL}/fabric-meta",
        forge_maven=f"{_BMCL}/maven",
        forge_promotions="https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json",
        strict_redirects=False,
        rewrites=_BMCL_REWRITES,
        allowed_hosts=frozenset(OFFICIAL_HOSTS | MIRROR_HOSTS | LOADER_HOSTS),
        asset_official_first=True,
    )
    return {source.id: source for source in (mojang, bmclapi)}


SOURCES = _build_sources()
# The UI and CLI default to the mirror because Forge's installer contacts
# maven.minecraftforge.net directly for its remaining files; that host is
# frequently unreachable from mainland China.  The official source stays one
# click away and all downloads are still SHA-1 verified.
DEFAULT_SOURCE_ID = "bmclapi"


def source_ids() -> list[str]:
    return list(SOURCES)


def get_source(source_id: str | None) -> DownloadSource:
    key = (source_id or DEFAULT_SOURCE_ID).strip().lower()
    if key not in SOURCES:
        raise KeyError(f"未知下载源：{source_id}")
    return SOURCES[key]


def source_labels() -> list[tuple[str, str]]:
    return [(source.id, source.name) for source in SOURCES.values()]


def source_by_label(label: str) -> DownloadSource:
    for source in SOURCES.values():
        if source.name == label:
            return source
    return SOURCES[DEFAULT_SOURCE_ID]


def all_hosts() -> frozenset[str]:
    hosts: set[str] = set()
    for source in SOURCES.values():
        hosts.update(source.allowed_hosts)
    return frozenset(hosts)


def loader_allowed_hosts(source: DownloadSource) -> frozenset[str]:
    """Hosts a loader install may need, including the BMCLAPI Maven mirror.

    Loader profiles and NeoForge installers deliberately try BMCLAPI first even
    when the user selected the official Mojang source (the official Fabric /
    NeoForge Maven hosts are frequently unreachable).  Those requests still go
    through HTTPS/public-address checks, but the client must allow the mirror
    host or it would reject our own fallback URL.
    """
    return frozenset(set(source.allowed_hosts) | set(MIRROR_HOSTS))
