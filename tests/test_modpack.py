"""Tests for launchable modpack instances (0.5.x, no network access)."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from mc_launcher.core.content import ContentError, ModpackIndex, read_mrpack
from mc_launcher.core.modpack import (
    LoaderInstallResult,
    build_instance_plan,
    download_and_install_launchable_modpack,
    install_instance,
    install_modpack_archive,
    resolve_modpack_dependencies,
)
from mc_launcher.core.models import GameRoot
from mc_launcher.core.paths import has_isolated_game_data, version_game_dir
from mc_launcher.core.version import list_versions
from mc_launcher.core.version_admin import detect_loader

MOD_URL = "https://cdn.modrinth.com/data/u6dRKJwZ/versions/qrP02aGf/jei.jar"
MOD_BYTES = b"fake mod jar bytes"


class FakeClient:
    def __init__(self, downloads=None) -> None:
        self.downloads = downloads or {}
        self.calls = []

    def download_file(self, spec, *, progress=None, cancel=None, pause=None, segments=None):
        data = self.downloads.get(spec.url)
        if data is None:
            raise RuntimeError(f"download missing: {spec.url}")
        actual = hashlib.sha1(data).hexdigest()
        if spec.sha1 and actual != spec.sha1:
            raise RuntimeError("sha1 mismatch")
        spec.dest.parent.mkdir(parents=True, exist_ok=True)
        if progress is not None:
            progress(0)
        spec.dest.write_bytes(data)
        if progress is not None:
            progress(len(data))
        self.calls.append(spec.url)
        return len(data)

    def validate_initial(self, url: str) -> str:
        return url


def _write_zip(path: Path, entries: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)


def _make_mrpack(
    path: Path,
    *,
    dependencies: dict | None = None,
    name: str = "Test Pack",
    version_id: str = "1.2.3",
) -> ModpackIndex:
    sha1 = hashlib.sha1(MOD_BYTES).hexdigest()
    payload = {
        "formatVersion": 1,
        "name": name,
        "versionId": version_id,
        "dependencies": dependencies
        or {"minecraft": "1.20.1", "fabric-loader": "0.16.9"},
        "files": [
            {
                "path": "mods/a.jar",
                "hashes": {"sha1": sha1},
                "downloads": [MOD_URL],
                "fileSize": len(MOD_BYTES),
                "env": {"client": "required", "server": "required"},
            }
        ],
    }
    _write_zip(
        path,
        {
            "modrinth.index.json": json.dumps(payload).encode("utf-8"),
            "overrides/config/test.toml": b"enabled = true\n",
            "client-overrides/options.txt": b"fov:0\n",
        },
    )
    return read_mrpack(path)


def _fake_loader_installer(version_id: str = "fabric-loader-0.16.9-1.20.1"):
    def installer(
        *,
        root,
        source,
        game_version,
        loader,
        loader_version,
        java_path,
        progress,
        cancel,
        logger,
        deep,
        workers,
        pause=None,
    ):
        folder = root.versions_dir / version_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{version_id}.json").write_text(
            json.dumps(
                {
                    "id": version_id,
                    "mainClass": "net.fabricmc.loader.impl.launch.knot.KnotClient",
                    "inheritsFrom": game_version,
                    "libraries": [],
                }
            ),
            encoding="utf-8",
        )
        return LoaderInstallResult(version_id=version_id)

    return installer


class DependencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.archive = Path(self.temp.name) / "pack.mrpack"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_fabric_dependency(self) -> None:
        index = _make_mrpack(self.archive)
        deps = resolve_modpack_dependencies(index)
        self.assertEqual(deps.minecraft, "1.20.1")
        self.assertEqual(deps.loader, "fabric")
        self.assertEqual(deps.loader_version, "0.16.9")
        self.assertTrue(deps.launchable)
        self.assertIn("Fabric", deps.loader_label)

    def test_forge_dependency(self) -> None:
        index = _make_mrpack(
            self.archive,
            dependencies={"minecraft": "1.20.1", "forge": "47.4.0"},
        )
        deps = resolve_modpack_dependencies(index)
        self.assertEqual(deps.loader, "forge")
        self.assertTrue(deps.launchable)

    def test_vanilla_pack(self) -> None:
        index = _make_mrpack(
            self.archive, dependencies={"minecraft": "1.20.1"}
        )
        deps = resolve_modpack_dependencies(index)
        self.assertEqual(deps.loader, "vanilla")
        self.assertTrue(deps.launchable)

    def test_neoforge_is_launchable(self) -> None:
        index = _make_mrpack(
            self.archive,
            dependencies={"minecraft": "1.20.1", "neoforge": "21.1.80"},
        )
        deps = resolve_modpack_dependencies(index)
        self.assertTrue(deps.launchable)
        self.assertEqual(deps.loader, "neoforge")
        self.assertEqual(deps.loader_label, "NeoForge 21.1.80")

    def test_quilt_is_launchable(self) -> None:
        index = _make_mrpack(
            self.archive,
            dependencies={"minecraft": "1.20.1", "quilt-loader": "0.26.0"},
        )
        deps = resolve_modpack_dependencies(index)
        self.assertTrue(deps.launchable)
        self.assertEqual(deps.loader, "quilt")
        self.assertIn("Quilt", deps.loader_label)

    def test_missing_minecraft_version(self) -> None:
        index = _make_mrpack(
            self.archive, dependencies={"fabric-loader": "0.16.9"}
        )
        with self.assertRaises(ContentError):
            resolve_modpack_dependencies(index)

    def test_multiple_loaders_rejected(self) -> None:
        index = _make_mrpack(
            self.archive,
            dependencies={
                "minecraft": "1.20.1",
                "fabric-loader": "0.16.9",
                "forge": "47.4.0",
            },
        )
        with self.assertRaises(ContentError):
            resolve_modpack_dependencies(index)


class InstancePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "game"
        self.root.mkdir()
        self.archive = Path(self.temp.name) / "pack.mrpack"
        self.index = _make_mrpack(self.archive)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_isolated_paths(self) -> None:
        plan = build_instance_plan(self.index, self.root, slug="test-pack")
        self.assertEqual(plan.instance_id, "test-pack-1.2.3")
        self.assertEqual(
            plan.version_dir, self.root / "versions" / "test-pack-1.2.3"
        )
        self.assertEqual(plan.game_dir, plan.version_dir)
        self.assertFalse((plan.version_dir / ".minecraft").exists())
        self.assertTrue(plan.launchable)

    def test_legacy_nested_minecraft_is_reused(self) -> None:
        version_dir = self.root / "versions" / "test-pack-1.2.3"
        legacy = version_dir / ".minecraft"
        legacy.mkdir(parents=True)
        (legacy / ".mc-launcher-modpack.json").write_text(
            json.dumps(
                {"name": self.index.name, "version_id": self.index.version_id}
            ),
            encoding="utf-8",
        )
        plan = build_instance_plan(self.index, self.root, slug="test-pack")
        self.assertEqual(plan.instance_id, "test-pack-1.2.3")
        self.assertEqual(plan.game_dir, legacy)

    def test_collision_gets_suffix(self) -> None:
        foreign = self.root / "versions" / "test-pack-1.2.3"
        foreign.mkdir(parents=True)
        (foreign / "user.txt").write_text("keep", encoding="utf-8")
        plan = build_instance_plan(self.index, self.root, slug="test-pack")
        self.assertEqual(plan.instance_id, "test-pack-1.2.3-2")
        self.assertEqual(plan.version_dir.name, "test-pack-1.2.3-2")

    def test_reuses_own_instance(self) -> None:
        plan = build_instance_plan(self.index, self.root, slug="test-pack")
        plan.game_dir.mkdir(parents=True)
        (plan.game_dir / ".mc-launcher-modpack.json").write_text(
            json.dumps({"name": self.index.name, "version_id": self.index.version_id}),
            encoding="utf-8",
        )
        again = build_instance_plan(self.index, self.root, slug="test-pack")
        self.assertEqual(again.instance_id, plan.instance_id)


class LaunchableInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "game"
        self.root.mkdir()
        self.archive = Path(self.temp.name) / "pack.mrpack"
        self.index = _make_mrpack(self.archive)
        self.client = FakeClient({MOD_URL: MOD_BYTES})

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_launchable_instance_layout(self) -> None:
        plan = build_instance_plan(self.index, self.root, slug="test-pack")
        outcome = install_instance(
            plan,
            confirmed=True,
            source=object(),
            client=self.client,
            loader_installer=_fake_loader_installer(),
        )
        self.assertTrue(outcome.complete)
        self.assertTrue(outcome.launchable)
        self.assertEqual(outcome.version_id, "test-pack-1.2.3")
        self.assertEqual(plan.game_dir, plan.version_dir)
        self.assertFalse((plan.version_dir / ".minecraft").exists())
        self.assertTrue(
            (plan.game_dir / "mods" / "a.jar").is_file()
        )
        marker = json.loads(
            (plan.game_dir / ".mc-launcher-modpack.json").read_text(encoding="utf-8")
        )
        self.assertTrue(marker["launchable"])
        self.assertEqual(marker["instance_version_id"], "test-pack-1.2.3")
        version_json = json.loads(
            plan.version_json_path.read_text(encoding="utf-8")
        )
        self.assertEqual(version_json["id"], "test-pack-1.2.3")
        self.assertEqual(version_json["inheritsFrom"], "fabric-loader-0.16.9-1.20.1")
        self.assertEqual(version_json["mcLauncher"]["gameVersion"], "1.20.1")

        root = GameRoot(
            root=self.root,
            versions_dir=self.root / "versions",
            libraries_dir=self.root / "libraries",
            assets_dir=self.root / "assets",
        )
        summaries = list_versions(root)
        summary = next(
            item for item in summaries if item.version_id == "test-pack-1.2.3"
        )
        self.assertEqual(version_game_dir(summary), plan.game_dir)
        self.assertTrue(has_isolated_game_data(summary))
        self.assertIn("Fabric", detect_loader(summary))

    def test_repair_skips_downloads(self) -> None:
        plan = build_instance_plan(self.index, self.root, slug="test-pack")
        first = install_instance(
            plan,
            confirmed=True,
            source=object(),
            client=self.client,
            loader_installer=_fake_loader_installer(),
        )
        self.assertTrue(first.complete)
        calls = len(self.client.calls)
        again = build_instance_plan(self.index, self.root, slug="test-pack")
        second = install_instance(
            again,
            confirmed=True,
            source=object(),
            client=self.client,
            loader_installer=_fake_loader_installer(),
        )
        self.assertTrue(second.complete)
        self.assertGreaterEqual(second.skipped_files, 1)
        self.assertEqual(len(self.client.calls), calls)

    def test_legacy_nested_layout_repair(self) -> None:
        version_dir = self.root / "versions" / "test-pack-1.2.3"
        legacy = version_dir / ".minecraft"
        legacy.mkdir(parents=True)
        (legacy / ".mc-launcher-modpack.json").write_text(
            json.dumps(
                {"name": self.index.name, "version_id": self.index.version_id}
            ),
            encoding="utf-8",
        )
        plan = build_instance_plan(self.index, self.root, slug="test-pack")
        self.assertEqual(plan.game_dir, legacy)
        outcome = install_instance(
            plan,
            confirmed=True,
            source=object(),
            client=self.client,
            loader_installer=_fake_loader_installer(),
        )
        self.assertTrue(outcome.complete)
        self.assertTrue((legacy / "mods" / "a.jar").is_file())
        self.assertFalse((version_dir / "mods").exists())
        self.assertTrue(plan.version_json_path.is_file())

    def test_neoforge_registers_launchable_version(self) -> None:
        index = _make_mrpack(
            self.archive,
            dependencies={"minecraft": "1.20.1", "neoforge": "21.1.80"},
        )
        plan = build_instance_plan(index, self.root, slug="neo-pack")
        outcome = install_instance(
            plan,
            confirmed=True,
            client=self.client,
            loader_installer=_fake_loader_installer("neoforge-21.1.80"),
        )
        self.assertTrue(outcome.complete)
        self.assertTrue(outcome.launchable)
        self.assertTrue(plan.version_json_path.is_file())
        self.assertTrue((plan.game_dir / "mods" / "a.jar").is_file())
        version_json = json.loads(plan.version_json_path.read_text(encoding="utf-8"))
        self.assertEqual(version_json["inheritsFrom"], "neoforge-21.1.80")
        marker = json.loads(
            (plan.game_dir / ".mc-launcher-modpack.json").read_text(encoding="utf-8")
        )
        self.assertTrue(marker["launchable"])

    def test_archive_install(self) -> None:
        outcome = install_modpack_archive(
            self.archive,
            self.root,
            slug="archive-pack",
            confirmed=True,
            source=object(),
            client=self.client,
            loader_installer=_fake_loader_installer(),
        )
        self.assertTrue(outcome.launchable)
        self.assertTrue(
            (
                self.root
                / "versions"
                / "archive-pack-1.2.3"
                / "mods"
                / "a.jar"
            ).is_file()
        )

    def test_download_and_install_launchable(self) -> None:
        archive_bytes = self.archive.read_bytes()
        file = type(
            "File",
            (),
            {
                "filename": "pack.mrpack",
                "url": "https://cdn.modrinth.com/data/x/versions/y/pack.mrpack",
                "size": len(archive_bytes),
                "sha1": hashlib.sha1(archive_bytes).hexdigest(),
                "primary": True,
            },
        )()
        version = type(
            "Version",
            (),
            {
                "primary_file": file,
            },
        )()
        project = type("Project", (), {"slug": "download-pack", "title": "Download Pack"})()
        client = FakeClient(
            {
                file.url: archive_bytes,
                MOD_URL: MOD_BYTES,
            }
        )
        outcome = download_and_install_launchable_modpack(
            project,
            version,
            file,
            self.root,
            confirmed=True,
            source=object(),
            client=client,
            cache_root=Path(self.temp.name) / "cache",
            loader_installer=_fake_loader_installer(),
        )
        self.assertTrue(outcome.launchable)
        self.assertTrue(
            (
                self.root
                / "versions"
                / "download-pack-1.2.3"
                / "mods"
                / "a.jar"
            ).is_file()
        )


if __name__ == "__main__":
    unittest.main()
