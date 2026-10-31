"""NeoForge / Quilt metadata and installer tests (0.6.0)."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from mc_launcher.core import loaders
from mc_launcher.core.errors import VersionError
from mc_launcher.core.install import _detect_loader, _fabric_library_urls
from mc_launcher.core.loaders import (
    _read_forge_profile_id,
    fetch_neoforge_builds,
    fetch_quilt_loaders,
    fetch_quilt_profile,
    neoforge_artifact,
    neoforge_installer_official_url,
    neoforge_version_prefixes,
    select_loader_builds,
)
from mc_launcher.core.mirrors import get_source

_NEOFORGE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<metadata>
  <groupId>net.neoforged</groupId>
  <artifactId>neoforge</artifactId>
  <versioning>
    <latest>21.1.266-beta</latest>
    <versions>
      <version>20.4.1</version>
      <version>21.1.80</version>
      <version>21.1.265</version>
      <version>21.1.266-beta</version>
    </versions>
  </versioning>
</metadata>
"""

_NEOFORGE_LEGACY_XML = """<?xml version="1.0" encoding="UTF-8"?>
<metadata>
  <groupId>net.neoforged</groupId>
  <artifactId>forge</artifactId>
  <versioning>
    <versions>
      <version>1.20.1-47.1.105</version>
      <version>1.20.1-47.1.106</version>
      <version>1.20.2-48.0.1</version>
    </versions>
  </versioning>
</metadata>
"""

_QUILT_LOADERS = json.dumps(
    [
        {"loader": {"version": "0.27.0"}},
        {"loader": {"version": "0.30.0-beta.8"}},
        {"loader": {"version": "0.26.0"}},
        {"loader": {"version": "0.27.1-beta.1"}},
        {"loader": {"version": "bad/version"}},
    ]
)

_QUILT_PROFILE = json.dumps(
    {
        "id": "quilt-loader-0.27.0-1.20.1",
        "inheritsFrom": "1.20.1",
        "type": "release",
        "mainClass": "org.quiltmc.loader.impl.launch.knot.KnotClient",
        "libraries": [
            {
                "name": "org.quiltmc:quilt-loader:0.27.0",
                "url": "https://maven.quiltmc.org/repository/release/",
            },
            {
                "name": "net.fabricmc:intermediary:1.20.1",
                "url": "https://maven.fabricmc.net/",
            },
        ],
        "releaseTime": "2023-06-12T13:25:51+00:00",
    }
)


class NeoForgeMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        loaders._NEOFORGE_CACHE.clear()

    def test_loader_metadata_client_always_allows_mirror(self) -> None:
        from mc_launcher.core.loaders import _fabric_http
        from mc_launcher.core.net import HttpClient, validate_url
        from mc_launcher.core.mirrors import loader_allowed_hosts

        mojang = get_source("mojang")
        restrictive = HttpClient(
            mojang.allowed_hosts, strict_redirects=True, timeout=5, retries=0
        )
        http = _fabric_http(mojang, restrictive, None)
        mirror = (
            "https://bmclapi2.bangbang93.com/maven/net/neoforged/neoforge/"
            "maven-metadata.xml"
        )
        validate_url(mirror, http.allowed_hosts, check_dns=False)
        self.assertIn("bmclapi2.bangbang93.com", http.allowed_hosts)
        self.assertIn("bmclapi2.bangbang93.com", loader_allowed_hosts(mojang))
        self.assertNotIn("bmclapi2.bangbang93.com", mojang.allowed_hosts)

    def test_artifact_choice(self) -> None:
        self.assertEqual(neoforge_artifact("1.20.1"), "forge")
        self.assertEqual(neoforge_artifact("1.20.4"), "neoforge")

    def test_version_prefixes(self) -> None:
        self.assertEqual(neoforge_version_prefixes("1.20.2"), ("20.2.",))
        self.assertEqual(neoforge_version_prefixes("1.21"), ("21.0.",))
        self.assertEqual(neoforge_version_prefixes("1.21.1"), ("21.1.",))
        self.assertEqual(neoforge_version_prefixes("1.21.11"), ("21.11.",))
        self.assertEqual(neoforge_version_prefixes("26.2"), ("26.2.0.",))
        self.assertEqual(neoforge_version_prefixes("26.3"), ("26.3.0.",))
        self.assertEqual(neoforge_version_prefixes("26.1.2"), ("26.1.2.",))
        self.assertEqual(neoforge_version_prefixes("1.20.1"), ())

    def test_modern_builds_use_mirror_first(self) -> None:
        seen: list[tuple[str, ...]] = []

        def fake_fetch(_client, urls, **_kwargs):
            seen.append(tuple(urls))
            return _NEOFORGE_XML.encode("utf-8")

        with mock.patch.object(loaders, "fetch_bytes", side_effect=fake_fetch):
            builds = fetch_neoforge_builds(
                get_source("mojang"), "1.21.1", force=True
            )
        self.assertEqual(
            [item.version for item in builds], ["21.1.266-beta", "21.1.265", "21.1.80"]
        )
        self.assertEqual(
            seen[0][0],
            "https://bmclapi2.bangbang93.com/maven/net/neoforged/neoforge/"
            "maven-metadata.xml",
        )
        self.assertIn(
            "https://maven.neoforged.net/releases/net/neoforged/neoforge/"
            "maven-metadata.xml",
            seen[0],
        )
        stable = [item.version for item in builds if item.stable]
        self.assertEqual(stable, ["21.1.265"])
        self.assertEqual(
            [item.version for item in select_loader_builds(builds, "stable")],
            ["21.1.265"],
        )
        self.assertEqual(
            [item.version for item in select_loader_builds(builds, "latest")],
            ["21.1.266-beta"],
        )

    def test_legacy_1201_builds(self) -> None:
        def fake_fetch(_client, urls, **_kwargs):
            self.assertIn("net/neoforged/forge/maven-metadata.xml", urls[0])
            return _NEOFORGE_LEGACY_XML.encode("utf-8")

        with mock.patch.object(loaders, "fetch_bytes", side_effect=fake_fetch):
            builds = fetch_neoforge_builds(
                get_source("bmclapi"), "1.20.1", force=True
            )
        self.assertEqual(
            [item.version for item in builds], ["47.1.106", "47.1.105"]
        )
        self.assertEqual(
            neoforge_installer_official_url("1.20.1", "47.1.106"),
            "https://maven.neoforged.net/releases/net/neoforged/forge/"
            "1.20.1-47.1.106/forge-1.20.1-47.1.106-installer.jar",
        )

    def test_modern_installer_url(self) -> None:
        self.assertEqual(
            neoforge_installer_official_url("1.21.1", "21.1.80"),
            "https://maven.neoforged.net/releases/net/neoforged/neoforge/"
            "21.1.80/neoforge-21.1.80-installer.jar",
        )

    def test_mirror_rewrite_strips_releases(self) -> None:
        source = get_source("bmclapi")
        urls = source.urls_for(
            "https://maven.neoforged.net/releases/net/neoforged/neoforge/"
            "21.1.80/neoforge-21.1.80-installer.jar"
        )
        self.assertEqual(
            urls[0],
            "https://bmclapi2.bangbang93.com/maven/net/neoforged/neoforge/"
            "21.1.80/neoforge-21.1.80-installer.jar",
        )
        self.assertIn("maven.neoforged.net", urls[1])

    def test_library_urls_mirror_first_for_mojang_source(self) -> None:
        urls = _fabric_library_urls(
            get_source("mojang"),
            "https://maven.neoforged.net/releases/net/neoforged/neoforge/"
            "21.1.80/neoforge-21.1.80-universal.jar",
        )
        self.assertEqual(
            urls[0],
            "https://bmclapi2.bangbang93.com/maven/net/neoforged/neoforge/"
            "21.1.80/neoforge-21.1.80-universal.jar",
        )
        self.assertTrue(any("maven.neoforged.net" in item for item in urls))

    def test_profile_id_and_internal_consistency_inputs(self) -> None:
        work = Path(tempfile.mkdtemp(prefix="mc-launcher-neoforge-"))
        try:
            installer = work / "installer.jar"
            with zipfile.ZipFile(installer, "w") as archive:
                archive.writestr(
                    "install_profile.json",
                    json.dumps(
                        {
                            "version": "neoforge-21.1.80",
                            "minecraft": "1.21.1",
                        }
                    ),
                )
                archive.writestr(
                    "version.json",
                    json.dumps({"id": "neoforge-21.1.80", "inheritsFrom": "1.21.1"}),
                )
            version_id, game = _read_forge_profile_id(installer, "NeoForge")
            self.assertEqual(version_id, "neoforge-21.1.80")
            self.assertEqual(game, "1.21.1")
        finally:
            import shutil

            shutil.rmtree(work, ignore_errors=True)


class QuiltMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        loaders._QUILT_LOADER_CACHE.clear()

    def test_builds_are_sorted_and_stable_prefers_release(self) -> None:
        def fake_fetch(_client, urls, **_kwargs):
            self.assertEqual(
                urls,
                ("https://meta.quiltmc.org/v3/versions/loader/1.20.1",),
            )
            return _QUILT_LOADERS.encode("utf-8")

        with mock.patch.object(loaders, "fetch_bytes", side_effect=fake_fetch):
            builds = fetch_quilt_loaders(
                get_source("bmclapi"), "1.20.1", force=True
            )
        versions = [item.version for item in builds]
        self.assertEqual(
            versions, ["0.30.0-beta.8", "0.27.1-beta.1", "0.27.0", "0.26.0"]
        )
        self.assertEqual(
            [item.version for item in select_loader_builds(builds, "stable")],
            ["0.27.0"],
        )
        self.assertEqual(
            [item.version for item in select_loader_builds(builds, "latest")],
            ["0.30.0-beta.8"],
        )

    def test_profile_validation(self) -> None:
        def fake_fetch(_client, urls, **_kwargs):
            self.assertTrue(urls[0].endswith("/profile/json"))
            return _QUILT_PROFILE.encode("utf-8")

        with mock.patch.object(loaders, "fetch_bytes", side_effect=fake_fetch):
            detail = fetch_quilt_profile(
                get_source("bmclapi"), "1.20.1", "0.27.0"
            )
        self.assertEqual(detail.remote.id, "quilt-loader-0.27.0-1.20.1")
        self.assertEqual(detail.data["inheritsFrom"], "1.20.1")

    def test_profile_rejects_wrong_game(self) -> None:
        def fake_fetch(_client, urls, **_kwargs):
            return _QUILT_PROFILE.encode("utf-8")

        with mock.patch.object(loaders, "fetch_bytes", side_effect=fake_fetch):
            with self.assertRaises(VersionError):
                fetch_quilt_profile(
                    get_source("bmclapi"), "1.21.1", "0.27.0"
                )


class LoaderDetectionTests(unittest.TestCase):
    def test_detects_neoforge_before_forge(self) -> None:
        self.assertEqual(
            _detect_loader(
                {
                    "mainClass": "cpw.mods.bootstraplauncher.BootstrapLauncher",
                    "libraries": [{"name": "net.neoforged:forge:1.20.1-47.1.106"}],
                }
            ),
            "neoforge",
        )

    def test_detects_quilt_before_fabric(self) -> None:
        self.assertEqual(
            _detect_loader(
                {
                    "mainClass": "org.quiltmc.loader.impl.launch.knot.KnotClient",
                    "libraries": [
                        {"name": "org.quiltmc:quilt-loader:0.27.0"},
                        {"name": "net.fabricmc:intermediary:1.20.1"},
                    ],
                }
            ),
            "quilt",
        )

    def test_forge_and_fabric_still_detected(self) -> None:
        self.assertEqual(
            _detect_loader({"libraries": [{"name": "net.minecraftforge:forge:1.20.1"}]}),
            "forge",
        )
        self.assertEqual(
            _detect_loader({"libraries": [{"name": "net.fabricmc:fabric-loader:0.16.9"}]}),
            "fabric",
        )


if __name__ == "__main__":
    unittest.main()
