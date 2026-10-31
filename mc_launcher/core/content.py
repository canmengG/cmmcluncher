"""Content download center: Modrinth / CurseForge search and safe installs.

0.5.0 extends the launcher from "game/loader downloader" to a small content
manager.  The module is deliberately split into three layers:

* **read-only sources** (:class:`ModrinthSource`, :class:`CurseForgeSource`)
  turn public APIs into stable dataclasses.  Search/version requests never
  write files;
* **plans** (:func:`build_content_plan`, :func:`build_modpack_plan`) validate
  every remote name, URL, hash and destination before anything is downloaded;
* **installers** (:func:`install_content`, :func:`install_modpack`) require an
  explicit ``confirmed=True`` (the UI/CLI only pass it after user consent),
  verify SHA-1 and write a sidecar so "already installed" can be shown later.

Only the standard library is used.  Every download URL is checked against
:data:`mc_launcher.core.net.CONTENT_HOSTS` and every archive entry is checked
for zip-slip, absolute paths, Windows reserved names, symlinks and size bombs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import threading
import time
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .. import APP_VERSION
from .errors import LauncherError, VersionError
from .net import (
    CONTENT_HOSTS,
    CancelledError,
    DownloadError,
    DownloadSpec,
    HttpClient,
    PauseToken,
    human_size,
    sha1_file,
    validate_url,
)
from .safety import is_within, normalise_sha1, safe_component, safe_relative_path
from .storage import cache_dir

CONTENT_TYPES: dict[str, str] = {
    "mod": "Mod",
    "modpack": "整合包",
    "datapack": "数据包",
    "resourcepack": "资源包",
    "shader": "光影包",
}
CONTENT_TYPE_ORDER = ("mod", "modpack", "datapack", "resourcepack", "shader")

SOURCE_MODRINTH = "modrinth"
SOURCE_CURSEFORGE = "curseforge"
SOURCE_LABELS = {SOURCE_MODRINTH: "Modrinth", SOURCE_CURSEFORGE: "CurseForge"}

MODRINTH_API = "https://api.modrinth.com/v2"
CURSEFORGE_API = "https://api.curseforge.com/v1"

SORT_OPTIONS = (
    ("relevance", "相关度"),
    ("downloads", "下载量"),
    ("follows", "关注数"),
    ("updated", "最近更新"),
)

MAX_SEARCH_LIMIT = 50
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_TAG_BYTES = 4 * 1024 * 1024
TAGS_TTL_SECONDS = 6 * 60 * 60
MAX_VERSIONS = 2000
MAX_ZIP_ENTRIES = 20000
MAX_ZIP_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_ZIP_FILE_BYTES = 1024 * 1024 * 1024
MAX_MRPACK_INDEX_BYTES = 4 * 1024 * 1024
MAX_MRPACK_FILES = 5000
MAX_CONTENT_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILENAME_LENGTH = 180

SIDECAR_NAME = ".mc-launcher-content.json"
SIDECAR_SCHEMA = 1
MODPACK_MARKER_NAME = ".mc-launcher-modpack.json"
MODPACK_MARKER_SCHEMA = 1

USER_AGENT = f"MinecraftLauncher/{APP_VERSION} (local; +https)"

CONTENT_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "mod": (".jar",),
    "resourcepack": (".zip",),
    "shader": (".zip",),
    "datapack": (".zip",),
    "modpack": (".mrpack", ".zip"),
}

MODRINTH_TYPE = {
    "mod": "mod",
    "modpack": "modpack",
    "datapack": "mod",  # Modrinth stores datapacks as mods with loader=datapack
    "resourcepack": "resourcepack",
    "shader": "shader",
}

CONTENT_LOADERS = ("fabric", "forge", "neoforge", "quilt")

CURSEFORGE_GAME_ID = 432
CURSEFORGE_CLASS_IDS = {
    "mod": 6,
    "modpack": 4471,
    "resourcepack": 12,
    "datapack": 6945,
    "shader": 6552,
}
CURSEFORGE_LOADER_IDS = {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6}
CURSEFORGE_SORT_FIELDS = {
    "relevance": 2,
    "downloads": 6,
    "follows": 2,
    "updated": 3,
}
CURSEFORGE_TYPE_PATHS = {
    "mod": "mc-mods",
    "modpack": "modpacks",
    "resourcepack": "texture-packs",
    "shader": "shaders",
    "datapack": "mc-mods",
}
CURSEFORGE_DEPENDENCY_TYPES = {
    1: "embedded",
    2: "optional",
    3: "required",
    4: "tool",
    5: "incompatible",
    6: "include",
}


class ContentError(LauncherError):
    """A content request, plan or install failed."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContentCategory:
    name: str
    project_type: str
    header: str = ""
    icon: str = ""
    id: str = ""

    @property
    def display(self) -> str:
        return self.name


@dataclass(frozen=True)
class ContentGameVersion:
    version: str
    version_type: str = ""
    date: datetime | None = None


@dataclass(frozen=True)
class ContentFile:
    filename: str
    url: str
    size: int = 0
    sha1: str = ""
    sha512: str = ""
    primary: bool = False

    @property
    def has_hash(self) -> bool:
        return bool(self.sha1)


@dataclass(frozen=True)
class ContentDependency:
    project_id: str
    dependency_type: str
    version_id: str = ""
    file_name: str = ""
    title: str = ""

    @property
    def type_label(self) -> str:
        return {
            "required": "必需",
            "optional": "可选",
            "incompatible": "不兼容",
            "embedded": "内置",
            "tool": "工具",
            "include": "包含",
        }.get(self.dependency_type, self.dependency_type or "未知")


@dataclass(frozen=True)
class ContentVersion:
    id: str
    project_id: str
    version_number: str
    name: str
    game_versions: tuple[str, ...]
    loaders: tuple[str, ...]
    version_type: str
    date_published: datetime | None
    downloads: int
    files: tuple[ContentFile, ...] = ()
    dependencies: tuple[ContentDependency, ...] = ()
    source: str = SOURCE_MODRINTH

    @property
    def primary_file(self) -> ContentFile | None:
        for item in self.files:
            if item.primary:
                return item
        return self.files[0] if self.files else None

    @property
    def size_bytes(self) -> int:
        item = self.primary_file
        return item.size if item else 0

    @property
    def date_text(self) -> str:
        if self.date_published is None:
            return "未知"
        return self.date_published.astimezone().strftime("%Y-%m-%d")

    @property
    def type_label(self) -> str:
        return {"release": "release", "beta": "beta", "alpha": "alpha"}.get(
            self.version_type, self.version_type or "未知"
        )

    @property
    def game_version_text(self) -> str:
        return ", ".join(self.game_versions[:4]) + (
            " …" if len(self.game_versions) > 4 else ""
        )

    @property
    def loader_text(self) -> str:
        return ", ".join(self.loaders) if self.loaders else "—"


@dataclass(frozen=True)
class ContentProject:
    id: str
    slug: str
    title: str
    description: str
    author: str
    project_type: str
    categories: tuple[str, ...]
    downloads: int
    follows: int
    date_modified: datetime | None
    icon_url: str = ""
    page_url: str = ""
    latest_version: str = ""
    source: str = SOURCE_MODRINTH

    @property
    def type_label(self) -> str:
        return CONTENT_TYPES.get(self.project_type, self.project_type or "未知")

    @property
    def date_text(self) -> str:
        if self.date_modified is None:
            return "未知"
        return self.date_modified.astimezone().strftime("%Y-%m-%d")

    @property
    def category_text(self) -> str:
        return ", ".join(self.categories[:4])


@dataclass
class ContentSearchResult:
    projects: list[ContentProject]
    total: int
    offset: int = 0
    limit: int = 20
    source: str = SOURCE_MODRINTH


@dataclass(frozen=True)
class ContentInstallPlan:
    project: ContentProject
    version: ContentVersion
    file: ContentFile
    content_type: str
    game_root: Path
    target_dir: Path
    dest: Path
    overwrite: bool = False

    @property
    def size(self) -> int:
        return self.file.size

    @property
    def sha1(self) -> str:
        return self.file.sha1

    @property
    def url(self) -> str:
        return self.file.url

    @property
    def filename(self) -> str:
        return self.dest.name


@dataclass
class ContentInstallOutcome:
    plan: ContentInstallPlan
    downloaded_bytes: int = 0
    already_installed: bool = False
    sidecar: Path | None = None

    @property
    def message(self) -> str:
        if self.already_installed:
            return f"文件已存在且 SHA-1 一致：{self.plan.dest}"
        return f"已安装：{self.plan.dest}"


@dataclass
class ContentProgress:
    phase: str = ""
    current: str = ""
    downloaded: int = 0
    total: int = 0
    files_done: int = 0
    files_total: int = 0

    @property
    def fraction(self) -> float:
        if self.total <= 0:
            return 0.0
        return max(0.0, min(1.0, self.downloaded / self.total))


@dataclass(frozen=True)
class WorldInfo:
    path: Path
    name: str


@dataclass
class ModpackFile:
    path: Path
    urls: tuple[str, ...]
    sha1: str
    sha512: str = ""
    size: int = 0
    env_client: str = "required"
    env_server: str = "required"

    @property
    def client_supported(self) -> bool:
        return self.env_client not in ("unsupported",)


