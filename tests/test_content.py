"""Tests for the 0.5.0 content download center (no network access)."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from mc_launcher.core.content import (
    CONTENT_TYPES,
    ContentError,
    ContentFile,
    ContentGameVersion,
    ContentProject,
    ContentSearchResult,
    ContentVersion,
    CurseForgeSource,
    ModrinthSource,
    build_content_plan,
    build_content_plan_for_version,
    build_facets,
    build_modpack_plan,
    content_sidecar_path,
    default_target_dir,
    download_and_install_modpack,
    extract_zip_prefix,
    format_search_result,
    format_versions,
    install_content,
    install_modpack,
    is_content_installed,
    list_worlds,
    read_content_sidecar,
    read_mrpack,
    record_content_install,
    sanitize_content_filename,
    select_best_version,
    validate_world_dir,
    validate_zip_archive,
)
from mc_launcher.core.net import DownloadError

FIXTURES = Path(__file__).parent / "fixtures" / "content"
MOD_URL = "https://cdn.modrinth.com/data/u6dRKJwZ/versions/qrP02aGf/jei.jar"
MOD_BYTES = b"fake mod jar bytes"
MOD_SHA1 = hashlib.sha1(MOD_BYTES).hexdigest()


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeClient:
    """In-memory stand-in for HttpClient (read_bytes + download_file)."""

    def __init__(self, responses=None, downloads=None) -> None:
        self.responses = responses or {}
        self.downloads = downloads or {}
        self.calls: list[dict] = []
        self.timeout = 20.0
        self.retries = 1
        self.segments = 0

    def _payload(self, url: str) -> bytes:
        for key, value in self.responses.items():
            if key in url:
                if isinstance(value, bytes):
                    return value
                if callable(value):
                    return value(url)
                return json.dumps(value, ensure_ascii=False).encode("utf-8")
        raise DownloadError(f"下载元数据失败：{url}\n  - HTTP 404 Not Found")

    def read_bytes(self, url: str, *, max_size: int = 0, headers=None) -> bytes:
        self.calls.append({"url": url, "headers": dict(headers or {})})
        return self._payload(url)

    def validate_initial(self, url: str) -> str:
        if not url.startswith("https://"):
            raise DownloadError("只允许 HTTPS 下载地址")
        return url

    def download_file(self, spec, *, progress=None, cancel=None, pause=None, segments=None):
        data = self.downloads.get(spec.url)
        if data is None:
            raise DownloadError(f"下载失败：{spec.url}")
        if spec.size is not None and len(data) != spec.size:
            raise DownloadError(
                f"文件大小不符：期望 {spec.size}，实际 {len(data)}"
            )
        actual = hashlib.sha1(data).hexdigest()
        if spec.sha1 and actual != spec.sha1.lower():
            raise DownloadError(
                f"SHA-1 校验失败：期望 {spec.sha1}，实际 {actual}"
            )
        spec.dest.parent.mkdir(parents=True, exist_ok=True)
        if progress is not None:
            progress(0)
        spec.dest.write_bytes(data)
        if progress is not None:
            progress(len(data))
        return len(data)


def _make_project(**overrides) -> ContentProject:
    data = {
        "id": "u6dRKJwZ",
        "slug": "jei",
        "title": "Just Enough Items",
        "description": "View items and recipes",
        "author": "mezz",
        "project_type": "mod",
        "categories": ("utility",),
        "downloads": 100,
        "follows": 10,
        "date_modified": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "page_url": "https://modrinth.com/mod/jei",
    }
    data.update(overrides)
    return ContentProject(**data)


def _make_file(**overrides) -> ContentFile:
    data = {
        "filename": "jei.jar",
        "url": MOD_URL,
        "size": len(MOD_BYTES),
        "sha1": MOD_SHA1,
        "primary": True,
    }
    data.update(overrides)
    return ContentFile(**data)


def _make_version(**overrides) -> ContentVersion:
    data = {
        "id": "qrP02aGf",
        "project_id": "u6dRKJwZ",
        "version_number": "15.0.0",
        "name": "JEI 15.0.0",
        "game_versions": ("1.20.1",),
        "loaders": ("fabric",),
        "version_type": "release",
        "date_published": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "downloads": 500,
        "files": (_make_file(),),
    }
    data.update(overrides)
    return ContentVersion(**data)


def _write_zip(path: Path, entries: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)


def _make_mrpack(
    path: Path,
    *,
    index: dict | None = None,
    mod_bytes: bytes = b"MOD",
    mod_name: str = "mods/a.jar",
    include_sha: bool = True,
) -> tuple[dict, str]:
    sha1 = hashlib.sha1(mod_bytes).hexdigest()
    payload = index or {
        "formatVersion": 1,
        "name": "Test Pack",
        "versionId": "1.2.3",
        "dependencies": {"minecraft": "1.20.1", "fabric-loader": "0.16.9"},
        "files": [
            {
                "path": mod_name,
                "hashes": {"sha1": sha1} if include_sha else {},
                "downloads": [MOD_URL],
                "fileSize": len(mod_bytes),
                "env": {"client": "required", "server": "required"},
            }
        ],
    }
    entries = {
        "modrinth.index.json": json.dumps(payload).encode("utf-8"),
        "overrides/config/test.toml": b"enabled = true\n",
        "client-overrides/options.txt": b"fov:0\n",
    }
    _write_zip(path, entries)
    return payload, sha1


class FacetTests(unittest.TestCase):
    def test_mod_facets(self) -> None:
        self.assertEqual(
            build_facets("mod", "1.20.1", "fabric", "utility"),
            [
                ["project_type:mod"],
                ["versions:1.20.1"],
                ["categories:fabric"],
                ["categories:utility"],
            ],
        )

    def test_datapack_uses_mod_type_and_loader(self) -> None:
        facets = build_facets("datapack", "1.20.1", "datapack")
        self.assertEqual(
            facets,
            [
                ["project_type:mod"],
                ["loaders:datapack"],
                ["versions:1.20.1"],
            ],
        )
        self.assertNotIn(["categories:datapack"], facets)

    def test_resourcepack_and_shader_ignore_loader(self) -> None:
        self.assertEqual(
            build_facets("resourcepack", "1.20.1", "fabric"),
            [["project_type:resourcepack"], ["versions:1.20.1"]],
        )
        self.assertEqual(
            build_facets("shader", "", "fabric"),
            [["project_type:shader"]],
        )

    def test_modpack_facets(self) -> None:
        self.assertEqual(
            build_facets("modpack", "26.3", "fabric"),
            [
                ["project_type:modpack"],
                ["versions:26.3"],
                ["categories:fabric"],
            ],
        )

    def test_unknown_type(self) -> None:
        with self.assertRaises(ContentError):
            build_facets("world")


class ModrinthSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.cache = Path(self.temp.name)
        self.client = FakeClient(
            {
                "/search": _fixture_bytes("modrinth_search_mod.json"),
                "/project/u6dRKJwZ/version": _fixture_bytes(
                    "modrinth_versions_jei.json"
                ),
                "/project/u6dRKJwZ": _fixture_bytes("modrinth_project_jei.json"),
                "/tag/category": _fixture_bytes("modrinth_tags_category.json"),
                "/tag/loader": _fixture_bytes("modrinth_tags_loader.json"),
                "/tag/game_version": _fixture_bytes(
                    "modrinth_tags_game_version.json"
                ),
            }
        )
        self.source = ModrinthSource(client=self.client, cache_root=self.cache)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_search_parses_hits_and_total(self) -> None:
        result = self.source.search(
            "jei", "mod", game_version="1.20.1", loader="fabric"
        )
        self.assertEqual(result.total, 38)
        self.assertEqual(result.source, "modrinth")
        self.assertEqual(len(result.projects), 3)
        project = result.projects[0]
        self.assertEqual(project.id, "u6dRKJwZ")
        self.assertEqual(project.slug, "jei")
        self.assertEqual(project.title, "Just Enough Items (JEI)")
        self.assertEqual(project.project_type, "mod")
        self.assertIn(project.page_url, "https://modrinth.com/mod/jei")

    def test_search_sends_facets_and_sort(self) -> None:
        self.source.search(
            "jei", "datapack", game_version="1.20.1", category="utility", sort="downloads"
        )
        call = self.client.calls[0]
        from urllib.parse import parse_qs, urlsplit

        query = parse_qs(urlsplit(call["url"]).query)
        facets = json.loads(query["facets"][0])
        self.assertIn(["project_type:mod"], facets)
        self.assertIn(["loaders:datapack"], facets)
        self.assertIn(["versions:1.20.1"], facets)
        self.assertIn(["categories:utility"], facets)
        self.assertEqual(query["index"][0], "downloads")
        self.assertEqual(query["limit"][0], "20")
        self.assertIn("MinecraftLauncher", call["headers"]["User-Agent"])

    def test_search_empty_result(self) -> None:
        self.client.responses["/search"] = {
            "hits": [],
            "total_hits": 0,
            "offset": 0,
            "limit": 20,
        }
        result = self.source.search("nothing", "mod")
        self.assertEqual(result.projects, [])
        self.assertEqual(result.total, 0)

    def test_versions_and_best_release(self) -> None:
        versions = self.source.versions(
            "u6dRKJwZ", game_version="1.20.1", loader="fabric"
        )
        self.assertGreaterEqual(len(versions), 2)
        self.assertTrue(all("1.20.1" in item.game_versions for item in versions))
        self.assertTrue(all("fabric" in item.loaders for item in versions))
        best = select_best_version(versions, "1.20.1", "fabric")
        self.assertIsNotNone(best)
        self.assertEqual(best.version_type, "release")
        self.assertEqual(best.version_number, "15.56.0.205")
        latest = select_best_version(versions, "1.20.1", "fabric", channel="latest")
        self.assertNotEqual(latest.id, best.id)
        self.assertEqual(latest.version_type, "beta")

    def test_versions_local_filter(self) -> None:
        raw = json.loads(_fixture_bytes("modrinth_versions_jei.json"))
        raw.append(
            {
                "id": "other111",
                "project_id": "u6dRKJwZ",
                "version_number": "1.0",
                "name": "other",
                "game_versions": ["1.19.2"],
                "loaders": ["forge"],
                "version_type": "release",
                "date_published": "2025-01-01T00:00:00Z",
                "downloads": 1,
                "files": [],
                "dependencies": [],
            }
        )
        self.client.responses["/project/u6dRKJwZ/version"] = raw
        versions = self.source.versions(
            "u6dRKJwZ", game_version="1.20.1", loader="fabric"
        )
        self.assertNotIn("other111", [item.id for item in versions])

    def test_project(self) -> None:
        project = self.source.project("u6dRKJwZ")
        self.assertEqual(project.title, "Just Enough Items (JEI)")
        self.assertEqual(project.project_type, "mod")
        self.assertIn("library", project.categories)

    def test_categories_are_cached_and_filtered(self) -> None:
        first = self.source.categories("mod")
        calls = len(self.client.calls)
        second = self.source.categories("mod")
        self.assertEqual(first, second)
        self.assertEqual(len(self.client.calls), calls)
        self.assertTrue(first)
        self.assertTrue(all(item.project_type == "mod" for item in first))
        self.assertIn("utility", [item.name for item in first])

    def test_datapack_categories_reuse_mod_categories(self) -> None:
        categories = self.source.categories("datapack")
        self.assertIn("utility", [item.name for item in categories])

    def test_game_versions_sorted(self) -> None:
        versions = self.source.game_versions()
        self.assertTrue(versions)
        self.assertEqual(versions[0].version, "26.4-snapshot-6")
        self.assertTrue(all(item.version for item in versions))

    def test_bad_json(self) -> None:
        self.client.responses["/search"] = b"not json"
        with self.assertRaises(ContentError):
            self.source.search("x", "mod")

    def test_http_429_message(self) -> None:
        def fail(_url):
            raise DownloadError("下载元数据失败：...\n  - HTTP 429 Too Many Requests")

        self.client.responses["/search"] = fail
        with self.assertRaises(ContentError) as context:
            self.source.search("x", "mod")
        self.assertIn("429", str(context.exception))


class SelectVersionTests(unittest.TestCase):
    def test_prefers_release_over_newer_beta(self) -> None:
        release = _make_version(
            id="rel", version_type="release", date_published=datetime(2025, 1, 1, tzinfo=timezone.utc)
        )
        beta = _make_version(
            id="beta", version_type="beta", date_published=datetime(2026, 1, 1, tzinfo=timezone.utc)
        )
        self.assertEqual(select_best_version([beta, release]).id, "rel")

    def test_falls_back_to_beta(self) -> None:
        beta = _make_version(id="beta", version_type="beta")
        alpha = _make_version(id="alpha", version_type="alpha", date_published=datetime(2024, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(select_best_version([alpha, beta]).id, "beta")

    def test_requires_game_version_and_loader(self) -> None:
        version = _make_version()
        self.assertIsNotNone(select_best_version([version], "1.20.1", "fabric"))
        self.assertIsNone(select_best_version([version], "1.19.2"))
        self.assertIsNone(select_best_version([version], "1.20.1", "forge"))

    def test_latest_channel_ignores_channel_type(self) -> None:
        release = _make_version(id="rel", date_published=datetime(2025, 1, 1, tzinfo=timezone.utc))
        beta = _make_version(
            id="beta",
            version_type="beta",
            date_published=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(
            select_best_version([release, beta], channel="latest").id, "beta"
        )


class FilenameAndPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = (Path(self.temp.name) / "game").resolve()
        self.root.mkdir()
        self.project = _make_project()
        self.version = _make_version()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_sanitize_filename_strips_paths(self) -> None:
        self.assertEqual(
            sanitize_content_filename("../../evil/jei.jar", "mod"), "jei.jar"
        )
        self.assertEqual(
            sanitize_content_filename(r"C:\temp\pack.zip", "resourcepack"),
            "pack.zip",
        )
        self.assertEqual(
            sanitize_content_filename("整合 包.jar", "mod"), "整合 包.jar"
        )

    def test_sanitize_rejects_bad_extensions_and_names(self) -> None:
        for name in ("jei.exe", "jei.jar.exe", "CON.jar", ".hidden.jar", "bad?.jar"):
            with self.subTest(name=name):
                with self.assertRaises(ContentError):
                    sanitize_content_filename(name, "mod")
        with self.assertRaises(ContentError):
            sanitize_content_filename("pack.zip", "mod")

    def test_default_targets(self) -> None:
        self.assertEqual(default_target_dir(self.root, "mod"), self.root / "mods")
        self.assertEqual(
            default_target_dir(self.root, "resourcepack"),
            self.root / "resourcepacks",
        )
        self.assertEqual(
            default_target_dir(self.root, "shader"), self.root / "shaderpacks"
        )
        with self.assertRaises(ContentError):
            default_target_dir(self.root, "datapack")

    def test_build_plan_mod(self) -> None:
        plan = build_content_plan(
            self.project, self.version, self.version.primary_file, self.root, "mod"
        )
        self.assertEqual(plan.dest, self.root / "mods" / "jei.jar")
        self.assertEqual(plan.target_dir, self.root / "mods")
        self.assertEqual(plan.sha1, MOD_SHA1)

    def test_build_plan_datapack_requires_world(self) -> None:
        world = self.root / "saves" / "world"
        world.mkdir(parents=True)
        (world / "level.dat").write_bytes(b"level")
        file = replace(self.version.primary_file, filename="datapack.zip")
        plan = build_content_plan(
            self.project,
            self.version,
            file,
            self.root,
            "datapack",
            world_dir=world,
        )
        self.assertEqual(plan.dest, world / "datapacks" / "datapack.zip")
        with self.assertRaises(ContentError):
            build_content_plan(
                self.project,
                self.version,
                file,
                self.root,
                "datapack",
            )

    def test_build_plan_rejects_outside_directory(self) -> None:
        with self.assertRaises(ContentError):
            build_content_plan(
                self.project,
                self.version,
                self.version.primary_file,
                self.root,
                "mod",
                target_dir=Path(self.temp.name) / "outside",
            )

    def test_build_plan_rejects_missing_hash(self) -> None:
        file = replace(self.version.primary_file, sha1="")
        with self.assertRaises(ContentError) as context:
            build_content_plan(self.project, self.version, file, self.root, "mod")
        self.assertIn("SHA-1", str(context.exception))

    def test_build_plan_rejects_unsafe_url(self) -> None:
        for url in ("http://cdn.modrinth.com/a.jar", "https://evil.example/a.jar"):
            with self.subTest(url=url):
                file = replace(self.version.primary_file, url=url)
                with self.assertRaises(ContentError):
                    build_content_plan(
                        self.project, self.version, file, self.root, "mod"
                    )

    def test_build_plan_rejects_project_mismatch(self) -> None:
        version = _make_version(project_id="other")
        with self.assertRaises(ContentError):
            build_content_plan(
                self.project, version, version.primary_file, self.root, "mod"
            )

    def test_build_plan_for_version(self) -> None:
        class StubSource:
            def versions(self, project_id, *, game_version="", loader=""):
                return [self.version_for_stub]

        stub = StubSource()
        stub.version_for_stub = self.version
        plan = build_content_plan_for_version(
            self.project, "mod", self.root, source=stub
        )
        self.assertEqual(plan.version.id, self.version.id)
        self.assertTrue(str(plan.dest).endswith("jei.jar"))

    def test_world_validation(self) -> None:
        world = self.root / "saves" / "world"
        world.mkdir(parents=True)
        with self.assertRaises(ContentError):
            validate_world_dir(world)
        (world / "level.dat").write_bytes(b"level")
        self.assertEqual(validate_world_dir(world), world.resolve())
        self.assertEqual([item.name for item in list_worlds(self.root)], ["world"])


class InstallContentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "game"
        self.root.mkdir()
        self.project = _make_project()
        self.version = _make_version()
        self.plan = build_content_plan(
            self.project, self.version, self.version.primary_file, self.root, "mod"
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_requires_confirmation(self) -> None:
        client = FakeClient(downloads={MOD_URL: MOD_BYTES})
        with self.assertRaises(ContentError):
            install_content(self.plan, client=client)
        self.assertFalse(self.plan.target_dir.exists())
        self.assertEqual(client.calls, [])

    def test_downloads_and_records_sidecar(self) -> None:
        client = FakeClient(downloads={MOD_URL: MOD_BYTES})
        outcome = install_content(self.plan, client=client, confirmed=True)
        self.assertFalse(outcome.already_installed)
        self.assertEqual(self.plan.dest.read_bytes(), MOD_BYTES)
        self.assertTrue(is_content_installed(self.root, self.plan.dest))
        entries = read_content_sidecar(self.root)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["project_id"], "u6dRKJwZ")
        self.assertEqual(entries[0]["sha1"], MOD_SHA1)
        self.assertEqual(entries[0]["path"], "mods/jei.jar")
        self.assertTrue(content_sidecar_path(self.root).is_file())

    def test_existing_valid_file_is_skipped(self) -> None:
        self.plan.target_dir.mkdir(parents=True)
        self.plan.dest.write_bytes(MOD_BYTES)
        client = FakeClient(downloads={MOD_URL: MOD_BYTES})
        outcome = install_content(self.plan, client=client, confirmed=True)
        self.assertTrue(outcome.already_installed)
        self.assertEqual(client.calls, [])

    def test_existing_different_file_requires_overwrite(self) -> None:
        self.plan.target_dir.mkdir(parents=True)
        self.plan.dest.write_bytes(b"old")
        client = FakeClient(downloads={MOD_URL: MOD_BYTES})
        with self.assertRaises(ContentError):
            install_content(self.plan, client=client, confirmed=True)
        outcome = install_content(
            replace(self.plan, overwrite=True), client=client, confirmed=True
        )
        self.assertEqual(self.plan.dest.read_bytes(), MOD_BYTES)
        self.assertFalse(outcome.already_installed)

    def test_bad_sidecar_is_ignored(self) -> None:
        content_sidecar_path(self.root).write_text("{broken", encoding="utf-8")
        self.assertEqual(read_content_sidecar(self.root), [])

    def test_record_replaces_existing_entry(self) -> None:
        record_content_install(self.plan)
        record_content_install(self.plan)
        self.assertEqual(len(read_content_sidecar(self.root)), 1)


class ZipSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_accepts_normal_archive(self) -> None:
        path = self.root / "ok.zip"
        _write_zip(path, {"a.txt": b"a", "dir/b.txt": b"b"})
        infos = validate_zip_archive(path)
        self.assertEqual(len(infos), 2)

    def test_rejects_traversal(self) -> None:
        path = self.root / "evil.zip"
        _write_zip(path, {"../evil.txt": b"x"})
        with self.assertRaises(ContentError):
            validate_zip_archive(path)

    def test_rejects_absolute_path(self) -> None:
        path = self.root / "evil.zip"
        _write_zip(path, {"/abs.txt": b"x"})
        with self.assertRaises(ContentError):
            validate_zip_archive(path)

    def test_rejects_symlink(self) -> None:
        path = self.root / "link.zip"
        with zipfile.ZipFile(path, "w") as archive:
            info = zipfile.ZipInfo("link")
            info.external_attr = 0o120777 << 16
            archive.writestr(info, b"target")
        with self.assertRaises(ContentError):
            validate_zip_archive(path)

    def test_rejects_too_many_entries(self) -> None:
        path = self.root / "many.zip"
        _write_zip(path, {"a": b"a", "b": b"b", "c": b"c"})
        with self.assertRaises(ContentError):
            validate_zip_archive(path, max_entries=2)

    def test_rejects_size_bomb(self) -> None:
        path = self.root / "big.zip"
        _write_zip(path, {"big": b"x" * 4096})
        with self.assertRaises(ContentError):
            validate_zip_archive(path, max_total_bytes=1000)
        with self.assertRaises(ContentError):
            validate_zip_archive(path, max_file_bytes=10)

    def test_extract_prefix(self) -> None:
        path = self.root / "pack.zip"
        _write_zip(
            path,
            {
                "overrides/config/a.toml": b"a",
                "overrides/mods/b.jar": b"b",
                "other/c.txt": b"c",
            },
        )
        dest = self.root / "dest"
        written = extract_zip_prefix(path, dest, "overrides")
        self.assertEqual(written, 2)
        self.assertEqual((dest / "config" / "a.toml").read_bytes(), b"a")
        self.assertEqual((dest / "mods" / "b.jar").read_bytes(), b"b")
        self.assertFalse((dest / "other").exists())


class ModpackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "game"
        self.root.mkdir()
        self.archive = Path(self.temp.name) / "pack.mrpack"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_read_mrpack(self) -> None:
        _make_mrpack(self.archive)
        index = read_mrpack(self.archive)
        self.assertEqual(index.name, "Test Pack")
        self.assertEqual(index.version_id, "1.2.3")
        self.assertEqual(index.minecraft_version, "1.20.1")
        self.assertIn("Fabric", index.loader_summary)
        self.assertEqual(len(index.files), 1)
        self.assertTrue(index.has_overrides)
        self.assertTrue(index.has_client_overrides)
        self.assertEqual(index.files[0].path, Path("mods") / "a.jar")

    def test_mrpack_requires_sha1(self) -> None:
        _make_mrpack(self.archive, include_sha=False)
        with self.assertRaises(ContentError):
            read_mrpack(self.archive)

    def test_missing_index(self) -> None:
        _write_zip(self.archive, {"readme.txt": b"x"})
        with self.assertRaises(ContentError) as context:
            read_mrpack(self.archive)
        self.assertIn("modrinth.index.json", str(context.exception))

    def test_mrpack_rejects_override_file_path(self) -> None:
        index = {
            "formatVersion": 1,
            "name": "Bad",
            "versionId": "1",
            "files": [
                {
                    "path": "overrides/evil.jar",
                    "hashes": {"sha1": "a" * 40},
                    "downloads": [MOD_URL],
                }
            ],
        }
        _make_mrpack(self.archive, index=index)
        with self.assertRaises(ContentError):
            read_mrpack(self.archive)

    def test_install_modpack(self) -> None:
        _make_mrpack(self.archive)
        index = read_mrpack(self.archive)
        plan = build_modpack_plan(index, self.root, slug="test")
        client = FakeClient(downloads={MOD_URL: b"MOD"})
        progress = []
        outcome = install_modpack(
            plan, client=client, confirmed=True, progress=progress.append
        )
        self.assertTrue(outcome.complete)
        self.assertEqual(outcome.downloaded_files, 1)
        self.assertEqual((plan.instance_dir / "mods" / "a.jar").read_bytes(), b"MOD")
        self.assertEqual(
            (plan.instance_dir / "config" / "test.toml").read_bytes(),
            b"enabled = true\n",
        )
        self.assertEqual(
            (plan.instance_dir / "options.txt").read_bytes(), b"fov:0\n"
        )
        marker = plan.instance_dir / ".mc-launcher-modpack.json"
        self.assertTrue(marker.is_file())
        payload = json.loads(marker.read_text(encoding="utf-8"))
        self.assertTrue(payload["complete"])
        self.assertFalse(payload["launchable"])

    def test_install_modpack_requires_confirmation(self) -> None:
        _make_mrpack(self.archive)
        plan = build_modpack_plan(read_mrpack(self.archive), self.root)
        with self.assertRaises(ContentError):
            install_modpack(plan, client=FakeClient(), confirmed=False)
        self.assertFalse(plan.instance_dir.exists())

    def test_install_modpack_repair_skips_existing(self) -> None:
        _make_mrpack(self.archive)
        plan = build_modpack_plan(read_mrpack(self.archive), self.root, slug="test")
        client = FakeClient(downloads={MOD_URL: b"MOD"})
        install_modpack(plan, client=client, confirmed=True)
        calls = len(client.calls)
        outcome = install_modpack(plan, client=client, confirmed=True)
        self.assertTrue(outcome.complete)
        self.assertGreaterEqual(outcome.skipped_files, 1)
        self.assertEqual(len(client.calls), calls)

    def test_install_modpack_refuses_foreign_directory(self) -> None:
        _make_mrpack(self.archive)
        plan = build_modpack_plan(read_mrpack(self.archive), self.root, slug="test")
        plan.instance_dir.mkdir(parents=True)
        (plan.instance_dir / "user.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(ContentError):
            install_modpack(plan, client=FakeClient(), confirmed=True)

    def test_download_and_install_modpack(self) -> None:
        payload, sha1 = _make_mrpack(self.archive)
        archive_bytes = self.archive.read_bytes()
        project = _make_project(project_type="modpack", slug="test-pack")
        version = _make_version(
            id="packver1",
            version_number="1.2.3",
            files=(
                ContentFile(
                    filename="pack.mrpack",
                    url="https://cdn.modrinth.com/data/x/versions/y/pack.mrpack",
                    size=len(archive_bytes),
                    sha1=hashlib.sha1(archive_bytes).hexdigest(),
                    primary=True,
                ),
            ),
        )
        client = FakeClient(
            downloads={
                version.files[0].url: archive_bytes,
                MOD_URL: b"MOD",
            }
        )
        outcome = download_and_install_modpack(
            project,
            version,
            version.primary_file,
            self.root,
            client=client,
            confirmed=True,
            cache_root=Path(self.temp.name) / "cache",
        )
        self.assertTrue(outcome.complete)
        self.assertTrue(
            (self.root / "modpacks" / "test-pack-1.2.3" / "mods" / "a.jar").is_file()
        )


class CurseForgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.cache = Path(self.temp.name)
        self.client = FakeClient(
            {
                "/mods/search": _fixture_bytes("curseforge_search.json"),
                "/mods/238222/files": _fixture_bytes("curseforge_files.json"),
                "/categories": _fixture_bytes("curseforge_categories.json"),
            }
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_requires_key(self) -> None:
        source = CurseForgeSource(client=self.client, cache_root=self.cache)
        self.assertFalse(source.enabled())
        with self.assertRaises(ContentError) as context:
            source.search("jei", "mod")
        self.assertIn("API Key", str(context.exception))

    def test_search_and_headers(self) -> None:
        source = CurseForgeSource(
            "test-key", client=self.client, cache_root=self.cache
        )
        self.assertTrue(source.enabled())
        result = source.search(
            "jei", "mod", game_version="1.20.1", loader="forge", sort="downloads"
        )
        self.assertEqual(result.total, 4321)
        self.assertEqual(len(result.projects), 1)
        project = result.projects[0]
        self.assertEqual(project.id, "238222")
        self.assertEqual(project.title, "Just Enough Items (JEI)")
        self.assertEqual(project.author, "mezz")
        self.assertEqual(project.source, "curseforge")
        self.assertIn("curseforge.com", project.page_url)
        call = self.client.calls[0]
        self.assertEqual(call["headers"]["x-api-key"], "test-key")
        from urllib.parse import parse_qs, urlsplit

        query = parse_qs(urlsplit(call["url"]).query)
        self.assertEqual(query["modLoaderType"][0], "1")
        self.assertEqual(query["gameVersion"][0], "1.20.1")
        self.assertEqual(query["sortField"][0], "6")

    def test_category_id_resolution(self) -> None:
        source = CurseForgeSource(
            "test-key", client=self.client, cache_root=self.cache
        )
        source.search("jei", "mod", category="Map and Information")
        from urllib.parse import parse_qs, urlsplit

        query = parse_qs(urlsplit(self.client.calls[-1]["url"]).query)
        self.assertEqual(query["categoryId"][0], "423")

    def test_versions_parse(self) -> None:
        source = CurseForgeSource(
            "test-key", client=self.client, cache_root=self.cache
        )
        versions = source.versions("238222", game_version="1.20.1", loader="forge")
        self.assertEqual(len(versions), 2)
        release = next(item for item in versions if item.version_type == "release")
        self.assertEqual(release.version_type, "release")
        self.assertEqual(release.game_versions, ("1.20.1",))
        self.assertEqual(release.loaders, ("forge",))
        self.assertEqual(
            release.primary_file.url,
            "https://edge.forgecdn.net/files/5555/555/jei-1.20.1-forge-15.56.0.205.jar",
        )
        self.assertEqual(len(release.dependencies), 1)
        self.assertEqual(release.dependencies[0].dependency_type, "required")
        blocked = next(item for item in versions if item.version_type == "beta")
        self.assertEqual(blocked.primary_file.url, "")
        self.assertEqual(blocked.primary_file.sha1, "")

    def test_null_download_url_is_explained(self) -> None:
        source = CurseForgeSource(
            "test-key", client=self.client, cache_root=self.cache
        )
        versions = source.versions("238222")
        blocked = next(item for item in versions if item.version_type == "beta")
        project = _make_project(
            id="238222", slug="jei", source="curseforge", project_type="mod"
        )
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ContentError) as context:
                build_content_plan(
                    project,
                    blocked,
                    blocked.primary_file,
                    Path(temp),
                    "mod",
                )
            self.assertIn("没有可用的下载地址", str(context.exception))

    def test_403_message(self) -> None:
        def fail(_url):
            raise DownloadError("下载元数据失败：...\n  - HTTP 403 Forbidden")

        self.client.responses["/mods/search"] = fail
        source = CurseForgeSource(
            "bad-key", client=self.client, cache_root=self.cache
        )
        with self.assertRaises(ContentError) as context:
            source.search("jei", "mod")
        self.assertIn("403", str(context.exception))


class FormattingTests(unittest.TestCase):
    def test_format_search_and_versions(self) -> None:
        project = _make_project()
        result = ContentSearchResult(projects=[project], total=1)
        text = format_search_result(result)
        self.assertIn(project.title, text)
        self.assertIn("共 1 个结果", text)
        versions = [_make_version()]
        text = format_versions(project, versions)
        self.assertIn("15.0.0", text)

    def test_content_types_cover_requirement(self) -> None:
        self.assertEqual(
            set(CONTENT_TYPES), {"mod", "modpack", "datapack", "resourcepack", "shader"}
        )


if __name__ == "__main__":
    unittest.main()
