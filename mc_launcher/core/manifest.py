"""Remote version manifest and version JSON helpers."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .errors import LauncherError, VersionError
from .mirrors import DownloadSource
from .net import HttpClient
from .safety import normalise_sha1, safe_component
from .storage import cache_dir, ensure_dir

MANIFEST_TTL_SECONDS = 600
MAX_MANIFEST_VERSIONS = 5000
MAX_VERSION_JSON_BYTES = 12 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024

TYPE_LABELS = {
    "release": "正式版",
    "snapshot": "快照",
    "old_beta": "旧 Beta",
    "old_alpha": "旧 Alpha",
}
KIND_LABELS = {
    "release": "正式版",
    "snapshot": "快照",
    "old": "远古版本",
    "all": "全部",
}


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class RemoteVersion:
    id: str
    type: str
    url: str
    sha1: str
    release_time: datetime | None
    compliance_level: int | None = None

    @property
    def type_label(self) -> str:
        return TYPE_LABELS.get(self.type, self.type or "未知")

    @property
    def date_text(self) -> str:
        if self.release_time is None:
            return "未知"
        return self.release_time.astimezone().strftime("%Y-%m-%d")


@dataclass
class Manifest:
    source: DownloadSource
    versions: list[RemoteVersion]
    latest_release: str = ""
    latest_snapshot: str = ""
    fetched_at: float = 0.0
    from_cache: bool = False

    def find(self, version_id: str) -> RemoteVersion | None:
        return next((item for item in self.versions if item.id == version_id), None)


@dataclass
class VersionDetail:
    remote: RemoteVersion
    data: dict[str, Any]
    raw: bytes
    sha1: str


def _manifest_cache_path(source: DownloadSource, root: Path | None = None) -> Path:
    return (root or cache_dir()) / source.id / "manifest.json"


def _read_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as file:
        return json.load(file)


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _parse_manifest_payload(source: DownloadSource, payload: bytes) -> tuple[dict[str, Any], Manifest]:
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VersionError(f"版本清单不是有效 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise VersionError("版本清单根节点不是对象。")
    raw_versions = data.get("versions")
    if not isinstance(raw_versions, list):
        raise VersionError("版本清单缺少 versions 列表。")
    if len(raw_versions) > MAX_MANIFEST_VERSIONS:
        raise VersionError("版本清单条目过多，已拒绝解析。")

    versions: list[RemoteVersion] = []
    seen: set[str] = set()
    for item in raw_versions:
        if not isinstance(item, dict):
            continue
        try:
            version_id = safe_component(item.get("id"), what="版本 ID", max_length=96)
        except VersionError:
            continue
        if version_id in seen:
            continue
        version_type = item.get("type")
        if version_type not in TYPE_LABELS:
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            continue
        sha1 = item.get("sha1")
        try:
            sha1 = normalise_sha1(sha1, what="版本 JSON 哈希")
        except VersionError:
            # Older/third-party manifests may omit it.  The file is still
            # fetched over HTTPS; the downloader records its own hash.
            sha1 = ""
        compliance = item.get("complianceLevel")
        versions.append(
            RemoteVersion(
                id=version_id,
                type=version_type,
                url=url,
                sha1=sha1,
                release_time=parse_time(item.get("releaseTime") or item.get("time")),
                compliance_level=compliance if isinstance(compliance, int) else None,
            )
        )
        seen.add(version_id)

    versions.sort(
        key=lambda item: (
            item.release_time.timestamp() if item.release_time else 0.0,
            item.id.lower(),
        ),
        reverse=True,
    )
    latest = data.get("latest") if isinstance(data.get("latest"), dict) else {}
    manifest = Manifest(
        source=source,
        versions=versions,
        latest_release=str(latest.get("release") or ""),
        latest_snapshot=str(latest.get("snapshot") or ""),
    )
    return data, manifest


def load_manifest(
    source: DownloadSource,
    *,
    client: HttpClient | None = None,
    force: bool = False,
    ttl: int = MANIFEST_TTL_SECONDS,
    cache_root: Path | None = None,
    logger: Callable[[str], None] | None = None,
) -> Manifest:
    """Load the version manifest, using a short cache and offline fallback."""
    log = logger or (lambda _message: None)
    cache_path = _manifest_cache_path(source, cache_root)
    cached_payload: bytes | None = None
    cached_saved_at = 0.0
    if cache_path.is_file():
        try:
            wrapper = _read_json_file(cache_path)
            if isinstance(wrapper, dict) and wrapper.get("schema") == 1:
                cached_saved_at = float(wrapper.get("saved_at") or 0)
                raw = wrapper.get("manifest")
                if isinstance(raw, dict):
                    cached_payload = json.dumps(raw, ensure_ascii=False).encode("utf-8")
        except (OSError, ValueError, json.JSONDecodeError):
            cached_payload = None

    if cached_payload is not None and not force and time.time() - cached_saved_at < ttl:
        try:
            _, manifest = _parse_manifest_payload(source, cached_payload)
            manifest.fetched_at = cached_saved_at
            manifest.from_cache = True
            return manifest
        except VersionError:
            cached_payload = None

    http = client or HttpClient(
        source.allowed_hosts,
        strict_redirects=source.strict_redirects,
        logger=log,
    )
    errors: list[str] = []
    try:
        payload = fetch_bytes(
            http,
            (source.manifest_url, source.fallback_manifest_url),
            max_size=MAX_MANIFEST_BYTES,
            logger=log,
        )
        _, manifest = _parse_manifest_payload(source, payload)
        manifest.fetched_at = time.time()
        wrapper = {
            "schema": 1,
            "source": source.id,
            "saved_at": manifest.fetched_at,
            "manifest": json.loads(payload),
        }
        try:
            _write_atomic(
                cache_path,
                json.dumps(wrapper, ensure_ascii=False).encode("utf-8"),
            )
        except OSError as exc:
            log(f"无法写入版本清单缓存：{exc}")
        return manifest
    except (LauncherError, OSError) as exc:
        errors.append(str(exc))

    if cached_payload is not None:
        try:
            _, manifest = _parse_manifest_payload(source, cached_payload)
            manifest.fetched_at = cached_saved_at
            manifest.from_cache = True
            log("网络不可用，使用缓存的版本清单。")
            return manifest
        except VersionError:
            pass
    detail = "\n".join(f"  - {item}" for item in errors)
    raise LauncherError(
        "无法获取 Minecraft 版本清单。\n"
        f"{detail}\n"
        "请检查网络、下载源，或稍后重试。"
    )


def filter_versions(versions: Iterable[RemoteVersion], kind: str) -> list[RemoteVersion]:
    kind = kind if kind in KIND_LABELS else "release"
    if kind == "all":
        return list(versions)
    if kind == "old":
        return [item for item in versions if item.type in ("old_beta", "old_alpha")]
    return [item for item in versions if item.type == kind]


def fetch_version_detail(
    source: DownloadSource,
    remote: RemoteVersion,
    *,
    client: HttpClient | None = None,
    cache_root: Path | None = None,
    logger: Callable[[str], None] | None = None,
) -> VersionDetail:
    """Fetch and verify a version JSON."""
    log = logger or (lambda _message: None)
    http = client or HttpClient(
        source.allowed_hosts,
        strict_redirects=source.strict_redirects,
        logger=log,
    )
    cache_path = (
        (cache_root or cache_dir())
        / source.id
        / "versions"
        / f"{safe_component(remote.id, what='版本 ID', max_length=96)}-{remote.sha1[:12]}.json"
    )

    def parse(payload: bytes) -> dict[str, Any]:
        try:
            data = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VersionError(f"版本 {remote.id} 的 JSON 无效：{exc}") from exc
        if not isinstance(data, dict):
            raise VersionError(f"版本 {remote.id} 的 JSON 根节点不是对象。")
        actual_id = data.get("id")
        if isinstance(actual_id, str) and actual_id and actual_id != remote.id:
            raise VersionError(
                f"版本 JSON 的 id（{actual_id}）与清单（{remote.id}）不一致。"
            )
        return data

    payload: bytes | None = None
    from_cache = False
    if cache_path.is_file():
        try:
            cached = cache_path.read_bytes()
            if not remote.sha1 or normalise_sha1(_sha1(cached)) == remote.sha1:
                payload = cached
                from_cache = True
        except (OSError, VersionError):
            payload = None

    data: dict[str, Any] | None = None
    if payload is not None:
        try:
            data = parse(payload)
        except VersionError:
            if not from_cache:
                raise
            # The cache can be edited or truncated independently of the
            # manifest hash (or the manifest may omit SHA-1).  Drop it and
            # fetch a fresh copy rather than failing the whole install.
            log("本地缓存的版本 JSON 已损坏，将重新下载。")
            try:
                cache_path.unlink(missing_ok=True)
            except OSError:
                pass
            payload = None

    if payload is None:
        try:
            payload = fetch_bytes(
                http,
                source.urls_for(remote.url),
                max_size=MAX_VERSION_JSON_BYTES,
                logger=log,
            )
            if remote.sha1 and _sha1(payload) != remote.sha1:
                raise VersionError(
                    f"版本 JSON SHA-1 不匹配：期望 {remote.sha1}，"
                    f"实际 {_sha1(payload)}"
                )
        except LauncherError as exc:
            raise LauncherError(
                f"无法下载版本 {remote.id} 的 JSON。\n  - {exc}"
            ) from exc
        try:
            _write_atomic(cache_path, payload)
        except OSError as exc:
            log(f"无法写入版本 JSON 缓存：{exc}")
        data = parse(payload)

    return VersionDetail(
        remote=remote,
        data=data or {},
        raw=payload,
        sha1=_sha1(payload),
    )


def fetch_bytes(
    client: HttpClient,
    urls: Iterable[str],
    *,
    max_size: int,
    logger: Callable[[str], None] | None = None,
) -> bytes:
    """Fetch a small file, racing provider and fallback URLs.

    Racing matters for mirrors whose redirect CDN can stall for tens of
    seconds: the official endpoint may answer immediately, and vice versa.
    The first successful response wins.  The losing requests run in daemon
    threads so they can never delay application shutdown.
    """
    import queue
    import threading

    log = logger or (lambda _message: None)
    unique = list(dict.fromkeys(url for url in urls if url))
    if not unique:
        raise LauncherError("没有可用的元数据下载地址。")
    if len(unique) == 1:
        return client.read_bytes(unique[0], max_size=max_size)

    results: queue.Queue[tuple[bool, object, str]] = queue.Queue()

    def request(url: str) -> None:
        try:
            results.put((True, client.read_bytes(url, max_size=max_size), url))
        except Exception as exc:  # noqa: BLE001 - reported by the caller
            results.put((False, exc, url))

    for url in unique:
        threading.Thread(
            target=request, args=(url,), daemon=True, name="metadata-fetch"
        ).start()

    errors: list[str] = []
    deadline = time.monotonic() + (client.timeout * (client.retries + 1)) + 5.0
    while len(errors) < len(unique):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            ok, value, url = results.get(timeout=min(1.0, remaining))
        except queue.Empty:
            continue
        if ok:
            return value  # type: ignore[return-value]
        errors.append(f"{url[:100]}：{value}")
        log(f"地址失败：{url[:100]}")
    detail = "\n".join(f"  - {item}" for item in errors) or "  - 请求超时"
    raise LauncherError(f"无法下载元数据。\n{detail}")


def _sha1(payload: bytes) -> str:
    import hashlib

    return hashlib.sha1(payload).hexdigest()
