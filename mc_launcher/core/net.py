"""Hardened HTTPS download primitives used by the installer.

The launcher talks to a small, fixed set of download providers.  Metadata
(version JSON, asset indexes, loader profiles) ultimately points at file URLs,
so every URL must be validated before it is fetched.  This module provides:

* an HTTPS-only policy with an explicit host allow-list;
* redirect validation (strict for Mojang, provider-scoped for BMCLAPI CDN
  redirects) and private-address blocking to prevent SSRF;
* bounded, streamed downloads with SHA-1 verification, atomic replacement and
  optional resume;
* retries with backoff and cancellation.

No third-party packages are required, which keeps the PyInstaller bundle small
and avoids supply-chain surprises.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import glob
import hashlib
import http.client
import ipaddress
import json
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .errors import LauncherError

# Files are small compared with the hard limits below; anything larger is a
# sign of a hostile/broken server rather than a real Minecraft file.
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_AUTH_REQUEST_BYTES = 64 * 1024
MAX_AUTH_RESPONSE_BYTES = 1024 * 1024
MAX_SMALL_FILE_BYTES = 4 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
CHUNK_SIZE = 256 * 1024
# Segmented (multi-connection) download of large files.  Small files gain
# nothing from extra connections and cost more requests, so only the client
# jar and a few large libraries are split.
SEGMENT_MIN_SIZE = 8 * 1024 * 1024
MIN_SEGMENT_SIZE = 2 * 1024 * 1024
MAX_SEGMENTS = 16

# Queued jobs may share a game folder, so two workers can legitimately target
# the same library/asset path.  Serialise per destination; a later worker then
# finds the completed file and skips it instead of interleaving writes to the
# same ``.part`` file.
_DEST_LOCKS: dict[str, tuple[threading.Lock, int]] = {}
_DEST_LOCKS_GUARD = threading.Lock()


@contextlib.contextmanager
def destination_lock(path: Path) -> Iterable[None]:
    key = os.path.normcase(os.path.abspath(str(path)))
    with _DEST_LOCKS_GUARD:
        lock, count = _DEST_LOCKS.get(key, (threading.Lock(), 0))
        _DEST_LOCKS[key] = (lock, count + 1)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _DEST_LOCKS_GUARD:
            current = _DEST_LOCKS.get(key)
            if current is None:
                return
            current_lock, current_count = current
            if current_count <= 1:
                _DEST_LOCKS.pop(key, None)
            else:
                _DEST_LOCKS[key] = (current_lock, current_count - 1)


OFFICIAL_HOSTS = frozenset(
    {
        "piston-meta.mojang.com",
        "launchermeta.mojang.com",
        "launcher.mojang.com",
        "piston-data.mojang.com",
        "libraries.minecraft.net",
        "resources.download.minecraft.net",
        "session.minecraft.net",
    }
)

MIRROR_HOSTS = frozenset(
    {
        "bmclapi2.bangbang93.com",
        "bmclapi.bangbang93.com",
        "bmclapi1.bangbang93.com",
    }
)

LOADER_HOSTS = frozenset(
    {
        "meta.fabricmc.net",
        "maven.fabricmc.net",
        "maven.minecraftforge.net",
        "files.minecraftforge.net",
        "maven.neoforged.net",
        "meta.quiltmc.org",
        "maven.quiltmc.org",
    }
)

# Content providers (0.5.0).  These are *not* part of provider metadata:
# a version JSON must never be able to point a game download at a content CDN.
# The content module passes this set explicitly to its own HttpClient.
CONTENT_HOSTS = frozenset(
    {
        "api.modrinth.com",
        "cdn.modrinth.com",
        "cdn-raw.modrinth.com",
        "api.curseforge.com",
        "edge.forgecdn.net",
        "mediafilez.forgecdn.net",
    }
)

PROVIDER_HOSTS = OFFICIAL_HOSTS | MIRROR_HOSTS | LOADER_HOSTS


class DownloadError(LauncherError):
    """A file could not be fetched or failed verification."""


class CancelledError(DownloadError):
    """The user cancelled the operation; partial files are deleted."""


class PauseToken:
    """A resumable pause switch checked between network chunks.

    ``cancel`` remains a separate event: cancelling while paused wakes the
    waiter, deletes the current partial file and aborts the operation.  A pause
    keeps ``.part``/``.part.segN`` files so the same download can continue.
    """

    def __init__(self) -> None:
        self._resume = threading.Event()
        self._resume.set()
        self._paused = threading.Event()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def pause(self) -> None:
        self._paused.set()
        self._resume.clear()

    def resume(self) -> None:
        self._paused.clear()
        self._resume.set()

    def toggle(self) -> bool:
        if self.paused:
            self.resume()
        else:
            self.pause()
        return self.paused

    def wait(self, cancel: threading.Event | None = None) -> None:
        while not self._resume.is_set():
            if cancel is not None and cancel.is_set():
                raise CancelledError("下载已取消。")
            self._resume.wait(0.1)
        if cancel is not None and cancel.is_set():
            raise CancelledError("下载已取消。")


def wait_for_pause(
    pause: PauseToken | None, cancel: threading.Event | None = None
) -> None:
    if pause is not None:
        pause.wait(cancel)



class _RangeUnsupported(DownloadError):
    """The server ignored a Range request (HTTP 200 instead of 206)."""


@dataclass(frozen=True)
class DownloadSpec:
    """One file to obtain.

    ``url`` is the primary provider URL.  ``fallback_urls`` are tried after the
    primary provider fails; they are validated with the same policy.
    """

    dest: Path
    url: str
    fallback_urls: tuple[str, ...] = ()
    sha1: str | None = None
    size: int | None = None
    label: str = ""

    @property
    def urls(self) -> tuple[str, ...]:
        return (self.url, *self.fallback_urls)


@dataclass
class DownloadReport:
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_downloaded: int = 0
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False


def _normalise_host(host: str) -> str:
    return host.strip().rstrip(".").lower()


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _host_is_public(host: str, port: int, allow_private: bool) -> bool:
    if _is_ip_literal(host):
        address = ipaddress.ip_address(host)
        return allow_private or address.is_global
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DownloadError(f"无法解析下载主机 {host}：{exc}") from exc
    if not infos:
        raise DownloadError(f"无法解析下载主机：{host}")
    seen_global = False
    seen_any = False
    for info in infos:
        seen_any = True
        address_text = info[4][0]
        try:
            address = ipaddress.ip_address(address_text)
        except ValueError:
            continue
        if allow_private or address.is_global:
            seen_global = True
            break
    return seen_global or not seen_any


def validate_url(
    url: str,
    allowed_hosts: Iterable[str],
    *,
    redirect: bool = False,
    allow_private: bool = False,
    check_dns: bool = True,
) -> str:
    """Validate a URL and return it unchanged.

    Initial URLs must use HTTPS, port 443, and a host from ``allowed_hosts``.
    Redirect targets may leave the allow-list (BMCLAPI returns signed CDN URLs
    on rotating mirrors) but must remain HTTPS and resolve to public addresses.
    """
    if not isinstance(url, str) or not url:
        raise DownloadError("下载地址为空。")
    if len(url) > 4096:
        raise DownloadError("下载地址过长。")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise DownloadError(f"只允许 HTTPS 下载地址：{url[:120]}")
    if parsed.username or parsed.password:
        raise DownloadError("下载地址不允许包含用户名或密码。")
    if parsed.fragment:
        raise DownloadError("下载地址不允许包含片段。")
    host = _normalise_host(parsed.hostname or "")
    if not host or len(host) > 253 or " " in host:
        raise DownloadError(f"下载地址主机无效：{url[:120]}")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise DownloadError(f"下载地址端口无效：{url[:120]}") from exc

    if redirect:
        if not (1 <= port <= 65535):
            raise DownloadError("重定向端口无效。")
    else:
        if port != 443:
            raise DownloadError(f"只允许 HTTPS 443 端口：{url[:120]}")
        if host not in {_normalise_host(item) for item in allowed_hosts}:
            raise DownloadError(f"下载地址不在允许的主机列表中：{host}")

    if check_dns and not _host_is_public(host, port, allow_private):
        raise DownloadError(f"拒绝访问私有或回环地址：{host}")
    return url


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that re-validates every Location."""

    max_redirections = 5

    def __init__(self, client: "HttpClient") -> None:
        self._client = client

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        self._client.validate_redirect(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpClient:
    """A small HTTPS client with retries, cancellation and streamed writes."""

    def __init__(
        self,
        allowed_hosts: Iterable[str] = PROVIDER_HOSTS,
        *,
        strict_redirects: bool = True,
        timeout: float = 20.0,
        retries: int = 1,
        allow_private: bool = False,
        check_dns: bool = True,
        segments: int = 4,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.allowed_hosts = frozenset(_normalise_host(item) for item in allowed_hosts)
        self.strict_redirects = strict_redirects
        self.timeout = timeout
        self.retries = max(0, retries)
        self.allow_private = allow_private
        self.check_dns = check_dns
        self.segments = max(0, min(MAX_SEGMENTS, int(segments)))
        self._logger = logger or (lambda _message: None)
        self._dns_cache: dict[tuple[str, int], tuple[float, bool]] = {}
        self._dns_lock = threading.Lock()
        self._tls = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
        try:
            self._tls.minimum_version = ssl.TLSVersion.TLSv1_2
        except (AttributeError, ValueError):
            pass

    # -- URL policy -------------------------------------------------

    def _host_ok(self, host: str, port: int) -> bool:
        if self.allow_private:
            return True
        key = (host, port)
        now = time.monotonic()
        with self._dns_lock:
            cached = self._dns_cache.get(key)
            if cached is not None and cached[0] >= now:
                return cached[1]
        try:
            public = _host_is_public(host, port, False)
        except DownloadError:
            public = False
        with self._dns_lock:
            self._dns_cache[key] = (now + 300.0, public)
        return public

    def validate_initial(self, url: str) -> str:
        validate_url(
            url,
            self.allowed_hosts,
            redirect=False,
            allow_private=self.allow_private,
            check_dns=False,
        )
        if not self.check_dns:
            return url
        parsed = urllib.parse.urlsplit(url)
        host = _normalise_host(parsed.hostname or "")
        port = parsed.port or 443
        if not self._host_ok(host, port):
            raise DownloadError(f"下载主机无法解析或不是公网地址：{host}")
        return url

    def validate_redirect(self, url: str) -> str:
        validate_url(
            url,
            self.allowed_hosts,
            redirect=not self.strict_redirects,
            allow_private=self.allow_private,
            check_dns=False,
        )
        if not self.check_dns:
            return url
        parsed = urllib.parse.urlsplit(url)
        host = _normalise_host(parsed.hostname or "")
        port = parsed.port or 443
        if _is_ip_literal(host) and not self.allow_private:
            raise DownloadError(f"下载重定向不允许直接使用 IP 地址：{host}")
        if not self._host_ok(host, port):
            raise DownloadError(f"下载主机无法解析或不是公网地址：{host}")
        return url

    # -- Basic requests ---------------------------------------------

    def _build_opener(self) -> urllib.request.OpenerDirector:
        return urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._tls),
            SafeRedirectHandler(self),
        )

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": "MinecraftLauncher/0.2 (+local; https)",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        if extra:
            headers.update(extra)
        return headers

    def open(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        method: str = "GET",
    ):
        self.validate_initial(url)
        request = urllib.request.Request(
            url, headers=self._headers(headers), method=method
        )
        opener = self._build_opener()
        return opener.open(request, timeout=self.timeout)

    def read_bytes(
        self,
        url: str,
        *,
        max_size: int = MAX_JSON_BYTES,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        """Fetch a small metadata file into memory."""
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with self.open(url, headers=headers) as response:
                    self.validate_redirect(response.geturl())
                    length_header = response.headers.get("Content-Length")
                    if length_header:
                        try:
                            length = int(length_header)
                        except ValueError:
                            length = 0
                        if length > max_size:
                            raise DownloadError(
                                f"元数据文件过大（{length} 字节）：{url[:120]}"
                            )
                    data = response.read(max_size + 1)
                    if len(data) > max_size:
                        raise DownloadError(f"元数据文件超过大小限制：{url[:120]}")
                    return data
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code in (404, 410):
                    break
                if exc.code == 416:
                    break
                if exc.code not in (408, 425, 429, 500, 502, 503, 504):
                    break
                self._sleep_backoff(attempt, exc.headers.get("Retry-After") if exc.headers else None)
            except (urllib.error.URLError, TimeoutError, ssl.SSLError, http.client.HTTPException, OSError) as exc:
                last_error = exc
                self._sleep_backoff(attempt)
        raise DownloadError(f"下载元数据失败：{url[:120]}\n{self._describe_error(last_error)}")

    # -- Small JSON requests (Microsoft/Xbox authentication) ---------

    def request_bytes(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        max_size: int = MAX_AUTH_RESPONSE_BYTES,
    ) -> tuple[int, bytes]:
        """Send a small request and return ``(status, body)``.

        Authentication endpoints return useful JSON on 4xx (for example
        ``authorization_pending``), so those bodies are returned to the caller
        instead of being raised as exceptions.  Transport errors still raise
        :class:`DownloadError`; callers decide which status codes to retry.
        """
        if data is not None and len(data) > MAX_AUTH_REQUEST_BYTES:
            raise DownloadError("认证请求体过大。")
        self.validate_initial(url)
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            request = urllib.request.Request(
                url, data=data, headers=self._headers(headers), method=method
            )
            try:
                with self._build_opener().open(
                    request, timeout=self.timeout
                ) as response:
                    self.validate_redirect(response.geturl())
                    length_header = response.headers.get("Content-Length")
                    if length_header:
                        try:
                            length = int(length_header)
                        except ValueError:
                            length = 0
                        if length > max_size:
                            raise DownloadError(
                                f"认证响应过大（{length} 字节）：{url[:120]}"
                            )
                    payload = response.read(max_size + 1)
                    if len(payload) > max_size:
                        raise DownloadError(f"认证响应超过大小限制：{url[:120]}")
                    status = int(
                        getattr(response, "status", 0)
                        or response.getcode()
                        or 0
                    )
                    return status, payload
            except urllib.error.HTTPError as exc:
                try:
                    payload = exc.read(max_size + 1)
                except OSError:
                    payload = b""
                if len(payload) > max_size:
                    payload = b""
                retryable = exc.code in (408, 425, 429, 500, 502, 503, 504)
                if retryable and attempt < self.retries:
                    self._sleep_backoff(
                        attempt,
                        exc.headers.get("Retry-After") if exc.headers else None,
                    )
                    continue
                return exc.code, payload
            except (
                urllib.error.URLError,
                TimeoutError,
                ssl.SSLError,
                http.client.HTTPException,
                OSError,
            ) as exc:
                last_error = exc
                if attempt < self.retries:
                    self._sleep_backoff(attempt)
        raise DownloadError(
            f"请求认证服务失败：{url[:120]}\n{self._describe_error(last_error)}"
        )

    def post_form(
        self,
        url: str,
        fields: dict[str, str],
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        body = urllib.parse.urlencode(fields).encode("utf-8")
        merged = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        if headers:
            merged.update(headers)
        return self.request_bytes(url, method="POST", headers=merged, data=body)

    def post_json(
        self,
        url: str,
        payload: dict,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        merged = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if headers:
            merged.update(headers)
        return self.request_bytes(url, method="POST", headers=merged, data=body)

    def get_json(
        self,
        url: str,
        *,
        bearer: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        merged = {"Accept": "application/json"}
        if headers:
            merged.update(headers)
        if bearer:
            merged["Authorization"] = f"Bearer {bearer}"
        return self.request_bytes(url, method="GET", headers=merged)

    # -- File downloads ---------------------------------------------

    def download_file(
        self,
        spec: DownloadSpec,
        *,
        progress: Callable[[int], None] | None = None,
        cancel: threading.Event | None = None,
        pause: PauseToken | None = None,
        segments: int | None = None,
    ) -> int:
        """Download one file without ever writing two workers to one path."""
        with destination_lock(spec.dest):
            return self._download_file_locked(
                spec,
                progress=progress,
                cancel=cancel,
                pause=pause,
                segments=segments,
            )

    def _download_file_locked(
        self,
        spec: DownloadSpec,
        *,
        progress: Callable[[int], None] | None = None,
        cancel: threading.Event | None = None,
        pause: PauseToken | None = None,
        segments: int | None = None,
    ) -> int:
        """Download one file, verifying size/SHA-1 before ``os.replace``.

        ``progress`` receives the *absolute* number of bytes currently present
        for this file, including a partial file resumed from a previous run.
        Absolute values let callers aggregate several workers without
        double-counting retried or resumed bytes.

        Large files are split across ``segments`` connections when the server
        supports ``Range``; a server that ignores ranges (HTTP 200) or a
        failing segmented attempt transparently falls back to the single-stream
        path below, which keeps its normal resume support.

        Returns bytes written by network transfer in this invocation (0 when
        the existing file was already valid or the file was fully resumed).
        """
        if spec.size is not None and spec.size > MAX_DOWNLOAD_BYTES:
            raise DownloadError(f"文件超过单文件大小限制：{spec.label or spec.dest}")
        dest = spec.dest
        part = dest.with_name(dest.name + ".part")
        wait_for_pause(pause, cancel)
        if cancel is not None and cancel.is_set():
            self._remove_segments(part)
            _unlink_quietly(part)
            raise CancelledError("下载已取消。")

        if dest.is_file() and _file_matches(dest, spec.sha1, spec.size, deep=bool(spec.sha1)):
            # A leftover partial file is useless once the destination is valid.
            _unlink_quietly(part)
            self._remove_segments(part)
            return 0
        dest.parent.mkdir(parents=True, exist_ok=True)
        wanted_segments = self.segments if segments is None else segments
        if (
            wanted_segments
            and wanted_segments > 1
            and spec.size is not None
            and spec.size >= SEGMENT_MIN_SIZE
            and not part.exists()
        ):
            try:
                return self._download_segmented(
                    spec,
                    part,
                    wanted_segments,
                    progress=progress,
                    cancel=cancel,
                    pause=pause,
                )
            except _RangeUnsupported:
                self._remove_segments(part)
                self._logger(
                    f"服务器不支持分段下载，改用单线程：{spec.label or dest.name}"
                )
            except CancelledError:
                self._remove_segments(part)
                _unlink_quietly(part)
                raise
            except DownloadError as exc:
                self._remove_segments(part)
                _unlink_quietly(part)
                self._logger(f"分段下载失败，改用单线程重试：{exc}")
        resumed = 0
        if part.exists():
            try:
                part_size = part.stat().st_size
                if spec.size is not None and part_size > spec.size:
                    part.unlink()
                elif part_size:
                    resumed = part_size
                    self._logger(
                        f"继续下载未完成的文件：{spec.label or dest.name}"
                        f"（已有 {human_size(part_size)}）"
                    )
            except OSError:
                pass

        errors: list[str] = []
        # A transient network failure must not throw away bytes that can be
        # resumed later, while corrupt data must never be resumed.
        keep_part = False
        urls = spec.urls
        for url_index, url in enumerate(urls):
            for attempt in range(self.retries + 1):
                if cancel is not None and cancel.is_set():
                    raise CancelledError("下载已取消。")
                try:
                    self.validate_initial(url)
                    written = self._stream_to_part(
                        url,
                        part,
                        expected_size=spec.size,
                        progress=progress,
                        cancel=cancel,
                        pause=pause,
                    )
                    if spec.size is not None:
                        actual_size = part.stat().st_size
                        if actual_size != spec.size:
                            raise DownloadError(
                                f"文件大小不符：期望 {spec.size}，实际 {actual_size}"
                            )
                    if spec.sha1:
                        actual = sha1_file(part)
                        if actual.lower() != spec.sha1.lower():
                            raise DownloadError(
                                f"SHA-1 校验失败：期望 {spec.sha1}，实际 {actual}"
                            )
                    os.replace(part, dest)
                    return written
                except CancelledError:
                    self._remove_segments(part)
                    _unlink_quietly(part)
                    raise
                except urllib.error.HTTPError as exc:
                    errors.append(f"{url[:100]} -> HTTP {exc.code}")
                    if exc.code in (404, 410, 416):
                        # 416 means the saved range no longer matches the file
                        # on the server; start over rather than retry forever.
                        _unlink_quietly(part)
                        keep_part = False
                        break
                    if exc.code not in (408, 425, 429, 500, 502, 503, 504):
                        break
                    keep_part = True
                    self._sleep_backoff(
                        attempt,
                        exc.headers.get("Retry-After") if exc.headers else None,
                        cancel,
                    )
                except DownloadError as exc:
                    message = str(exc)
                    errors.append(f"{url[:100]} -> {message}")
                    # Corrupt data must not be resumed.
                    if "SHA-1" in message or "大小不符" in message:
                        _unlink_quietly(part)
                        keep_part = False
                        break
                    # Size limits / bad ranges also poison the partial file.
                    if "超过允许大小" in message or "断点范围" in message:
                        _unlink_quietly(part)
                        keep_part = False
                        break
                    keep_part = True
                    self._sleep_backoff(attempt, None, cancel)
                except (urllib.error.URLError, TimeoutError, ssl.SSLError, http.client.HTTPException, OSError) as exc:
                    errors.append(f"{url[:100]} -> {self._describe_error(exc)}")
                    keep_part = True
                    self._sleep_backoff(attempt, None, cancel)
            if url_index + 1 < len(urls):
                self._logger(f"下载源失败，尝试备用地址：{spec.label or dest.name}")
        if keep_part and part.is_file():
            try:
                remaining = part.stat().st_size
            except OSError:
                remaining = 0
            if remaining and (spec.size is None or remaining < spec.size):
                self._logger(
                    f"保留未完成的分片以便下次续传：{spec.label or dest.name}"
                    f"（{human_size(remaining)}）"
                )
            else:
                _unlink_quietly(part)
        else:
            _unlink_quietly(part)
        detail = "\n".join(f"  - {item}" for item in errors[-6:])
        raise DownloadError(
            f"下载失败：{spec.label or dest}\n{detail or '  未知错误'}"
        )

    def _stream_to_part(
        self,
        url: str,
        part: Path,
        *,
        expected_size: int | None,
        progress: Callable[[int], None] | None,
        cancel: threading.Event | None,
        pause: PauseToken | None = None,
    ) -> int:
        if expected_size == 0:
            # Java runtimes (and a few other metadata files) contain empty
            # files.  ``existing == expected_size`` would otherwise return
            # without creating ``.part``, and the caller's verification would
            # fail with "file not found".
            part.parent.mkdir(parents=True, exist_ok=True)
            part.write_bytes(b"")
            if progress is not None:
                progress(0)
            return 0
        existing = part.stat().st_size if part.is_file() else 0
        if expected_size is not None and existing > expected_size:
            _unlink_quietly(part)
            existing = 0
        if expected_size is not None and existing == expected_size:
            if progress is not None:
                progress(existing)
            return 0

        headers: dict[str, str] = {}
        if existing:
            headers["Range"] = f"bytes={existing}-"

        with self.open(url, headers=headers) as response:
            self.validate_redirect(response.geturl())
            status = getattr(response, "status", response.getcode())
            if status == 206:
                content_range = response.headers.get("Content-Range", "")
                if not content_range.startswith(f"bytes {existing}-"):
                    _unlink_quietly(part)
                    raise DownloadError("服务器返回了无法验证的断点范围。")
                mode = "ab"
                written = 0
            elif status == 200:
                mode = "wb"
                existing = 0
                written = 0
            else:
                raise DownloadError(f"意外的 HTTP 状态：{status}")

            length_header = response.headers.get("Content-Length")
            if length_header:
                try:
                    response_length = int(length_header)
                except ValueError:
                    response_length = 0
                if (
                    expected_size is not None
                    and mode == "wb"
                    and response_length
                    and response_length != expected_size
                ):
                    raise DownloadError(
                        f"服务器声明的大小不符：期望 {expected_size}，实际 {response_length}"
                    )
                limit = expected_size if expected_size is not None else MAX_DOWNLOAD_BYTES
                if response_length and response_length + existing > limit:
                    raise DownloadError("服务器返回的文件超过允许大小。")

            limit = expected_size if expected_size is not None else MAX_DOWNLOAD_BYTES
            # Report the resumed prefix first: the caller's aggregate then
            # reflects the real amount of data already on disk.
            if progress is not None:
                progress(existing)
            with part.open(mode) as output:
                while True:
                    wait_for_pause(pause, cancel)
                    if cancel is not None and cancel.is_set():
                        raise CancelledError("下载已取消。")
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    written += len(chunk)
                    if existing + written > limit:
                        raise DownloadError("下载内容超过允许大小。")
                    output.write(chunk)
                    if progress is not None:
                        try:
                            progress(existing + written)
                        except Exception:
                            pass
                output.flush()
                try:
                    os.fsync(output.fileno())
                except OSError:
                    pass
        return written

    # -- Segmented large-file downloads ------------------------------

    def _download_segmented(
        self,
        spec: DownloadSpec,
        part: Path,
        segments: int,
        *,
        progress: Callable[[int], None] | None,
        cancel: threading.Event | None,
        pause: PauseToken | None = None,
    ) -> int:
        """Split one large file into parallel ``Range`` requests.

        Segments are written to ``<file>.part.seg0..N``; only after every
        segment is complete are they merged, size/SHA-1 verified and the
        destination atomically replaced.  Any failure raises and the caller
        falls back to the single-stream path.
        """
        size = spec.size
        if size is None or size <= 0:
            raise DownloadError("分段下载需要已知的文件大小。")
        count = min(
            MAX_SEGMENTS,
            max(2, min(int(segments), size // max(1, MIN_SEGMENT_SIZE))),
        )
        base_length = size // count
        remainder = size % count
        ranges: list[tuple[int, int, int]] = []  # (index, start, length)
        offset = 0
        for index in range(count):
            length = base_length + (1 if index < remainder else 0)
            if length <= 0:
                continue
            ranges.append((index, offset, length))
            offset += length
        if len(ranges) < 2:
            raise DownloadError("文件太小，无法分段下载。")

        segment_paths = {
            index: part.with_name(part.name + f".seg{index}")
            for index, _start, _length in ranges
        }
        positions = {index: 0 for index, _start, _length in ranges}
        lock = threading.Lock()
        downloaded = 0

        def report() -> None:
            if progress is None:
                return
            with lock:
                # Keep the callback inside the lock so concurrent segment
                # reports cannot reach the caller out of order.
                total = sum(positions.values())
                progress(min(total, size))

        def segment_progress(index: int) -> Callable[[int], None]:
            def update(value: int) -> None:
                with lock:
                    positions[index] = max(0, min(value, size))
                report()

            return update

        def task(index: int, start: int, length: int) -> int:
            wait_for_pause(pause, cancel)
            path = segment_paths[index]
            try:
                initial = path.stat().st_size if path.is_file() else 0
            except OSError:
                initial = 0
            if initial > length:
                _unlink_quietly(path)
                initial = 0
            with lock:
                positions[index] = initial
            report()
            if initial == length:
                return 0
            errors: list[str] = []
            for url in spec.urls:
                for attempt in range(self.retries + 1):
                    if cancel is not None and cancel.is_set():
                        raise CancelledError("下载已取消。")
                    try:
                        self._fetch_segment(
                            url,
                            path,
                            start,
                            start + length - 1,
                            length,
                            segment_progress(index),
                            cancel,
                            pause=pause,
                        )
                        final = path.stat().st_size
                        return max(0, final - initial)
                    except _RangeUnsupported:
                        raise
                    except CancelledError:
                        raise
                    except urllib.error.HTTPError as exc:
                        errors.append(f"{url[:100]} -> HTTP {exc.code}")
                        if exc.code in (404, 410, 416):
                            _unlink_quietly(path)
                            initial = 0
                            with lock:
                                positions[index] = 0
                            break
                        if exc.code not in (408, 425, 429, 500, 502, 503, 504):
                            break
                        self._sleep_backoff(
                            attempt,
                            exc.headers.get("Retry-After") if exc.headers else None,
                            cancel,
                        )
                    except DownloadError as exc:
                        message = str(exc)
                        errors.append(f"{url[:100]} -> {message}")
                        if "SHA-1" in message or "大小不符" in message or "范围" in message:
                            _unlink_quietly(path)
                            initial = 0
                            with lock:
                                positions[index] = 0
                            break
                        self._sleep_backoff(attempt, None, cancel)
                    except (
                        urllib.error.URLError,
                        TimeoutError,
                        ssl.SSLError,
                        http.client.HTTPException,
                        OSError,
                    ) as exc:
                        errors.append(f"{url[:100]} -> {self._describe_error(exc)}")
                        self._sleep_backoff(attempt, None, cancel)
            detail = "\n".join(f"  - {item}" for item in errors[-4:])
            raise DownloadError(
                f"分段下载失败（第 {index + 1} 段）：{spec.label or spec.dest}\n"
                f"{detail or '  未知错误'}"
            )

        self._logger(
            f"分段下载 {segments if len(ranges) == segments else len(ranges)} 段："
            f"{spec.label or spec.dest}（{human_size(size)}）"
        )
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(ranges), thread_name_prefix="mc-download"
        ) as executor:
            futures = [
                executor.submit(task, index, start, length)
                for index, start, length in ranges
            ]
            try:
                for future in concurrent.futures.as_completed(futures):
                    downloaded += future.result()
            except CancelledError:
                for future in futures:
                    future.cancel()
                raise
            except Exception:
                for future in futures:
                    future.cancel()
                raise

        if cancel is not None and cancel.is_set():
            raise CancelledError("下载已取消。")

        total_size = 0
        for index, _start, length in ranges:
            path = segment_paths[index]
            if not path.is_file():
                raise DownloadError("分段下载缺少数据，已回退单线程。")
            actual = path.stat().st_size
            if actual != length:
                raise DownloadError(
                    f"分段大小不符（第 {index + 1} 段）：期望 {length}，实际 {actual}"
                )
            total_size += actual
        if total_size != size:
            raise DownloadError(
                f"分段合并大小不符：期望 {size}，实际 {total_size}"
            )

        with part.open("wb") as output:
            for index, _start, _length in ranges:
                segment = segment_paths[index]
                with segment.open("rb") as source:
                    while True:
                        wait_for_pause(pause, cancel)
                        if cancel is not None and cancel.is_set():
                            raise CancelledError("下载已取消。")
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                _unlink_quietly(segment)
            output.flush()
            try:
                os.fsync(output.fileno())
            except OSError:
                pass

        if spec.sha1:
            actual_sha1 = sha1_file(part)
            if actual_sha1.lower() != spec.sha1.lower():
                _unlink_quietly(part)
                raise DownloadError(
                    f"SHA-1 校验失败：期望 {spec.sha1}，实际 {actual_sha1}"
                )
        if progress is not None:
            progress(size)
        os.replace(part, spec.dest)
        self._logger(f"分段下载完成：{spec.label or spec.dest.name}")
        return downloaded

    def _fetch_segment(
        self,
        url: str,
        path: Path,
        start: int,
        end: int,
        length: int,
        progress: Callable[[int], None] | None,
        cancel: threading.Event | None,
        *,
        pause: PauseToken | None = None,
    ) -> int:
        """Fetch ``bytes=start-end`` into ``path``, resuming an existing part."""
        existing = path.stat().st_size if path.is_file() else 0
        if existing > length:
            _unlink_quietly(path)
            existing = 0
        if existing == length:
            if progress is not None:
                progress(existing)
            return 0
        absolute_start = start + existing
        headers = {"Range": f"bytes={absolute_start}-{end}"}
        with self.open(url, headers=headers) as response:
            self.validate_redirect(response.geturl())
            status = getattr(response, "status", response.getcode())
            if status == 200:
                raise _RangeUnsupported("服务器忽略了 Range 请求。")
            if status != 206:
                raise DownloadError(f"意外的 HTTP 状态：{status}")
            content_range = response.headers.get("Content-Range", "")
            if not content_range.startswith(f"bytes {absolute_start}-"):
                _unlink_quietly(path)
                raise DownloadError("服务器返回了无法验证的分段范围。")
            limit = length - existing
            length_header = response.headers.get("Content-Length")
            if length_header:
                try:
                    declared = int(length_header)
                except ValueError:
                    declared = 0
                if declared and declared > limit:
                    raise DownloadError("服务器返回的分段超过允许大小。")
            if progress is not None:
                progress(existing)
            written = 0
            mode = "ab" if existing else "wb"
            with path.open(mode) as output:
                while True:
                    wait_for_pause(pause, cancel)
                    if cancel is not None and cancel.is_set():
                        raise CancelledError("下载已取消。")
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > limit:
                        raise DownloadError("下载内容超过允许大小。")
                    output.write(chunk)
                    if progress is not None:
                        try:
                            progress(existing + written)
                        except Exception:
                            pass
                output.flush()
                try:
                    os.fsync(output.fileno())
                except OSError:
                    pass
        if existing + written != length:
            raise DownloadError(
                f"分段大小不符：期望 {length}，实际 {existing + written}"
            )
        return written

    @staticmethod
    def _remove_segments(part: Path) -> None:
        try:
            pattern = glob.escape(part.name) + ".seg*"
            for candidate in part.parent.glob(pattern):
                _unlink_quietly(candidate)
        except OSError:
            pass

    # -- Helpers ----------------------------------------------------

    def _sleep_backoff(
        self,
        attempt: int,
        retry_after: str | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        delay = min(8.0, 0.75 * (2**attempt))
        if retry_after:
            try:
                delay = max(delay, min(30.0, float(retry_after)))
            except ValueError:
                pass
        end = time.monotonic() + delay
        while True:
            if cancel is not None and cancel.is_set():
                raise CancelledError("下载已取消。")
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.2, remaining))

    @staticmethod
    def _describe_error(error: Exception | None) -> str:
        if error is None:
            return "未知错误"
        if isinstance(error, urllib.error.HTTPError):
            return f"HTTP {error.code} {error.reason}"
        if isinstance(error, urllib.error.URLError):
            return f"网络错误：{error.reason}"
        return str(error) or error.__class__.__name__


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_matches(
    path: Path, sha1: str | None, size: int | None, *, deep: bool
) -> bool:
    try:
        if size is not None and path.stat().st_size != size:
            return False
        if deep and sha1:
            return sha1_file(path).lower() == sha1.lower()
        return True
    except OSError:
        return False


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def human_size(value: int | float) -> str:
    value = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(value) < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"