@dataclass
class ModpackIndex:
    archive: Path
    format_version: int
    name: str
    version_id: str
    dependencies: dict[str, str]
    files: list[ModpackFile]
    has_overrides: bool = False
    has_client_overrides: bool = False

    @property
    def minecraft_version(self) -> str:
        return self.dependencies.get("minecraft", "")

    @property
    def loader_summary(self) -> str:
        parts: list[str] = []
        for key, label in (
            ("fabric-loader", "Fabric"),
            ("forge", "Forge"),
            ("neoforge", "NeoForge"),
            ("quilt-loader", "Quilt"),
        ):
            if self.dependencies.get(key):
                parts.append(f"{label} {self.dependencies[key]}")
        return " / ".join(parts) if parts else "未声明加载器"


@dataclass(frozen=True)
class ModpackPlan:
    index: ModpackIndex
    game_root: Path
    instance_dir: Path
    overwrite: bool = False
    # Optional launchable-instance fields.  Content-only installs (the legacy
    # ``<game>/modpacks`` layout and 0.6.0 loaders) leave these empty.
    version_dir: Path | None = None
    version_id: str = ""
    launchable: bool = False
    loader_label: str = ""


@dataclass
class ModpackOutcome:
    archive: Path
    instance_dir: Path
    index: ModpackIndex
    downloaded_files: int = 0
    downloaded_bytes: int = 0
    skipped_files: int = 0
    failed_files: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    complete: bool = False
    version_dir: Path | None = None
    version_id: str = ""
    launchable: bool = False
    loader_label: str = ""

    @property
    def message(self) -> str:
        status = "完成" if self.complete else "未完全完成"
        lines = [
            f"整合包{status}：{self.index.name} {self.index.version_id}",
            f"实例目录：{self.instance_dir}",
            f"下载 {self.downloaded_files} 个文件 / "
            f"{human_size(self.downloaded_bytes)}，跳过 {self.skipped_files} 个",
        ]
        if self.launchable and self.version_id:
            lines.append(f"可启动版本：{self.version_id}")
        else:
            lines.append("未创建可启动版本（仅整理整合包内容）")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_SHA512_RE = re.compile(r"^[0-9a-f]{128}$")
_VERSIONISH_RE = re.compile(r"^[0-9][0-9A-Za-z._\-+ ]{0,63}$")


def _clean_id(value: object, what: str) -> str:
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        raise ContentError(f"{what} 无效。")
    text = value.strip()
    if not _ID_RE.fullmatch(text):
        raise ContentError(f"{what} 无效：{value!r}")
    return text


def _optional_int(value: object, default: int = 0, *, maximum: int | None = None) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        result = value
    elif isinstance(value, float):
        result = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        result = int(value.strip())
    else:
        return default
    if result < 0:
        return default
    if maximum is not None and result > maximum:
        return maximum
    return result


def _optional_text(value: object, *, maximum: int = 4000) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:maximum]


def _string_list(value: object, *, maximum: int = 64, item_length: int = 80) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text or len(text) > item_length or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= maximum:
            break
    return tuple(result)


def parse_content_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sha1_or_empty(value: object) -> str:
    try:
        return normalise_sha1(value, what="内容文件 SHA-1")
    except VersionError:
        return ""


