"""Tests for download planning, verification, repair and security policy."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import zipfile
from pathlib import Path

from mc_launcher.core.errors import LauncherError, VersionError
from mc_launcher.core.install import (
    AssetIndexPlan,
    InstallPlan,
    PlannedFile,
    Progress,
    _download_tasks,
    _ensure_version_json,
    _fabric_library_urls,
    build_plan_from_detail,
    materialize_assets,
    verify_plan,
)
from mc_launcher.core.loaders import (
    ForgeCatalog,
    LoaderBuild,
    _read_forge_profile_id,
    forge_builds,
    forge_installer_official_url,
    select_loader_builds,
)
from mc_launcher.core.manifest import (
    RemoteVersion,
    VersionDetail,
    _parse_manifest_payload,
    fetch_bytes,
    fetch_version_detail,
    filter_versions,
)
from mc_launcher.core.mirrors import get_source
from mc_launcher.core.models import GameRoot
from mc_launcher.core.net import (
    CancelledError,
    DownloadError,
    DownloadSpec,
    HttpClient,
    PauseToken,
    sha1_file,
    validate_url,
)
from mc_launcher.core.paths import ensure_game_root, normalize_game_root
from mc_launcher.core.safety import (
    check_asset_name,
    normalise_sha1,
    safe_component,
    safe_relative_path,
)


class DownloadPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="mc-launcher-test-"))
        self.root = ensure_game_root(self.work / "game")
        self.asset_data = b"asset payload"
        self.asset_hash = hashlib.sha1(self.asset_data).hexdigest()
        self.index_payload = json.dumps(
            {
                "objects": {
                    "lang/en_us.lang": {
                        "hash": self.asset_hash,
                        "size": len(self.asset_data),
                    }
                }
            }
        ).encode("utf-8")
        self.index_hash = hashlib.sha1(self.index_payload).hexdigest()
        (self.root.assets_dir / "indexes").mkdir(parents=True, exist_ok=True)
        (self.root.assets_dir / "indexes" / "5.json").write_bytes(self.index_payload)
        (self.root.assets_dir / "objects" / self.asset_hash[:2]).mkdir(
            parents=True, exist_ok=True
        )
        (self.root.assets_dir / "objects" / self.asset_hash[:2] / self.asset_hash).write_bytes(
            self.asset_data
        )
        self.version = {
            "id": "1.20.1",
            "type": "release",
            "mainClass": "net.minecraft.client.main.Main",
            "assets": "5",
            "assetIndex": {
                "id": "5",
                "sha1": self.index_hash,
                "size": len(self.index_payload),
                "url": "https://piston-meta.mojang.com/v1/packages/"
                "62b36534d83c028050e6fafe8c742879d1f3f1ca/5.json",
            },
            "downloads": {
                "client": {
                    "sha1": "a" * 40,
                    "size": 123,
                    "url": "https://piston-data.mojang.com/v1/objects/"
                    + "a" * 40
                    + "/client.jar",
                }
            },
            "libraries": [
                {
                    "name": "com.example:demo:1.0",
                    "downloads": {
                        "artifact": {
                            "path": "com/example/demo/1.0/demo-1.0.jar",
                            "sha1": "b" * 40,
                            "size": 456,
                            "url": "https://libraries.minecraft.net/"
                            "com/example/demo/1.0/demo-1.0.jar",
                        }
                    },
                }
            ],
            "logging": {
                "client": {
                    "file": {
                        "id": "client-1.12.xml",
                        "sha1": "c" * 40,
                        "size": 9,
                        "url": "https://launchermeta.mojang.com/v1/objects/"
                        + "c" * 40,
                    }
                }
            },
        }
        self.raw = json.dumps(self.version).encode("utf-8")
        self.detail = VersionDetail(
            remote=RemoteVersion(
                id="1.20.1",
                type="release",
                url="https://piston-meta.mojang.com/v1/packages/x/1.20.1.json",
                sha1="",
                release_time=None,
            ),
            data=self.version,
            raw=self.raw,
            sha1=hashlib.sha1(self.raw).hexdigest(),
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)

    def test_build_vanilla_plan_mirror_urls(self) -> None:
        source = get_source("bmclapi")
        plan = build_plan_from_detail(self.root, source, self.detail)
        files = {item.relative.as_posix(): item for item in plan.files}
        self.assertIn("versions/1.20.1/1.20.1.jar", files)
        self.assertIn("libraries/com/example/demo/1.0/demo-1.0.jar", files)
        self.assertIn("assets/indexes/5.json", files)
        self.assertIn(
            f"assets/objects/{self.asset_hash[:2]}/{self.asset_hash}", files
        )
        self.assertIn("assets/log_configs/client-1.12.xml", files)
        client = files["versions/1.20.1/1.20.1.jar"]
        self.assertTrue(client.urls[0].startswith("https://bmclapi2.bangbang93.com/"))
        self.assertTrue(client.urls[1].startswith("https://piston-data.mojang.com/"))
        # Asset objects prefer Mojang's CDN even when the metadata mirror is
        # BMCLAPI, because BMCLAPI rate-limits thousands of tiny requests.
        asset = files[f"assets/objects/{self.asset_hash[:2]}/{self.asset_hash}"]
        self.assertTrue(asset.urls[0].startswith("https://resources.download.minecraft.net/"))
        self.assertTrue(asset.urls[1].startswith("https://bmclapi2.bangbang93.com/assets/"))
        self.assertEqual(plan.asset_index.id, "5")
        self.assertEqual(len(plan.asset_index.objects), 1)

    def test_verify_reports_missing_and_corrupt(self) -> None:
        plan = build_plan_from_detail(self.root, get_source("mojang"), self.detail)
        client_file = next(item for item in plan.files if item.category == "client")
        client_path = client_file.destination(self.root)
        client_path.parent.mkdir(parents=True, exist_ok=True)
        client_path.write_bytes(b"x" * 123)
        # Hash is wrong (all zero bytes), size is correct.
        report = verify_plan(plan, deep=True)
        statuses = {item.file.relative.as_posix(): item.status for item in report.statuses}
        self.assertEqual(
            statuses["versions/1.20.1/1.20.1.jar"], "hash"
        )
        self.assertEqual(
            statuses[f"assets/objects/{self.asset_hash[:2]}/{self.asset_hash}"], "ok"
        )
        self.assertEqual(len(report.problematic), 3)  # client hash + lib + log

    def test_materialize_virtual_assets(self) -> None:
        plan = InstallPlan(
            root=self.root,
            source=get_source("mojang"),
            version_id="1.6.4",
            version_type="release",
            loader="vanilla",
            game_version="1.6.4",
            version_json_relative=Path("versions/1.6.4/1.6.4.json"),
            version_json_payload=b"{}",
            version_json_sha1=hashlib.sha1(b"{}").hexdigest(),
            files=[],
            asset_index=AssetIndexPlan(
                id="legacy",
                relative=Path("assets/indexes/legacy.json"),
                virtual=True,
                map_to_resources=False,
                objects={"lang/en_us.lang": self.asset_hash},
            ),
        )
        created = materialize_assets(plan)
        self.assertGreaterEqual(created, 1)
        target = (
            self.root.assets_dir
            / "virtual"
            / "legacy"
            / "lang"
            / "en_us.lang"
        )
        self.assertEqual(target.read_bytes(), self.asset_data)

    def test_materialize_rejects_traversal_name(self) -> None:
        plan = InstallPlan(
            root=self.root,
            source=get_source("mojang"),
            version_id="1.6.4",
            version_type="release",
            loader="vanilla",
            game_version="1.6.4",
            version_json_relative=Path("versions/1.6.4/1.6.4.json"),
            version_json_payload=b"{}",
            version_json_sha1=hashlib.sha1(b"{}").hexdigest(),
            files=[],
            asset_index=AssetIndexPlan(
                id="legacy",
                relative=Path("assets/indexes/legacy.json"),
                virtual=True,
                map_to_resources=True,
                objects={"../escape.txt": self.asset_hash},
            ),
        )
        materialize_assets(plan)
        self.assertFalse((self.work / "escape.txt").exists())
        self.assertFalse((self.root.root / "escape.txt").exists())

    def test_malicious_library_path_rejected(self) -> None:
        broken = json.loads(json.dumps(self.version))
        broken["libraries"][0]["downloads"]["artifact"]["path"] = "../../evil.jar"
        raw = json.dumps(broken).encode("utf-8")
        detail = VersionDetail(
            remote=self.detail.remote,
            data=broken,
            raw=raw,
            sha1=hashlib.sha1(raw).hexdigest(),
        )
        with self.assertRaises(VersionError):
            build_plan_from_detail(self.root, get_source("mojang"), detail)

    def test_malicious_version_id_rejected(self) -> None:
        with self.assertRaises(VersionError):
            safe_component("../../evil", what="版本 ID")

    def test_http_library_url_rejected(self) -> None:
        broken = json.loads(json.dumps(self.version))
        broken["libraries"][0]["downloads"]["artifact"]["url"] = (
            "http://libraries.minecraft.net/com/example/demo/1.0/demo-1.0.jar"
        )
        raw = json.dumps(broken).encode("utf-8")
        detail = VersionDetail(
            remote=self.detail.remote,
            data=broken,
            raw=raw,
            sha1=hashlib.sha1(raw).hexdigest(),
        )
        with self.assertRaises(VersionError):
            build_plan_from_detail(self.root, get_source("mojang"), detail)


class FakeResponse:
    def __init__(self, payload: bytes, *, status: int = 200, headers=None):
        self._stream = io.BytesIO(payload)
        self.status = status
        self.headers = headers or {"Content-Length": str(len(payload))}
        self._url = "https://example.com/file.bin"

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def geturl(self) -> str:
        return self._url

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class FakeRangeResponse(FakeResponse):
    """A 206 response whose body is the tail of a partially saved file."""

    def __init__(self, payload: bytes, offset: int):
        tail = payload[offset:]
        super().__init__(
            tail,
            status=206,
            headers={
                "Content-Length": str(len(tail)),
                "Content-Range": f"bytes {offset}-{len(payload) - 1}/{len(payload)}",
            },
        )

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


class HttpClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="mc-launcher-http-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)

    def _client(self, response, retries: int = 0) -> HttpClient:
        client = HttpClient(
            {"example.com"}, check_dns=False, allow_private=True, retries=retries
        )
        client.open = types.MethodType(  # type: ignore[method-assign]
            lambda _self, _url, headers=None, method="GET": response, client
        )
        return client

    def test_download_verifies_and_replaces(self) -> None:
        payload = b"hello minecraft"
        dest = self.work / "nested" / "file.bin"
        client = self._client(FakeResponse(payload))
        spec = DownloadSpec(
            dest=dest,
            url="https://example.com/file.bin",
            sha1=hashlib.sha1(payload).hexdigest(),
            size=len(payload),
        )
        progress: list[int] = []
        written = client.download_file(spec, progress=progress.append)
        self.assertEqual(written, len(payload))
        self.assertEqual(dest.read_bytes(), payload)
        self.assertEqual(sum(progress), len(payload))
        self.assertFalse(dest.with_name(dest.name + ".part").exists())

    def test_hash_mismatch_leaves_no_file(self) -> None:
        dest = self.work / "file.bin"
        client = self._client(FakeResponse(b"bad"))
        spec = DownloadSpec(
            dest=dest,
            url="https://example.com/file.bin",
            sha1="0" * 40,
            size=3,
        )
        with self.assertRaises(DownloadError):
            client.download_file(spec)
        self.assertFalse(dest.exists())
        self.assertFalse(dest.with_name(dest.name + ".part").exists())

    def test_zero_byte_file_is_created_without_network(self) -> None:
        # Mojang's Java runtime contains empty files such as
        # lib/security/trusted.libraries.  They must not open a connection and
        # must still produce a verified zero-byte destination.
        dest = self.work / "empty.bin"
        client = HttpClient(
            {"example.com"}, check_dns=False, allow_private=True, retries=0
        )
        calls: list[tuple] = []

        def fake_open(_self, *args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("zero-byte files must not use the network")

        client.open = types.MethodType(fake_open, client)  # type: ignore[method-assign]
        spec = DownloadSpec(
            dest=dest,
            url="https://example.com/empty.bin",
            sha1=hashlib.sha1(b"").hexdigest(),
            size=0,
        )
        written = client.download_file(spec)
        self.assertEqual(written, 0)
        self.assertTrue(dest.is_file())
        self.assertEqual(dest.read_bytes(), b"")
        self.assertEqual(calls, [])
        self.assertFalse(dest.with_name(dest.name + ".part").exists())

    def test_size_mismatch_is_rejected(self) -> None:
        dest = self.work / "file.bin"
        client = self._client(FakeResponse(b"1234"))
        spec = DownloadSpec(
            dest=dest,
            url="https://example.com/file.bin",
            size=5,
        )
        with self.assertRaises(DownloadError):
            client.download_file(spec)
        self.assertFalse(dest.exists())

    def test_fallback_url_used_after_primary_failure(self) -> None:
        payload = b"fallback payload"
        dest = self.work / "fallback.bin"
        client = HttpClient(
            {"example.com"}, check_dns=False, allow_private=True, retries=0
        )

        def fake_open(_self, url, headers=None, method="GET"):
            if url.endswith("/bad"):
                raise DownloadError("primary failed")
            return FakeResponse(payload)

        client.open = types.MethodType(fake_open, client)  # type: ignore[method-assign]
        spec = DownloadSpec(
            dest=dest,
            url="https://example.com/bad",
            fallback_urls=("https://example.com/good",),
            sha1=hashlib.sha1(payload).hexdigest(),
            size=len(payload),
        )
        client.download_file(spec)
        self.assertEqual(dest.read_bytes(), payload)

    def test_cancelled_before_start(self) -> None:
        dest = self.work / "file.bin"
        client = self._client(FakeResponse(b"data"))
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(CancelledError):
            client.download_file(
                DownloadSpec(dest=dest, url="https://example.com/file.bin"),
                cancel=cancel,
            )
        self.assertFalse(dest.exists())

    def test_pause_keeps_partial_until_resumed(self) -> None:
        payload = b"pause-resume-payload"
        dest = self.work / "paused.bin"
        client = self._client(FakeResponse(payload))
        pause = PauseToken()
        pause.pause()
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                client.download_file(
                    DownloadSpec(
                        dest=dest,
                        url="https://example.com/file.bin",
                        size=len(payload),
                    ),
                    pause=pause,
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(timeout=0.4)
        self.assertTrue(thread.is_alive())
        self.assertFalse(dest.exists())
        pause.resume()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(dest.read_bytes(), payload)

    def test_cancel_while_paused_deletes_partial(self) -> None:
        dest = self.work / "cancel-paused.bin"
        client = self._client(FakeResponse(b"cancel-paused-payload"))
        pause = PauseToken()
        cancel = threading.Event()
        pause.pause()
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                client.download_file(
                    DownloadSpec(
                        dest=dest,
                        url="https://example.com/file.bin",
                        size=21,
                    ),
                    pause=pause,
                    cancel=cancel,
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        thread.join(timeout=0.3)
        self.assertTrue(thread.is_alive())
        cancel.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors and isinstance(errors[0], CancelledError))
        self.assertFalse(dest.exists())
        self.assertFalse(dest.with_name(dest.name + ".part").exists())

    def test_validate_url_policy(self) -> None:
        allowed = {"piston-meta.mojang.com", "example.com"}
        self.assertEqual(
            validate_url(
                "https://piston-meta.mojang.com/a",
                allowed,
                check_dns=False,
            ),
            "https://piston-meta.mojang.com/a",
        )
        with self.assertRaises(DownloadError):
            validate_url("http://piston-meta.mojang.com/a", allowed, check_dns=False)
        with self.assertRaises(DownloadError):
            validate_url("file:///etc/passwd", allowed, check_dns=False)
        with self.assertRaises(DownloadError):
            validate_url("https://evil.example/a", allowed, check_dns=False)
        with self.assertRaises(DownloadError):
            validate_url(
                "https://user:pass@example.com/a", allowed, check_dns=False
            )
        with self.assertRaises(DownloadError):
            validate_url(
                "https://example.com:8443/a", allowed, check_dns=False
            )
        self.assertEqual(
            validate_url(
                "https://cdn.example.net/a",
                allowed,
                redirect=True,
                check_dns=False,
            ),
            "https://cdn.example.net/a",
        )

    def test_private_address_rejected(self) -> None:
        with self.assertRaises(DownloadError):
            validate_url(
                "https://127.0.0.1/a",
                {"127.0.0.1"},
                check_dns=True,
            )
        with self.assertRaises(DownloadError):
            validate_url(
                "https://10.0.0.1/a",
                {"10.0.0.1"},
                check_dns=True,
            )

    def test_resume_uses_range_and_appends(self) -> None:
        payload = b"0123456789" * 100
        offset = 437
        dest = self.work / "resume.bin"
        part = dest.with_name(dest.name + ".part")
        dest.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(payload[:offset])
        client = self._client(FakeRangeResponse(payload, offset))
        progress: list[int] = []
        written = client.download_file(
            DownloadSpec(
                dest=dest,
                url="https://example.com/resume.bin",
                sha1=hashlib.sha1(payload).hexdigest(),
                size=len(payload),
            ),
            progress=progress.append,
        )
        self.assertEqual(written, len(payload) - offset)
        self.assertEqual(dest.read_bytes(), payload)
        self.assertFalse(part.exists())
        # Progress values are absolute: the resumed prefix is reported before
        # any new chunk, and the last value is the complete file size.
        self.assertEqual(progress[0], offset)
        self.assertEqual(max(progress), len(payload))

    def test_transient_failure_keeps_partial_file(self) -> None:
        payload = b"abcdefghij"
        dest = self.work / "keep.bin"
        part = dest.with_name(dest.name + ".part")
        dest.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(payload[:4])
        client = HttpClient(
            {"example.com"}, check_dns=False, allow_private=True, retries=0
        )

        def failing_open(_self, _url, headers=None, method="GET"):
            raise urllib.error.URLError("connection reset")

        client.open = types.MethodType(failing_open, client)  # type: ignore[method-assign]
        with self.assertRaises(DownloadError):
            client.download_file(
                DownloadSpec(
                    dest=dest,
                    url="https://example.com/keep.bin",
                    sha1=hashlib.sha1(payload).hexdigest(),
                    size=len(payload),
                )
            )
        self.assertEqual(part.read_bytes(), payload[:4])
        self.assertFalse(dest.exists())

    def test_stale_part_removed_when_destination_is_valid(self) -> None:
        payload = b"already here"
        dest = self.work / "done.bin"
        part = dest.with_name(dest.name + ".part")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        part.write_bytes(b"garbage")
        client = self._client(FakeResponse(b"should not be read"))
        written = client.download_file(
            DownloadSpec(
                dest=dest,
                url="https://example.com/done.bin",
                sha1=hashlib.sha1(payload).hexdigest(),
                size=len(payload),
            )
        )
        self.assertEqual(written, 0)
        self.assertFalse(part.exists())
        self.assertEqual(dest.read_bytes(), payload)


class DownloadExecutionTests(unittest.TestCase):
    """Progress aggregation, plan ordering and version-JSON repair."""

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="mc-launcher-exec-"))
        self.root = ensure_game_root(self.work / "game")

    def tearDown(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)

    def _plan(self, files: list[PlannedFile]) -> InstallPlan:
        payload = b"{}"
        return InstallPlan(
            root=self.root,
            source=get_source("mojang"),
            version_id="1.0",
            version_type="release",
            loader="vanilla",
            game_version="1.0",
            version_json_relative=Path("versions/1.0/1.0.json"),
            version_json_payload=payload,
            version_json_sha1=hashlib.sha1(payload).hexdigest(),
            files=files,
        )

    def test_progress_is_absolute_and_never_double_counts(self) -> None:
        first = PlannedFile(
            Path("libraries/a.jar"),
            urls=("https://libraries.minecraft.net/a.jar",),
            size=100,
            category="library",
        )
        second = PlannedFile(
            Path("libraries/b.jar"),
            urls=("https://libraries.minecraft.net/b.jar",),
            size=300,
            category="library",
        )

        class SlowRetryClient:
            def download_file(self, spec, *, progress=None, cancel=None, pause=None, segments=None):
                size = spec.size or 0
                # Simulate an interrupted attempt followed by a retry that
                # restarts from zero: absolute callbacks must not add up.
                if progress:
                    progress(0)
                    progress(size // 2)
                    progress(0)
                    progress(size // 2)
                    progress(size)
                spec.dest.parent.mkdir(parents=True, exist_ok=True)
                spec.dest.write_bytes(b"x" * size)
                return size

        seen: list[Progress] = []
        _download_tasks(
            self._plan([first, second]),
            [first, second],
            SlowRetryClient(),  # type: ignore[arg-type]
            workers=1,
            progress=seen.append,
            log=lambda _message, _level="info": None,
            cancel=None,
        )
        self.assertTrue(seen)
        self.assertEqual(seen[-1].total_bytes, 400)
        self.assertEqual(seen[-1].done_bytes, 400)
        self.assertEqual(seen[-1].done_files, 2)
        self.assertLessEqual(max(item.done_bytes for item in seen), 400)
        self.assertTrue(seen[-1].current)

    def test_plan_orders_base_files_before_assets(self) -> None:
        detail = VersionDetail(
            remote=RemoteVersion(
                id="1.0",
                type="release",
                url="https://piston-meta.mojang.com/v1/packages/x/1.0.json",
                sha1="",
                release_time=None,
            ),
            data={
                "id": "1.0",
                "type": "release",
                "assets": "legacy",
                "assetIndex": {"id": "legacy"},
                "downloads": {
                    "client": {
                        "sha1": "a" * 40,
                        "size": 1,
                        "url": "https://piston-data.mojang.com/v1/objects/x/client.jar",
                    }
                },
                "libraries": [
                    {
                        "name": "com.example:demo:1.0",
                        "downloads": {
                            "artifact": {
                                "path": "com/example/demo/1.0/demo-1.0.jar",
                                "sha1": "b" * 40,
                                "size": 1,
                                "url": "https://libraries.minecraft.net/com/example/demo/1.0/demo-1.0.jar",
                            }
                        },
                    }
                ],
            },
            raw=b"{}",
            sha1=hashlib.sha1(b"{}").hexdigest(),
        )
        # A locally installed asset index keeps the test offline; the plan
        # then contains client, index, library and (no) assets.
        (self.root.assets_dir / "indexes").mkdir(parents=True, exist_ok=True)
        (self.root.assets_dir / "indexes" / "legacy.json").write_text(
            '{"objects": {}}', encoding="utf-8"
        )
        plan = build_plan_from_detail(self.root, get_source("mojang"), detail)
        categories = [item.category for item in plan.files]
        self.assertEqual(categories[0], "client")
        self.assertEqual(categories[1], "asset_index")
        self.assertIn("library", categories)
        self.assertLess(categories.index("asset_index"), categories.index("library"))

    def test_broken_version_json_is_repaired(self) -> None:
        plan = self._plan([])
        plan.version_json_path.parent.mkdir(parents=True, exist_ok=True)
        plan.version_json_path.write_bytes(b"{ this is not json")
        messages: list[tuple[str, str]] = []
        result = _ensure_version_json(
            plan, lambda message, level="info": messages.append((message, level))
        )
        self.assertEqual(result, "repaired")
        self.assertEqual(plan.version_json_path.read_bytes(), plan.version_json_payload)
        self.assertTrue(any(level == "warning" for _message, level in messages))

    def test_valid_version_json_is_kept(self) -> None:
        plan = self._plan([])
        plan.version_json_path.parent.mkdir(parents=True, exist_ok=True)
        existing = json.dumps({"id": "1.0", "customLauncherField": True}).encode("utf-8")
        plan.version_json_path.write_bytes(existing)
        result = _ensure_version_json(plan, lambda _message, _level="info": None)
        self.assertEqual(result, "kept")
        self.assertEqual(plan.version_json_path.read_bytes(), existing)

    def test_version_json_with_wrong_id_is_repaired(self) -> None:
        plan = self._plan([])
        plan.version_json_path.parent.mkdir(parents=True, exist_ok=True)
        plan.version_json_path.write_text('{"id": "other"}', encoding="utf-8")
        result = _ensure_version_json(plan, lambda _message, _level="info": None)
        self.assertEqual(result, "repaired")
        self.assertEqual(plan.version_json_path.read_bytes(), plan.version_json_payload)


class ManifestLoaderTests(unittest.TestCase):
    def test_filter_kinds(self) -> None:
        payload = json.dumps(
            {
                "latest": {"release": "1.21", "snapshot": "1.21-snapshot-1"},
                "versions": [
                    {
                        "id": "1.21",
                        "type": "release",
                        "url": "https://piston-meta.mojang.com/a/1.21.json",
                        "sha1": "a" * 40,
                        "releaseTime": "2024-01-01T00:00:00Z",
                    },
                    {
                        "id": "1.21-snapshot-1",
                        "type": "snapshot",
                        "url": "https://piston-meta.mojang.com/a/s.json",
                        "sha1": "b" * 40,
                        "releaseTime": "2024-02-01T00:00:00Z",
                    },
                    {
                        "id": "a1.0",
                        "type": "old_alpha",
                        "url": "https://piston-meta.mojang.com/a/a.json",
                        "sha1": "c" * 40,
                        "releaseTime": "2010-01-01T00:00:00Z",
                    },
                ],
            }
        ).encode("utf-8")
        _, manifest = _parse_manifest_payload(get_source("mojang"), payload)
        self.assertEqual(len(manifest.versions), 3)
        self.assertEqual(len(filter_versions(manifest.versions, "release")), 1)
        self.assertEqual(len(filter_versions(manifest.versions, "snapshot")), 1)
        self.assertEqual(len(filter_versions(manifest.versions, "old")), 1)
        self.assertEqual(len(filter_versions(manifest.versions, "all")), 3)

    def test_bad_sha1_is_tolerated(self) -> None:
        payload = json.dumps(
            {
                "versions": [
                    {
                        "id": "1.20.1",
                        "type": "release",
                        "url": "https://piston-meta.mojang.com/a/1.20.1.json",
                        "releaseTime": "2023-06-12T00:00:00Z",
                    }
                ]
            }
        ).encode("utf-8")
        _, manifest = _parse_manifest_payload(get_source("mojang"), payload)
        self.assertEqual(manifest.versions[0].sha1, "")

    def test_corrupt_version_cache_is_refetched(self) -> None:
        work = Path(tempfile.mkdtemp(prefix="mc-launcher-cache-"))
        try:
            source = get_source("mojang")
            remote = RemoteVersion(
                id="1.0",
                type="release",
                url="https://piston-meta.mojang.com/v1/packages/x/1.0.json",
                sha1="",
                release_time=None,
            )
            cache_dir = work / source.id / "versions"
            cache_dir.mkdir(parents=True)
            cache_file = cache_dir / "1.0-.json"
            cache_file.write_bytes(b"this is not json")

            valid = json.dumps({"id": "1.0", "type": "release"}).encode("utf-8")
            client = HttpClient(
                {"piston-meta.mojang.com"}, check_dns=False, allow_private=True, retries=0
            )

            def fake_open(_self, url, headers=None, method="GET"):
                response = FakeResponse(valid)
                response._url = url
                return response

            client.open = types.MethodType(fake_open, client)  # type: ignore[method-assign]
            detail = fetch_version_detail(
                source, remote, client=client, cache_root=work
            )
            self.assertEqual(detail.data["id"], "1.0")
            self.assertEqual(cache_file.read_bytes(), valid)
        finally:
            shutil.rmtree(work, ignore_errors=True)


class LoaderTests(unittest.TestCase):
    def test_fabric_urls_include_bmclapi_fallback(self) -> None:
        from mc_launcher.core.loaders import _fabric_urls

        urls = _fabric_urls(
            get_source("mojang"), "v2/versions/loader/1.20.1"
        )
        self.assertIn(
            "https://meta.fabricmc.net/v2/versions/loader/1.20.1", urls
        )
        self.assertIn(
            "https://bmclapi2.bangbang93.com/fabric-meta/v2/versions/loader/1.20.1",
            urls,
        )

    def test_fabric_channel_selection(self) -> None:
        builds = [
            LoaderBuild("fabric", "1.20.1", "0.16.9", stable=True, recommended=True),
            LoaderBuild("fabric", "1.20.1", "0.17.0", stable=False),
        ]
        stable = select_loader_builds(builds, "stable")
        self.assertEqual([item.version for item in stable], ["0.16.9"])
        latest = select_loader_builds(builds, "latest")
        self.assertEqual(latest[0].version, "0.16.9")

    def test_forge_channels(self) -> None:
        catalog = ForgeCatalog(
            versions={"1.20.1": ["47.4.0", "47.3.0", "47.2.0"]},
            promotions={
                "1.20.1-recommended": "47.3.0",
                "1.20.1-latest": "47.4.0",
            },
        )
        stable = forge_builds(catalog, "1.20.1", "stable")
        self.assertEqual([item.version for item in stable], ["47.3.0"])
        self.assertTrue(stable[0].recommended)
        latest = forge_builds(catalog, "1.20.1", "latest")
        self.assertEqual([item.version for item in latest], ["47.4.0"])
        all_builds = forge_builds(catalog, "1.20.1", "all")
        self.assertEqual(len(all_builds), 3)
        self.assertEqual(
            forge_installer_official_url("1.20.1", "47.3.0"),
            "https://maven.minecraftforge.net/net/minecraftforge/forge/"
            "1.20.1-47.3.0/forge-1.20.1-47.3.0-installer.jar",
        )
        self.assertEqual(
            latest[0].version_id, "1.20.1-forge-47.4.0"
        )

    def test_read_forge_profile_id(self) -> None:
        work = Path(tempfile.mkdtemp(prefix="mc-launcher-forge-"))
        try:
            installer = work / "installer.jar"
            with zipfile.ZipFile(installer, "w") as archive:
                archive.writestr(
                    "install_profile.json",
                    json.dumps(
                        {
                            "version": "1.20.1-forge-47.4.0",
                            "minecraft": "1.20.1",
                        }
                    ),
                )
            version_id, game = _read_forge_profile_id(installer)
            self.assertEqual(version_id, "1.20.1-forge-47.4.0")
            self.assertEqual(game, "1.20.1")
        finally:
            shutil.rmtree(work, ignore_errors=True)


class PathSafetyTests(unittest.TestCase):
    def test_safe_component(self) -> None:
        self.assertEqual(safe_component("1.14 Pre-Release 1"), "1.14 Pre-Release 1")
        self.assertEqual(safe_component("a "), "a")
        for bad in ("", ".", "..", "a/b", "a\\b", "CON", "a:b", "a."):
            with self.assertRaises(VersionError):
                safe_component(bad)

    def test_safe_relative_path(self) -> None:
        self.assertEqual(
            safe_relative_path("com/example/demo/1.0/demo-1.0.jar").as_posix(),
            "com/example/demo/1.0/demo-1.0.jar",
        )
        for bad in ("../x", "/x", "C:/x", "a/../../x"):
            with self.assertRaises(VersionError):
                safe_relative_path(bad)

    def test_asset_name(self) -> None:
        self.assertEqual(check_asset_name("lang/en_us.lang").as_posix(), "lang/en_us.lang")
        with self.assertRaises(VersionError):
            check_asset_name("../escape")

    def test_sha1(self) -> None:
        self.assertEqual(normalise_sha1("A" * 40), "a" * 40)
        with self.assertRaises(VersionError):
            normalise_sha1("xyz")

    def test_ensure_game_root_creates_layout(self) -> None:
        work = Path(tempfile.mkdtemp(prefix="mc-launcher-root-"))
        try:
            root = ensure_game_root(work / "new")
            self.assertTrue(root.versions_dir.is_dir())
            self.assertTrue(root.libraries_dir.is_dir())
            self.assertTrue(root.assets_dir.is_dir())
            normalized = normalize_game_root(root.root)
            self.assertEqual(normalized.root, root.root.resolve())
        finally:
            shutil.rmtree(work, ignore_errors=True)


class SourcePolicyTests(unittest.TestCase):
    def test_fabric_maven_uses_bmclapi_first_even_for_official_source(self) -> None:
        urls = _fabric_library_urls(
            get_source("mojang"),
            "https://maven.fabricmc.net/net/fabricmc/intermediary/"
            "1.21.11/intermediary-1.21.11.jar",
        )
        self.assertEqual(
            urls[0],
            "https://bmclapi2.bangbang93.com/maven/net/fabricmc/intermediary/"
            "1.21.11/intermediary-1.21.11.jar",
        )
        self.assertTrue(
            any("maven.fabricmc.net" in item for item in urls), urls
        )

    def test_all_provider_urls_https(self) -> None:
        for source_id in ("mojang", "bmclapi"):
            source = get_source(source_id)
            for url in (
                source.manifest_url,
                source.fallback_manifest_url,
                source.fabric_meta,
                source.forge_maven,
                source.forge_promotions,
            ):
                self.assertTrue(url.startswith("https://"), (source_id, url))

    def test_no_insecure_patterns_in_source(self) -> None:
        root = Path(__file__).resolve().parent.parent / "mc_launcher"
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            self.assertNotIn("shell=True", text, path)
            self.assertNotIn("verify=False", text, path)
            self.assertNotIn("_create_unverified_context", text, path)


class DestinationLockTests(unittest.TestCase):
    def test_same_destination_is_serialised(self) -> None:
        from mc_launcher.core.net import destination_lock

        work = Path(tempfile.mkdtemp(prefix="mc-launcher-lock-"))
        try:
            target = work / "libraries" / "demo.jar"
            active = 0
            maximum = 0
            guard = threading.Lock()

            def worker() -> None:
                nonlocal active, maximum
                with destination_lock(target):
                    with guard:
                        active += 1
                        maximum = max(maximum, active)
                    time.sleep(0.05)
                    with guard:
                        active -= 1

            threads = [threading.Thread(target=worker) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertEqual(maximum, 1)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_different_destinations_run_in_parallel(self) -> None:
        from mc_launcher.core.net import destination_lock

        work = Path(tempfile.mkdtemp(prefix="mc-launcher-lock-"))
        try:
            a = work / "a.jar"
            b = work / "b.jar"
            entered = threading.Barrier(2, timeout=5)

            def worker(path: Path) -> None:
                with destination_lock(path):
                    entered.wait()

            threads = [
                threading.Thread(target=worker, args=(a,)),
                threading.Thread(target=worker, args=(b,)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertFalse(any(thread.is_alive() for thread in threads))
        finally:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
