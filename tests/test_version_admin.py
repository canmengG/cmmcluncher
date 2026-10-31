"""Tests for local version inventory and safe version deletion."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from mc_launcher.core.errors import LauncherError, VersionError
from mc_launcher.core.paths import ensure_game_root
from mc_launcher.core.version_admin import (
    format_version_table,
    list_version_details,
    remove_version,
    version_details,
)
from mc_launcher.core.version import base_game_version, list_versions


def _write_version(
    root,
    folder: str,
    version_id: str,
    *,
    version_type: str = "release",
    inherits: str | None = None,
    main_class: str = "net.minecraft.client.main.Main",
    payload: bytes = b"jar",
) -> None:
    directory = root.versions_dir / folder
    directory.mkdir(parents=True, exist_ok=True)
    data = {
        "id": version_id,
        "type": version_type,
        "mainClass": main_class,
        "libraries": [],
    }
    if inherits:
        data["inheritsFrom"] = inherits
    (directory / f"{folder}.json").write_text(
        json.dumps(data), encoding="utf-8"
    )
    (directory / f"{folder}.jar").write_bytes(payload)


class VersionAdminTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="mc-launcher-versions-"))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.root = ensure_game_root(self.work / "game")
        (self.root.libraries_dir / "shared.jar").write_bytes(b"l" * 33)
        (self.root.assets_dir / "indexes").mkdir(parents=True, exist_ok=True)
        (self.root.assets_dir / "indexes" / "5.json").write_text(
            '{"objects": {}}', encoding="utf-8"
        )

        _write_version(self.root, "1.20.1", "1.20.1", payload=b"v" * 1000)
        saves = self.root.versions_dir / "1.20.1" / "saves" / "world"
        saves.mkdir(parents=True)
        (saves / "level.dat").write_bytes(b"s" * 500)

        _write_version(
            self.root,
            "1.20.1-forge-47.4.0",
            "1.20.1-forge-47.4.0",
            inherits="1.20.1",
            main_class="cpw.mods.bootstraplauncher.BootstrapLauncher",
            payload=b"f" * 200,
        )
        (self.root.root / "saves").mkdir()
        (self.root.root / "saves" / "shared.dat").write_bytes(b"g" * 21)

    def test_details_include_loader_size_isolation(self) -> None:
        details = list_version_details(self.root)
        self.assertEqual({item.folder_name for item in details}, {"1.20.1", "1.20.1-forge-47.4.0"})
        vanilla = next(item for item in details if item.folder_name == "1.20.1")
        self.assertEqual(vanilla.loader, "原版")
        self.assertTrue(vanilla.isolated)
        self.assertGreaterEqual(vanilla.size_bytes, 1500)
        self.assertGreaterEqual(vanilla.file_count, 3)  # json, jar, level.dat
        self.assertIsNotNone(vanilla.modified)

        forge = next(item for item in details if item.folder_name == "1.20.1-forge-47.4.0")
        self.assertEqual(forge.loader, "Forge")
        self.assertEqual(forge.inherits_from, "1.20.1")
        # Inherited loader versions are isolated by default so they do not read
        # the shared .minecraft/mods folder.
        self.assertTrue(forge.isolated)
        self.assertGreater(forge.size_bytes, 200)

        table = format_version_table(details)
        self.assertIn("1.20.1-forge-47.4.0", table)
        self.assertIn("Forge", table)
        self.assertIn("合计", table)

    def test_detect_neoforge_legacy_and_quilt(self) -> None:
        legacy_id = "1.20.1-forge-47.1.106"
        legacy = self.root.versions_dir / legacy_id
        legacy.mkdir(parents=True)
        (legacy / f"{legacy_id}.json").write_text(
            json.dumps(
                {
                    "id": legacy_id,
                    "inheritsFrom": "1.20.1",
                    "mainClass": "cpw.mods.bootstraplauncher.BootstrapLauncher",
                    "libraries": [
                        {"name": "net.neoforged:forge:1.20.1-47.1.106"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        (legacy / f"{legacy_id}.jar").write_bytes(b"n" * 10)

        quilt_id = "quilt-loader-0.27.0-1.20.1"
        quilt = self.root.versions_dir / quilt_id
        quilt.mkdir(parents=True)
        (quilt / f"{quilt_id}.json").write_text(
            json.dumps(
                {
                    "id": quilt_id,
                    "inheritsFrom": "1.20.1",
                    "mainClass": "org.quiltmc.loader.impl.launch.knot.KnotClient",
                    "libraries": [{"name": "org.quiltmc:quilt-loader:0.27.0"}],
                }
            ),
            encoding="utf-8",
        )
        (quilt / f"{quilt_id}.jar").write_bytes(b"q" * 10)

        details = {item.folder_name: item for item in list_version_details(self.root)}
        self.assertEqual(details[legacy_id].loader, "NeoForge")
        self.assertEqual(details[quilt_id].loader, "Quilt")
        self.assertTrue(details[legacy_id].isolated)
        self.assertTrue(details[quilt_id].isolated)

    def test_base_game_version_through_loader_chain(self) -> None:
        _write_version(
            self.root,
            "fabric-loader-0.19.5-1.20.1",
            "fabric-loader-0.19.5-1.20.1",
            inherits="1.20.1",
        )
        _write_version(
            self.root,
            "test-pack-1.0",
            "test-pack-1.0",
            inherits="fabric-loader-0.19.5-1.20.1",
        )
        summaries = {item.version_id: item for item in list_versions(self.root)}
        self.assertEqual(
            base_game_version(self.root, summaries["test-pack-1.0"]), "1.20.1"
        )
        self.assertEqual(
            base_game_version(self.root, summaries["1.20.1"]), "1.20.1"
        )

    def test_remove_version_keeps_shared_files_and_other_versions(self) -> None:
        details = version_details(
            self.root,
            next(
                item
                for item in list_versions(self.root)
                if item.folder_name == "1.20.1-forge-47.4.0"
            ),
        )
        result = remove_version(self.root, "1.20.1-forge-47.4.0")
        self.assertEqual(result.freed_bytes, details.size_bytes)
        self.assertFalse((self.root.versions_dir / "1.20.1-forge-47.4.0").exists())
        self.assertTrue((self.root.versions_dir / "1.20.1" / "1.20.1.jar").is_file())
        self.assertTrue((self.root.libraries_dir / "shared.jar").is_file())
        self.assertTrue((self.root.assets_dir / "indexes" / "5.json").is_file())
        self.assertTrue((self.root.root / "saves" / "shared.dat").is_file())
        self.assertEqual([item.folder_name for item in list_versions(self.root)], ["1.20.1"])

    def test_remove_by_version_id_when_folder_differs(self) -> None:
        _write_version(self.root, "custom-folder", "1.20.1-custom")
        result = remove_version(self.root, "1.20.1-custom")
        self.assertEqual(result.version_id, "custom-folder")
        self.assertFalse((self.root.versions_dir / "custom-folder").exists())

    def test_missing_version_is_rejected(self) -> None:
        with self.assertRaises(VersionError):
            remove_version(self.root, "0.0-does-not-exist")

    def test_path_traversal_is_rejected(self) -> None:
        for identifier in ("..", "../1.20.1", "sub/1.20.1", "C:\\evil", "."):
            with self.assertRaises(VersionError):
                remove_version(self.root, identifier)
        self.assertTrue((self.root.versions_dir / "1.20.1" / "1.20.1.jar").is_file())

    def test_running_version_is_rejected(self) -> None:
        folder = self.root.versions_dir / "1.20.1"
        with self.assertRaises(VersionError):
            remove_version(self.root, "1.20.1", protected_folders=[folder])
        self.assertTrue((folder / "1.20.1.jar").is_file())

    def test_hidden_trash_folder_is_rejected(self) -> None:
        trash = self.root.versions_dir / ".mc-launcher-delete-deadbeef"
        trash.mkdir()
        (trash / "leftover.bin").write_bytes(b"x")
        with self.assertRaises(VersionError):
            remove_version(self.root, trash.name)
        self.assertTrue(trash.is_dir())

    @unittest.skipUnless(os.name == "nt", "junction test is Windows-only")
    def test_directory_junction_is_rejected(self) -> None:
        outside = self.work / "outside-version"
        outside.mkdir()
        (outside / "1.9.json").write_text(
            '{"id": "1.9", "type": "release", "mainClass": "x"}', encoding="utf-8"
        )
        junction = self.root.versions_dir / "junction-version"
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
            capture_output=True,
            text=True,
        )
        if created.returncode != 0:
            self.skipTest(f"mklink /J unavailable: {created.stderr or created.stdout}")
        try:
            with self.assertRaises(VersionError):
                remove_version(self.root, "junction-version")
            self.assertTrue(junction.exists())
            self.assertTrue((outside / "1.9.json").is_file())
        finally:
            try:
                os.rmdir(junction)
            except OSError:
                pass

    def test_remove_allows_stray_folder_without_json(self) -> None:
        stray = self.root.versions_dir / "stray-folder"
        stray.mkdir()
        (stray / "notes.txt").write_text("x", encoding="utf-8")
        result = remove_version(self.root, "stray-folder")
        self.assertGreater(result.freed_bytes, 0)
        self.assertFalse(stray.exists())


if __name__ == "__main__":
    unittest.main()
