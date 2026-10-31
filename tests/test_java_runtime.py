"""Tests for the user-consented Java runtime installer."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from mc_launcher.core.errors import LauncherError, VersionError
from mc_launcher.core.java_runtime import (
    COMPONENT_FOR_MAJOR,
    MARKER_NAME,
    ComponentManifest,
    JavaRuntimeCatalog,
    JavaRuntimeComponent,
    RuntimeFile,
    _parse_catalog,
    _parse_component_manifest,
    build_install_plan,
    choose_component,
    default_install_root,
    find_installed_runtime,
    format_component_table,
    format_runtime_report,
    install_java_runtime,
    manual_java_links,
    resolve_component,
    scan_installed_runtimes,
    validate_install_root,
    verify_java_runtime,
)
from mc_launcher.core.mirrors import get_source
from mc_launcher.core.net import CancelledError, DownloadError

REFERENCE_SHA = "89ce85ccb518c62e18b4b58d63399ba2d9611426"


def _sha1(payload: bytes) -> str:
    return hashlib.sha1(payload).hexdigest()


def _component(
    component_id: str = "java-runtime-gamma",
    version: str = "17.0.15",
    major: int = 17,
    *,
    platform: str = "windows-x64",
    experimental: bool = False,
) -> JavaRuntimeComponent:
    return JavaRuntimeComponent(
        id=component_id,
        version=version,
        platform=platform,
        manifest_url=(
            "https://piston-meta.mojang.com/v1/packages/"
            f"{REFERENCE_SHA}/manifest.json"
        ),
        manifest_sha1=REFERENCE_SHA,
        manifest_size=None,
        java_major=major,
        experimental=experimental,
    )


def _manifest(
    entries: list[tuple[str, bytes]],
    *,
    component: JavaRuntimeComponent | None = None,
    directories: list[str] | None = None,
) -> ComponentManifest:
    component = component or _component()
    files = [
        RuntimeFile(
            relative=Path(path),
            url=f"https://piston-data.mojang.com/v1/objects/{_sha1(data)}/{Path(path).name}",
            sha1=_sha1(data),
            size=len(data),
            executable=path.endswith(".exe"),
        )
        for path, data in entries
    ]
    return ComponentManifest(
        component=component,
        files=files,
        directories=[Path(item) for item in (directories or [])],
        total_bytes=sum(item.size for item in files),
        manifest_sha1=component.manifest_sha1,
    )


class FakeClient:
    """Minimal stand-in for HttpClient (no network, records calls)."""

    def __init__(self, payloads: dict[str, bytes] | None = None) -> None:
        self.payloads = payloads or {}
        self.downloaded: list[str] = []
        self.reads: list[str] = []

    def read_bytes(self, url: str, max_size: int | None = None) -> bytes:
        self.reads.append(url)
        if url not in self.payloads:
            raise DownloadError(f"未找到：{url}")
        return self.payloads[url]

    def download_file(self, spec, *, progress=None, cancel=None, pause=None, segments=None) -> int:
        if cancel is not None and cancel.is_set():
            raise CancelledError("已取消")
        for url in spec.urls:
            if url in self.payloads:
                data = self.payloads[url]
                break
        else:
            raise DownloadError(f"未找到：{spec.url}")
        if spec.size is not None and len(data) != spec.size:
            raise DownloadError("大小不符")
        if spec.sha1 and _sha1(data) != spec.sha1:
            raise DownloadError("SHA-1 校验失败")
        spec.dest.parent.mkdir(parents=True, exist_ok=True)
        if progress is not None:
            progress(0)
            progress(len(data))
        spec.dest.write_bytes(data)
        self.downloaded.append(spec.label)
        return len(data)


class JavaRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="mc-launcher-java-"))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.install_root = self.work / "runtime"
        self.source = get_source("mojang")

    def _plan(self, entries, *, root: Path | None = None, component=None):
        manifest = _manifest(entries, component=component)
        self._payload_data = {
            item.relative.as_posix(): data for item, (_path, data) in zip(manifest.files, entries)
        }
        return build_install_plan(
            manifest.component, manifest, root or self.install_root, self.source
        )

    def _payloads(self, plan) -> dict[str, bytes]:
        return {
            item.urls[0]: self._payload_data[item.file.relative.as_posix()]
            for item in plan.files
        }

    # -- catalog -----------------------------------------------------

    def test_catalog_parses_components_and_ignores_launcher_helper(self) -> None:
        payload = json.dumps(
            {
                "windows-x64": {
                    "jre-legacy": [
                        {
                            "manifest": {
                                "url": "https://piston-meta.mojang.com/v1/packages/a/manifest.json",
                                "sha1": "a" * 40,
                                "size": 100,
                            },
                            "version": {"name": "8u51-cacert462b08"},
                        }
                    ],
                    "java-runtime-gamma": [
                        {
                            "manifest": {
                                "url": "https://piston-meta.mojang.com/v1/packages/b/manifest.json",
                                "sha1": "b" * 40,
                                "size": 200,
                            },
                            "version": {"name": "17.0.15"},
                        },
                        {
                            "manifest": {
                                "url": "https://piston-meta.mojang.com/v1/packages/c/manifest.json",
                                "sha1": "c" * 40,
                                "size": 201,
                            },
                            "version": {"name": "17.0.9"},
                        },
                    ],
                    "minecraft-java-exe": [
                        {
                            "manifest": {
                                "url": "https://piston-meta.mojang.com/v1/packages/d/manifest.json",
                                "sha1": "d" * 40,
                            },
                            "version": {"name": "14"},
                        }
                    ],
                },
                "linux": {},
            }
        ).encode()
        catalog = _parse_catalog(self.source, payload)
        components = catalog.components()
        self.assertEqual([item.id for item in components], ["java-runtime-gamma", "jre-legacy"])
        gamma = catalog.find("java-runtime-gamma")
        self.assertIsNotNone(gamma)
        self.assertEqual(gamma.version, "17.0.15")
        self.assertEqual(gamma.java_major, 17)
        self.assertIsNone(catalog.find("minecraft-java-exe"))
        self.assertEqual(COMPONENT_FOR_MAJOR[17], "java-runtime-gamma")

    def test_catalog_snapshot_is_marked_experimental(self) -> None:
        payload = json.dumps(
            {
                "windows-x64": {
                    "java-runtime-gamma-snapshot": [
                        {
                            "manifest": {
                                "url": "https://piston-meta.mojang.com/v1/packages/e/manifest.json",
                                "sha1": "e" * 40,
                            },
                            "version": {"name": "17.0.15"},
                        }
                    ]
                }
            }
        ).encode()
        catalog = _parse_catalog(self.source, payload)
        component = catalog.find("java-runtime-gamma-snapshot")
        self.assertTrue(component.experimental)
        # A stable runtime with the same major would win, but here there is
        # only the snapshot; resolving by major still finds it.
        self.assertEqual(resolve_component(catalog, "17").id, "java-runtime-gamma-snapshot")

    def test_choose_component_prefers_exact_then_newer_then_lower(self) -> None:
        items = [
            _component("jre-legacy", "8u51", 8),
            _component("java-runtime-beta", "17.0.15", 17),
            _component("java-runtime-gamma", "17.0.15", 17),
            _component("java-runtime-delta", "21.0.7", 21),
        ]
        self.assertEqual(choose_component(items, 17).id, "java-runtime-gamma")
        self.assertEqual(choose_component(items, 20).id, "java-runtime-delta")
        self.assertEqual(choose_component(items, 8).id, "jre-legacy")
        self.assertEqual(choose_component([], 17), None)

    def test_catalog_round_trip_through_cache(self) -> None:
        raw = json.dumps(
            {
                "windows-x64": {
                    "jre-legacy": [
                        {
                            "manifest": {
                                "url": "https://piston-meta.mojang.com/v1/packages/f/manifest.json",
                                "sha1": "f" * 40,
                            },
                            "version": {"name": "8u51"},
                        }
                    ]
                }
            }
        ).encode()
        client = FakeClient({"__catalog__": raw})
        from mc_launcher.core.java_runtime import JAVA_RUNTIME_MANIFEST_URL, fetch_catalog

        # Override the constant so the fake client can serve the URL.
        with mock.patch(
            "mc_launcher.core.java_runtime.JAVA_RUNTIME_MANIFEST_URL",
            "__catalog__",
            create=False,
        ):
            catalog = fetch_catalog(
                self.source, client=client, cache_root=self.work / "cache"
            )
        self.assertEqual(catalog.find("jre-legacy").version, "8u51")
        self.assertTrue((self.work / "cache" / "mojang" / "java-runtime" / "all.json").is_file())

        # No network: the short-lived cache must answer.
        catalog2 = fetch_catalog(
            self.source, client=FakeClient({}), cache_root=self.work / "cache"
        )
        self.assertTrue(catalog2.from_cache)
        self.assertEqual(catalog2.find("jre-legacy").version, "8u51")

    # -- manifest safety --------------------------------------------

    def test_manifest_rejects_path_traversal(self) -> None:
        payload = json.dumps(
            {
                "files": {
                    "../evil.dll": {
                        "type": "file",
                        "downloads": {
                            "raw": {
                                "url": "https://piston-data.mojang.com/v1/objects/a/evil.dll",
                                "sha1": "a" * 40,
                                "size": 1,
                            }
                        },
                    }
                }
            }
        ).encode()
        with self.assertRaises(VersionError):
            _parse_component_manifest(_component(), payload, "b" * 40)

    def test_manifest_rejects_links_and_lzma_only(self) -> None:
        link_payload = json.dumps(
            {
                "files": {
                    "bin/java.exe": {
                        "type": "link",
                        "target": "../outside",
                    }
                }
            }
        ).encode()
        with self.assertRaises(VersionError):
            _parse_component_manifest(_component(), link_payload, "b" * 40)

        lzma_only = json.dumps(
            {
                "files": {
                    "bin/java.exe": {
                        "type": "file",
                        "downloads": {
                            "lzma": {
                                "url": "https://piston-data.mojang.com/v1/objects/a/java.exe",
                                "sha1": "a" * 40,
                                "size": 1,
                            }
                        },
                    }
                }
            }
        ).encode()
        with self.assertRaises(VersionError):
            _parse_component_manifest(_component(), lzma_only, "b" * 40)

    def test_manifest_rejects_file_directory_conflict(self) -> None:
        payload = json.dumps(
            {
                "files": {
                    "bin": {
                        "type": "file",
                        "downloads": {
                            "raw": {
                                "url": "https://piston-data.mojang.com/v1/objects/a/bin",
                                "sha1": "a" * 40,
                                "size": 1,
                            }
                        },
                    },
                    "bin/java.exe": {
                        "type": "file",
                        "downloads": {
                            "raw": {
                                "url": "https://piston-data.mojang.com/v1/objects/b/java.exe",
                                "sha1": "b" * 40,
                                "size": 1,
                            }
                        },
                    },
                }
            }
        ).encode()
        with self.assertRaises(VersionError):
            _parse_component_manifest(_component(), payload, "c" * 40)

    # -- install ----------------------------------------------------

    def test_install_requires_explicit_confirmation(self) -> None:
        plan = self._plan([("bin/java.exe", b"fake")])
        client = FakeClient(self._payloads(plan))
        with self.assertRaises(LauncherError):
            install_java_runtime(plan, client=client, confirmed=False)
        self.assertFalse(self.install_root.exists())
        self.assertEqual(client.downloaded, [])

    def test_install_writes_files_marker_and_verifies(self) -> None:
        entries = [
            ("bin/java.exe", b"fake-java"),
            ("bin/server/jvm.cfg", b"cfg"),
            ("lib/modules", b"modules"),
            ("lib/security/trusted.libraries", b""),
        ]
        plan = self._plan(entries, root=self.install_root)
        client = FakeClient(self._payloads(plan))
        progress = []
        logs = []
        outcome = install_java_runtime(
            plan,
            client=client,
            confirmed=True,
            workers=2,
            progress=progress.append,
            log=lambda message, level="info": logs.append((level, message)),
        )
        self.assertEqual(outcome.downloaded_files, 4)
        self.assertGreater(outcome.downloaded_bytes, 0)
        self.assertTrue(plan.java_path.is_file())
        marker = json.loads((plan.install_dir / MARKER_NAME).read_text(encoding="utf-8"))
        self.assertEqual(marker["schema"], 1)
        self.assertEqual(marker["component"], plan.component.id)
        self.assertEqual(len(marker["files"]), 4)
        # The fake java.exe is not a real executable, so the installer reports
        # a warning instead of claiming success.
        self.assertIsNone(outcome.runtime)
        self.assertTrue(outcome.warnings)
        self.assertTrue(progress)
        self.assertEqual(progress[-1].total_files, 4)
        self.assertEqual(progress[-1].done_files, 4)

        report = verify_java_runtime(plan.install_dir)
        self.assertTrue(report.managed)
        self.assertTrue(report.healthy)
        self.assertEqual(report.ok, 4)

    def test_repair_only_downloads_missing_and_corrupt_files(self) -> None:
        entries = [
            ("bin/java.exe", b"fake-java"),
            ("lib/a.jar", b"aaaa"),
            ("lib/b.jar", b"bbbb"),
        ]
        plan = self._plan(entries)
        client = FakeClient(self._payloads(plan))
        install_java_runtime(plan, client=client, confirmed=True)
        self.assertEqual(len(client.downloaded), 3)

        # Corrupt one file and remove another; the intact file must be skipped.
        (plan.install_dir / "lib" / "a.jar").write_bytes(b"broken")
        (plan.install_dir / "lib" / "b.jar").unlink()
        client.downloaded.clear()
        outcome = install_java_runtime(plan, client=client, confirmed=True)
        self.assertEqual(outcome.skipped_files, 1)
        self.assertEqual(outcome.downloaded_files, 2)
        self.assertEqual(sorted(client.downloaded), ["lib/a.jar", "lib/b.jar"])
        self.assertTrue(verify_java_runtime(plan.install_dir).healthy)

    def test_cancel_leaves_no_marker(self) -> None:
        plan = self._plan([("bin/java.exe", b"fake")])
        client = FakeClient(self._payloads(plan))
        event = threading.Event()
        event.set()
        with self.assertRaises(CancelledError):
            install_java_runtime(
                plan, client=client, confirmed=True, cancel=event
            )
        self.assertFalse((plan.install_dir / MARKER_NAME).exists())

    def test_verify_reports_missing_and_corrupt(self) -> None:
        plan = self._plan(
            [("bin/java.exe", b"fake"), ("lib/a.jar", b"aaaa"), ("lib/b.jar", b"bbbb")]
        )
        install_java_runtime(plan, client=FakeClient(self._payloads(plan)), confirmed=True)
        (plan.install_dir / "lib" / "a.jar").write_bytes(b"zzzz")
        (plan.install_dir / "lib" / "b.jar").unlink()
        report = verify_java_runtime(plan.install_dir)
        self.assertEqual(len(report.missing), 1)
        self.assertEqual(len(report.corrupt), 1)
        self.assertFalse(report.healthy)
        self.assertIn("需要补全", format_runtime_report(report))

        # Fast verification only notices the missing file.
        fast = verify_java_runtime(plan.install_dir, deep=False)
        self.assertEqual(len(fast.missing), 1)
        self.assertEqual(len(fast.corrupt), 0)

    def test_install_rejects_unwritable_style_invalid_root(self) -> None:
        plan = self._plan([("bin/java.exe", b"fake")])
        with self.assertRaises(LauncherError):
            build_install_plan(
                plan.component, _manifest([("bin/java.exe", b"fake")]), "", self.source
            )

    def test_install_root_rejects_drive_root(self) -> None:
        root = Path.cwd().anchor or "/"
        if not root:
            self.skipTest("no drive root on this platform")
        with self.assertRaises(LauncherError):
            validate_install_root(root)

    def test_plan_uses_component_subfolder(self) -> None:
        plan = self._plan([("bin/java.exe", b"fake")])
        self.assertEqual(plan.install_dir, (self.install_root / "java-runtime-gamma").resolve())
        self.assertEqual(plan.java_path.name, "java.exe")
        self.assertTrue(plan.marker_path.name == MARKER_NAME)
        self.assertTrue(plan.directories)
        self.assertIn(Path("bin"), plan.directories)

    # -- discovery / links ------------------------------------------

    def test_scan_and_find_installed_runtimes(self) -> None:
        plan = self._plan([("bin/java.exe", b"fake")])
        install_java_runtime(plan, client=FakeClient(self._payloads(plan)), confirmed=True)
        unmanaged = self.install_root / "my-jdk" / "bin"
        unmanaged.mkdir(parents=True)
        (unmanaged / "java.exe").write_bytes(b"fake")

        found = scan_installed_runtimes([self.install_root])
        self.assertEqual({item.component_id for item in found}, {"java-runtime-gamma", "my-jdk"})
        self.assertTrue(next(item for item in found if item.component_id == "java-runtime-gamma").managed)
        self.assertFalse(next(item for item in found if item.component_id == "my-jdk").managed)

        located = find_installed_runtime(self.install_root, "java-runtime-gamma")
        self.assertIsNotNone(located)
        self.assertTrue(located.managed)
        self.assertIsNone(find_installed_runtime(self.install_root, "..\\escape"))

    def test_default_install_root_is_below_game_root(self) -> None:
        game = self.work / "game"
        self.assertEqual(default_install_root(game), (game / "runtime").resolve())

    def test_manual_links_include_mojang_and_vendor_pages(self) -> None:
        links = manual_java_links(21, _component("java-runtime-delta", "21.0.7", 21))
        labels = [item.label for item in links]
        self.assertTrue(any("Mojang" in item for item in labels))
        self.assertTrue(any("Adoptium" in item for item in labels))
        self.assertTrue(any("Microsoft" in item for item in labels))
        urls = [item.url for item in links]
        self.assertTrue(any("launchermeta.mojang.com" in item for item in urls))
        self.assertTrue(any("adoptium.net" in item for item in urls))
        self.assertTrue(any("version=21" in item for item in urls))

    def test_format_component_table_marks_required_major(self) -> None:
        text = format_component_table(
            [_component("java-runtime-gamma", "17.0.15", 17)], required_major=17
        )
        self.assertIn("java-runtime-gamma", text)
        self.assertIn("匹配当前版本", text)


if __name__ == "__main__":
    unittest.main()
