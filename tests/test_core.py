"""Core tests that do not require a real Minecraft installation."""

from __future__ import annotations

import json
import shutil
import sys
import unittest
import zipfile
from pathlib import Path

from mc_launcher.core.auth import offline_uuid
from mc_launcher.core.errors import LauncherError
from mc_launcher.core.install import _maven_url
from mc_launcher.core.java import required_java_major
from mc_launcher.core.launch import build_launch_plan, start_game
from mc_launcher.core.models import LaunchOptions
from mc_launcher.core.paths import (
    ensure_directory_writable,
    has_isolated_game_data,
    normalize_game_root,
    version_game_dir,
)
from mc_launcher.core.version import (
    is_allowed,
    list_versions,
    load_merged_version,
    maven_path,
)

WORK = Path(__file__).resolve().parent / "_work"


class LauncherCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        shutil.rmtree(WORK, ignore_errors=True)
        cls.root = WORK / "game"
        versions = cls.root / "versions"
        libraries = cls.root / "libraries"
        assets = cls.root / "assets"
        log_dir = assets / "log_configs"
        (versions / "1.20.1").mkdir(parents=True)
        (versions / "1.20.1-forge-47.2.0" / "mods").mkdir(parents=True)
        (assets / "indexes").mkdir(parents=True)
        log_dir.mkdir(parents=True)

        (assets / "indexes" / "5.json").write_text('{"objects": {}}', encoding="utf-8")
        (log_dir / "client-1.12.xml").write_text("<Configuration/>", encoding="utf-8")
        (versions / "1.20.1" / "1.20.1.jar").write_bytes(b"fake client jar")

        core_jar = libraries / "com/example/core/1.0/core-1.0.jar"
        core_jar.parent.mkdir(parents=True)
        core_jar.write_bytes(b"fake library")
        native_jar = libraries / "com/example/native/1.0/native-1.0-natives-windows.jar"
        native_jar.parent.mkdir(parents=True)
        with zipfile.ZipFile(native_jar, "w") as archive:
            archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
            archive.writestr("lwjgl.dll", b"fake native")

        forge_jar = libraries / "net/minecraftforge/forge/1.20.1-47.2.0/forge-1.20.1-47.2.0-universal.jar"
        forge_jar.parent.mkdir(parents=True)
        forge_jar.write_bytes(b"fake forge")

        parent = {
            "id": "1.20.1",
            "type": "release",
            "releaseTime": "2023-06-12T00:00:00+00:00",
            "mainClass": "net.minecraft.client.main.Main",
            "assets": "5",
            "assetIndex": {"id": "5"},
            "javaVersion": {"component": "java-runtime-gamma", "majorVersion": 17},
            "libraries": [
                {
                    "name": "com.example:core:1.0",
                    "downloads": {
                        "artifact": {"path": "com/example/core/1.0/core-1.0.jar"}
                    },
                },
                {
                    "name": "com.example:native:1.0",
                    "downloads": {
                        "classifiers": {
                            "natives-windows": {
                                "path": "com/example/native/1.0/native-1.0-natives-windows.jar"
                            }
                        }
                    },
                    "natives": {"windows": "natives-windows"},
                    "extract": {"exclude": ["META-INF/"]},
                },
            ],
            "arguments": {
                "jvm": [
                    "-Djava.library.path=${natives_directory}",
                    "-cp",
                    "${classpath}",
                    "-Dlog4j.configurationFile=${path}",
                ],
                "game": [
                    "--username",
                    "${auth_player_name}",
                    "--version",
                    "${version_name}",
                    "--gameDir",
                    "${game_directory}",
                    "--assetsDir",
                    "${assets_root}",
                    "--assetIndex",
                    "${assets_index_name}",
                    "--uuid",
                    "${auth_uuid}",
                    "--accessToken",
                    "${auth_access_token}",
                    "--userType",
                    "${user_type}",
                    "--versionType",
                    "${version_type}",
                ],
            },
            "logging": {
                "client": {
                    "argument": "-Dlog4j.configurationFile=${path}",
                    "file": {"id": "client-1.12.xml"},
                }
            },
        }
        child = {
            "id": "1.20.1-forge-47.2.0",
            "inheritsFrom": "1.20.1",
            "type": "release",
            "mainClass": "cpw.mods.bootstraplauncher.BootstrapLauncher",
            "libraries": [
                {
                    "name": "net.minecraftforge:forge:1.20.1-47.2.0:universal",
                    "downloads": {
                        "artifact": {
                            "path": "net/minecraftforge/forge/1.20.1-47.2.0/"
                            "forge-1.20.1-47.2.0-universal.jar"
                        }
                    },
                }
            ],
            "arguments": {
                "jvm": ["-DignoreList=client-extra,forge-"],
                "game": ["--launchTarget", "forgeclient"],
            },
        }
        (versions / "1.20.1" / "1.20.1.json").write_text(
            json.dumps(parent), encoding="utf-8"
        )
        (versions / "1.20.1-forge-47.2.0" / "1.20.1-forge-47.2.0.json").write_text(
            json.dumps(child), encoding="utf-8"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(WORK, ignore_errors=True)

    def setUp(self) -> None:
        self.game_root = normalize_game_root(self.root)
        self.versions = list_versions(self.game_root)
        self.forge = next(
            item for item in self.versions if "forge" in item.version_id
        )

    def test_normalize_from_version_folder(self) -> None:
        root = normalize_game_root(self.root / "versions" / "1.20.1")
        self.assertEqual(root.root, self.root.resolve())

    def test_versions_discovered_and_sorted(self) -> None:
        self.assertEqual(len(self.versions), 2)
        self.assertEqual(self.forge.version_id, "1.20.1-forge-47.2.0")

    def test_inheritance_merge(self) -> None:
        loaded = load_merged_version(self.game_root, self.forge)
        self.assertEqual(
            loaded.data["mainClass"], "cpw.mods.bootstraplauncher.BootstrapLauncher"
        )
        self.assertEqual(len(loaded.data["libraries"]), 3)
        game_args = loaded.data["arguments"]["game"]
        self.assertIn("--launchTarget", game_args)
        self.assertIn("forgeclient", game_args)
        self.assertIn("1.20.1", loaded.parent_ids)

    def test_isolation_detection(self) -> None:
        self.assertTrue(has_isolated_game_data(self.forge))
        self.assertEqual(version_game_dir(self.forge), self.forge.folder)

    def test_build_plan(self) -> None:
        natives = WORK / "natives"
        plan = build_launch_plan(
            self.game_root,
            self.forge,
            LaunchOptions(
                username="Steve",
                java_path=Path("java"),
                memory_mb=3072,
                isolated=True,
                natives_dir=natives,
            ),
        )
        self.assertEqual(plan.cwd, self.forge.folder)
        self.assertIn("-Xmx3072M", plan.command)
        self.assertIn("--launchTarget", plan.command)
        self.assertIn("forgeclient", plan.command)
        self.assertIn("--username", plan.command)
        self.assertIn("Steve", plan.command)
        self.assertIn("--assetIndex", plan.command)
        self.assertIn("5", plan.command)
        self.assertIn(f"-Djava.library.path={natives}", plan.command)
        classpath = plan.command[plan.command.index("-cp") + 1]
        self.assertIn(str(self.root / "versions" / "1.20.1" / "1.20.1.jar"), classpath)
        self.assertIn(
            str(
                self.root
                / "libraries/net/minecraftforge/forge/1.20.1-47.2.0/"
                "forge-1.20.1-47.2.0-universal.jar"
            ),
            classpath,
        )
        self.assertTrue((natives / "lwjgl.dll").is_file())
        self.assertFalse(any("${" in part for part in plan.command))
        self.assertEqual(plan.log_config.name, "client-1.12.xml")
        ignore = next(
            part for part in plan.command if part.startswith("-DignoreList=")
        )
        entries = ignore.split("=", 1)[1].split(",")
        # The parent's client jar must be ignored by Forge's module finder;
        # otherwise BootstrapLauncher registers its packages twice.
        self.assertIn("1.20.1.jar", entries)

    def test_start_process_captures_output(self) -> None:
        plan = build_launch_plan(
            self.game_root,
            self.forge,
            LaunchOptions(
                username="Steve",
                java_path=Path("java"),
                memory_mb=1024,
                isolated=True,
                natives_dir=WORK / "natives-start",
            ),
        )
        plan.command = [sys.executable, "-c", "print('launcher-test')"]
        process = start_game(plan)
        output, _ = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0)
        self.assertIn("launcher-test", output)

    def test_write_preflight_reports_non_directory(self) -> None:
        with self.assertRaises(LauncherError):
            ensure_directory_writable(self.root / "versions" / "1.20.1" / "1.20.1.jar")

    def test_maven_path(self) -> None:
        self.assertEqual(
            maven_path("com.example:demo:1.0:natives-windows"),
            Path("com/example/demo/1.0/demo-1.0-natives-windows.jar"),
        )

    def test_maven_url_keeps_plus_for_bmclapi(self) -> None:
        url = _maven_url(
            "https://maven.fabricmc.net",
            Path(
                "net/fabricmc/sponge-mixin/0.12.5+mixin.0.8.5/"
                "sponge-mixin-0.12.5+mixin.0.8.5.jar"
            ),
        )
        self.assertIn("0.12.5+mixin.0.8.5", url)
        self.assertNotIn("%2B", url)

    def test_rules(self) -> None:
        windows_allow = {
            "rules": [{"action": "allow", "os": {"name": "windows"}}]
        }
        windows_deny = {
            "rules": [{"action": "disallow", "os": {"name": "windows"}}]
        }
        self.assertTrue(is_allowed(windows_allow))
        self.assertFalse(is_allowed(windows_deny))

    def test_offline_uuid(self) -> None:
        value = offline_uuid("Steve")
        self.assertEqual(value.version, 3)
        self.assertEqual(value.variant, "specified in RFC 4122")
        self.assertEqual(value.hex, offline_uuid("Steve").hex)

    def test_required_java(self) -> None:
        self.assertEqual(required_java_major({"javaVersion": {"majorVersion": 17}}), 17)
        self.assertEqual(required_java_major({"id": "1.16.5"}), 8)
        self.assertEqual(required_java_major({"id": "1.21.1"}), 21)


if __name__ == "__main__":
    unittest.main()