def _sha512_or_empty(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip().lower()
    return text if _SHA512_RE.fullmatch(text) else ""


def _validate_content_url(url: object, *, what: str = "内容下载地址") -> str:
    if not isinstance(url, str) or not url:
        raise ContentError(f"{what}为空。")
    try:
        validate_url(url, CONTENT_HOSTS, check_dns=False)
    except DownloadError as exc:
        raise ContentError(f"{what}不安全或不支持：{exc}") from exc
    return url


def _safe_rel(value: object, *, what: str = "内容文件路径") -> Path:
    try:
        return safe_relative_path(value, what=what)
    except VersionError as exc:
        raise ContentError(str(exc)) from exc


def sanitize_content_filename(filename: object, content_type: str) -> str:
    """Return a single safe filename with an extension allowed for the type."""
    if content_type not in CONTENT_EXTENSIONS:
        raise ContentError(f"未知内容类型：{content_type}")
    if not isinstance(filename, str) or not filename.strip():
        raise ContentError("内容文件名无效。")
    # Never trust a path from a remote API: keep the last component only, then
    # apply the same rules used for version folders.
    name = filename.replace("\\", "/").split("/")[-1].strip()
    if not name:
        raise ContentError("内容文件名为空。")
    try:
        name = safe_component(name, what="内容文件名", max_length=MAX_FILENAME_LENGTH)
    except VersionError as exc:
        raise ContentError(str(exc)) from exc
    if name.startswith("."):
        raise ContentError(f"内容文件名不能是隐藏文件：{filename!r}")
    extension = Path(name).suffix.lower()
    if extension not in CONTENT_EXTENSIONS[content_type]:
        allowed = " / ".join(CONTENT_EXTENSIONS[content_type])
        raise ContentError(
            f"文件扩展名 {extension or '（无）'} 不适合 {CONTENT_TYPES[content_type]}；"
            f"允许：{allowed}"
        )
    return name


def _project_page_url(source: str, project_type: str, slug: str) -> str:
    if not slug:
        return ""
    if source == SOURCE_MODRINTH:
        path = {
            "mod": "mod",
            "modpack": "modpack",
            "resourcepack": "resourcepack",
            "shader": "shader",
            "datapack": "datapack",
        }.get(project_type, project_type or "mod")
        return f"https://modrinth.com/{path}/{urllib.parse.quote(slug)}"
    path = CURSEFORGE_TYPE_PATHS.get(project_type, "mc-mods")
    return f"https://www.curseforge.com/minecraft/{path}/{urllib.parse.quote(slug)}"


def _parse_modrinth_project(data: dict[str, Any], *, source: str = SOURCE_MODRINTH) -> ContentProject:
    project_id = _clean_id(data.get("project_id") or data.get("id"), "项目 ID")
    slug = _optional_text(data.get("slug"), maximum=120)
    project_type = _optional_text(
        data.get("project_type") or data.get("type"), maximum=32
    ) or "mod"
    categories = _string_list(
        data.get("display_categories") or data.get("categories"), maximum=24
    )
    author = _optional_text(data.get("author"), maximum=120)
    if not author:
        organization = data.get("organization")
        if isinstance(organization, str):
            author = organization[:120]
    page_url = _project_page_url(SOURCE_MODRINTH, project_type, slug or project_id)
    return ContentProject(
        id=project_id,
        slug=slug or project_id,
        title=_optional_text(data.get("title") or data.get("name"), maximum=200)
        or project_id,
        description=_optional_text(data.get("description") or data.get("summary")),
        author=author or "未知作者",
        project_type=project_type,
        categories=categories,
        downloads=_optional_int(data.get("downloads"), maximum=2**63 - 1),
        follows=_optional_int(
            data.get("follows") or data.get("followers"), maximum=2**63 - 1
        ),
        date_modified=parse_content_time(data.get("date_modified") or data.get("updated")),
        icon_url=_optional_text(data.get("icon_url"), maximum=1024),
        page_url=page_url,
        latest_version=_optional_text(data.get("latest_version"), maximum=80),
        source=source,
    )


def _parse_modrinth_file(data: object) -> ContentFile | None:
    if not isinstance(data, dict):
        return None
    filename = data.get("filename")
    url = data.get("url")
    if not isinstance(filename, str) or not filename.strip():
        return None
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    hashes = data.get("hashes") if isinstance(data.get("hashes"), dict) else {}
    return ContentFile(
        filename=filename.strip(),
        url=url,
        size=_optional_int(data.get("size"), maximum=MAX_CONTENT_FILE_BYTES),
        sha1=_sha1_or_empty(hashes.get("sha1")),
        sha512=_sha512_or_empty(hashes.get("sha512")),
        primary=bool(data.get("primary")),
    )


def _parse_modrinth_dependency(data: object) -> ContentDependency | None:
    if not isinstance(data, dict):
        return None
    project_id = data.get("project_id")
    try:
        project_id = _clean_id(project_id, "依赖项目 ID")
    except ContentError:
        return None
    dependency_type = _optional_text(data.get("dependency_type"), maximum=24) or "required"
    if dependency_type not in ("required", "optional", "incompatible", "embedded"):
        dependency_type = "required"
    return ContentDependency(
        project_id=project_id,
        dependency_type=dependency_type,
        version_id=_optional_text(data.get("version_id"), maximum=80),
        file_name=_optional_text(data.get("file_name"), maximum=200),
    )


def _parse_modrinth_version(
    data: object, *, default_project_id: str = "", source: str = SOURCE_MODRINTH
) -> ContentVersion | None:
    if not isinstance(data, dict):
        return None
    try:
        version_id = _clean_id(data.get("id"), "版本 ID")
        project_id = _clean_id(
            data.get("project_id") or default_project_id, "项目 ID"
        )
    except ContentError:
        return None
    files = tuple(
        item
        for item in (_parse_modrinth_file(raw) for raw in (data.get("files") or []))
        if item is not None
    )
    dependencies = tuple(
        item
        for item in (
            _parse_modrinth_dependency(raw)
            for raw in (data.get("dependencies") or [])
        )
        if item is not None
    )
    version_type = _optional_text(data.get("version_type"), maximum=16) or "release"
    if version_type not in ("release", "beta", "alpha"):
        version_type = "release"
    return ContentVersion(
        id=version_id,
        project_id=project_id,
        version_number=_optional_text(
            data.get("version_number"), maximum=80
        )
        or version_id,
        name=_optional_text(data.get("name"), maximum=200),
        game_versions=_string_list(data.get("game_versions"), maximum=200),
        loaders=_string_list(data.get("loaders"), maximum=16, item_length=32),
        version_type=version_type,
        date_published=parse_content_time(data.get("date_published")),
        downloads=_optional_int(data.get("downloads"), maximum=2**63 - 1),
        files=files,
        dependencies=dependencies,
        source=source,
    )


# ---------------------------------------------------------------------------
# Facets and version selection
# ---------------------------------------------------------------------------


def build_facets(
    content_type: str,
    game_version: str = "",
    loader: str = "",
    category: str = "",
) -> list[list[str]]:
    """Build Modrinth search facets for one content type.

    Datapacks are stored by Modrinth as ``project_type=mod`` and must be
    selected with ``loaders:datapack``; using ``project_type:datapack`` returns
    ordinary mods (verified 2026-10-31).
    """
    if content_type not in CONTENT_TYPES:
        raise ContentError(f"未知内容类型：{content_type}")
    facets: list[list[str]] = [[f"project_type:{MODRINTH_TYPE[content_type]}"]]
    if content_type == "datapack":
        facets.append(["loaders:datapack"])
    if game_version.strip():
        facets.append([f"versions:{game_version.strip()}"])
    loader_key = loader.strip().lower()
    if loader_key and content_type in ("mod", "modpack"):
        facets.append([f"categories:{loader_key}"])
    category_key = category.strip()
    if category_key:
        facets.append([f"categories:{category_key}"])
    return facets


def select_best_version(
    versions: Sequence[ContentVersion],
    game_version: str | None = None,
    loader: str | None = None,
    channel: str = "release",
) -> ContentVersion | None:
    """Pick the preferred version, while the UI still lets users override it.

    ``release`` prefers release builds but falls back to beta/alpha when no
    release matches, because a hard filter would hide the only usable build for
    many old Minecraft versions.
    """
    game_version = (game_version or "").strip()
    loader_key = (loader or "").strip().lower()

    def matches(version: ContentVersion) -> bool:
        if game_version and game_version not in version.game_versions:
            return False
        if loader_key:
            if loader_key not in {item.lower() for item in version.loaders}:
                return False
        return True

    pool = [item for item in versions if matches(item)]
    if not pool:
        return None

    def date_key(version: ContentVersion) -> datetime:
        return version.date_published or datetime.min.replace(tzinfo=timezone.utc)

    if channel == "latest":
        return max(pool, key=lambda item: (date_key(item), item.downloads))
    releases = [item for item in pool if item.version_type == "release"]
    preferred = releases or pool
    return max(preferred, key=lambda item: (date_key(item), item.downloads))


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


class ContentSource:
    id = ""
    name = ""
    requires_key = False

    def search(
        self,
        query: str,
        content_type: str,
        *,
        game_version: str = "",
        loader: str = "",
        category: str = "",
        sort: str = "relevance",
        offset: int = 0,
        limit: int = 20,
    ) -> ContentSearchResult:
        raise NotImplementedError

    def categories(self, content_type: str) -> list[ContentCategory]:
        raise NotImplementedError

    def loaders(self) -> list[str]:
        raise NotImplementedError

    def game_versions(self) -> list[ContentGameVersion]:
        raise NotImplementedError

    def versions(
        self,
        project_id: str,
        *,
        game_version: str = "",
        loader: str = "",
    ) -> list[ContentVersion]:
        raise NotImplementedError

    def project(self, project_id: str) -> ContentProject:
        raise NotImplementedError

    def enabled(self) -> bool:
        return True


def _clamp_limit(limit: int) -> int:
    return max(1, min(MAX_SEARCH_LIMIT, int(limit)))


class ModrinthSource(ContentSource):
    id = SOURCE_MODRINTH
    name = "Modrinth"

    def __init__(
        self,
        *,
        client: HttpClient | None = None,
        cache_root: Path | None = None,
        logger: Callable[[str], None] | None = None,
        timeout: float = 20.0,
        segments: int = 4,
    ) -> None:
        self._log = logger or (lambda _message: None)
        self.client = client or HttpClient(
            CONTENT_HOSTS,
            strict_redirects=False,
            timeout=timeout,
            retries=1,
            segments=max(0, min(8, int(segments))),
            logger=self._log,
        )
        self.cache_root = cache_root or cache_dir("content")
        self._tag_cache: dict[str, list[Any]] = {}

    # -- HTTP -------------------------------------------------------

    def _get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        max_size: int = MAX_JSON_BYTES,
    ) -> Any:
        url = MODRINTH_API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            payload = self.client.read_bytes(
                url,
                max_size=max_size,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            )
        except DownloadError as exc:
            raise ContentError(self._describe_http_error(exc, url)) from exc
        try:
            data = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContentError(f"Modrinth 返回的内容不是有效 JSON：{exc}") from exc
        return data

    @staticmethod
    def _describe_http_error(exc: Exception, url: str) -> str:
        text = str(exc)
        if "HTTP 429" in text:
            return "Modrinth 请求过于频繁（429），请稍后再试。"
        if "HTTP 5" in text:
            return "Modrinth 服务器暂时不可用，请稍后重试。"
        return f"无法访问 Modrinth：{text}"

    def _tag(self, name: str, loader: Callable[[], Any]) -> list[Any]:
        if name in self._tag_cache:
            return self._tag_cache[name]
        path = self.cache_root / "tags" / f"{name}.json"
        cached: list[Any] | None = None
        if path.is_file():
            try:
                wrapper = json.loads(path.read_text(encoding="utf-8-sig"))
                if (
                    isinstance(wrapper, dict)
                    and wrapper.get("schema") == 1
                    and time.time() - float(wrapper.get("saved_at") or 0) < TAGS_TTL_SECONDS
                ):
                    data = wrapper.get("data")
                    if isinstance(data, list):
                        cached = data
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                cached = None
        if cached is None:
            try:
                data = loader()
                if not isinstance(data, list):
                    raise ContentError("Modrinth 标签接口返回了意外的数据。")
                cached = data
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = path.with_name(path.name + ".tmp")
                    temporary.write_text(
                        json.dumps(
                            {"schema": 1, "saved_at": time.time(), "data": data},
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                    os.replace(temporary, path)
                except OSError:
                    pass
            except ContentError:
                # Offline fallback: an old cache is better than no categories.
                if not path.is_file():
                    raise
                try:
                    wrapper = json.loads(path.read_text(encoding="utf-8-sig"))
                    data = wrapper.get("data") if isinstance(wrapper, dict) else None
                    if not isinstance(data, list):
                        raise
                    cached = data
                    self._log("Modrinth 标签请求失败，使用本地缓存。")
                except Exception:
                    raise
        self._tag_cache[name] = list(cached or [])
        return self._tag_cache[name]

    # -- Read-only API ---------------------------------------------

    def search(
        self,
        query: str,
        content_type: str,
        *,
        game_version: str = "",
        loader: str = "",
        category: str = "",
        sort: str = "relevance",
        offset: int = 0,
        limit: int = 20,
    ) -> ContentSearchResult:
        if content_type not in CONTENT_TYPES:
            raise ContentError(f"未知内容类型：{content_type}")
        index = "relevance"
        for key, _label in SORT_OPTIONS:
            if key == sort:
                index = key
                break
        params: dict[str, Any] = {
            "query": (query or "").strip()[:120],
            "limit": _clamp_limit(limit),
            "offset": max(0, min(10000, int(offset))),
            "index": index,
            "facets": json.dumps(
                build_facets(content_type, game_version, loader, category),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
        data = self._get_json("/search", params)
        if not isinstance(data, dict):
            raise ContentError("Modrinth 搜索响应格式错误。")
        raw_hits = data.get("hits")
        if not isinstance(raw_hits, list):
            raise ContentError("Modrinth 搜索响应缺少 hits。")
        projects: list[ContentProject] = []
        for raw in raw_hits[:MAX_SEARCH_LIMIT]:
            if not isinstance(raw, dict):
                continue
            try:
                projects.append(_parse_modrinth_project(raw))
            except ContentError:
                continue
        return ContentSearchResult(
            projects=projects,
            total=_optional_int(data.get("total_hits"), maximum=2**31 - 1),
            offset=_optional_int(data.get("offset"), maximum=10000),
            limit=_optional_int(data.get("limit"), default=len(projects), maximum=MAX_SEARCH_LIMIT),
            source=self.id,
        )

    def categories(self, content_type: str) -> list[ContentCategory]:
        if content_type not in CONTENT_TYPES:
            raise ContentError(f"未知内容类型：{content_type}")
        raw = self._tag("categories", lambda: self._get_json("/tag/category", max_size=MAX_TAG_BYTES))
        lookup_type = "mod" if content_type == "datapack" else content_type
        result: list[ContentCategory] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            if item.get("project_type") != lookup_type:
                continue
            # "resolutions"/"features"/"performance impact" are useful filters
            # but not categories; keep the combo focused.
            if item.get("header") != "categories":
                continue
            name = _optional_text(item.get("name"), maximum=60)
            if not name:
                continue
            result.append(
                ContentCategory(
                    name=name,
                    project_type=content_type,
                    header=_optional_text(item.get("header"), maximum=40),
                    icon=_optional_text(item.get("icon"), maximum=512),
                    id=name,
                )
            )
        result.sort(key=lambda item: item.name)
        return result

    def loaders(self) -> list[str]:
        raw = self._tag("loaders", lambda: self._get_json("/tag/loader", max_size=MAX_TAG_BYTES))
        names = {
            _optional_text(item.get("name"), maximum=32).lower()
            for item in raw
            if isinstance(item, dict) and item.get("name")
        }
        return [name for name in CONTENT_LOADERS if name in names] + sorted(
            name for name in names if name not in CONTENT_LOADERS and name in ("datapack", "iris")
        )

    def game_versions(self) -> list[ContentGameVersion]:
        raw = self._tag(
            "game_versions",
            lambda: self._get_json("/tag/game_version", max_size=MAX_TAG_BYTES),
        )
        result: list[ContentGameVersion] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            version = _optional_text(item.get("version"), maximum=64)
            if not version:
                continue
            result.append(
                ContentGameVersion(
                    version=version,
                    version_type=_optional_text(item.get("version_type"), maximum=16),
                    date=parse_content_time(item.get("date")),
                )
            )
        result.sort(
            key=lambda item: item.date or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return result

    def versions(
        self,
        project_id: str,
        *,
        game_version: str = "",
        loader: str = "",
    ) -> list[ContentVersion]:
        project_id = _clean_id(project_id, "项目 ID")
        params: dict[str, Any] = {}
        if game_version.strip():
            params["game_versions"] = json.dumps(
                [game_version.strip()], separators=(",", ":")
            )
        if loader.strip():
            params["loaders"] = json.dumps(
                [loader.strip().lower()], separators=(",", ":")
            )
        data = self._get_json(
            f"/project/{urllib.parse.quote(project_id)}/version",
            params or None,
            max_size=MAX_JSON_BYTES,
        )
        if not isinstance(data, list):
            raise ContentError("Modrinth 版本响应格式错误。")
        if len(data) > MAX_VERSIONS:
            raise ContentError("Modrinth 返回的版本数量异常，已拒绝解析。")
        result: list[ContentVersion] = []
        for raw in data:
            parsed = _parse_modrinth_version(raw, default_project_id=project_id)
            if parsed is not None:
                result.append(parsed)
        # Server-side filtering is authoritative, but keep a local check so a
        # proxy that ignores query parameters cannot silently show invalid
        # versions for the selected game version/loader.
        if game_version.strip():
            result = [item for item in result if game_version.strip() in item.game_versions]
        if loader.strip():
            wanted = loader.strip().lower()
            result = [
                item
                for item in result
                if wanted in {value.lower() for value in item.loaders}
            ]
        result.sort(
            key=lambda item: item.date_published
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return result

    def project(self, project_id: str) -> ContentProject:
        project_id = _clean_id(project_id, "项目 ID")
        data = self._get_json(
            f"/project/{urllib.parse.quote(project_id)}", max_size=MAX_JSON_BYTES
        )
        if not isinstance(data, dict):
            raise ContentError("Modrinth 项目响应格式错误。")
        return _parse_modrinth_project(data)

    def projects(self, ids: Iterable[str]) -> list[ContentProject]:
        cleaned = []
        for item in ids:
            try:
                cleaned.append(_clean_id(item, "项目 ID"))
            except ContentError:
                continue
        if not cleaned:
            return []
        data = self._get_json(
            "/projects",
            {"ids": json.dumps(cleaned[:50], separators=(",", ":"))},
            max_size=MAX_JSON_BYTES,
        )
        if not isinstance(data, list):
            return []
        result = []
        for raw in data:
            if not isinstance(raw, dict):
                continue
            try:
                result.append(_parse_modrinth_project(raw))
            except ContentError:
                continue
        return result


class CurseForgeSource(ContentSource):
    id = SOURCE_CURSEFORGE
    name = "CurseForge"
    requires_key = True

    def __init__(
        self,
        api_key: str = "",
        *,
        client: HttpClient | None = None,
        cache_root: Path | None = None,
        logger: Callable[[str], None] | None = None,
        timeout: float = 20.0,
        segments: int = 4,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self._log = logger or (lambda _message: None)
        self.client = client or HttpClient(
            CONTENT_HOSTS,
            strict_redirects=False,
            timeout=timeout,
            retries=1,
            segments=max(0, min(8, int(segments))),
            logger=self._log,
        )
        self.cache_root = cache_root or cache_dir("content")
        self._category_cache: dict[str, list[ContentCategory]] = {}

    def enabled(self) -> bool:
        return bool(self.api_key)

    def missing_key_message(self) -> str:
        return (
            "CurseForge 需要 API Key。\n"
            "请到 https://console.curseforge.com/ 申请 Key，"
            "然后在“内容 / 资源”页点击“CurseForge Key…”保存到本机配置。\n"
            "Key 只保存在本机 config.json，不会写入日志或发布包。"
        )

    def _require_key(self) -> None:
        if not self.api_key:
            raise ContentError(self.missing_key_message())

    def _get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        max_size: int = MAX_JSON_BYTES,
    ) -> Any:
        self._require_key()
        url = CURSEFORGE_API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            payload = self.client.read_bytes(
                url,
                max_size=max_size,
                headers={
                    "Accept": "application/json",
                    "User-Agent": USER_AGENT,
                    "x-api-key": self.api_key,
                },
            )
        except DownloadError as exc:
            text = str(exc)
            if "HTTP 403" in text:
                raise ContentError(
                    "CurseForge 拒绝了请求（HTTP 403）。请检查 API Key 是否正确、"
                    "是否已启用 CurseForge API 访问。"
                ) from exc
            if "HTTP 429" in text:
                raise ContentError("CurseForge 请求过于频繁（429），请稍后再试。") from exc
            raise ContentError(f"无法访问 CurseForge：{text}") from exc
        try:
            data = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContentError(f"CurseForge 返回的内容不是有效 JSON：{exc}") from exc
        return data

    def _loader_id(self, loader: str) -> int:
        return CURSEFORGE_LOADER_IDS.get((loader or "").strip().lower(), 0)

    def _category_id(self, name: str, content_type: str) -> int:
        name = (name or "").strip()
        if not name:
            return 0
        for item in self.categories(content_type):
            if item.id and (item.name == name or item.id == name):
                try:
                    return int(item.id)
                except ValueError:
                    return 0
        return 0

    def search(
        self,
        query: str,
        content_type: str,
        *,
        game_version: str = "",
        loader: str = "",
        category: str = "",
        sort: str = "relevance",
        offset: int = 0,
        limit: int = 20,
    ) -> ContentSearchResult:
        if content_type not in CONTENT_TYPES:
            raise ContentError(f"未知内容类型：{content_type}")
        params: dict[str, Any] = {
            "gameId": CURSEFORGE_GAME_ID,
            "classId": CURSEFORGE_CLASS_IDS[content_type],
            "searchFilter": (query or "").strip()[:100],
            "sortField": CURSEFORGE_SORT_FIELDS.get(sort, 2),
            "sortOrder": "desc",
            "pageSize": _clamp_limit(limit),
            "index": max(0, int(offset)),
        }
        if game_version.strip():
            params["gameVersion"] = game_version.strip()
        loader_id = self._loader_id(loader)
        if loader_id and content_type in ("mod", "modpack"):
            params["modLoaderType"] = loader_id
        category_id = self._category_id(category, content_type)
        if category_id:
            params["categoryId"] = category_id
        data = self._get_json("/mods/search", params)
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise ContentError("CurseForge 搜索响应格式错误。")
        pagination = data.get("pagination") if isinstance(data.get("pagination"), dict) else {}
        projects = []
        for raw in data["data"][:MAX_SEARCH_LIMIT]:
            if not isinstance(raw, dict):
                continue
            parsed = self._parse_mod(raw, content_type)
            if parsed is not None:
                projects.append(parsed)
        return ContentSearchResult(
            projects=projects,
            total=_optional_int(pagination.get("totalCount"), maximum=2**31 - 1),
            offset=_optional_int(pagination.get("index"), maximum=10000),
            limit=_optional_int(
                pagination.get("pageSize"), default=len(projects), maximum=MAX_SEARCH_LIMIT
            ),
            source=self.id,
        )

    def _parse_mod(self, data: dict[str, Any], content_type: str) -> ContentProject | None:
        try:
            mod_id = _clean_id(data.get("id"), "项目 ID")
        except ContentError:
            return None
        slug = _optional_text(data.get("slug"), maximum=120) or mod_id
        authors = data.get("authors")
        author = ""
        if isinstance(authors, list) and authors and isinstance(authors[0], dict):
            author = _optional_text(authors[0].get("name"), maximum=120)
        categories = []
        raw_categories = data.get("categories")
        if isinstance(raw_categories, list):
            categories = tuple(
                _optional_text(item.get("name"), maximum=60)
                for item in raw_categories
                if isinstance(item, dict) and item.get("name")
            )[:24]
        links = data.get("links") if isinstance(data.get("links"), dict) else {}
        logo = data.get("logo")
        icon_url = ""
        if isinstance(logo, dict):
            icon_url = _optional_text(logo.get("thumbnailUrl") or logo.get("url"), maximum=1024)
        latest_files = data.get("latestFiles")
        latest_version = ""
        if isinstance(latest_files, list) and latest_files and isinstance(latest_files[0], dict):
            latest_version = _optional_text(
                latest_files[0].get("displayName") or latest_files[0].get("fileName"),
                maximum=80,
            )
        return ContentProject(
            id=mod_id,
            slug=slug,
            title=_optional_text(data.get("name"), maximum=200) or mod_id,
            description=_optional_text(data.get("summary")),
            author=author or "未知作者",
            project_type=content_type,
            categories=categories,
            downloads=_optional_int(data.get("downloadCount"), maximum=2**63 - 1),
            follows=_optional_int(data.get("thumbsUpCount"), maximum=2**63 - 1),
            date_modified=parse_content_time(data.get("dateModified")),
            icon_url=icon_url,
            page_url=_optional_text(links.get("websiteUrl"), maximum=1024)
            or _project_page_url(SOURCE_CURSEFORGE, content_type, slug),
            latest_version=latest_version,
            source=self.id,
        )

    def categories(self, content_type: str) -> list[ContentCategory]:
        if content_type in self._category_cache:
            return list(self._category_cache[content_type])
        class_id = CURSEFORGE_CLASS_IDS[content_type]
        data = self._get_json(
            "/categories",
            {"gameId": CURSEFORGE_GAME_ID, "classId": class_id},
            max_size=MAX_TAG_BYTES,
        )
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise ContentError("CurseForge 分类响应格式错误。")
        result: list[ContentCategory] = []
        for raw in data["data"]:
            if not isinstance(raw, dict):
                continue
            name = _optional_text(raw.get("name"), maximum=60)
            if not name:
                continue
            result.append(
                ContentCategory(
                    name=name,
                    project_type=content_type,
                    icon=_optional_text(raw.get("iconUrl"), maximum=512),
                    id=str(_optional_int(raw.get("id"), maximum=2**31 - 1)),
                )
            )
        result.sort(key=lambda item: item.name)
        self._category_cache[content_type] = list(result)
        return result

    def loaders(self) -> list[str]:
        return list(CONTENT_LOADERS)

    def game_versions(self) -> list[ContentGameVersion]:
        data = self._get_json("/minecraft/version", max_size=MAX_TAG_BYTES)
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            return []
        result: list[ContentGameVersion] = []
        for raw in data["data"]:
            if not isinstance(raw, dict):
                continue
            version = _optional_text(
                raw.get("versionString") or raw.get("name"), maximum=64
            )
            if not version or not _VERSIONISH_RE.fullmatch(version):
                continue
            result.append(
                ContentGameVersion(
                    version=version,
                    version_type="release",
                    date=parse_content_time(raw.get("dateModified")),
                )
            )
        return result

    def versions(
        self,
        project_id: str,
        *,
        game_version: str = "",
        loader: str = "",
    ) -> list[ContentVersion]:
        project_id = _clean_id(project_id, "项目 ID")
        params: dict[str, Any] = {
            "pageSize": 50,
            "index": 0,
        }
        if game_version.strip():
            params["gameVersion"] = game_version.strip()
        loader_id = self._loader_id(loader)
        if loader_id:
            params["modLoaderType"] = loader_id
        data = self._get_json(
            f"/mods/{urllib.parse.quote(project_id)}/files", params
        )
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise ContentError("CurseForge 文件响应格式错误。")
        result: list[ContentVersion] = []
        for raw in data["data"]:
            if not isinstance(raw, dict):
                continue
            parsed = self._parse_file(raw, project_id)
            if parsed is not None:
                result.append(parsed)
        result.sort(
            key=lambda item: item.date_published
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        return result

    def _parse_file(self, data: dict[str, Any], project_id: str) -> ContentVersion | None:
        try:
            file_id = _clean_id(data.get("id"), "文件 ID")
        except ContentError:
            return None
        display = _optional_text(
            data.get("displayName") or data.get("fileName"), maximum=200
        ) or file_id
        file_name = _optional_text(data.get("fileName"), maximum=200) or display
        download_url = data.get("downloadUrl")
        if not isinstance(download_url, str):
            download_url = ""
        hashes = data.get("hashes") if isinstance(data.get("hashes"), list) else []
        sha1 = ""
        for item in hashes:
            if not isinstance(item, dict):
                continue
            if item.get("algo") == 1:
                sha1 = _sha1_or_empty(item.get("value"))
                break
        release_type = _optional_int(data.get("releaseType"), default=1)
        version_type = {1: "release", 2: "beta", 3: "alpha"}.get(release_type, "release")
        game_versions = []
        loaders = []
        raw_game_versions = data.get("gameVersions")
        if isinstance(raw_game_versions, list):
            for item in raw_game_versions:
                if not isinstance(item, str):
                    continue
                lowered = item.strip().lower()
                if lowered in CONTENT_LOADERS:
                    loaders.append(lowered)
                elif _VERSIONISH_RE.fullmatch(item.strip()):
                    game_versions.append(item.strip())
        dependencies = []
        raw_dependencies = data.get("dependencies")
        if isinstance(raw_dependencies, list):
            for item in raw_dependencies:
                if not isinstance(item, dict):
                    continue
                try:
                    dependency_id = _clean_id(item.get("modId"), "依赖项目 ID")
                except ContentError:
                    continue
                relation = _optional_int(item.get("relationType"), default=3)
                dependencies.append(
                    ContentDependency(
                        project_id=dependency_id,
                        dependency_type=CURSEFORGE_DEPENDENCY_TYPES.get(
                            relation, "required"
                        ),
                    )
                )
        file = ContentFile(
            filename=file_name,
            url=download_url,
            size=_optional_int(data.get("fileLength"), maximum=MAX_CONTENT_FILE_BYTES),
            sha1=sha1,
            primary=True,
        )
        return ContentVersion(
            id=file_id,
            project_id=project_id,
            version_number=display,
            name=display,
            game_versions=tuple(dict.fromkeys(game_versions)),
            loaders=tuple(dict.fromkeys(loaders)),
            version_type=version_type,
            date_published=parse_content_time(data.get("fileDate")),
            downloads=_optional_int(data.get("downloadCount"), maximum=2**63 - 1),
            files=(file,),
            dependencies=tuple(dependencies),
            source=self.id,
        )

    def project(self, project_id: str) -> ContentProject:
        project_id = _clean_id(project_id, "项目 ID")
        data = self._get_json(f"/mods/{urllib.parse.quote(project_id)}")
        if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
            raise ContentError("CurseForge 项目响应格式错误。")
        parsed = self._parse_mod(data["data"], "mod")
        if parsed is None:
            raise ContentError("CurseForge 项目数据无效。")
        return parsed


def get_content_source(
    source_id: str = SOURCE_MODRINTH,
    *,
    api_key: str = "",
    client: HttpClient | None = None,
    cache_root: Path | None = None,
    logger: Callable[[str], None] | None = None,
    segments: int = 4,
) -> ContentSource:
    key = (source_id or SOURCE_MODRINTH).strip().lower()
    if key == SOURCE_MODRINTH:
        return ModrinthSource(
            client=client,
            cache_root=cache_root,
            logger=logger,
            segments=segments,
        )
    if key == SOURCE_CURSEFORGE:
        return CurseForgeSource(
            api_key,
            client=client,
            cache_root=cache_root,
            logger=logger,
            segments=segments,
        )
    raise ContentError(f"未知内容来源：{source_id}")


# ---------------------------------------------------------------------------
# Destinations and plans
# ---------------------------------------------------------------------------


def content_type_label(content_type: str) -> str:
    return CONTENT_TYPES.get(content_type, content_type or "未知")


def default_target_dir(
    game_root: Path, content_type: str, world_dir: Path | None = None
) -> Path:
    game_root = Path(game_root)
    if content_type == "mod":
        return game_root / "mods"
    if content_type == "resourcepack":
        return game_root / "resourcepacks"
    if content_type == "shader":
        return game_root / "shaderpacks"
    if content_type == "datapack":
        if world_dir is None:
            raise ContentError("安装数据包前必须选择世界文件夹（包含 level.dat）。")
        return Path(world_dir) / "datapacks"
    if content_type == "modpack":
        return game_root / "modpacks"
    raise ContentError(f"未知内容类型：{content_type}")


def list_worlds(game_root: Path) -> list[WorldInfo]:
    saves = Path(game_root) / "saves"
    result: list[WorldInfo] = []
    if not saves.is_dir():
        return result
    try:
        for item in saves.iterdir():
            try:
                if item.is_dir() and (item / "level.dat").is_file():
                    result.append(WorldInfo(path=item, name=item.name))
            except OSError:
                continue
    except OSError:
        return result
    result.sort(key=lambda item: item.name.lower())
    return result


def validate_world_dir(world_dir: Path) -> Path:
    world = Path(world_dir).expanduser()
    try:
        world = world.resolve()
    except OSError as exc:
        raise ContentError(f"世界目录无效：{world_dir}") from exc
    if not world.is_dir():
        raise ContentError(f"世界目录不存在：{world}")
    if not (world / "level.dat").is_file():
        raise ContentError(
            f"这不是 Minecraft 世界文件夹（缺少 level.dat）：{world}"
        )
    return world


def _validate_target_root(target: Path, game_root: Path, content_type: str, world_dir: Path | None) -> Path:
    target = Path(target).expanduser()
    try:
        resolved = target.resolve()
    except OSError as exc:
        raise ContentError(f"目标目录无效：{target}") from exc
    if resolved.parent == resolved:
        raise ContentError("拒绝把内容安装到磁盘根目录。")
    windows_dir = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    if is_within(windows_dir, resolved):
        raise ContentError(f"拒绝把内容安装到 Windows 系统目录：{resolved}")
    allowed_roots: list[Path] = [Path(game_root).resolve()]
    if content_type == "datapack" and world_dir is not None:
        allowed_roots.append(Path(world_dir).resolve())
    if not any(is_within(root, resolved) for root in allowed_roots):
        raise ContentError(
            f"目标目录必须位于游戏目录（或所选世界目录）内：{resolved}"
        )
    return resolved


def build_content_plan(
    project: ContentProject,
    version: ContentVersion,
    file: ContentFile | None,
    game_root: Path,
    content_type: str,
    *,
    target_dir: Path | None = None,
    world_dir: Path | None = None,
    overwrite: bool = False,
) -> ContentInstallPlan:
    """Validate everything about one file before it is downloaded."""
    if content_type not in CONTENT_TYPES:
        raise ContentError(f"未知内容类型：{content_type}")
    if project.id and version.project_id and project.id != version.project_id:
        raise ContentError("项目与版本不匹配，已拒绝安装。")
    file = file or version.primary_file
    if file is None:
        raise ContentError("该版本没有可下载的文件。")
    filename = sanitize_content_filename(file.filename, content_type)
    if not file.url:
        raise ContentError(
            "该文件没有可用的下载地址（CurseForge 作者可能禁止第三方分发）。\n"
            f"请打开项目页面手动下载：{project.page_url or '(无链接)'}"
        )
    _validate_content_url(file.url)
    if not file.sha1:
        raise ContentError(
            "文件缺少 SHA-1，已拒绝自动下载。\n"
            "这通常意味着来源元数据不完整；请改用其他来源或手动下载。"
        )
    game_root = Path(game_root).resolve()
    if target_dir is None:
        target = default_target_dir(game_root, content_type, world_dir)
    else:
        target = Path(target_dir)
    target = _validate_target_root(target, game_root, content_type, world_dir)
    dest = target / filename
    if not is_within(target, dest):
        raise ContentError("目标文件路径越界，已拒绝安装。")
    if len(str(dest)) > 250:
        raise ContentError("目标路径过长，请缩短游戏目录或文件名。")
    return ContentInstallPlan(
        project=project,
        version=version,
        file=file,
        content_type=content_type,
        game_root=game_root,
        target_dir=target,
        dest=dest,
        overwrite=bool(overwrite),
    )


def build_content_plan_for_version(
    project: ContentProject,
    content_type: str,
    game_root: Path,
    *,
    game_version: str = "",
    loader: str = "",
    channel: str = "release",
    version_id: str = "",
    target_dir: Path | None = None,
    world_dir: Path | None = None,
    overwrite: bool = False,
    source: ContentSource | None = None,
) -> ContentInstallPlan:
    """Fetch versions, select one (or ``version_id``) and build a plan."""
    src = source or get_content_source(project.source)
    versions = src.versions(
        project.id, game_version=game_version, loader=loader
    )
    version: ContentVersion | None = None
    if version_id:
        version = next((item for item in versions if item.id == version_id), None)
        if version is None:
            raise ContentError(f"版本 {version_id} 不在 {project.title} 的版本列表中。")
    else:
        version = select_best_version(
            versions, game_version=game_version, loader=loader, channel=channel
        )
    if version is None:
        raise ContentError(
            f"{project.title} 没有匹配的版本"
            f"（游戏 {game_version or '全部'} / 加载器 {loader or '全部'}）。"
        )
    return build_content_plan(
        project,
        version,
        version.primary_file,
        game_root,
        content_type,
        target_dir=target_dir,
        world_dir=world_dir,
        overwrite=overwrite,
    )


# ---------------------------------------------------------------------------
# Sidecar (installed content index)
# ---------------------------------------------------------------------------


def content_sidecar_path(game_root: Path) -> Path:
    return Path(game_root) / SIDECAR_NAME


def _entry_path(game_root: Path, dest: Path) -> str:
    root = Path(game_root).resolve()
    target = Path(dest).resolve()
    try:
        return target.relative_to(root).as_posix()
    except ValueError:
        return target.as_posix()


def read_content_sidecar(game_root: Path) -> list[dict[str, Any]]:
    path = content_sidecar_path(game_root)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict) or data.get("schema") != SIDECAR_SCHEMA:
        return []
    entries = data.get("entries")
    if not isinstance(entries, list):
        return []
    result = []
    for item in entries:
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            result.append(item)
    return result


def record_content_install(plan: ContentInstallPlan) -> Path:
    """Record an installed file without discarding unrelated entries."""
    path = content_sidecar_path(plan.game_root)
    entries = read_content_sidecar(plan.game_root)
    key = _entry_path(plan.game_root, plan.dest)
    entry = {
        "content_type": plan.content_type,
        "project_id": plan.project.id,
        "slug": plan.project.slug,
        "title": plan.project.title,
        "version_id": plan.version.id,
        "version_number": plan.version.version_number,
        "filename": plan.dest.name,
        "path": key,
        "sha1": plan.file.sha1,
        "size": plan.file.size,
        "source": plan.project.source,
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    entries = [item for item in entries if item.get("path") != key]
    entries.append(entry)
    payload = {"schema": SIDECAR_SCHEMA, "entries": entries[-2000:]}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)
    except OSError as exc:
        raise ContentError(f"无法写入已安装内容记录：{exc}") from exc
    return path


def is_content_installed(game_root: Path, dest: Path) -> bool:
    key = _entry_path(game_root, dest)
    return any(item.get("path") == key for item in read_content_sidecar(game_root))


# ---------------------------------------------------------------------------
# Single-file installation
# ---------------------------------------------------------------------------


def _default_client(logger: Callable[[str], None] | None = None) -> HttpClient:
    return HttpClient(
        CONTENT_HOSTS,
        strict_redirects=False,
        timeout=30.0,
        retries=1,
        logger=logger,
    )


def _file_matches(path: Path, sha1: str, size: int = 0) -> bool:
    try:
        if size and path.stat().st_size != size:
            return False
        return bool(sha1) and sha1_file(path).lower() == sha1.lower()
    except OSError:
        return False


def install_content(
    plan: ContentInstallPlan,
    *,
    client: HttpClient | None = None,
    progress: Callable[[int], None] | None = None,
    cancel: threading.Event | None = None,
    pause: PauseToken | None = None,
    confirmed: bool = False,
    logger: Callable[[str], None] | None = None,
) -> ContentInstallOutcome:
    """Download one content file after explicit confirmation."""
    if not confirmed:
        raise ContentError(
            "尚未确认下载。请点击“下载并安装”并在确认对话框中选择继续。"
        )
    log = logger or (lambda _message: None)
    if plan.dest.exists() and not plan.overwrite:
        if _file_matches(plan.dest, plan.file.sha1, plan.file.size):
            record_content_install(plan)
            return ContentInstallOutcome(
                plan=plan, already_installed=True, sidecar=content_sidecar_path(plan.game_root)
            )
        raise ContentError(
            f"目标文件已存在且内容不同：\n{plan.dest}\n"
            "请确认是否覆盖，或先备份原文件。"
        )
    http = client or _default_client(log)
    # Re-check with the actual client's own host policy.
    http.validate_initial(plan.file.url)
    try:
        plan.target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ContentError(f"无法创建目标目录：{plan.target_dir}\n{exc}") from exc
    spec = DownloadSpec(
        dest=plan.dest,
        url=plan.file.url,
        sha1=plan.file.sha1,
        size=plan.file.size or None,
        label=f"{plan.project.title} / {plan.dest.name}",
    )
    downloaded = http.download_file(
        spec, progress=progress, cancel=cancel, pause=pause
    )
    record_content_install(plan)
    return ContentInstallOutcome(
        plan=plan,
        downloaded_bytes=downloaded,
        sidecar=content_sidecar_path(plan.game_root),
    )


# ---------------------------------------------------------------------------
# ZIP safety and .mrpack
# ---------------------------------------------------------------------------


def _zip_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return stat.S_ISLNK(mode)


def validate_zip_archive(
    path: Path,
    *,
    max_entries: int = MAX_ZIP_ENTRIES,
    max_total_bytes: int = MAX_ZIP_TOTAL_BYTES,
    max_file_bytes: int = MAX_ZIP_FILE_BYTES,
) -> list[zipfile.ZipInfo]:
    """Validate every entry before any byte is extracted."""
    path = Path(path)
    if not path.is_file():
        raise ContentError(f"压缩包不存在：{path}")
    if not zipfile.is_zipfile(path):
        raise ContentError(f"不是有效的 ZIP / .mrpack 文件：{path.name}")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > max_entries:
                raise ContentError(
                    f"压缩包条目过多（{len(infos)} > {max_entries}），已拒绝。"
                )
            total = 0
            for info in infos:
                name = info.filename
                if "\x00" in name:
                    raise ContentError("压缩包条目包含空字符。")
                if info.flag_bits & 0x1:
                    raise ContentError(f"压缩包条目被加密，已拒绝：{name}")
                if _zip_symlink(info):
                    raise ContentError(f"压缩包包含符号链接，已拒绝：{name}")
                normalized = name[:-1] if name.endswith("/") else name
                if not normalized:
                    continue
                _safe_rel(normalized, what="压缩包条目")
                if info.file_size > max_file_bytes:
                    raise ContentError(
                        f"压缩包内单个文件过大：{name}"
                        f"（{human_size(info.file_size)}）"
                    )
                total += max(0, info.file_size)
                if total > max_total_bytes:
                    raise ContentError(
                        f"压缩包解压后总大小超过限制（{human_size(max_total_bytes)}）。"
                    )
            return infos
    except zipfile.BadZipFile as exc:
        raise ContentError(f"压缩包已损坏：{path.name}") from exc
    except OSError as exc:
        raise ContentError(f"无法读取压缩包：{path.name}\n{exc}") from exc


def extract_zip_prefix(
    archive_path: Path,
    dest_root: Path,
    prefix: str,
    *,
    overwrite: bool = False,
    max_file_bytes: int = MAX_ZIP_FILE_BYTES,
) -> int:
    """Extract entries below ``prefix`` into ``dest_root`` safely.

    Returns the number of files written.  Paths are re-validated both before
    and after joining, so a malicious entry cannot escape ``dest_root``.
    """
    dest_root = Path(dest_root).resolve()
    prefix = prefix.replace("\\", "/").strip("/")
    written = 0
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                name = info.filename
                if info.is_dir():
                    continue
                if prefix:
                    if not name.startswith(prefix + "/"):
                        continue
                    relative = name[len(prefix) + 1 :]
                else:
                    relative = name
                if not relative:
                    continue
                rel_path = _safe_rel(relative, what="整合包覆盖文件")
                target = dest_root / rel_path
                if not is_within(dest_root, target):
                    raise ContentError(f"整合包覆盖文件越界：{name}")
                if target.exists() and not overwrite:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    remaining = max_file_bytes
                    while True:
                        chunk = source.read(256 * 1024)
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        if remaining < 0:
                            raise ContentError(
                                f"整合包覆盖文件超过大小限制：{name}"
                            )
                        output.write(chunk)
                written += 1
    except zipfile.BadZipFile as exc:
        raise ContentError(f"整合包已损坏：{archive_path.name}") from exc
    except OSError as exc:
        raise ContentError(f"解压整合包失败：{exc}") from exc
    return written


def read_mrpack(archive_path: Path) -> ModpackIndex:
    """Parse and validate ``modrinth.index.json`` from a ``.mrpack``."""
    archive_path = Path(archive_path)
    infos = validate_zip_archive(archive_path)
    names = {info.filename: info for info in infos}
    index_info = names.get("modrinth.index.json")
    if index_info is None:
        raise ContentError(
            "不是 Modrinth 整合包：缺少 modrinth.index.json。\n"
            "CurseForge 整合包使用 manifest.json，本版本尚未适配。"
        )
    if index_info.file_size > MAX_MRPACK_INDEX_BYTES:
        raise ContentError("整合包索引文件过大，已拒绝。")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            payload = archive.read(index_info)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ContentError(f"无法读取整合包索引：{exc}") from exc
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContentError(f"整合包索引不是有效 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ContentError("整合包索引根节点不是对象。")
    format_version = _optional_int(data.get("formatVersion"), default=1)
    if format_version != 1:
        raise ContentError(f"不支持的整合包格式版本：{format_version}")
    name = _optional_text(data.get("name"), maximum=200) or archive_path.stem
    version_id = _optional_text(data.get("versionId"), maximum=120) or "1"
    raw_dependencies = data.get("dependencies")
    dependencies: dict[str, str] = {}
    if isinstance(raw_dependencies, dict):
        for key, value in raw_dependencies.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            if len(key) > 60 or len(value) > 120:
                continue
            dependencies[key] = value
    raw_files = data.get("files")
    if not isinstance(raw_files, list):
        raise ContentError("整合包索引缺少 files 列表。")
    if len(raw_files) > MAX_MRPACK_FILES:
        raise ContentError("整合包文件数量过多，已拒绝。")
    files: list[ModpackFile] = []
    for raw in raw_files:
        if not isinstance(raw, dict):
            continue
        rel = _safe_rel(raw.get("path"), what="整合包文件路径")
        if rel.parts and rel.parts[0] in ("overrides", "client-overrides"):
            raise ContentError(f"整合包文件路径不能位于 overrides 下：{rel}")
        hashes = raw.get("hashes") if isinstance(raw.get("hashes"), dict) else {}
        sha1 = _sha1_or_empty(hashes.get("sha1"))
        if not sha1:
            raise ContentError(
                f"整合包文件缺少 SHA-1，已拒绝自动下载：{rel}"
            )
        downloads = raw.get("downloads")
        urls: list[str] = []
        if isinstance(downloads, list):
            for item in downloads[:5]:
                if not isinstance(item, str) or not item:
                    continue
                try:
                    urls.append(_validate_content_url(item, what="整合包文件地址"))
                except ContentError:
                    continue
        if not urls:
            raise ContentError(f"整合包文件没有可用的下载地址：{rel}")
        env = raw.get("env") if isinstance(raw.get("env"), dict) else {}
        files.append(
            ModpackFile(
                path=rel,
                urls=tuple(urls),
                sha1=sha1,
                sha512=_sha512_or_empty(hashes.get("sha512")),
                size=_optional_int(
                    raw.get("fileSize"), maximum=MAX_CONTENT_FILE_BYTES
                ),
                env_client=_optional_text(env.get("client"), maximum=16) or "required",
                env_server=_optional_text(env.get("server"), maximum=16) or "required",
            )
        )
    has_overrides = any(
        info.filename.startswith("overrides/") for info in infos if not info.is_dir()
    )
    has_client_overrides = any(
        info.filename.startswith("client-overrides/")
        for info in infos
        if not info.is_dir()
    )
    return ModpackIndex(
        archive=archive_path,
        format_version=format_version,
        name=name,
        version_id=version_id,
        dependencies=dependencies,
        files=files,
        has_overrides=has_overrides,
        has_client_overrides=has_client_overrides,
    )


def content_instance_name(slug: str, version_id: str) -> str:
    """Return a filesystem-safe instance name.

    Chinese pack names are common on Modrinth, so Unicode letters are kept;
    only separators, control characters and other Windows-invalid characters
    are collapsed to ``-``.
    """
    safe_base = re.sub(
        r"[^\w.-]+", "-", (slug or "").strip(), flags=re.UNICODE
    ).strip(".-")[:48] or "modpack"
    safe_version = re.sub(
        r"[^\w.-]+", "-", (version_id or "").strip(), flags=re.UNICODE
    ).strip(".-")[:40] or "1"
    return f"{safe_base}-{safe_version}"


def build_modpack_instance_name(index: ModpackIndex, slug: str = "") -> str:
    base = (slug or "").strip() or "modpack"
    return content_instance_name(base, index.version_id)


def build_modpack_plan(
    index: ModpackIndex,
    game_root: Path,
    *,
    slug: str = "",
    overwrite: bool = False,
    instance_dir: Path | None = None,
    version_dir: Path | None = None,
    version_id: str = "",
    launchable: bool = False,
    loader_label: str = "",
) -> ModpackPlan:
    game_root = Path(game_root).resolve()
    name = build_modpack_instance_name(index, slug)
    try:
        safe_name = safe_component(name, what="整合包实例名", max_length=120)
    except VersionError as exc:
        raise ContentError(str(exc)) from exc
    if instance_dir is None:
        instance_dir = game_root / "modpacks" / safe_name
    instance_dir = Path(instance_dir).resolve()
    if not is_within(game_root, instance_dir):
        raise ContentError("整合包实例目录越界，已拒绝。")
    if len(str(instance_dir)) > 230:
        raise ContentError("整合包实例路径过长，请缩短游戏目录。")
    if version_dir is not None:
        version_dir = Path(version_dir).resolve()
        if not is_within(game_root / "versions", version_dir):
            raise ContentError("整合包版本目录越界，已拒绝。")
    return ModpackPlan(
        index=index,
        game_root=game_root,
        instance_dir=instance_dir,
        overwrite=bool(overwrite),
        version_dir=version_dir,
        version_id=version_id or (safe_name if version_dir is not None else ""),
        launchable=bool(launchable),
        loader_label=loader_label,
    )


def _marker_path(instance_dir: Path) -> Path:
    return Path(instance_dir) / MODPACK_MARKER_NAME


def read_modpack_marker(instance_dir: Path) -> dict[str, Any] | None:
    path = _marker_path(instance_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_modpack_marker(plan: ModpackPlan, outcome: ModpackOutcome) -> None:
    """Write the instance marker after content (and optionally version) install."""
    index = plan.index
    marker_payload = {
        "schema": MODPACK_MARKER_SCHEMA,
        "name": index.name,
        "version_id": index.version_id,
        "dependencies": index.dependencies,
        "archive": str(index.archive),
        "files": [
            {
                "path": item.path.as_posix(),
                "sha1": item.sha1,
                "size": item.size,
            }
            for item in index.files
        ],
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "complete": outcome.complete,
        "launchable": bool(plan.launchable and outcome.complete),
        "game_dir": str(plan.instance_dir),
        "loader": plan.loader_label,
        "instance_version_id": plan.version_id,
        "version_dir": str(plan.version_dir) if plan.version_dir is not None else "",
    }
    try:
        _marker_path(plan.instance_dir).write_text(
            json.dumps(marker_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        outcome.warnings.append(f"无法写入整合包安装记录：{exc}")


def install_modpack(
    plan: ModpackPlan,
    *,
    client: HttpClient | None = None,
    progress: Callable[[ContentProgress], None] | None = None,
    cancel: threading.Event | None = None,
    pause: PauseToken | None = None,
    confirmed: bool = False,
    logger: Callable[[str], None] | None = None,
    write_marker: bool = True,
) -> ModpackOutcome:
    """Install a parsed ``.mrpack``'s content into an instance directory.

    The low-level installer only writes overrides/files and the marker.  A
    launchable instance is orchestrated by :mod:`mc_launcher.core.modpack`,
    which installs the game + loader first and writes the version JSON after
    this function succeeds (``write_marker=False`` then a final marker write).
    """
    if not confirmed:
        raise ContentError("尚未确认安装整合包。")
    log = logger or (lambda _message: None)
    http = client or _default_client(log)
    index = plan.index
    instance_dir = plan.instance_dir
    marker = read_modpack_marker(instance_dir)
    if instance_dir.exists() and any(instance_dir.iterdir()) and not plan.overwrite:
        if marker is None:
            raise ContentError(
                f"实例目录已存在且非本启动器创建：\n{instance_dir}\n"
                "请选择覆盖或删除该目录后重试。"
            )

    outcome = ModpackOutcome(
        archive=index.archive,
        instance_dir=instance_dir,
        index=index,
        version_dir=plan.version_dir,
        version_id=plan.version_id,
        launchable=plan.launchable,
        loader_label=plan.loader_label,
    )
    try:
        instance_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ContentError(f"无法创建整合包实例目录：{instance_dir}\n{exc}") from exc

    total_bytes = sum(item.size or 0 for item in index.files if item.client_supported)
    state = ContentProgress(
        phase="安装整合包",
        total=total_bytes,
        files_total=len([item for item in index.files if item.client_supported]),
    )
    last_emit = 0.0

    def emit(force: bool = False) -> None:
        nonlocal last_emit
        if progress is None:
            return
        now = time.monotonic()
        if not force and now - last_emit < 0.1:
            return
        last_emit = now
        try:
            progress(
                ContentProgress(
                    phase=state.phase,
                    current=state.current,
                    downloaded=state.downloaded,
                    total=state.total,
                    files_done=state.files_done,
                    files_total=state.files_total,
                )
            )
        except Exception:
            pass

    emit(force=True)
    for prefix, enabled in (
        ("overrides", index.has_overrides),
        ("client-overrides", index.has_client_overrides),
    ):
        if not enabled:
            continue
        state.phase = "解压覆盖文件"
        state.current = prefix
        emit(force=True)
        written = extract_zip_prefix(
            index.archive,
            instance_dir,
            prefix,
            overwrite=plan.overwrite,
        )
        if written:
            outcome.warnings.append(f"已解压 {prefix}/：{written} 个文件")

    failures: list[str] = []
    downloaded_paths: list[Path] = []
    completed_bytes = 0
    for item in index.files:
        if cancel is not None and cancel.is_set():
            raise CancelledError("安装已取消。")
        if not item.client_supported:
            outcome.skipped_files += 1
            outcome.warnings.append(
                f"跳过客户端不支持的文件（env.client={item.env_client}）：{item.path}"
            )
            continue
        state.phase = "下载整合包文件"
        state.current = item.path.as_posix()
        state.downloaded = completed_bytes
        emit(force=True)
        dest = instance_dir / item.path
        if not is_within(instance_dir, dest):
            failures.append(f"{item.path}：路径越界")
            continue
        if dest.is_file() and _file_matches(dest, item.sha1, item.size):
            outcome.skipped_files += 1
            state.files_done += 1
            completed_bytes += item.size or 0
            state.downloaded = completed_bytes
            continue
        if dest.exists() and not plan.overwrite:
            failures.append(f"{item.path}：目标已存在且未确认覆盖")
            continue
        spec = DownloadSpec(
            dest=dest,
            url=item.urls[0],
            fallback_urls=tuple(item.urls[1:]),
            sha1=item.sha1,
            size=item.size or None,
            label=item.path.as_posix(),
        )
        last_value = [0]

        def file_progress(value: int, _base: int = completed_bytes) -> None:
            last_value[0] = max(0, value)
            state.downloaded = _base + last_value[0]
            emit()

        try:
            http.download_file(
                spec,
                progress=file_progress,
                cancel=cancel,
                pause=pause,
            )
            downloaded_paths.append(dest)
            outcome.downloaded_files += 1
            outcome.downloaded_bytes += item.size or 0
            state.files_done += 1
            completed_bytes += item.size or last_value[0]
            state.downloaded = completed_bytes
            state.total = max(state.total, completed_bytes)
            emit(force=True)
        except CancelledError:
            for path in downloaded_paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                try:
                    path.with_name(path.name + ".part").unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        except (DownloadError, OSError) as exc:
            failures.append(f"{item.path}：{exc}")
            log(f"整合包文件失败：{item.path}：{exc}")
    outcome.failed_files = failures
    outcome.complete = not failures
    if write_marker:
        write_modpack_marker(plan, outcome)
    if failures:
        detail = "\n".join(f"  - {item}" for item in failures[:8])
        raise ContentError(
            f"整合包安装未完全完成（{len(failures)} 个文件失败）：\n{detail}\n"
            f"实例目录：{instance_dir}"
        )
    emit(force=True)
    return outcome


def download_modpack_archive(
    project: ContentProject,
    version: ContentVersion,
    file: ContentFile | None,
    *,
    client: HttpClient | None = None,
    progress: Callable[[ContentProgress], None] | None = None,
    cancel: threading.Event | None = None,
    pause: PauseToken | None = None,
    confirmed: bool = False,
    logger: Callable[[str], None] | None = None,
    cache_root: Path | None = None,
) -> tuple[Path, ModpackIndex]:
    """Download a ``.mrpack`` to the cache and return its parsed index."""
    if not confirmed:
        raise ContentError("尚未确认下载并安装整合包。")
    log = logger or (lambda _message: None)
    file = file or version.primary_file
    if file is None:
        raise ContentError("该整合包版本没有可下载的文件。")
    filename = sanitize_content_filename(file.filename, "modpack")
    if not file.sha1:
        raise ContentError("整合包文件缺少 SHA-1，已拒绝自动下载。")
    if not file.url:
        raise ContentError(
            f"整合包没有可用下载地址，请打开项目页面手动下载：{project.page_url}"
        )
    _validate_content_url(file.url)
    download_dir = (cache_root or cache_dir("content")) / "modpacks"
    archive_path = download_dir / f"{file.sha1[:12]}-{filename}"
    http = client or _default_client(log)
    archive_total = file.size or 0
    state = ContentProgress(phase="下载整合包", total=archive_total, files_total=1)
    last_emit = [0.0]

    def emit_archive(value: int) -> None:
        state.downloaded = max(0, value)
        state.current = filename
        if progress is None:
            return
        now = time.monotonic()
        if now - last_emit[0] < 0.1 and value < archive_total:
            return
        last_emit[0] = now
        try:
            progress(
                ContentProgress(
                    phase=state.phase,
                    current=state.current,
                    downloaded=state.downloaded,
                    total=state.total,
                    files_done=0,
                    files_total=1,
                )
            )
        except Exception:
            pass

    try:
        download_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ContentError(f"无法创建整合包缓存目录：{download_dir}\n{exc}") from exc
    if not _file_matches(archive_path, file.sha1, file.size):
        http.validate_initial(file.url)
        http.download_file(
            DownloadSpec(
                dest=archive_path,
                url=file.url,
                sha1=file.sha1,
                size=file.size or None,
                label=f"{project.title} / {filename}",
            ),
            progress=emit_archive,
            cancel=cancel,
            pause=pause,
        )
    return archive_path, read_mrpack(archive_path)


def download_and_install_modpack(
    project: ContentProject,
    version: ContentVersion,
    file: ContentFile | None,
    game_root: Path,
    *,
    slug: str = "",
    overwrite: bool = False,
    client: HttpClient | None = None,
    progress: Callable[[ContentProgress], None] | None = None,
    cancel: threading.Event | None = None,
    pause: PauseToken | None = None,
    confirmed: bool = False,
    logger: Callable[[str], None] | None = None,
    cache_root: Path | None = None,
) -> ModpackOutcome:
    """Download a ``.mrpack`` to the cache, then install a content instance."""
    log = logger or (lambda _message: None)
    http = client or _default_client(log)
    archive_path, index = download_modpack_archive(
        project,
        version,
        file,
        client=http,
        progress=progress,
        cancel=cancel,
        pause=pause,
        confirmed=confirmed,
        logger=log,
        cache_root=cache_root,
    )
    plan = build_modpack_plan(
        index, game_root, slug=slug or project.slug, overwrite=overwrite
    )

    base_downloaded = (
        (file.size if file is not None else 0) or archive_path.stat().st_size
    )
    combined_total = base_downloaded + sum(
        item.size or 0 for item in index.files if item.client_supported
    )

    def combined_progress(inner: ContentProgress) -> None:
        if progress is None:
            return
        try:
            progress(
                ContentProgress(
                    phase=inner.phase,
                    current=inner.current,
                    downloaded=base_downloaded + inner.downloaded,
                    total=combined_total,
                    files_done=inner.files_done,
                    files_total=inner.files_total,
                )
            )
        except Exception:
            pass

    return install_modpack(
        plan,
        client=http,
        progress=combined_progress if progress is not None else None,
        cancel=cancel,
        pause=pause,
        confirmed=True,
        logger=log,
    )


# ---------------------------------------------------------------------------
# Formatting helpers for UI/CLI
# ---------------------------------------------------------------------------


def format_search_result(result: ContentSearchResult) -> str:
    if not result.projects:
        return "没有找到匹配的内容。"
    lines = [f"共 {result.total} 个结果（显示 {len(result.projects)} 个）："]
    for index, item in enumerate(result.projects, 1):
        lines.append(
            f"{index:>2}. {item.title}  [{item.type_label}]  {item.author}\n"
            f"    {item.description[:120]}\n"
            f"    下载 {item.downloads:,} · 更新 {item.date_text} · "
            f"{item.page_url}"
        )
    return "\n".join(lines)


def format_versions(project: ContentProject, versions: Sequence[ContentVersion]) -> str:
    if not versions:
        return f"{project.title} 没有匹配的版本。"
    lines = [f"{project.title}：{len(versions)} 个版本"]
    for item in versions[:40]:
        file = item.primary_file
        size = human_size(file.size) if file else "—"
        lines.append(
            f"- {item.version_number} [{item.type_label}] "
            f"{item.game_version_text} / {item.loader_text} · "
            f"{item.date_text} · {size}"
        )
    if len(versions) > 40:
        lines.append(f"… 另有 {len(versions) - 40} 个版本")
    return "\n".join(lines)
