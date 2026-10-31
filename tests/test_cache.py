"""Tests for cache accounting and selective clean-up."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mc_launcher.core.cache import (
    CATEGORY_INSTALLERS,
    CATEGORY_MANIFESTS,
    CATEGORY_NATIVES,
    CATEGORY_ORDER,
    CATEGORY_PARTIAL,
    CacheTarget,
    clean_cache,
    format_cache_usage,
    format_clean_report,
    scan_cache,
    total_cache_bytes,
)
from mc_launcher.core.errors import LauncherError
from mc_launcher.core.paths import ensure_game_root


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="mc-launcher-cache-"))
        self.appdata = self.work / "appdata"
        self.localappdata = self.work / "localappdata"
        self.environment = mock.patch.dict(
            os.environ,
            {
                "APPDATA": str(self.appdata),
                "LOCALAPPDATA": str(self.localappdata),
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(shutil.rmtree, self.work, True)

        self.cache_root = self.appdata / "MinecraftLauncher" / "cache"
        self.installer_root = self.localappdata / "MinecraftLauncher" / "installers"
        self.natives_root = self.localappdata / "MinecraftLauncher" / "natives"
        for path in (self.cache_root, self.installer_root, self.natives_root):
            path.mkdir(parents=True, exist_ok=True)

        (self.cache_root / "mojang").mkdir()
        (self.cache_root / "mojang" / "manifest.json").write_bytes(b"m" * 100)
        (self.cache_root / "bmclapi" / "versions").mkdir(parents=True)
        (self.cache_root / "bmclapi" / "versions" / "1.20.1-abc.json").write_bytes(
            b"v" * 40
        )
        (self.cache_root / "other").mkdir()
        (self.cache_root / "other" / "keep.txt").write_bytes(b"k" * 8)

        (self.installer_root / "forge-1.20.1-installer.jar").write_bytes(b"i" * 200)

        native_folder = self.natives_root / "mc-launcher-natives-abc"
        (native_folder / "natives").mkdir(parents=True)
        (native_folder / "natives" / "lwjgl.dll").write_bytes(b"n" * 50)
        (self.natives_root / "keepme").mkdir()
        (self.natives_root / "keepme" / "user.txt").write_bytes(b"u" * 9)

        self.root = ensure_game_root(self.work / "game")
        version = self.root.versions_dir / "1.0"
        version.mkdir()
        (version / "1.0.json").write_text('{"id": "1.0"}', encoding="utf-8")
        (version / "1.0.jar").write_bytes(b"j" * 10)
        (version / "1.0.jar.part").write_bytes(b"p" * 5)
        objects = self.root.assets_dir / "objects" / "aa"
        objects.mkdir(parents=True)
        (objects / ("a" * 40 + ".part")).write_bytes(b"o" * 6)
        (objects / ("a" * 40)).write_bytes(b"x" * 12)
        (self.root.libraries_dir / "lib.jar.part").write_bytes(b"l" * 8)
        (self.root.root / "saves").mkdir()
        (self.root.root / "saves" / "world.dat").write_bytes(b"w" * 7)

    def _usage(self, category):
        return next(item for item in scan_cache(self.root) if item.category == category)

    def test_scan_reports_expected_sizes(self) -> None:
        manifests = self._usage(CATEGORY_MANIFESTS)
        installers = self._usage(CATEGORY_INSTALLERS)
        natives = self._usage(CATEGORY_NATIVES)
        partial = self._usage(CATEGORY_PARTIAL)

        self.assertEqual(manifests.bytes, 140)
        self.assertEqual(manifests.files, 2)
        # Unknown directories are neither counted nor cleaned.
        self.assertNotIn(self.cache_root / "other", manifests.paths)

        self.assertEqual(installers.bytes, 200)
        self.assertEqual(installers.files, 1)
        self.assertEqual(natives.bytes, 50)
        self.assertEqual(natives.files, 1)
        self.assertEqual(partial.bytes, 19)
        self.assertEqual(partial.files, 3)
        self.assertEqual(len(partial.paths), 3)

        text = format_cache_usage(scan_cache(self.root))
        self.assertIn("元数据缓存", text)
        self.assertIn("合计", text)
        self.assertEqual(total_cache_bytes(scan_cache(self.root)), 140 + 200 + 50 + 19)

    def test_clean_manifests_keeps_everything_else(self) -> None:
        report = clean_cache([CATEGORY_MANIFESTS], self.root)
        self.assertTrue(report.ok)
        self.assertEqual(report.freed_bytes, 140)
        self.assertEqual(report.removed_files, 2)
        self.assertFalse((self.cache_root / "mojang").exists())
        self.assertFalse((self.cache_root / "bmclapi").exists())
        self.assertTrue((self.cache_root / "other" / "keep.txt").is_file())
        self.assertTrue((self.installer_root / "forge-1.20.1-installer.jar").is_file())
        self.assertTrue((self.root.versions_dir / "1.0" / "1.0.jar").is_file())
        self.assertTrue((self.root.versions_dir / "1.0" / "1.0.jar.part").is_file())

    def test_clean_installers_and_natives_only(self) -> None:
        report = clean_cache([CATEGORY_INSTALLERS, CATEGORY_NATIVES], self.root)
        self.assertTrue(report.ok)
        self.assertEqual(report.freed_bytes, 250)
        self.assertFalse((self.installer_root / "forge-1.20.1-installer.jar").exists())
        self.assertFalse((self.natives_root / "mc-launcher-natives-abc").exists())
        self.assertTrue((self.natives_root / "keepme" / "user.txt").is_file())
        self.assertTrue((self.root.versions_dir / "1.0" / "1.0.jar").is_file())

    def test_clean_partial_removes_only_part_files(self) -> None:
        report = clean_cache([CATEGORY_PARTIAL], self.root)
        self.assertTrue(report.ok)
        self.assertEqual(report.freed_bytes, 19)
        self.assertFalse((self.root.versions_dir / "1.0" / "1.0.jar.part").exists())
        self.assertFalse((self.root.libraries_dir / "lib.jar.part").exists())
        self.assertTrue((self.root.versions_dir / "1.0" / "1.0.jar").is_file())
        self.assertTrue((self.root.assets_dir / "objects" / "aa" / ("a" * 40)).is_file())
        self.assertTrue((self.root.root / "saves" / "world.dat").is_file())

    def test_unknown_category_is_rejected(self) -> None:
        with self.assertRaises(LauncherError):
            clean_cache(["everything"], self.root)

    def test_target_outside_allowed_base_is_refused(self) -> None:
        outside = self.work / "outside.bin"
        outside.write_bytes(b"do not delete")
        report = clean_cache(
            [CATEGORY_MANIFESTS],
            self.root,
            targets=[CacheTarget(CATEGORY_MANIFESTS, outside)],
        )
        self.assertFalse(report.ok)
        self.assertEqual(report.freed_bytes, 0)
        self.assertTrue(outside.is_file())
        self.assertTrue(any("允许" in item for item in report.errors))

    def test_clean_all_and_report(self) -> None:
        report = clean_cache(None, self.root)
        self.assertTrue(report.ok)
        self.assertEqual(report.freed_bytes, 409)
        self.assertEqual(sorted(report.categories), sorted(CATEGORY_ORDER))
        text = format_clean_report(report)
        self.assertIn("已释放空间", text)
        self.assertTrue((self.root.root / "saves" / "world.dat").is_file())
        self.assertTrue((self.cache_root / "other" / "keep.txt").is_file())


if __name__ == "__main__":
    unittest.main()
