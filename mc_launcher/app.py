"""Tkinter user interface for the offline Minecraft launcher."""

from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import APP_NAME, APP_VERSION
from .core.auth import validate_username
from .core.cache import (
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    clean_cache,
    format_clean_report,
    scan_cache,
    total_cache_bytes,
)
from .core.content import install_content
from .core.errors import LauncherError
from .core.install import (
    Progress,
    build_local_plan,
    build_plan_from_detail,
    format_verify_report,
    verify_plan,
)
from .core.java import (
    discover_java,
    pick_java,
    probe_java,
    required_java_major,
)
from .core.java_runtime import (
    JavaRuntimeCatalog,
    JavaRuntimeComponent,
    JavaRuntimeOutcome,
    RuntimeProgress,
    build_install_plan,
    default_install_root,
    default_platform,
    fetch_catalog,
    fetch_component_manifest,
    find_installed_runtime,
    format_runtime_report,
    install_java_runtime,
    manual_java_links,
    resolve_component,
    scan_installed_runtimes,
    validate_install_root,
    verify_java_runtime,
)
from .core.launch import build_launch_plan, cleanup_plan, start_game
from .core.msauth import (
    AuthCancelled,
    AuthError,
    HttpAuthTransport,
    clear_saved_account,
    ensure_account_fresh,
    load_saved_account,
    login_device_code,
    save_account,
    validate_client_id,
)
from .core.settings import resolve_client_id
from .core.loaders import (
    ForgeCatalog,
    LoaderBuild,
    build_fabric_plan,
    fetch_fabric_loaders,
    fetch_neoforge_builds,
    fetch_quilt_loaders,
    forge_builds,
    install_selected,
    load_forge_catalog,
    select_loader_builds,
    selected_game_detail,
)
from .core.manifest import (
    KIND_LABELS,
    Manifest,
    RemoteVersion,
    filter_versions,
    load_manifest,
)
from .core.mirrors import (
    DEFAULT_SOURCE_ID,
    get_source,
    loader_allowed_hosts,
    source_by_label,
    source_labels,
)
from .core.models import (
    GameRoot,
    JavaRuntime,
    LaunchOptions,
    LaunchPlan,
    MicrosoftAccount,
    VersionSummary,
)
from .core.modpack import (
    download_and_install_launchable_modpack,
    install_instance,
)
from .core.net import CancelledError, HttpClient, PauseToken, human_size
from .core.paths import (
    ensure_game_root,
    has_isolated_game_data,
    normalize_game_root,
    version_game_dir,
)
from .core.taskqueue import TaskQueue, TaskResult, TaskSnapshot, state_label
from .core.version import (
    list_versions,
    load_merged_version,
)
from .core.version_admin import (
    VersionDetails,
    list_version_details,
    remove_version,
    version_details,
)
from .content_tab import ContentTab

# Palette
BG = "#0b0d10"
CARD = "#151920"
CARD_ALT = "#1a2029"
INPUT = "#0d1117"
BORDER = "#293241"
TEXT = "#e6edf3"
MUTED = "#8b949e"
ACCENT = "#58d36c"
ACCENT_HOVER = "#76e589"
ACCENT_DARK = "#08130a"
RED = "#ff6b6b"
YELLOW = "#f2cc60"
BLUE = "#79c0ff"


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / relative


class LauncherApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.withdraw()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.configure(bg=BG)
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        width = min(1080, max(1000, screen_w - 40))
        height = min(880, max(780, screen_h - 140))
        left = max(0, (screen_w - width) // 2)
        top = max(0, (screen_h - height) // 2 - 40)
        self.geometry(f"{width}x{height}+{left}+{top}")
        self.minsize(1000, 780)

        self._font_family = self._choose_font()
        self._load_config()
        self._ms_account: MicrosoftAccount | None = None
        self._account_warning = ""
        try:
            self._ms_account = load_saved_account()
        except LauncherError as exc:
            self._account_warning = str(exc)
        self._ms_dialog: _MicrosoftLoginDialog | None = None

        self.root_info: GameRoot | None = None
        self.versions: list[VersionSummary] = []
        self.selected_version: VersionSummary | None = None
        self.java_runtimes: list[JavaRuntime] = []
        self.required_java_major: int | None = None
        self._java_runtime: JavaRuntime | None = None
        self._java_manual = bool(self._config.get("java_manual", False))
        self._isolation_manual = bool(self._config.get("isolated_manual", False))
        self._preferred_version = self._config.get("version")
        self._load_generation = 0
        self._java_generation = 0
        self._closing = False
        self._busy = False
        self.process: subprocess.Popen[str] | None = None
        self._active_plan: LaunchPlan | None = None
        self._queue: queue.Queue[tuple[str, tuple]] = queue.Queue()
        try:
            queue_concurrency = max(
                1, min(3, int(self._config.get("queue_concurrency", 1) or 1))
            )
        except (TypeError, ValueError):
            queue_concurrency = 1
        self.task_queue = TaskQueue(
            max_concurrent=queue_concurrency,
            on_change=self._queue_changed,
            log_callback=self._queue_log,
        )
        self._queue_dialog: _DownloadQueueDialog | None = None

        # Downloader state
        self._remote_manifest: Manifest | None = None
        self._remote_versions: list[RemoteVersion] = []
        self._fabric_games: dict[str, bool] = {}
        self._forge_catalog: ForgeCatalog | None = None
        self._loader_builds: list[LoaderBuild] = []
        self._dl_generation = 0
        self._dl_busy = False
        self._dl_cancel: threading.Event | None = None
        self._dl_pause: PauseToken | None = None
        self._dl_operation = ""
        self._dl_expected_version_id = ""
        self._version_dialog: _VersionManagerDialog | None = None
        self._cache_dialog: _CacheDialog | None = None
        self._java_dialog: _JavaRuntimeDialog | None = None
        self._java_warning_key: tuple | None = None

        self._build_variables()
        self._build_styles()
        self._build_ui()
        self._set_icon()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.report_callback_exception = self._on_tk_exception

        self.after(80, self._poll_queue)
        self.after(160, self._initial_load)
        if self._account_warning:
            self.after(
                300,
                lambda: self._append_log(
                    f"无法读取已保存的微软登录：{self._account_warning}",
                    "warning",
                ),
            )
        self.deiconify()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _choose_font(self) -> str:
        available = set(tkfont.families(self))
        for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "Arial"):
            if name in available:
                return name
        return "TkDefaultFont"

    def _config_file(self) -> Path:
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "MinecraftLauncher" / "config.json"
        return Path.home() / ".minecraft_launcher" / "config.json"

    def _load_config(self) -> None:
        self._config: dict = {}
        try:
            with self._config_file().open("r", encoding="utf-8-sig") as file:
                data = json.load(file)
            if isinstance(data, dict):
                self._config = data
        except (OSError, json.JSONDecodeError):
            pass

    def _save_config(self) -> None:
        data = {
            "game_dir": str(self.root_info.root) if self.root_info else self.game_dir_var.get(),
            "version": self.selected_version.version_id if self.selected_version else "",
            "username": self.username_var.get().strip(),
            "java": self.java_var.get().strip(),
            "java_manual": self._java_manual,
            "java_runtime_dir": str(self._config.get("java_runtime_dir", "")),
            "java_component": str(self._config.get("java_component", "")),
            "memory_mb": self.memory_var.get().strip(),
            "isolated": bool(self.isolation_var.get()),
            "isolated_manual": self._isolation_manual,
            "download_source": self.dl_source_var.get(),
            "download_kind": self.dl_kind_var.get(),
            "download_loader": self.dl_loader_var.get(),
            "download_channel": self.dl_channel_var.get(),
            "deep_verify": bool(self.deep_verify_var.get()),
            "threads": self._threads_value(),
            "segments": self._segments_value(),
            "queue_concurrency": (
                self.task_queue.max_concurrent
                if hasattr(self, "task_queue")
                else int(self._config.get("queue_concurrency", 1) or 1)
            ),
            "ms_client_id": str(
                self._config.get("ms_client_id", "")
                or (
                    self.ms_client_id_var.get()
                    if hasattr(self, "ms_client_id_var")
                    else ""
                )
            ).strip(),
            "account_type": "microsoft" if self._ms_account is not None else "offline",
            "curseforge_api_key": str(self._config.get("curseforge_api_key", "")),
            "content_type": self.content_page.type_var.get()
            if hasattr(self, "content_page")
            else "Mod",
            "content_source": self.content_page.source_var.get()
            if hasattr(self, "content_page")
            else "Modrinth",
        }
        try:
            path = self._config_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _build_variables(self) -> None:
        default_user = re.sub(r"[^A-Za-z0-9_]", "", os.environ.get("USERNAME", ""))[:16]
        if not default_user:
            default_user = "Steve"
        if self._ms_account is not None:
            default_user = self._ms_account.name
        self.game_dir_var = tk.StringVar(value=str(self._config.get("game_dir", "")))
        self.version_var = tk.StringVar()
        self.username_var = tk.StringVar(value=str(self._config.get("username", default_user)))
        if self._ms_account is not None:
            self.username_var.set(self._ms_account.name)
        self.account_var = tk.StringVar(value="")
        self.ms_client_id_var = tk.StringVar(
            value=str(self._config.get("ms_client_id", ""))
        )
        self.java_var = tk.StringVar(value=str(self._config.get("java", "")))
        self.memory_var = tk.StringVar(value=str(self._config.get("memory_mb", "2048")))
        self.isolation_var = tk.BooleanVar(value=bool(self._config.get("isolated", True)))
        self.status_var = tk.StringVar(value="等待选择游戏目录")
        self.version_hint_var = tk.StringVar(value="选择游戏目录后自动扫描 versions 文件夹")
        self.java_hint_var = tk.StringVar(value="尚未检测 Java")
        self.game_dir_hint_var = tk.StringVar(value="实际游戏目录：—")
        self.footer_var = tk.StringVar(
            value="本地启动 · 官方/镜像下载 · SHA-1 校验 · 无远程管理"
        )

        source_names = [label for _source_id, label in source_labels()]
        configured_source = str(self._config.get("download_source", ""))
        if configured_source not in source_names:
            configured_source = get_source(DEFAULT_SOURCE_ID).name
        kind_labels = list(KIND_LABELS.values())
        configured_kind = str(self._config.get("download_kind", "正式版"))
        if configured_kind not in kind_labels:
            configured_kind = "正式版"
        loader_labels = ["原版（Vanilla）", "Fabric", "Forge"]
        configured_loader = str(self._config.get("download_loader", loader_labels[0]))
        if configured_loader not in loader_labels:
            configured_loader = loader_labels[0]
        channel_labels = ["稳定 / 推荐", "最新 / 实验", "全部"]
        configured_channel = str(self._config.get("download_channel", channel_labels[0]))
        if configured_channel not in channel_labels:
            configured_channel = channel_labels[0]
        self.dl_source_var = tk.StringVar(value=configured_source)
        self.dl_kind_var = tk.StringVar(value=configured_kind)
        self.dl_loader_var = tk.StringVar(value=configured_loader)
        self.dl_channel_var = tk.StringVar(value=configured_channel)
        self.dl_loader_version_var = tk.StringVar()
        self.dl_filter_var = tk.StringVar()
        self.dl_status_var = tk.StringVar(value="尚未获取远程版本列表")
        self.dl_detail_var = tk.StringVar(value="选择版本后显示下载源与完整性信息")
        self.deep_verify_var = tk.BooleanVar(
            value=bool(self._config.get("deep_verify", True))
        )
        self.dl_source_hint_var = tk.StringVar(value="")
        self.dl_version_hint_var = tk.StringVar(value="")
        self.dl_loader_hint_var = tk.StringVar(value="")
        self.threads_var = tk.StringVar(value=str(self._config.get("threads", 6)))
        self.segments_var = tk.StringVar(value=str(self._config.get("segments", 4)))

    def _build_styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Dark.TCombobox",
            fieldbackground=INPUT,
            background=INPUT,
            foreground=TEXT,
            arrowcolor=MUTED,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            padding=7,
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[("readonly", INPUT), ("disabled", "#11151c")],
            foreground=[("readonly", TEXT), ("disabled", MUTED)],
            selectbackground=[("readonly", INPUT)],
            selectforeground=[("readonly", TEXT)],
        )
        self.option_add("*TCombobox*Listbox.background", INPUT)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", "#23472c")
        self.option_add("*TCombobox*Listbox.selectForeground", TEXT)
        self.option_add("*TCombobox*Listbox.font", (self._font_family, 10))

        style.configure(
            "Dark.TNotebook",
            background=BG,
            borderwidth=0,
            tabmargins=(0, 4, 0, 0),
        )
        style.configure(
            "Dark.TNotebook.Tab",
            background=CARD_ALT,
            foreground=MUTED,
            padding=(20, 9),
            font=(self._font_family, 10, "bold"),
            borderwidth=0,
        )
        style.map(
            "Dark.TNotebook.Tab",
            background=[("selected", CARD), ("active", "#202733")],
            foreground=[("selected", ACCENT), ("active", TEXT)],
        )
        style.configure(
            "Dark.Treeview",
            background=INPUT,
            fieldbackground=INPUT,
            foreground=TEXT,
            borderwidth=0,
            rowheight=25,
            font=(self._font_family, 9),
        )
        style.map(
            "Dark.Treeview",
            background=[("selected", "#23472c")],
            foreground=[("selected", TEXT)],
        )
        style.configure(
            "Dark.Treeview.Heading",
            background=CARD_ALT,
            foreground=MUTED,
            relief="flat",
            borderwidth=0,
            font=(self._font_family, 9, "bold"),
        )
        style.map("Dark.Treeview.Heading", background=[("active", BORDER)])
        style.configure(
            "Accent.Horizontal.TProgressbar",
            troughcolor=INPUT,
            background=ACCENT,
            bordercolor=BORDER,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            thickness=10,
        )

    def _set_icon(self) -> None:
        ico = resource_path("assets/icon.ico")
        if ico.is_file():
            try:
                self.iconbitmap(default=str(ico))
            except tk.TclError:
                pass

    # ------------------------------------------------------------------
    # Widget construction
    # ------------------------------------------------------------------

    def _label(self, parent: tk.Misc, text: str, **kwargs) -> tk.Label:
        options = {
            "bg": CARD,
            "fg": MUTED,
            "font": (self._font_family, 10),
        }
        options.update(kwargs)
        return tk.Label(parent, text=text, **options)

    def _entry(self, parent: tk.Misc, variable: tk.StringVar) -> tk.Entry:
        return tk.Entry(
            parent,
            textvariable=variable,
            bg=INPUT,
            fg=TEXT,
            insertbackground=TEXT,
            disabledbackground="#11151c",
            disabledforeground=MUTED,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            font=(self._font_family, 10),
        )

    def _spinbox(
        self,
        parent: tk.Misc,
        variable: tk.StringVar,
        *,
        from_: int,
        to: int,
        increment: int = 1,
        width: int = 5,
    ) -> tk.Spinbox:
        return tk.Spinbox(
            parent,
            from_=from_,
            to=to,
            increment=increment,
            textvariable=variable,
            width=width,
            bg=INPUT,
            fg=TEXT,
            insertbackground=TEXT,
            buttonbackground=CARD_ALT,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            font=(self._font_family, 10),
        )

    def _button(
        self,
        parent: tk.Misc,
        text: str,
        command,
        *,
        primary: bool = False,
        width: int | None = None,
    ) -> tk.Button:
        bg = ACCENT if primary else CARD_ALT
        fg = ACCENT_DARK if primary else TEXT
        active_bg = ACCENT_HOVER if primary else "#26303d"
        button = tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=active_bg,
            activeforeground=fg,
            disabledforeground="#5c6674",
            relief="flat",
            bd=0,
            padx=14,
            pady=8,
            cursor="hand2",
            font=(self._font_family, 9, "bold" if primary else "normal"),
        )
        if width:
            button.configure(width=width)
        normal = bg

        def on_enter(_event):
            if str(button["state"]) != "disabled":
                button.configure(bg=active_bg)

        def on_leave(_event):
            if str(button["state"]) != "disabled":
                button.configure(bg=normal)

        button.bind("<Enter>", on_enter)
        button.bind("<Leave>", on_leave)
        return button

    def _build_ui(self) -> None:
        main = tk.Frame(self, bg=BG, padx=24, pady=16)
        main.pack(fill="both", expand=True)
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=1)

        self._build_header(main)

        self.notebook = ttk.Notebook(main, style="Dark.TNotebook")
        self.notebook.grid(row=1, column=0, sticky="nsew", pady=(0, 4))

        self.launch_page = tk.Frame(self.notebook, bg=BG, padx=16, pady=12)
        self.download_page = tk.Frame(self.notebook, bg=BG, padx=16, pady=12)
        self.content_page = ContentTab(self.notebook, self)
        self.notebook.add(self.launch_page, text="启动游戏")
        self.notebook.add(self.download_page, text="下载 / 安装")
        self.notebook.add(self.content_page, text="内容 / 资源")

        self._build_launch_page(self.launch_page)
        self._build_download_page(self.download_page)
        self._build_footer(main)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    def _on_tab_changed(self, _event=None) -> None:
        try:
            if self.notebook.index("current") == self.notebook.index(self.content_page):
                self.content_page.on_tab_shown()
        except (tk.TclError, AttributeError):
            pass

    def _build_launch_page(self, parent: tk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(2, weight=1)
        self._build_settings_card(parent)
        self._build_actions(parent)
        self._build_log(parent)

    def _build_header(self, parent: tk.Frame) -> None:
        header = tk.Frame(parent, bg=BG)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        header.grid_columnconfigure(1, weight=1)

        logo = tk.Canvas(header, width=48, height=48, bg=BG, highlightthickness=0)
        logo.grid(row=0, column=0, rowspan=2, padx=(0, 14), sticky="w")
        # A tiny isometric grass block.
        logo.create_polygon(8, 15, 24, 6, 40, 15, 24, 24, fill="#6fbf4a", outline="#8fe36b")
        logo.create_polygon(8, 15, 24, 24, 24, 41, 8, 32, fill="#7a5335", outline="#9a6b43")
        logo.create_polygon(40, 15, 24, 24, 24, 41, 40, 32, fill="#5d3e27", outline="#7d5636")

        title = tk.Frame(header, bg=BG)
        title.grid(row=0, column=1, sticky="w")
        tk.Label(
            title,
            text="MINECRAFT",
            bg=BG,
            fg=ACCENT,
            font=(self._font_family, 9, "bold"),
        ).pack(anchor="w")
        tk.Label(
            title,
            text="启动器",
            bg=BG,
            fg=TEXT,
            font=(self._font_family, 23, "bold"),
        ).pack(anchor="w", pady=(0, 1))
        tk.Label(
            title,
            text="微软/离线登录 · 版本隔离 · 官方/镜像下载",
            bg=BG,
            fg=MUTED,
            font=(self._font_family, 9),
        ).pack(anchor="w")

        badge = tk.Frame(header, bg=CARD_ALT, highlightbackground=BORDER, highlightthickness=1)
        badge.grid(row=0, column=2, sticky="e")
        tk.Label(
            badge,
            text="LOCAL",
            bg=CARD_ALT,
            fg=ACCENT,
            font=(self._font_family, 8, "bold"),
        ).pack(side="left", padx=(10, 6), pady=7)
        tk.Label(
            badge,
            text=f"v{APP_VERSION}",
            bg=CARD_ALT,
            fg=MUTED,
            font=(self._font_family, 8),
        ).pack(side="left", padx=(0, 10), pady=7)

    def _build_settings_card(self, parent: tk.Frame) -> None:
        card = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        card.grid(row=0, column=0, sticky="ew")
        body = tk.Frame(card, bg=CARD, padx=18, pady=14)
        body.pack(fill="x")
        body.grid_columnconfigure(1, weight=1)

        # Game directory
        self._label(body, "游戏目录").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=3)
        self.game_dir_entry = self._entry(body, self.game_dir_var)
        self.game_dir_entry.grid(row=0, column=1, sticky="ew", ipady=7, pady=3)
        self.game_dir_entry.bind("<Return>", lambda _event: self._load_game_dir(self.game_dir_var.get()))
        self.browse_game_button = self._button(
            body, "浏览…", self._browse_game_dir, width=8
        )
        self.browse_game_button.grid(row=0, column=2, sticky="e", padx=(8, 0), pady=3)

        self._label(
            body,
            "选择包含 versions、libraries、assets 的文件夹",
            bg=CARD,
            fg=MUTED,
            font=(self._font_family, 8),
        ).grid(row=1, column=1, columnspan=2, sticky="w", pady=(0, 8))

        # Version
        self._label(body, "游戏版本").grid(row=2, column=0, sticky="w", padx=(0, 12), pady=3)
        self.version_combo = ttk.Combobox(
            body,
            textvariable=self.version_var,
            state="readonly",
            style="Dark.TCombobox",
            font=(self._font_family, 10),
        )
        self.version_combo.grid(row=2, column=1, sticky="ew", pady=3)
        self.version_combo.bind("<<ComboboxSelected>>", lambda _event: self._on_version_change())
        version_buttons = tk.Frame(body, bg=CARD)
        version_buttons.grid(row=2, column=2, sticky="e", padx=(8, 0), pady=3)
        self.refresh_button = self._button(
            version_buttons, "刷新", self._refresh_versions, width=7
        )
        self.refresh_button.pack(side="left")
        self.manage_versions_button = self._button(
            version_buttons, "版本管理…", self._open_version_manager, width=10
        )
        self.manage_versions_button.pack(side="left", padx=(6, 0))

        self.version_hint_label = self._label(
            body,
            "",
            textvariable=self.version_hint_var,
            fg=MUTED,
            font=(self._font_family, 8),
            wraplength=760,
            justify="left",
        )
        self.version_hint_label.grid(row=3, column=1, columnspan=2, sticky="w", pady=(0, 8))

        # Account (offline or Microsoft)
        self._label(body, "账户").grid(row=4, column=0, sticky="w", padx=(0, 12), pady=3)
        account_row = tk.Frame(body, bg=CARD)
        account_row.grid(row=4, column=1, columnspan=2, sticky="ew", pady=3)
        self.account_label = tk.Label(
            account_row,
            textvariable=self.account_var,
            bg=CARD,
            fg=TEXT,
            font=(self._font_family, 9),
            anchor="w",
        )
        self.account_label.pack(side="left")
        self.ms_logout_button = self._button(
            account_row, "退出登录", self._logout_ms, width=9
        )
        self.ms_logout_button.pack(side="right")
        self.ms_login_button = self._button(
            account_row, "微软登录…", self._open_ms_login, width=11
        )
        self.ms_login_button.pack(side="right", padx=(0, 6))

        # Username
        self._label(body, "玩家名").grid(row=5, column=0, sticky="w", padx=(0, 12), pady=3)
        self.username_entry = self._entry(body, self.username_var)
        self.username_entry.grid(row=5, column=1, sticky="ew", ipady=7, pady=3)
        self._label(
            body,
            "离线账户用户名；微软登录后自动使用正版档案",
            font=(self._font_family, 8),
        ).grid(row=5, column=2, sticky="w", padx=(8, 0), pady=3)

        # Java
        self._label(body, "Java").grid(row=6, column=0, sticky="w", padx=(0, 12), pady=3)
        self.java_entry = self._entry(body, self.java_var)
        self.java_entry.grid(row=6, column=1, sticky="ew", ipady=7, pady=3)
        java_buttons = tk.Frame(body, bg=CARD)
        java_buttons.grid(row=6, column=2, sticky="e", padx=(8, 0), pady=3)
        self.browse_java_button = self._button(java_buttons, "浏览…", self._browse_java, width=8)
        self.browse_java_button.pack(side="left")
        self.auto_java_button = self._button(java_buttons, "自动检测", self._auto_java, width=9)
        self.auto_java_button.pack(side="left", padx=(6, 0))
        self.java_manager_button = self._button(
            java_buttons, "获取 Java…", self._open_java_manager, width=11
        )
        self.java_manager_button.pack(side="left", padx=(6, 0))

        self.java_hint_label = self._label(
            body,
            "",
            textvariable=self.java_hint_var,
            fg=MUTED,
            font=(self._font_family, 8),
            wraplength=760,
            justify="left",
        )
        self.java_hint_label.grid(row=7, column=1, columnspan=2, sticky="w", pady=(0, 8))

        # Memory + isolation
        self._label(body, "内存").grid(row=8, column=0, sticky="w", padx=(0, 12), pady=3)
        runtime_row = tk.Frame(body, bg=CARD)
        runtime_row.grid(row=8, column=1, columnspan=2, sticky="ew", pady=3)
        self.memory_spin = tk.Spinbox(
            runtime_row,
            from_=512,
            to=32768,
            increment=512,
            textvariable=self.memory_var,
            width=7,
            bg=INPUT,
            fg=TEXT,
            insertbackground=TEXT,
            buttonbackground=CARD_ALT,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            font=(self._font_family, 10),
        )
        self.memory_spin.pack(side="left", ipady=6)
        tk.Label(
            runtime_row,
            text="MB",
            bg=CARD,
            fg=MUTED,
            font=(self._font_family, 9),
        ).pack(side="left", padx=(7, 20))
        self.isolation_check = tk.Checkbutton(
            runtime_row,
            text="版本隔离（游戏目录使用 versions/<版本>）",
            variable=self.isolation_var,
            command=self._on_isolation_toggle,
            bg=CARD,
            fg=TEXT,
            activebackground=CARD,
            activeforeground=TEXT,
            selectcolor=INPUT,
            font=(self._font_family, 9),
            cursor="hand2",
            bd=0,
            highlightthickness=0,
        )
        self.isolation_check.pack(side="left")

        self.game_dir_hint_label = self._label(
            body,
            "",
            textvariable=self.game_dir_hint_var,
            fg=MUTED,
            font=(self._font_family, 8),
        )
        self.game_dir_hint_label.grid(row=9, column=1, columnspan=2, sticky="w", pady=(2, 0))
        self._apply_account_state()

    def _apply_account_state(self) -> None:
        account = self._ms_account
        if account is not None:
            self.account_var.set(f"微软账户：{account.name}")
            self.username_var.set(account.name)
            self.username_entry.configure(state="disabled")
            self.ms_login_button.configure(state="disabled")
            self.ms_logout_button.configure(state="normal")
        else:
            self.account_var.set("离线账户")
            self.username_entry.configure(state="normal")
            self.ms_login_button.configure(state="normal")
            self.ms_logout_button.configure(state="disabled")

    def _open_ms_login(self) -> None:
        if self._closing:
            return
        if self._ms_dialog is not None:
            try:
                if self._ms_dialog.winfo_exists():
                    self._ms_dialog.deiconify()
                    self._ms_dialog.lift()
                    self._ms_dialog.focus_set()
                    return
            except tk.TclError:
                pass
        initial = resolve_client_id(self.ms_client_id_var.get().strip())
        self._ms_dialog = _MicrosoftLoginDialog(self, initial_client_id=initial)

    def _on_ms_login_success(
        self, account: MicrosoftAccount, client_id: str
    ) -> None:
        self._ms_account = account
        self._config["ms_client_id"] = client_id
        try:
            save_account(account)
        except LauncherError as exc:
            messagebox.showwarning(
                "登录成功，但令牌未保存",
                f"{exc}\n\n本次运行仍可使用该账户，下次启动需要重新登录。",
                parent=self,
            )
        self._apply_account_state()
        self._save_config()
        self._append_log(
            f"微软登录成功：{account.name}（{account.uuid}）。", "success"
        )

    def _logout_ms(self) -> None:
        if self._ms_account is None:
            return
        if not messagebox.askyesno(
            "退出微软登录",
            "退出后将以离线账户启动，并删除本机保存的微软令牌。\n确定退出吗？",
            parent=self,
        ):
            return
        try:
            clear_saved_account()
        except LauncherError as exc:
            messagebox.showerror("无法删除登录令牌", str(exc), parent=self)
            return
        self._ms_account = None
        self._config["account_type"] = "offline"
        self._apply_account_state()
        self._save_config()
        self._append_log("已退出微软登录，使用离线账户。", "muted")

    def _event_ms_account_refreshed(self, account: MicrosoftAccount) -> None:
        self._ms_account = account
        try:
            save_account(account)
        except LauncherError as exc:
            self._append_log(f"微软令牌刷新成功，但保存失败：{exc}", "warning")
        self._apply_account_state()
        self._append_log("微软登录已自动刷新。", "muted")

    def _build_actions(self, parent: tk.Frame) -> None:
        actions = tk.Frame(parent, bg=BG)
        actions.grid(row=1, column=0, sticky="ew", pady=10)
        actions.grid_columnconfigure(0, weight=1)

        status_frame = tk.Frame(actions, bg=BG)
        status_frame.grid(row=0, column=0, sticky="w")
        self.status_dot = tk.Canvas(
            status_frame, width=10, height=10, bg=BG, highlightthickness=0
        )
        self.status_dot.pack(side="left", padx=(1, 8))
        self.status_dot_id = self.status_dot.create_oval(2, 2, 9, 9, fill=MUTED, outline="")
        self.status_label = tk.Label(
            status_frame,
            textvariable=self.status_var,
            bg=BG,
            fg=MUTED,
            font=(self._font_family, 9),
        )
        self.status_label.pack(side="left")

        self.launch_button = self._button(
            actions, "启动游戏", self._launch, primary=True, width=16
        )
        self.launch_button.grid(row=0, column=1, sticky="e")
        self.launch_button.configure(pady=10)

    def _build_log(self, parent: tk.Frame) -> None:
        log_card = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        log_card.grid(row=2, column=0, sticky="nsew")
        log_card.grid_rowconfigure(1, weight=1)
        log_card.grid_columnconfigure(0, weight=1)

        log_header = tk.Frame(log_card, bg=CARD)
        log_header.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 6))
        log_header.grid_columnconfigure(0, weight=1)
        tk.Label(
            log_header,
            text="运行日志",
            bg=CARD,
            fg=TEXT,
            font=(self._font_family, 10, "bold"),
        ).grid(row=0, column=0, sticky="w")
        clear_button = self._button(log_header, "清空", self._clear_log, width=7)
        clear_button.grid(row=0, column=1, sticky="e")

        text_frame = tk.Frame(log_card, bg=INPUT)
        text_frame.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 14))
        text_frame.grid_rowconfigure(0, weight=1)
        text_frame.grid_columnconfigure(0, weight=1)

        self.log_text = tk.Text(
            text_frame,
            bg=INPUT,
            fg="#c9d1d9",
            insertbackground=TEXT,
            selectbackground="#23472c",
            selectforeground=TEXT,
            relief="flat",
            bd=0,
            wrap="word",
            font=("Consolas", 9),
            padx=10,
            pady=8,
            state="disabled",
            height=6,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = tk.Scrollbar(
            text_frame,
            command=self.log_text.yview,
            bg=CARD_ALT,
            troughcolor=INPUT,
            activebackground=BORDER,
            relief="flat",
            bd=0,
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

        self.log_text.tag_configure("info", foreground="#c9d1d9")
        self.log_text.tag_configure("muted", foreground="#6e7681")
        self.log_text.tag_configure("success", foreground=ACCENT)
        self.log_text.tag_configure("warning", foreground=YELLOW)
        self.log_text.tag_configure("error", foreground=RED)
        self.log_text.tag_configure("command", foreground=BLUE)
        self.log_text.tag_configure("game", foreground="#a5b4c4")

    def _build_footer(self, parent: tk.Frame) -> None:
        footer = tk.Frame(parent, bg=BG)
        footer.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        footer.grid_columnconfigure(0, weight=1)
        tk.Label(
            footer,
            textvariable=self.footer_var,
            bg=BG,
            fg="#5c6674",
            font=(self._font_family, 8),
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            footer,
            text=f"{APP_NAME} {APP_VERSION}",
            bg=BG,
            fg="#5c6674",
            font=(self._font_family, 8),
        ).grid(row=0, column=1, sticky="e")

    def _build_download_page(self, parent: tk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)
        parent.grid_rowconfigure(4, weight=1)

        top = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        top.grid(row=0, column=0, sticky="ew")
        body = tk.Frame(top, bg=CARD, padx=14, pady=8)
        body.pack(fill="x")
        for column, weight in ((1, 3), (3, 2), (5, 2)):
            body.grid_columnconfigure(column, weight=weight)

        self._label(body, "游戏目录").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=2)
        self.dl_dir_entry = self._entry(body, self.game_dir_var)
        self.dl_dir_entry.grid(row=0, column=1, columnspan=4, sticky="ew", ipady=5, pady=2)
        self.dl_dir_entry.bind("<Return>", lambda _event: self._dl_set_root())
        self.dl_browse_button = self._button(
            body, "浏览…", self._dl_browse_dir, width=8
        )
        self.dl_browse_button.grid(row=0, column=5, sticky="e", padx=(8, 0), pady=2)

        self._label(body, "下载源").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=2)
        self.dl_source_combo = ttk.Combobox(
            body,
            textvariable=self.dl_source_var,
            values=[label for _id, label in source_labels()],
            state="readonly",
            style="Dark.TCombobox",
            font=(self._font_family, 10),
        )
        self.dl_source_combo.grid(row=1, column=1, sticky="ew", pady=2)
        self.dl_source_combo.bind("<<ComboboxSelected>>", self._dl_on_source_change)
        self.dl_source_hint_label = self._label(
            body,
            "",
            textvariable=self.dl_source_hint_var,
            fg=MUTED,
            font=(self._font_family, 8),
        )
        self.dl_source_hint_label.grid(row=1, column=2, columnspan=4, sticky="w", padx=(10, 0))

        self._label(body, "版本类型").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=2)
        kind_row = tk.Frame(body, bg=CARD)
        kind_row.grid(row=2, column=1, columnspan=5, sticky="ew", pady=2)
        self.dl_kind_combo = ttk.Combobox(
            kind_row,
            textvariable=self.dl_kind_var,
            values=list(KIND_LABELS.values()),
            state="readonly",
            width=10,
            style="Dark.TCombobox",
            font=(self._font_family, 10),
        )
        self.dl_kind_combo.pack(side="left")
        self.dl_kind_combo.bind("<<ComboboxSelected>>", lambda _event: self._dl_refresh_tree())
        self.dl_filter_entry = self._entry(kind_row, self.dl_filter_var)
        self.dl_filter_entry.pack(side="left", fill="x", expand=True, padx=(10, 10), ipady=5)
        self.dl_filter_entry.bind("<KeyRelease>", lambda _event: self._dl_refresh_tree())
        self._label(
            kind_row,
            "搜索版本号",
            font=(self._font_family, 8),
        ).pack(side="left", padx=(0, 10))
        self.dl_refresh_button = self._button(
            kind_row, "刷新远程列表", self._dl_load_remote, width=12
        )
        self.dl_refresh_button.pack(side="right")

        self._label(
            body,
            "仅 HTTPS · 主机允许列表 · SHA-1 校验 · 失败自动回退官方源",
            fg=MUTED,
            font=(self._font_family, 8),
        ).grid(row=3, column=1, columnspan=5, sticky="w", pady=(1, 0))

        # Remote version list
        tree_card = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        tree_card.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        tree_card.grid_rowconfigure(1, weight=1)
        tree_card.grid_columnconfigure(0, weight=1)
        tree_header = tk.Frame(tree_card, bg=CARD)
        tree_header.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 4))
        tree_header.grid_columnconfigure(1, weight=1)
        tk.Label(
            tree_header,
            text="可选版本",
            bg=CARD,
            fg=TEXT,
            font=(self._font_family, 10, "bold"),
        ).grid(row=0, column=0, sticky="w")
        self._label(
            tree_header,
            "",
            textvariable=self.dl_version_hint_var,
            fg=MUTED,
            font=(self._font_family, 8),
        ).grid(row=0, column=1, sticky="e")

        tree_frame = tk.Frame(tree_card, bg=INPUT)
        tree_frame.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 10))
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)
        self.dl_tree = ttk.Treeview(
            tree_frame,
            columns=("version", "type", "date", "state"),
            show="headings",
            height=5,
            selectmode="browse",
            style="Dark.Treeview",
        )
        self.dl_tree.heading("version", text="版本")
        self.dl_tree.heading("type", text="类型")
        self.dl_tree.heading("date", text="发布日期")
        self.dl_tree.heading("state", text="本地状态")
        self.dl_tree.column("version", width=260, anchor="w")
        self.dl_tree.column("type", width=90, anchor="center")
        self.dl_tree.column("date", width=110, anchor="center")
        self.dl_tree.column("state", width=110, anchor="center")
        self.dl_tree.grid(row=0, column=0, sticky="nsew")
        self.dl_tree.bind("<<TreeviewSelect>>", self._dl_on_version_select)
        tree_scroll = tk.Scrollbar(
            tree_frame,
            command=self.dl_tree.yview,
            bg=CARD_ALT,
            troughcolor=INPUT,
            activebackground=BORDER,
            relief="flat",
            bd=0,
        )
        tree_scroll.grid(row=0, column=1, sticky="ns")
        self.dl_tree.configure(yscrollcommand=tree_scroll.set)
        self.dl_tree.tag_configure("installed", foreground=ACCENT)
        self.dl_tree.tag_configure("notinstalled", foreground="#a5b4c4")

        # Loader options
        options = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        options.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        options_body = tk.Frame(options, bg=CARD, padx=14, pady=7)
        options_body.pack(fill="x")
        options_body.grid_columnconfigure(1, weight=1)
        options_body.grid_columnconfigure(3, weight=1)
        options_body.grid_columnconfigure(5, weight=2)

        self._label(options_body, "加载器").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.dl_loader_combo = ttk.Combobox(
            options_body,
            textvariable=self.dl_loader_var,
            values=["原版（Vanilla）", "Fabric", "Forge", "NeoForge", "Quilt"],
            state="readonly",
            width=15,
            style="Dark.TCombobox",
            font=(self._font_family, 10),
        )
        self.dl_loader_combo.grid(row=0, column=1, sticky="ew", padx=(0, 14))
        self.dl_loader_combo.bind("<<ComboboxSelected>>", self._dl_on_loader_change)

        self._label(options_body, "通道").grid(row=0, column=2, sticky="w", padx=(0, 8))
        self.dl_channel_combo = ttk.Combobox(
            options_body,
            textvariable=self.dl_channel_var,
            values=["稳定 / 推荐", "最新 / 实验", "全部"],
            state="disabled",
            width=12,
            style="Dark.TCombobox",
            font=(self._font_family, 10),
        )
        self.dl_channel_combo.grid(row=0, column=3, sticky="ew", padx=(0, 14))
        self.dl_channel_combo.bind("<<ComboboxSelected>>", self._dl_on_channel_change)

        self._label(options_body, "加载器版本").grid(row=0, column=4, sticky="w", padx=(0, 8))
        self.dl_loader_version_combo = ttk.Combobox(
            options_body,
            textvariable=self.dl_loader_version_var,
            state="disabled",
            style="Dark.TCombobox",
            font=(self._font_family, 10),
        )
        self.dl_loader_version_combo.grid(row=0, column=5, sticky="ew")
        self.dl_loader_version_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._dl_update_expected()
        )

        self.dl_loader_hint_label = self._label(
            options_body,
            "",
            textvariable=self.dl_loader_hint_var,
            fg=MUTED,
            font=(self._font_family, 8),
        )
        self.dl_loader_hint_label.grid(
            row=1, column=0, columnspan=5, sticky="w", pady=(6, 0)
        )
        self.dl_deep_check = tk.Checkbutton(
            options_body,
            text="深度校验（SHA-1）",
            variable=self.deep_verify_var,
            bg=CARD,
            fg=TEXT,
            activebackground=CARD,
            activeforeground=TEXT,
            selectcolor=INPUT,
            font=(self._font_family, 9),
            cursor="hand2",
            bd=0,
            highlightthickness=0,
        )
        self.dl_deep_check.grid(row=1, column=5, sticky="e", pady=(6, 0))

        perf_row = tk.Frame(options_body, bg=CARD)
        perf_row.grid(row=2, column=0, columnspan=6, sticky="ew", pady=(6, 0))
        self._label(perf_row, "文件并发").pack(side="left")
        self.threads_spin = self._spinbox(
            perf_row, self.threads_var, from_=1, to=12, width=4
        )
        self.threads_spin.pack(side="left", padx=(6, 14), ipady=3)
        self._label(perf_row, "大文件分段").pack(side="left")
        self.segments_spin = self._spinbox(
            perf_row, self.segments_var, from_=0, to=8, width=4
        )
        self.segments_spin.pack(side="left", padx=(6, 8), ipady=3)
        self._label(
            perf_row,
            "≥8 MB 且服务器支持 Range 时启用；0/1 关闭",
            fg=MUTED,
            font=(self._font_family, 8),
        ).pack(side="left")
        self.cache_button = self._button(
            perf_row, "缓存清理…", self._open_cache_dialog, width=10
        )
        self.cache_button.pack(side="right")
        self.queue_button = self._button(
            perf_row, "下载队列…", self._open_queue_dialog, width=11
        )
        self.queue_button.pack(side="right", padx=(0, 8))
        self.enqueue_button = self._button(
            perf_row, "加入队列", self._dl_enqueue_install, width=9
        )
        self.enqueue_button.pack(side="right", padx=(0, 8))

        # Actions
        actions = tk.Frame(parent, bg=BG)
        actions.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        actions.grid_columnconfigure(0, weight=1)
        self.dl_status_label = tk.Label(
            actions,
            textvariable=self.dl_status_var,
            bg=BG,
            fg=MUTED,
            font=(self._font_family, 9),
            anchor="w",
        )
        self.dl_status_label.grid(row=0, column=0, sticky="ew")
        self.dl_pause_button = self._button(
            actions, "暂停", self._dl_toggle_pause, width=8
        )
        self.dl_pause_button.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.dl_pause_button.configure(state="disabled")
        self.dl_cancel_button = self._button(
            actions, "取消", self._dl_cancel_operation, width=8
        )
        self.dl_cancel_button.grid(row=0, column=2, sticky="e", padx=(8, 0))
        self.dl_cancel_button.configure(state="disabled")
        self.dl_verify_button = self._button(
            actions, "检查完整性", self._dl_verify, width=11
        )
        self.dl_verify_button.grid(row=0, column=3, sticky="e", padx=(8, 0))
        self.dl_install_button = self._button(
            actions, "下载 / 补全", self._dl_install, primary=True, width=13
        )
        self.dl_install_button.grid(row=0, column=4, sticky="e", padx=(8, 0))
        self.dl_progress = ttk.Progressbar(
            actions,
            style="Accent.Horizontal.TProgressbar",
            maximum=1000,
            value=0,
        )
        self.dl_progress.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(8, 0))

        # Download log
        log_card = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        log_card.grid(row=4, column=0, sticky="nsew", pady=(10, 0))
        log_card.grid_rowconfigure(1, weight=1)
        log_card.grid_columnconfigure(0, weight=1)
        log_header = tk.Frame(log_card, bg=CARD)
        log_header.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 4))
        log_header.grid_columnconfigure(0, weight=1)
        tk.Label(
            log_header,
            text="下载 / 安装日志",
            bg=CARD,
            fg=TEXT,
            font=(self._font_family, 10, "bold"),
        ).grid(row=0, column=0, sticky="w")
        dl_clear_button = self._button(log_header, "清空", self._clear_dl_log, width=7)
        dl_clear_button.grid(row=0, column=1, sticky="e")

        dl_text_frame = tk.Frame(log_card, bg=INPUT)
        dl_text_frame.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 10))
        dl_text_frame.grid_rowconfigure(0, weight=1)
        dl_text_frame.grid_columnconfigure(0, weight=1)
        self.dl_log_text = tk.Text(
            dl_text_frame,
            bg=INPUT,
            fg="#c9d1d9",
            insertbackground=TEXT,
            selectbackground="#23472c",
            selectforeground=TEXT,
            relief="flat",
            bd=0,
            wrap="word",
            font=("Consolas", 9),
            padx=10,
            pady=8,
            state="disabled",
            height=4,
        )
        self.dl_log_text.grid(row=0, column=0, sticky="nsew")
        dl_scroll = tk.Scrollbar(
            dl_text_frame,
            command=self.dl_log_text.yview,
            bg=CARD_ALT,
            troughcolor=INPUT,
            activebackground=BORDER,
            relief="flat",
            bd=0,
        )
        dl_scroll.grid(row=0, column=1, sticky="ns")
        self.dl_log_text.configure(yscrollcommand=dl_scroll.set)
        for tag, color in (
            ("info", "#c9d1d9"),
            ("muted", "#6e7681"),
            ("success", ACCENT),
            ("warning", YELLOW),
            ("error", RED),
            ("command", BLUE),
            ("game", "#a5b4c4"),
        ):
            self.dl_log_text.tag_configure(tag, foreground=color)

        self._update_dl_source_hint()

    # ------------------------------------------------------------------
    # Downloader helpers
    # ------------------------------------------------------------------

    def _dl_kind_key(self) -> str:
        label = self.dl_kind_var.get()
        for key, value in KIND_LABELS.items():
            if value == label:
                return key
        return "release"

    def _dl_loader_key(self) -> str:
        label = self.dl_loader_var.get()
        if label.startswith("Fabric"):
            return "fabric"
        if label.startswith("Forge"):
            return "forge"
        if label.startswith("NeoForge"):
            return "neoforge"
        if label.startswith("Quilt"):
            return "quilt"
        return "vanilla"

    def _dl_channel_key(self) -> str:
        label = self.dl_channel_var.get()
        if label.startswith("最新"):
            return "latest"
        if label.startswith("全部"):
            return "all"
        return "stable"

    def _threads_value(self) -> int:
        try:
            value = int(str(self.threads_var.get()).strip())
        except (TypeError, ValueError):
            value = 6
        return max(1, min(12, value))

    def _segments_value(self) -> int:
        try:
            value = int(str(self.segments_var.get()).strip())
        except (TypeError, ValueError):
            value = 4
        return max(0, min(16, value))

    def _open_version_manager(self) -> None:
        if self.root_info is None:
            messagebox.showwarning(
                "还没有选择目录",
                "请先选择游戏目录并等待版本扫描完成。",
                parent=self,
            )
            return
        if self._dl_busy:
            messagebox.showwarning(
                "下载 / 安装正在进行",
                "请等待当前下载或安装结束后再管理版本，"
                "避免删除正在写入的版本目录。",
                parent=self,
            )
            return
        if self._version_dialog is not None and self._version_dialog.winfo_exists():
            self._version_dialog.lift()
            self._version_dialog.focus_set()
            self._version_dialog.reload()
            return
        self._version_dialog = _VersionManagerDialog(self)

    def _open_cache_dialog(self) -> None:
        if self.process is not None or self._busy or self._dl_busy:
            messagebox.showwarning(
                "暂时不能清理缓存",
                "游戏正在运行、启动器正在启动游戏，或下载 / 安装仍在进行。\n"
                "请等待这些操作结束后再清理缓存，避免删除正在使用的临时文件。",
                parent=self,
            )
            return
        if self._cache_dialog is not None and self._cache_dialog.winfo_exists():
            self._cache_dialog.lift()
            self._cache_dialog.focus_set()
            self._cache_dialog.reload()
            return
        self._cache_dialog = _CacheDialog(self)

    def _update_dl_source_hint(self) -> None:
        if not hasattr(self, "dl_source_combo"):
            return
        source = source_by_label(self.dl_source_var.get())
        self.dl_source_hint_var.set(f"当前源：{source.description}")
        self.dl_source_combo.configure(values=[label for _id, label in source_labels()])

    def _dl_set_root(self, root: GameRoot | None = None) -> GameRoot | None:
        if root is None:
            text = self.game_dir_var.get().strip().strip('"')
            if not text:
                messagebox.showwarning(
                    "还没有选择目录",
                    "请选择要安装 Minecraft 的目录（可以是空文件夹）。",
                    parent=self,
                )
                return None
            try:
                root = ensure_game_root(text)
            except LauncherError as exc:
                messagebox.showerror("目录不可用", str(exc), parent=self)
                return None
        self.root_info = root
        self.game_dir_var.set(str(root.root))
        return root

    def _dl_browse_dir(self) -> None:
        initial = self.game_dir_var.get().strip()
        if not initial or not Path(initial).exists():
            initial = str(Path.home())
        selected = filedialog.askdirectory(
            title="选择 Minecraft 安装目录（可为空文件夹）",
            initialdir=initial,
            mustexist=True,
        )
        if not selected:
            return
        try:
            root = ensure_game_root(selected)
        except LauncherError as exc:
            messagebox.showerror("目录不可用", str(exc), parent=self)
            return
        root = self._dl_set_root(root)
        if root is not None:
            self._append_log(f"下载目录：{root.root}", "info")
            self._dl_refresh_tree()

    def _dl_on_source_change(self, _event=None) -> None:
        self._update_dl_source_hint()
        self._dl_load_remote()

    def _dl_load_remote(self) -> None:
        root = self._dl_set_root()
        if root is None:
            return
        if self._dl_busy:
            return
        source = source_by_label(self.dl_source_var.get())
        self._dl_generation += 1
        generation = self._dl_generation
        self._dl_set_busy(True, "正在获取远程版本列表…")
        self._append_dl_log(f"下载源：{source.name}", "info")
        self._append_dl_log(f"安装目录：{root.root}", "muted")

        def worker() -> None:
            try:
                client = HttpClient(
                    source.allowed_hosts,
                    strict_redirects=source.strict_redirects,
                    timeout=10.0,
                    retries=1,
                    logger=lambda message: self._post("dl_log", message, "muted"),
                )
                manifest = load_manifest(
                    source,
                    client=client,
                    force=True,
                    logger=lambda message: self._post("dl_log", message, "muted"),
                )
                self._post("dl_manifest", generation, manifest)
            except Exception as exc:  # noqa: BLE001
                self._post("dl_error", generation, str(exc))

        threading.Thread(target=worker, daemon=True, name="remote-manifest").start()

    def _event_dl_manifest(self, generation: int, manifest: Manifest) -> None:
        if generation != self._dl_generation or self._closing:
            return
        self._remote_manifest = manifest
        self._remote_versions = manifest.versions
        self._dl_set_busy(False, "远程版本列表已加载")
        self._append_dl_log(
            f"获取到 {len(manifest.versions)} 个版本（最新正式版 "
            f"{manifest.latest_release or '-'}，最新快照 {manifest.latest_snapshot or '-'}）。",
            "success",
        )
        if manifest.from_cache:
            self._append_dl_log("当前使用本地缓存的版本清单。", "warning")
        self._dl_refresh_tree(select_default=True)
        if self._dl_selected_version_id() is None:
            self._dl_on_loader_change()

    def _dl_refresh_tree(self, select_default: bool = False) -> None:
        if not hasattr(self, "dl_tree"):
            return
        previous = self._dl_selected_version_id()
        self.dl_tree.delete(*self.dl_tree.get_children())
        kind = self._dl_kind_key()
        versions = filter_versions(self._remote_versions, kind)
        needle = self.dl_filter_var.get().strip().lower()
        if needle:
            versions = [item for item in versions if needle in item.id.lower()]
        self.dl_version_hint_var.set(
            f"{len(versions)} 个版本 · {KIND_LABELS.get(kind, kind)}"
        )
        installed_dir = self.root_info.versions_dir if self.root_info else None
        for version in versions:
            installed = bool(
                installed_dir
                and (installed_dir / version.id / f"{version.id}.json").is_file()
            )
            self.dl_tree.insert(
                "",
                "end",
                iid=version.id,
                values=(
                    version.id,
                    version.type_label,
                    version.date_text,
                    "已安装" if installed else "—",
                ),
                tags=("installed" if installed else "notinstalled",),
            )
        chosen = previous
        if chosen is None or not self.dl_tree.exists(chosen):
            chosen = None
            if select_default:
                preferred = (
                    self._remote_manifest.latest_release
                    if self._remote_manifest
                    else ""
                )
                if preferred and self.dl_tree.exists(preferred):
                    chosen = preferred
            if chosen is None and versions:
                chosen = versions[0].id
        if chosen is not None and self.dl_tree.exists(chosen):
            self.dl_tree.selection_set(chosen)
            self.dl_tree.focus(chosen)
            self.dl_tree.see(chosen)
            self._dl_on_version_select()
        elif not versions:
            self.dl_detail_var.set("没有匹配的版本")
            self.dl_status_var.set("没有匹配的版本")
            self._dl_clear_loader_builds()

    def _dl_selected_version_id(self) -> str | None:
        if not hasattr(self, "dl_tree"):
            return None
        selection = self.dl_tree.selection()
        return selection[0] if selection else None

    def _dl_selected_remote(self) -> RemoteVersion | None:
        version_id = self._dl_selected_version_id()
        if version_id is None:
            return None
        return next(
            (item for item in self._remote_versions if item.id == version_id), None
        )

    def _dl_on_version_select(self, _event=None) -> None:
        remote = self._dl_selected_remote()
        if remote is None:
            return
        self._dl_expected_version_id = remote.id
        installed = (
            self.root_info is not None
            and (self.root_info.versions_dir / remote.id / f"{remote.id}.json").is_file()
        )
        self.dl_detail_var.set(
            f"{remote.id} · {remote.type_label} · {remote.date_text} · "
            f"{'已安装' if installed else '未安装'}"
        )
        self._dl_on_loader_change()

    def _dl_clear_loader_builds(self) -> None:
        self._loader_builds = []
        self.dl_loader_version_combo.configure(values=[], state="disabled")
        self.dl_loader_version_var.set("")
        self.dl_loader_hint_var.set("")

    def _dl_on_loader_change(self, _event=None) -> None:
        loader = self._dl_loader_key()
        if loader == "vanilla":
            self._dl_clear_loader_builds()
            self.dl_channel_combo.configure(state="disabled")
            self.dl_loader_hint_var.set("安装/补全所选原版游戏文件。")
            self._dl_update_expected()
            return
        self.dl_channel_combo.configure(state="readonly")
        self._dl_load_loader_versions(loader)

    def _dl_on_channel_change(self, _event=None) -> None:
        self._dl_populate_loader_versions()

    def _dl_load_loader_versions(self, loader: str) -> None:
        remote = self._dl_selected_remote()
        if remote is None or self.root_info is None:
            return
        source = source_by_label(self.dl_source_var.get())
        self._dl_generation += 1
        generation = self._dl_generation
        game_version = remote.id
        self.dl_loader_hint_var.set(f"正在获取 {loader} 版本列表…")
        self.dl_loader_version_combo.configure(values=[], state="disabled")
        self.dl_loader_version_var.set("")

        def worker() -> None:
            try:
                client = HttpClient(
                    source.allowed_hosts,
                    strict_redirects=source.strict_redirects,
                    timeout=15.0,
                    retries=1,
                    logger=lambda message: self._post("dl_log", message, "muted"),
                )
                if loader == "fabric":
                    builds = fetch_fabric_loaders(
                        source,
                        game_version,
                        client=client,
                        logger=lambda message, level="muted": self._post(
                            "dl_log", message, level
                        ),
                    )
                elif loader == "quilt":
                    builds = fetch_quilt_loaders(
                        source,
                        game_version,
                        client=client,
                        logger=lambda message, level="muted": self._post(
                            "dl_log", message, level
                        ),
                    )
                elif loader == "neoforge":
                    builds = fetch_neoforge_builds(
                        source,
                        game_version,
                        client=client,
                        logger=lambda message, level="muted": self._post(
                            "dl_log", message, level
                        ),
                    )
                else:
                    catalog = load_forge_catalog(
                        source,
                        client=client,
                        logger=lambda message, level="muted": self._post(
                            "dl_log", message, level
                        ),
                    )
                    builds = forge_builds(catalog, game_version, "all")
                self._post("dl_builds", generation, loader, game_version, builds)
            except Exception as exc:  # noqa: BLE001
                self._post("dl_loader_error", generation, str(exc))

        threading.Thread(target=worker, daemon=True, name="loader-versions").start()

    def _event_dl_builds(
        self,
        generation: int,
        loader: str,
        game_version: str,
        builds: list[LoaderBuild],
    ) -> None:
        if generation != self._dl_generation or self._closing:
            return
        if self._dl_loader_key() != loader:
            return
        self._loader_builds = builds
        self._dl_populate_loader_versions()

    def _event_dl_loader_error(self, generation: int, message: str) -> None:
        if generation != self._dl_generation or self._closing:
            return
        self._dl_clear_loader_builds()
        self.dl_loader_hint_var.set(f"获取加载器版本失败：{message}")
        self._append_dl_log(message, "error")

    def _dl_populate_loader_versions(self) -> None:
        loader = self._dl_loader_key()
        if loader == "vanilla":
            return
        channel = self._dl_channel_key()
        builds = self._loader_builds
        # All loaders expose stable/latest/all through the same helper.  Forge
        # still uses "recommended" as its stable marker.
        filtered = select_loader_builds(builds, channel)
        builds = filtered or builds[:1]
        labels = [item.label for item in builds]
        self.dl_loader_version_combo.configure(
            values=labels,
            state="readonly" if labels else "disabled",
        )
        if labels:
            self.dl_loader_version_var.set(labels[0])
            self.dl_loader_hint_var.set(
                f"{len(builds)} 个 {loader.title()} 版本可用于 "
                f"{self._dl_selected_remote().id if self._dl_selected_remote() else ''}"
            )
        else:
            self.dl_loader_version_var.set("")
            self.dl_loader_hint_var.set("该游戏版本没有匹配的加载器版本。")
        self._dl_update_expected()

    def _dl_selected_build(self) -> LoaderBuild | None:
        label = self.dl_loader_version_var.get()
        if not label:
            return None
        return next((item for item in self._loader_builds if item.label == label), None)

    def _dl_update_expected(self) -> None:
        loader = self._dl_loader_key()
        remote = self._dl_selected_remote()
        if remote is None:
            return
        if loader == "vanilla":
            self._dl_expected_version_id = remote.id
        else:
            build = self._dl_selected_build()
            if build is None:
                self._dl_expected_version_id = ""
            else:
                self._dl_expected_version_id = build.version_id
        self.dl_loader_hint_var.set(
            "目标版本目录："
            + (
                f"versions/{self._dl_expected_version_id}"
                if self._dl_expected_version_id
                else "—"
            )
        )

    def _dl_verify(self) -> None:
        if self._dl_busy:
            return
        remote = self._dl_selected_remote()
        root = self._dl_set_root()
        if remote is None or root is None:
            messagebox.showwarning("还没有选择版本", "请先选择要检查的游戏版本。", parent=self)
            return
        source = source_by_label(self.dl_source_var.get())
        loader = self._dl_loader_key()
        build = self._dl_selected_build()
        self._dl_operation = "verify"
        self._dl_expected_version_id = (
            build.version_id if build is not None else remote.id
        )
        self._dl_set_busy(True, "正在检查文件完整性…")
        self._append_dl_log(
            f"检查完整性：{self._dl_expected_version_id}（"
            f"{'深度 SHA-1' if self.deep_verify_var.get() else '快速大小检查'}）",
            "info",
        )

        def worker() -> None:
            try:
                client = HttpClient(
                    source.allowed_hosts,
                    strict_redirects=source.strict_redirects,
                    timeout=15.0,
                    retries=1,
                    logger=lambda message: self._post("dl_log", message, "muted"),
                )
                try:
                    if loader == "fabric" and build is not None:
                        detail = selected_game_detail(
                            source, remote.id, client=client
                        )
                        plan = build_fabric_plan(
                            root,
                            source,
                            detail,
                            build,
                            client=client,
                            logger=lambda message, level="info": self._post(
                                "dl_log", message, level
                            ),
                        )
                    elif loader == "vanilla":
                        detail = selected_game_detail(
                            source, remote.id, client=client
                        )
                        plan = build_plan_from_detail(
                            root,
                            source,
                            detail,
                            client=client,
                            logger=lambda message, level="info": self._post(
                                "dl_log", message, level
                            ),
                        )
                    else:
                        plan = build_local_plan(
                            root, self._dl_expected_version_id, source
                        )
                except Exception as remote_exc:  # noqa: BLE001
                    installed = (
                        root.versions_dir
                        / self._dl_expected_version_id
                        / f"{self._dl_expected_version_id}.json"
                    ).is_file()
                    if not installed:
                        raise
                    self._post(
                        "dl_log",
                        f"远程元数据不可用，改用本地已安装文件检查：{remote_exc}",
                        "warning",
                    )
                    plan = build_local_plan(
                        root, self._dl_expected_version_id, source
                    )
                report = verify_plan(
                    plan,
                    deep=bool(self.deep_verify_var.get()),
                    progress=lambda progress: self._post("dl_progress", progress),
                )
                self._post("dl_verified", plan, report)
            except Exception as exc:  # noqa: BLE001
                self._post("dl_error", 0, str(exc))

        threading.Thread(target=worker, daemon=True, name="verify-worker").start()

    def _dl_install(self) -> None:
        if self._dl_busy:
            return
        remote = self._dl_selected_remote()
        root = self._dl_set_root()
        if remote is None or root is None:
            messagebox.showwarning("还没有选择版本", "请先选择要安装的游戏版本。", parent=self)
            return
        loader = self._dl_loader_key()
        build = self._dl_selected_build()
        if loader != "vanilla" and build is None:
            messagebox.showwarning(
                "还没有选择加载器版本",
                "请选择 Fabric/Forge 版本，或等待版本列表加载完成。",
                parent=self,
            )
            return
        if loader in ("forge", "neoforge"):
            loader_name = "Forge" if loader == "forge" else "NeoForge"
            proceed = messagebox.askyesno(
                f"{loader_name} 安装确认",
                f"{loader_name} 安装需要运行官方 {loader_name} 安装器，"
                "这会执行安装器内的官方代码。\n\n"
                "启动器会先通过所选下载源预取依赖并校验安装器；"
                "NeoForge 镜像可能没有官方 SHA-1，此时会做结构校验并在日志中提示。\n\n"
                "是否继续？",
                parent=self,
            )
            if not proceed:
                return
        source = source_by_label(self.dl_source_var.get())
        self._dl_operation = "install"
        self._dl_expected_version_id = build.version_id if build is not None else remote.id
        self._dl_cancel = threading.Event()
        self._dl_pause = PauseToken()
        self._dl_set_busy(True, "正在准备下载…")
        self.dl_progress.configure(value=0)
        self._append_dl_log(
            f"开始安装：Minecraft {remote.id} / {loader}"
            + (f" / {build.version}" if build is not None else ""),
            "info",
        )
        self._append_dl_log(f"下载源：{source.name} · 目录：{root.root}", "muted")
        self._append_dl_log(
            f"文件并发：{self._threads_value()} · 大文件分段：{self._segments_value()}"
            "（分段仅用于 ≥8 MB 且支持 Range 的文件）",
            "muted",
        )
        java_text = self.java_var.get().strip()
        java_path = Path(java_text) if java_text else None
        cancel = self._dl_cancel
        pause = self._dl_pause
        threads = self._threads_value()
        segments = self._segments_value()

        def worker() -> None:
            try:
                client = HttpClient(
                    loader_allowed_hosts(source),
                    strict_redirects=False,
                    timeout=20.0,
                    retries=1,
                    segments=segments,
                    logger=lambda message: self._post("dl_log", message, "muted"),
                )
                outcome = install_selected(
                    root,
                    source,
                    remote.id,
                    loader=loader,
                    loader_version=build.version if build is not None else None,
                    loader_channel=self._dl_channel_key(),
                    java_path=java_path,
                    client=client,
                    logger=lambda message, level="info": self._post(
                        "dl_log", message, level
                    ),
                    progress=lambda progress: self._post("dl_progress", progress),
                    cancel=cancel,
                    pause=pause,
                    deep=bool(self.deep_verify_var.get()),
                    workers=threads,
                )
                self._post("dl_installed", outcome)
            except CancelledError:
                self._post("dl_cancelled")
            except Exception as exc:  # noqa: BLE001
                self._post("dl_error", 0, str(exc))

        threading.Thread(target=worker, daemon=True, name="install-worker").start()

    def _dl_cancel_operation(self) -> None:
        if self._dl_cancel is not None:
            self._dl_cancel.set()
            self.dl_status_var.set("正在取消…")
            self.dl_cancel_button.configure(state="disabled")
            self.dl_pause_button.configure(state="disabled")

    def _dl_toggle_pause(self) -> None:
        if self._dl_pause is None:
            return
        paused = self._dl_pause.toggle()
        self.dl_pause_button.configure(text="继续" if paused else "暂停")
        if paused:
            self.dl_status_var.set("已暂停；点击“继续”恢复下载")
            self._append_dl_log("下载已暂停，未完成分片会保留。", "warning")
        else:
            self.dl_status_var.set("正在继续下载…")
            self._append_dl_log("继续下载。", "info")

    def _dl_set_busy(self, busy: bool, text: str | None = None) -> None:
        self._dl_busy = busy
        state = "disabled" if busy else "normal"
        for widget in (
            self.dl_browse_button,
            self.dl_refresh_button,
            self.dl_verify_button,
            self.dl_install_button,
            self.enqueue_button,
            self.cache_button,
            self.threads_spin,
            self.segments_spin,
            self.manage_versions_button,
        ):
            widget.configure(state=state)
        self.dl_source_combo.configure(state="disabled" if busy else "readonly")
        self.dl_kind_combo.configure(state="disabled" if busy else "readonly")
        self.dl_loader_combo.configure(state="disabled" if busy else "readonly")
        if busy:
            self.dl_channel_combo.configure(state="disabled")
            self.dl_loader_version_combo.configure(state="disabled")
            self.dl_cancel_button.configure(
                state="normal" if self._dl_operation == "install" else "disabled"
            )
            self.dl_pause_button.configure(
                state="normal" if self._dl_operation == "install" else "disabled"
            )
        else:
            self._dl_cancel = None
            self._dl_pause = None
            self.dl_cancel_button.configure(state="disabled")
            self.dl_pause_button.configure(state="disabled", text="暂停")
            self._dl_on_loader_change_safe()
        if text:
            self.dl_status_var.set(text)
            self.dl_status_label.configure(fg=MUTED if not busy else BLUE)
        if hasattr(self, "launch_button"):
            if busy:
                self.launch_button.configure(state="disabled")
            elif not self._busy and self.process is None:
                self.launch_button.configure(state="normal")

    def _dl_on_loader_change_safe(self) -> None:
        """Restore loader controls after an operation without network I/O."""
        if self._dl_loader_key() != "vanilla":
            self.dl_channel_combo.configure(state="readonly")
            if self._loader_builds:
                self.dl_loader_version_combo.configure(state="readonly")
        else:
            self.dl_channel_combo.configure(state="disabled")

    def _event_dl_progress(self, progress: Progress) -> None:
        if self._closing:
            return
        current = progress.current or progress.label
        if progress.phase.startswith("verify"):
            self.dl_status_var.set(
                f"检查文件 {progress.done_files}/{progress.total_files} · "
                f"{current[:60]}"
            )
        elif progress.phase == "download":
            if progress.total_bytes:
                percent = min(100.0, progress.done_bytes * 100.0 / progress.total_bytes)
                self.dl_progress.configure(
                    value=(progress.done_bytes * 1000 // progress.total_bytes) or 0
                )
                self.dl_status_var.set(
                    f"下载 {percent:.1f}% · {progress.done_files}/{progress.total_files} · "
                    f"{progress.label} · {current[:56]}"
                )
            else:
                self.dl_status_var.set(
                    f"下载 {progress.done_files}/{progress.total_files} · {current[:56]}"
                )
        else:
            self.dl_status_var.set(
                f"{progress.phase} {progress.done_files}/{progress.total_files} · "
                f"{current[:50]}"
            )

    def _event_dl_verified(self, plan, report) -> None:
        if self._closing:
            return
        self._dl_set_busy(False, "检查完成")
        self.dl_progress.configure(value=1000 if report.healthy else 0)
        self._append_dl_log(format_verify_report(report), "success" if report.healthy else "warning")
        if report.healthy:
            self.dl_status_var.set("文件完整，无需补全")
            self.dl_status_label.configure(fg=ACCENT)
            messagebox.showinfo(
                "完整性检查",
                f"版本 {plan.version_id} 的文件完整。\n\n"
                f"检查 {report.total} 个文件，健康 {report.ok} 个。",
                parent=self,
            )
        else:
            self.dl_status_var.set(
                f"缺少/损坏 {len(report.problematic)} 个文件，可点击“下载 / 补全”"
            )
            self.dl_status_label.configure(fg=YELLOW)
            messagebox.showwarning(
                "发现缺失或损坏",
                f"版本 {plan.version_id} 需要补全。\n\n"
                f"缺失 {len(report.missing)} 个，损坏 {len(report.corrupt)} 个。\n"
                "点击“下载 / 补全”可自动修复。",
                parent=self,
            )

    def _event_dl_installed(self, outcome) -> None:
        if self._closing:
            return
        self._dl_set_busy(False, "安装完成")
        self.dl_progress.configure(value=1000)
        self.dl_status_label.configure(fg=ACCENT)
        self._append_dl_log(format_verify_report(outcome.verify), "success" if outcome.healthy else "error")
        if outcome.warnings:
            for warning in outcome.warnings:
                self._append_dl_log(warning, "muted")
        version_id = outcome.plan.version_id
        self._append_dl_log(
            f"安装结束：{version_id}，下载 {outcome.downloaded_files} 个文件，"
            f"{human_size(outcome.downloaded_bytes)}；"
            f"跳过 {outcome.skipped_files} 个已完整文件。",
            "success" if outcome.healthy else "warning",
        )
        if outcome.healthy:
            messagebox.showinfo(
                "安装完成",
                f"版本 {version_id} 已安装并通过校验。\n\n"
                f"下载 {outcome.downloaded_files} 个文件，"
                f"{human_size(outcome.downloaded_bytes)}。",
                parent=self,
            )
            self._preferred_version = version_id
            self._load_game_dir(str(self.root_info.root))
            self._dl_refresh_tree()
        else:
            messagebox.showwarning(
                "安装后仍有异常",
                f"版本 {version_id} 安装后仍有文件未通过校验。\n"
                "请查看下载日志，必要时重试。",
                parent=self,
            )

    def _event_dl_cancelled(self) -> None:
        if self._closing:
            return
        self._dl_set_busy(False, "已取消")
        self.dl_status_label.configure(fg=YELLOW)
        self._append_dl_log(
            "操作已取消；本次下载的文件和未完成分片已删除。", "warning"
        )

    def _event_dl_error(self, generation: int, message: str) -> None:
        if self._closing:
            return
        if generation and generation != self._dl_generation:
            return
        self._dl_set_busy(False, "操作失败")
        self.dl_status_label.configure(fg=RED)
        self._append_dl_log(message, "error")
        self._append_log(message, "error")
        self._show_error_dialog("下载 / 安装失败", message)

    def _event_dl_log(self, message: str, level: str) -> None:
        if self._closing:
            return
        self._append_dl_log(message, level if level in ("info", "muted", "success", "warning", "error", "command", "game") else "info")

    def _append_dl_log(self, message: str, tag: str = "info") -> None:
        if not hasattr(self, "dl_log_text"):
            return
        clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", str(message)).rstrip("\r\n")
        if not clean:
            return
        timestamp = time.strftime("%H:%M:%S")
        self.dl_log_text.configure(state="normal")
        self.dl_log_text.insert("end", f"[{timestamp}] ", "muted")
        self.dl_log_text.insert("end", clean + "\n", tag)
        line_count = int(self.dl_log_text.index("end-1c").split(".")[0])
        if line_count > 3000:
            self.dl_log_text.delete("1.0", "600.0")
        self.dl_log_text.configure(state="disabled")
        self.dl_log_text.see("end")

    def _clear_dl_log(self) -> None:
        if not hasattr(self, "dl_log_text"):
            return
        self.dl_log_text.configure(state="normal")
        self.dl_log_text.delete("1.0", "end")
        self.dl_log_text.configure(state="disabled")

    # ------------------------------------------------------------------
    # Queue / log helpers
    # ------------------------------------------------------------------

    def _post(self, event: str, *payload) -> None:
        self._queue.put((event, payload))

    def _poll_queue(self) -> None:
        if self._closing:
            return
        try:
            while True:
                event, payload = self._queue.get_nowait()
                handler = getattr(self, f"_event_{event}", None)
                if handler is not None:
                    try:
                        handler(*payload)
                    except Exception as exc:  # keep the UI alive
                        self._append_log(f"界面事件处理失败：{exc}", "error")
        except queue.Empty:
            pass
        self.after(80, self._poll_queue)

    # ------------------------------------------------------------------
    # Download queue (0.6.0)
    # ------------------------------------------------------------------

    def _queue_changed(self) -> None:
        """Called from queue worker threads; marshal to the Tk thread."""
        self._post("queue_changed")

    def _queue_log(self, message: str, level: str = "info") -> None:
        self._post("dl_log", message, level)

    def _event_queue_changed(self) -> None:
        if self._closing:
            return
        if self._queue_dialog is not None:
            try:
                if self._queue_dialog.winfo_exists():
                    self._queue_dialog.refresh()
                else:
                    self._queue_dialog = None
            except tk.TclError:
                self._queue_dialog = None

    def _open_queue_dialog(self) -> None:
        if self._closing:
            return
        if self._queue_dialog is not None:
            try:
                if self._queue_dialog.winfo_exists():
                    self._queue_dialog.deiconify()
                    self._queue_dialog.lift()
                    self._queue_dialog.focus_set()
                    self._queue_dialog.refresh()
                    return
            except tk.TclError:
                pass
        self._queue_dialog = _DownloadQueueDialog(self)

    def _on_queue_dialog_closed(self) -> None:
        self._queue_dialog = None

    def _dl_enqueue_install(self) -> None:
        if self._closing or self._dl_busy:
            return
        remote = self._dl_selected_remote()
        root = self._dl_set_root()
        if remote is None or root is None:
            messagebox.showwarning("还没有选择版本", "请先选择要安装的游戏版本。", parent=self)
            return
        loader = self._dl_loader_key()
        build = self._dl_selected_build()
        if loader != "vanilla" and build is None:
            messagebox.showwarning(
                "还没有选择加载器版本",
                "请选择加载器版本，或等待版本列表加载完成。",
                parent=self,
            )
            return
        source = source_by_label(self.dl_source_var.get())
        java_text = self.java_var.get().strip()
        java_path = Path(java_text) if java_text else None
        threads = self._threads_value()
        segments = self._segments_value()
        deep = bool(self.deep_verify_var.get())
        channel = self._dl_channel_key()
        loader_version = build.version if build is not None else None
        title = f"安装 {remote.id}"
        if build is not None:
            title += f" / {build.label}"
        detail = str(root.root)

        def runner(context):
            client = HttpClient(
                loader_allowed_hosts(source),
                strict_redirects=False,
                timeout=20.0,
                retries=1,
                segments=segments,
                logger=lambda message: context.log(message, "muted"),
            )
            outcome = install_selected(
                root,
                source,
                remote.id,
                loader=loader,
                loader_version=loader_version,
                loader_channel=channel,
                java_path=java_path,
                client=client,
                logger=lambda message, level="info": context.log(message, level),
                progress=lambda progress: context.update(
                    done_bytes=progress.done_bytes,
                    total_bytes=progress.total_bytes,
                    files_done=progress.done_files,
                    files_total=progress.total_files,
                    message=progress.current or progress.label,
                ),
                cancel=context.cancel,
                pause=context.pause,
                deep=deep,
                workers=threads,
            )
            healthy = bool(outcome.healthy)
            self._post("queue_install_done", outcome.plan.version_id, healthy)
            prefix = "安装完成" if healthy else "安装结束但校验异常"
            return TaskResult(
                message=f"{prefix}：{outcome.plan.version_id}",
                detail=f"{outcome.downloaded_files} 个文件 / "
                f"{human_size(outcome.downloaded_bytes)}",
            )

        self.task_queue.add("install", title, detail, runner)
        self._append_dl_log(f"已加入下载队列：{title}", "info")
        self.dl_status_var.set("已加入下载队列；点击“下载队列…”查看进度")

    def enqueue_content_install(
        self,
        plan,
        *,
        client=None,
        title: str = "",
        detail: str = "",
    ) -> None:
        """Queue a single content/mod download prepared by the content tab."""
        if self._closing:
            return
        task_title = title or f"下载 {plan.filename}"
        task_detail = detail or str(plan.dest)

        def runner(context):
            outcome = install_content(
                plan,
                client=client,
                progress=lambda written: context.update(
                    done_bytes=int(written),
                    total_bytes=int(plan.size),
                    message=plan.filename,
                ),
                cancel=context.cancel,
                pause=context.pause,
                confirmed=True,
                logger=lambda message: context.log(message, "muted"),
            )
            self._post(
                "queue_content_done",
                plan.project.id,
                str(plan.dest),
                outcome.message,
            )
            return TaskResult(message=outcome.message, detail=str(plan.dest))

        self.task_queue.add("content", task_title, task_detail, runner)
        self._append_dl_log(f"已加入下载队列：{task_title}", "info")

    def enqueue_modpack_install(
        self,
        project,
        version,
        file,
        game_root,
        *,
        source,
        client,
        java_path=None,
        deep: bool = True,
        workers: int = 6,
    ) -> None:
        if self._closing:
            return
        task_title = f"安装整合包 {project.title} / {version.version_number}"

        def runner(context):
            outcome = download_and_install_launchable_modpack(
                project,
                version,
                file,
                game_root,
                slug=project.slug,
                source=source,
                client=client,
                java_path=java_path,
                deep=deep,
                workers=workers,
                progress=lambda progress: context.update(
                    done_bytes=int(progress.downloaded),
                    total_bytes=int(progress.total),
                    message=str(progress.current or progress.phase),
                ),
                cancel=context.cancel,
                pause=context.pause,
                logger=lambda message: context.log(message, "muted"),
            )
            self._post(
                "queue_modpack_done",
                str(outcome.version_id or ""),
                bool(outcome.launchable),
                outcome.message,
            )
            return TaskResult(message=outcome.message, detail=str(outcome.instance_dir))

        self.task_queue.add("modpack", task_title, str(game_root), runner)
        self._append_dl_log(f"已加入下载队列：{task_title}", "info")

    def enqueue_archive_install(
        self,
        plan,
        *,
        source,
        client,
        java_path=None,
        deep: bool = True,
        workers: int = 6,
    ) -> None:
        if self._closing:
            return
        task_title = f"安装本地整合包 {plan.index.name} {plan.index.version_id}"

        def runner(context):
            outcome = install_instance(
                plan,
                confirmed=True,
                source=source,
                client=client,
                java_path=java_path,
                deep=deep,
                workers=workers,
                progress=lambda progress: context.update(
                    done_bytes=int(progress.downloaded),
                    total_bytes=int(progress.total),
                    message=str(progress.current or progress.phase),
                ),
                cancel=context.cancel,
                pause=context.pause,
                logger=lambda message: context.log(message, "muted"),
            )
            self._post(
                "queue_modpack_done",
                str(outcome.version_id or ""),
                bool(outcome.launchable),
                outcome.message,
            )
            return TaskResult(message=outcome.message, detail=str(outcome.instance_dir))

        self.task_queue.add("modpack", task_title, str(plan.game_dir), runner)
        self._append_dl_log(f"已加入下载队列：{task_title}", "info")

    def _event_queue_install_done(self, version_id: str, healthy: bool) -> None:
        if self._closing:
            return
        tag = "success" if healthy else "warning"
        self._append_dl_log(
            f"队列安装完成：{version_id}" + ("" if healthy else "（仍有文件未通过校验）"),
            tag,
        )
        if healthy and self.root_info is not None:
            self._preferred_version = version_id
            self._load_game_dir(str(self.root_info.root))
            self._dl_refresh_tree()

    def _event_queue_content_done(
        self, project_id: str, dest: str, message: str
    ) -> None:
        if self._closing:
            return
        self._append_dl_log(f"队列内容下载完成：{message}", "success")
        try:
            self.content_page.refresh_context()
        except Exception:  # noqa: BLE001 - the tab may be closing
            pass

    def _event_queue_modpack_done(
        self, version_id: str, launchable: bool, message: str
    ) -> None:
        if self._closing:
            return
        self._append_dl_log(
            f"队列整合包安装完成：{message}",
            "success" if launchable else "warning",
        )
        if self.root_info is not None:
            if version_id:
                self._preferred_version = version_id
            self._load_game_dir(str(self.root_info.root))
            self._dl_refresh_tree()


    def _append_log(self, message: str, tag: str = "info") -> None:
        if not hasattr(self, "log_text"):
            return
        clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", str(message)).rstrip("\r\n")
        if not clean:
            return
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] ", "muted")
        self.log_text.insert("end", clean + "\n", tag)
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 5000:
            self.log_text.delete("1.0", "1000.0")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _set_status(self, text: str, color: str = MUTED) -> None:
        self.status_var.set(text)
        self.status_label.configure(fg=color)
        self.status_dot.itemconfigure(self.status_dot_id, fill=color)

    def _set_busy(self, busy: bool, text: str | None = None) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.launch_button.configure(state=state)
        self.browse_game_button.configure(state=state)
        self.refresh_button.configure(state=state)
        self.manage_versions_button.configure(state=state)
        self.version_combo.configure(state="disabled" if busy else "readonly")
        self.browse_java_button.configure(state=state)
        self.auto_java_button.configure(state=state)
        self.java_manager_button.configure(state=state)
        if hasattr(self, "dl_install_button"):
            self.dl_install_button.configure(
                state="disabled" if busy or self._dl_busy else "normal"
            )
            if hasattr(self, "dl_verify_button"):
                self.dl_verify_button.configure(
                    state="disabled" if busy or self._dl_busy else "normal"
                )
        if hasattr(self, "cache_button"):
            self.cache_button.configure(
                state="disabled" if busy or self._dl_busy else "normal"
            )
        if busy:
            self.launch_button.configure(bg="#243027", text="启动中…")
        else:
            self.launch_button.configure(bg=ACCENT, text="启动游戏")
        if text:
            self._set_status(text)

    # ------------------------------------------------------------------
    # Configuration / directory loading
    # ------------------------------------------------------------------

    def _initial_load(self) -> None:
        if self.root_info is not None:
            return
        configured = str(self._config.get("game_dir", "")).strip()
        candidate = Path(configured) if configured else None
        if candidate is None or not candidate.exists():
            default = Path(os.environ.get("APPDATA", "")) / ".minecraft"
            if default.is_dir():
                candidate = default
        if candidate is not None and candidate.exists():
            self.game_dir_var.set(str(candidate))
            self._load_game_dir(candidate)
        else:
            self._set_status("等待选择游戏目录")
            self._append_log("启动器已就绪。", "success")
            self._append_log(
                "请选择包含 versions、libraries、assets 的游戏目录；"
                "支持版本隔离目录。",
                "muted",
            )
            if self._preferred_version:
                self._preferred_version = None

    def _browse_game_dir(self) -> None:
        initial = self.game_dir_var.get().strip()
        if not initial:
            initial = str(Path.home())
        selected = filedialog.askdirectory(
            title="选择 Minecraft 游戏目录",
            initialdir=initial,
            mustexist=True,
        )
        if selected:
            self.game_dir_var.set(selected)
            self._load_game_dir(selected)

    def _load_game_dir(self, path_text: str | Path) -> None:
        if self._busy or self.process is not None:
            return
        path_text = str(path_text).strip().strip('"')
        if not path_text:
            return
        try:
            root = normalize_game_root(path_text)
        except LauncherError as exc:
            self._set_status("游戏目录无效", RED)
            self._append_log(str(exc), "error")
            messagebox.showerror("游戏目录无效", str(exc), parent=self)
            return

        self.root_info = root
        self.game_dir_var.set(str(root.root))
        if self.selected_version is not None and not self._preferred_version:
            self._preferred_version = self.selected_version.version_id
        self.versions = []
        self.selected_version = None
        self.version_combo.configure(values=[])
        self.version_var.set("")
        self._load_generation += 1
        generation = self._load_generation
        self._java_generation += 1
        self._set_busy(True, "正在扫描版本…")
        self._append_log(f"游戏目录：{root.root}", "info")
        if not root.assets_dir.is_dir():
            self._append_log("警告：未找到 assets 文件夹，启动时可能缺少资源。", "warning")

        def worker() -> None:
            try:
                versions = list_versions(root)
                self._post("versions_loaded", generation, root, versions)
                runtimes = discover_java(root.root)
                self._post("java_loaded", generation, root, runtimes)
            except Exception as exc:
                self._post("load_error", generation, str(exc))

        threading.Thread(target=worker, daemon=True, name="version-scan").start()

    def _refresh_versions(self) -> None:
        if self.root_info is not None:
            self._load_game_dir(str(self.root_info.root))
        else:
            self._load_game_dir(self.game_dir_var.get())

    def _event_load_error(self, generation: int, message: str) -> None:
        if generation != self._load_generation or self._closing:
            return
        self._set_busy(False, "加载失败")
        self._append_log(message, "error")
        messagebox.showerror("加载游戏目录失败", message, parent=self)

    def _event_versions_loaded(
        self, generation: int, root: GameRoot, versions: list[VersionSummary]
    ) -> None:
        if generation != self._load_generation or self._closing:
            return
        self.versions = versions
        labels = [version.label for version in versions]
        self.version_combo.configure(values=labels)
        if not versions:
            self._set_busy(False, "未找到版本")
            self._append_log("versions 文件夹中没有可用版本。", "error")
            messagebox.showerror(
                "未找到版本",
                "没有扫描到可启动的版本。\n"
                "请确认 versions/<版本>/<版本>.json 和客户端 jar 存在。",
                parent=self,
            )
            return

        chosen: VersionSummary | None = None
        if self._preferred_version:
            chosen = next(
                (
                    item
                    for item in versions
                    if item.version_id == self._preferred_version
                    or item.folder_name == self._preferred_version
                ),
                None,
            )
            self._preferred_version = None
        if chosen is None:
            chosen = next(
                (
                    item
                    for item in versions
                    if item.version_type == "release" and item.jar_path is not None
                ),
                None,
            )
        if chosen is None:
            chosen = next(
                (item for item in versions if item.version_type == "release"), versions[0]
            )
        self.version_var.set(chosen.label)
        self.selected_version = chosen
        self._on_version_change()
        self._set_busy(False, "就绪")
        self._append_log(f"发现 {len(versions)} 个版本。", "success")
        self._append_log(f"已选择：{chosen.version_id}", "info")

    def _event_java_loaded(
        self, generation: int, root: GameRoot, runtimes: list[JavaRuntime]
    ) -> None:
        if generation != self._load_generation or self._closing:
            return
        self.java_runtimes = runtimes
        if not runtimes:
            self._append_log(
                "未检测到 Java。点击“获取 Java…”可选择下载、查看手动下载网址，"
                "或手动选择 java.exe。",
                "warning",
            )
            self.java_hint_var.set(
                "未检测到 Java；点击“获取 Java…”下载、查看官方下载网址或手动指定"
            )
            self.java_hint_label.configure(fg=YELLOW)
            return
        self._append_log(
            "检测到 Java：" + "，".join(f"Java {item.major}" for item in runtimes),
            "muted",
        )
        if not self._java_manual:
            self._select_java_for_current_version()

    # ------------------------------------------------------------------
    # Version / Java selection
    # ------------------------------------------------------------------

    def _on_version_change(self) -> None:
        if not self.versions:
            return
        label = self.version_var.get()
        summary = next((item for item in self.versions if item.label == label), None)
        if summary is None:
            return
        self.selected_version = summary

        size_text = "未知"
        loader = "原版"
        isolated_text = "否"
        modified_text = "未知"
        if self.root_info is not None:
            try:
                details = version_details(self.root_info, summary)
                size_text = human_size(details.size_bytes)
                loader = details.loader
                isolated_text = details.isolation_text
                modified_text = details.modified_text
            except Exception:  # noqa: BLE001 - the hint must never block selection
                pass
        jar_state = "" if summary.jar_path else "  ·  客户端 jar 继承自父版本"
        self.version_hint_var.set(
            f"ID：{summary.version_id}  ·  类型：{summary.version_type or '未知'}  ·  "
            f"加载器：{loader}  ·  占用：{size_text}  ·  修改：{modified_text}  ·  "
            f"隔离：{isolated_text}{jar_state}"
        )
        self.version_hint_label.configure(fg=MUTED)

        detected = has_isolated_game_data(summary)
        if not self._isolation_manual:
            self.isolation_var.set(detected)
        self._update_actual_game_dir()
        # Always resolve the version's required Java so the hint is accurate.
        # The selection worker will not overwrite a manually chosen java.exe.
        self._select_java_for_current_version()
        if self._java_manual:
            self._update_java_hint()

    def _on_isolation_toggle(self) -> None:
        self._isolation_manual = True
        self._update_actual_game_dir()

    def _update_actual_game_dir(self) -> None:
        if self.root_info is None or self.selected_version is None:
            self.game_dir_hint_var.set("实际游戏目录：—")
            return
        if self.isolation_var.get():
            game_dir = version_game_dir(self.selected_version)
            suffix = "（版本隔离）"
        else:
            game_dir = self.root_info.root
            suffix = "（游戏根目录）"
        self.game_dir_hint_var.set(f"实际游戏目录：{game_dir}  {suffix}")

    def _select_java_for_current_version(self) -> None:
        if self.root_info is None or self.selected_version is None:
            return
        self._java_generation += 1
        generation = self._java_generation
        root = self.root_info
        summary = self.selected_version
        runtimes = list(self.java_runtimes)

        def worker() -> None:
            try:
                loaded = load_merged_version(root, summary)
                required = required_java_major(loaded.data)
                chosen = pick_java(runtimes, required, root.root)
                self._post("java_selected", generation, summary.folder, required, chosen)
            except Exception:
                # Java selection is best-effort; launch will report the real error.
                self._post("java_selected", generation, summary.folder, None, None)

        threading.Thread(target=worker, daemon=True, name="java-select").start()

    def _event_java_selected(
        self,
        generation: int,
        version_folder: Path,
        required: int | None,
        runtime: JavaRuntime | None,
    ) -> None:
        if generation != self._java_generation or self._closing:
            return
        if self.selected_version is None or self.selected_version.folder != version_folder:
            return
        self.required_java_major = required
        if runtime is not None and not self._java_manual:
            self._java_runtime = runtime
            self.java_var.set(str(runtime.path))
        self._update_java_hint()

        mismatched = required is not None and (
            runtime is None or runtime.major < required
        )
        if mismatched:
            key = (
                str(version_folder),
                required,
                runtime.major if runtime is not None else None,
            )
            if key != self._java_warning_key:
                self._java_warning_key = key
                detail = (
                    "未检测到可用 Java"
                    if runtime is None
                    else f"当前 Java {runtime.major} 低于要求的 Java {required}"
                )
                self._append_log(
                    f"此版本需要 Java {required}，{detail}。点击“获取 Java…”可下载"
                    "匹配运行时、选择安装位置、查看手动下载网址，或手动指定 java.exe。",
                    "warning",
                )
        else:
            self._java_warning_key = None

    def _update_java_hint(self) -> None:
        path_text = self.java_var.get().strip()
        if not path_text:
            if self.required_java_major is not None:
                self.java_hint_var.set(
                    f"未选择 Java；此版本需要 Java {self.required_java_major}。"
                    "点击“获取 Java…”下载或查看手动下载网址"
                )
            else:
                self.java_hint_var.set(
                    "未选择 Java，请自动检测、手动浏览，或点击“获取 Java…”"
                )
            self.java_hint_label.configure(fg=YELLOW)
            return

        runtime = self._java_runtime
        if runtime is None or str(runtime.path) != path_text:
            runtime = probe_java(path_text)
            if runtime is not None:
                self._java_runtime = runtime

        if runtime is None:
            self.java_hint_var.set(
                "无法运行该 Java，请检查路径；也可点击“获取 Java…”重新选择"
            )
            self.java_hint_label.configure(fg=RED)
            return

        if self.required_java_major is None:
            self.java_hint_var.set(f"Java {runtime.version} · 未检测到版本要求")
            self.java_hint_label.configure(fg=MUTED)
            return

        if runtime.major == self.required_java_major:
            self.java_hint_var.set(
                f"Java {runtime.version} · 与版本要求（Java {self.required_java_major}）匹配"
            )
            self.java_hint_label.configure(fg=ACCENT)
        elif runtime.major < self.required_java_major:
            self.java_hint_var.set(
                f"Java {runtime.major} 过低，此版本建议 Java {self.required_java_major}；"
                "点击“获取 Java…”下载匹配运行时或查看手动下载网址"
            )
            self.java_hint_label.configure(fg=RED)
        else:
            self.java_hint_var.set(
                f"Java {runtime.major} 高于建议的 Java {self.required_java_major}，"
                "可能仍可运行；也可点击“获取 Java…”安装匹配版本"
            )
            self.java_hint_label.configure(fg=YELLOW)

    def _browse_java(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择 java.exe",
            filetypes=[("Java executable", "java.exe"), ("All files", "*.*")],
        )
        if not selected:
            return
        self._java_manual = True
        self.java_var.set(selected)
        self._java_runtime = None
        self._update_java_hint()
        self._append_log(f"已选择 Java：{selected}", "muted")

        def worker() -> None:
            runtime = probe_java(selected)
            self._post("java_probed", selected, runtime)

        threading.Thread(target=worker, daemon=True, name="java-probe").start()

    def _event_java_probed(self, path_text: str, runtime: JavaRuntime | None) -> None:
        if self._closing or self.java_var.get().strip() != path_text:
            return
        if runtime is None:
            self._java_runtime = None
            self._append_log("无法运行所选的 Java。", "error")
        else:
            self._java_runtime = runtime
            self._append_log(f"Java 可用：{runtime.version}", "success")
        self._update_java_hint()

    def _auto_java(self) -> None:
        if not self.java_runtimes:
            answer = messagebox.askyesno(
                "没有可用 Java",
                "没有自动检测到 Java。\n\n"
                "是否打开“Java 运行时管理”？\n"
                "你可以在那里选择安装位置、确认后再下载，"
                "或查看可自行下载的官方网址。",
                parent=self,
            )
            if answer:
                self._open_java_manager()
            return
        self._java_manual = False
        self._java_runtime = None
        self._select_java_for_current_version()
        self._append_log("正在自动选择匹配的 Java。", "muted")

    def _open_java_manager(self) -> None:
        if self._java_dialog is not None:
            try:
                self._java_dialog.deiconify()
                self._java_dialog.lift()
                self._java_dialog.focus_force()
            except tk.TclError:
                self._java_dialog = None
            return
        if self._busy or self.process is not None:
            messagebox.showinfo(
                "暂时无法打开",
                "游戏正在运行或启动器正在处理其他任务，请稍后再打开 Java 管理。",
                parent=self,
            )
            return
        self._java_dialog = _JavaRuntimeDialog(self)

    def _event_java_dialog_event(self, kind: str, *payload) -> None:
        dialog = self._java_dialog
        if dialog is None or self._closing:
            return
        try:
            dialog.handle_event(kind, *payload)
        except tk.TclError:
            pass
        except Exception as exc:  # keep the main window alive
            self._append_log(f"Java 管理界面事件失败：{exc}", "error")

    def _remind_missing_java(self, reason: str) -> None:
        required = self.required_java_major
        need = f"此版本建议 Java {required}" if required else "未检测到 Java 版本要求"
        answer = messagebox.askyesnocancel(
            "缺少可用的 Java",
            f"{reason}\n\n{need}，当前无法直接启动。\n\n"
            "是：打开“Java 运行时管理”（可选择安装位置、确认后下载，"
            "或查看手动下载网址）\n"
            "否：手动选择已有的 java.exe\n"
            "取消：返回启动页",
            parent=self,
        )
        if answer is True:
            self._open_java_manager()
        elif answer is False:
            self._browse_java()

    # ------------------------------------------------------------------
    # Launch
    # ------------------------------------------------------------------

    def _launch(self) -> None:
        if self._busy or self.process is not None:
            return
        if self.root_info is None or self.selected_version is None:
            messagebox.showwarning(
                "还没有选择版本",
                "请先选择游戏目录，并等待版本扫描完成。",
                parent=self,
            )
            return

        # The directory entry is editable.  If the user pasted a new path and
        # clicked Launch without pressing Enter, load it first instead of
        # silently launching from the previous root.
        try:
            entry_root = normalize_game_root(self.game_dir_var.get())
        except LauncherError as exc:
            messagebox.showerror("游戏目录无效", str(exc), parent=self)
            return
        if entry_root.root != self.root_info.root:
            self._load_game_dir(entry_root.root)
            messagebox.showinfo(
                "正在加载新目录",
                "检测到游戏目录已修改，正在重新扫描版本。\n"
                "扫描完成后请再次点击“启动游戏”。",
                parent=self,
            )
            return

        account = self._ms_account
        try:
            username = validate_username(
                account.name if account is not None else self.username_var.get()
            )
        except LauncherError as exc:
            messagebox.showerror("玩家名无效", str(exc), parent=self)
            return
        try:
            memory = int(self.memory_var.get())
            if not 512 <= memory <= 65536:
                raise ValueError
        except ValueError:
            messagebox.showerror("内存设置无效", "内存需为 512 到 65536 之间的整数 MB。", parent=self)
            return

        java_text = self.java_var.get().strip()
        if not java_text:
            self._remind_missing_java("还没有选择 Java。")
            return
        java_path = Path(java_text)
        if not java_path.is_file():
            self._remind_missing_java(f"找不到 Java：\n{java_path}")
            return
        runtime = (
            self._java_runtime
            if self._java_runtime is not None
            and str(self._java_runtime.path) == java_text
            else probe_java(java_path)
        )
        if runtime is None:
            self._remind_missing_java(f"无法运行所选 Java：\n{java_path}")
            return
        if (
            self.required_java_major is not None
            and runtime.major < self.required_java_major
        ):
            answer = messagebox.askyesnocancel(
                "Java 版本过低",
                f"此版本建议 Java {self.required_java_major}，"
                f"当前选择的是 Java {runtime.major}，直接启动很可能失败。\n\n"
                "是：打开“Java 运行时管理”，下载或选择匹配的 Java\n"
                "否：仍然用当前 Java 尝试启动\n"
                "取消：返回启动页",
                parent=self,
            )
            if answer is None:
                return
            if answer is True:
                self._open_java_manager()
                return
            self._append_log(
                f"用户选择继续使用 Java {runtime.major}（建议 Java "
                f"{self.required_java_major}）。",
                "warning",
            )
        elif (
            self.required_java_major is not None
            and runtime.major > self.required_java_major
        ):
            self._append_log(
                f"提示：Java {runtime.major} 高于建议的 Java "
                f"{self.required_java_major}，若启动失败请改用匹配版本。",
                "warning",
            )

        self._save_config()
        self._set_busy(True, "正在准备启动…")
        account_text = (
            f"微软账户：{username}" if account is not None else f"玩家：{username}"
        )
        self._append_log(
            f"准备启动 {self.selected_version.version_id}"
            f"（{account_text}，内存：{memory} MB）",
            "info",
        )
        options = LaunchOptions(
            username=username,
            java_path=java_path,
            memory_mb=memory,
            isolated=bool(self.isolation_var.get()),
            account=account,
        )
        root = self.root_info
        summary = self.selected_version

        def worker() -> None:
            plan: LaunchPlan | None = None
            launch_options = options
            try:
                runtime = probe_java(java_path)
                if runtime is None:
                    raise LauncherError(f"无法运行 Java：{java_path}")
                self._post("java_runtime", runtime)
                if self.required_java_major is not None and runtime.major != self.required_java_major:
                    self._post(
                        "log",
                        f"警告：当前 Java {runtime.major} 与建议的 Java "
                        f"{self.required_java_major} 不一致，若启动失败请更换 Java。",
                        "warning",
                    )
                if launch_options.account is not None:
                    self._post("log", "正在检查微软登录状态…", "muted")
                    fresh = ensure_account_fresh(
                        launch_options.account, HttpAuthTransport()
                    )
                    if fresh != launch_options.account:
                        launch_options = replace(launch_options, account=fresh)
                        self._post("ms_account_refreshed", fresh)
                if self._closing:
                    return
                plan = build_launch_plan(root, summary, launch_options)
                self._post("plan", plan)
                if self._closing:
                    cleanup_plan(plan)
                    return
                process = start_game(plan)
                self._post("started", plan, process)
                if process.stdout is not None:
                    for line in process.stdout:
                        self._post("game_log", line)
                code = process.wait()
                self._post("game_exit", code, plan)
            except Exception as exc:
                if plan is not None:
                    cleanup_plan(plan)
                self._post("launch_error", str(exc))
            finally:
                self._post("worker_done")

        threading.Thread(target=worker, daemon=True, name="launch-worker").start()

    def _event_java_runtime(self, runtime: JavaRuntime) -> None:
        self._java_runtime = runtime

    def _event_plan(self, plan: LaunchPlan) -> None:
        self._append_log(f"游戏目录：{plan.game_dir}", "muted")
        self._append_log(f"资源目录：{plan.assets_dir}", "muted")
        command_line = plan.command_line
        # Never print a Microsoft access token, even though the command line
        # contains it by design.
        command_line = re.sub(
            r"(--accessToken(?:=|\s+))(\S+)", r"\1<redacted>", command_line
        )
        if len(command_line) > 5000:
            command_line = command_line[:5000] + " …"
        self._append_log("启动命令：" + command_line, "command")
        for warning in plan.warnings:
            self._append_log(warning, "warning")

    def _event_started(
        self, plan: LaunchPlan, process: subprocess.Popen[str]
    ) -> None:
        self.process = process
        self._active_plan = plan
        self._set_busy(True, "游戏运行中")
        self.launch_button.configure(text="游戏运行中", state="disabled")
        self._append_log("游戏进程已启动。", "success")

    def _event_game_log(self, line: str) -> None:
        text = line.rstrip("\r\n")
        if not text:
            return
        tag = "warning" if re.search(r"\bWARN\b|警告", text) else "game"
        if re.search(r"\bERROR\b|Exception|错误", text):
            tag = "error"
        self._append_log(text, tag)

    def _event_game_exit(self, code: int, plan: LaunchPlan) -> None:
        cleanup_plan(plan)
        self.process = None
        self._active_plan = None
        self._set_busy(False)
        if code == 0:
            self._set_status("游戏已正常退出", ACCENT)
            self._append_log("游戏进程已退出（代码 0）。", "success")
        else:
            self._set_status(f"游戏异常退出（代码 {code}）", RED)
            self._append_log(f"游戏进程已退出，退出代码：{code}。", "error")

    def _event_launch_error(self, message: str) -> None:
        self.process = None
        self._active_plan = None
        self._set_busy(False, "启动失败")
        self._append_log(message, "error")
        if not self._closing:
            self._show_error_dialog("启动失败", message)

    def _show_error_dialog(self, title: str, message: str) -> None:
        """Show a copyable, scrollable error dialog.

        Windows message boxes can grow wider than the screen for long paths
        and permission errors.  A small Toplevel keeps the message readable.
        """
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.configure(bg=BG)
        dialog.geometry("760x430")
        dialog.minsize(560, 280)
        dialog.transient(self)
        dialog.grab_set()

        header = tk.Frame(dialog, bg=BG, padx=18, pady=12)
        header.pack(fill="x")
        tk.Label(
            header,
            text=title,
            bg=BG,
            fg=RED,
            font=(self._font_family, 13, "bold"),
        ).pack(side="left")

        text_frame = tk.Frame(dialog, bg=INPUT)
        text_frame.pack(fill="both", expand=True, padx=18)
        text_frame.grid_rowconfigure(0, weight=1)
        text_frame.grid_columnconfigure(0, weight=1)
        details = tk.Text(
            text_frame,
            bg=INPUT,
            fg="#c9d1d9",
            insertbackground=TEXT,
            selectbackground="#23472c",
            selectforeground=TEXT,
            relief="flat",
            bd=0,
            wrap="char",
            font=("Consolas", 9),
            padx=12,
            pady=10,
            height=10,
        )
        details.grid(row=0, column=0, sticky="nsew")
        scrollbar = tk.Scrollbar(
            text_frame,
            command=details.yview,
            bg=CARD_ALT,
            troughcolor=INPUT,
            activebackground=BORDER,
            relief="flat",
            bd=0,
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        details.configure(yscrollcommand=scrollbar.set)
        details.insert("1.0", message)
        details.configure(state="disabled")

        buttons = tk.Frame(dialog, bg=BG, padx=18, pady=12)
        buttons.pack(fill="x")

        def copy_message() -> None:
            self.clipboard_clear()
            self.clipboard_append(message)

        def close_dialog() -> None:
            try:
                dialog.grab_release()
            except tk.TclError:
                pass
            dialog.destroy()

        copy_button = self._button(buttons, "复制错误信息", copy_message)
        copy_button.pack(side="left")
        close_button = self._button(buttons, "关闭", close_dialog, primary=True)
        close_button.pack(side="right")

        dialog.update_idletasks()
        width, height = 760, 430
        x = self.winfo_rootx() + max(0, (self.winfo_width() - width) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")

        dialog.protocol("WM_DELETE_WINDOW", close_dialog)
        dialog.wait_window()

    def _event_worker_done(self) -> None:
        if self._closing:
            return
        if self.process is None and not self._busy:
            self._set_busy(False)

    # ------------------------------------------------------------------
    # Shutdown / errors
    # ------------------------------------------------------------------

    def _on_close(self) -> None:
        if self.process is not None:
            if not messagebox.askyesno(
                "游戏正在运行",
                "游戏仍在运行。退出启动器不会关闭游戏，确定退出吗？",
                parent=self,
            ):
                return
        elif self._busy:
            if not messagebox.askyesno(
                "启动器正在工作",
                "启动器正在准备或启动游戏，确定退出吗？",
                parent=self,
            ):
                return
        elif self._dl_busy:
            if not messagebox.askyesno(
                "下载 / 安装正在进行",
                "下载或安装仍在进行。退出会中断操作，确定退出吗？",
                parent=self,
            ):
                return
            if self._dl_cancel is not None:
                self._dl_cancel.set()
            # Give the worker a moment to terminate child Java processes
            # (especially the Forge installer) before the interpreter exits.
            deadline = time.monotonic() + 5.0
            while self._dl_busy and time.monotonic() < deadline:
                try:
                    self.update()
                except tk.TclError:
                    break
                time.sleep(0.1)
        elif any(
            item.state in ("queued", "running", "paused", "cancelling")
            for item in self.task_queue.snapshots()
        ):
            if not messagebox.askyesno(
                "下载队列仍在工作",
                "下载队列中还有任务在等待或执行。退出会中断它们，确定退出吗？",
                parent=self,
            ):
                return
            self.task_queue.stop(timeout=5.0)
        elif self.content_page.busy:
            if not messagebox.askyesno(
                "内容下载正在进行",
                "内容下载或安装仍在进行。退出会中断操作，确定退出吗？",
                parent=self,
            ):
                return
        self.task_queue.stop(timeout=3.0)
        self.content_page.shutdown()
        self._save_config()
        self._closing = True
        self.destroy()

    def _on_tk_exception(self, exc_type, exc_value, exc_traceback) -> None:
        self._append_log(f"界面异常：{exc_type.__name__}: {exc_value}", "error")


class _DownloadQueueDialog(tk.Toplevel):
    """Monitor and control the in-memory download queue."""

    def __init__(self, owner: "LauncherApp") -> None:
        super().__init__(owner)
        self.owner = owner
        self._closed = False
        self._selected_id: int | None = None
        self._rows: dict[int, TaskSnapshot] = {}
        self.title("下载队列")
        self.configure(bg=BG)
        self.transient(owner)
        self.minsize(820, 440)
        width, height = 900, 520
        x = owner.winfo_rootx() + max(0, (owner.winfo_width() - width) // 2)
        y = owner.winfo_rooty() + max(0, (owner.winfo_height() - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.refresh()
        self.after(600, self._tick)

    def _build(self) -> None:
        outer = tk.Frame(self, bg=BG, padx=14, pady=12)
        outer.pack(fill="both", expand=True)
        outer.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(2, weight=1)

        header = tk.Frame(outer, bg=BG)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        tk.Label(
            header,
            text="下载队列",
            bg=BG,
            fg=TEXT,
            font=(self.owner._font_family, 13, "bold"),
        ).grid(row=0, column=0, sticky="w")
        self.summary_var = tk.StringVar(value="")
        tk.Label(
            header,
            textvariable=self.summary_var,
            bg=BG,
            fg=MUTED,
            font=(self.owner._font_family, 9),
        ).grid(row=0, column=1, sticky="e")

        tk.Label(
            outer,
            text=(
                "队列按顺序执行；同一游戏目录的下载会共享文件锁。"
                "关闭窗口不会停止队列。"
            ),
            bg=BG,
            fg=MUTED,
            font=(self.owner._font_family, 8),
        ).grid(row=1, column=0, sticky="w", pady=(2, 8))

        tree_frame = tk.Frame(outer, bg=INPUT)
        tree_frame.grid(row=2, column=0, sticky="nsew")
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            tree_frame,
            columns=("state", "task", "progress", "size", "message"),
            show="headings",
            selectmode="browse",
            style="Dark.Treeview",
        )
        for column, text, width in (
            ("state", "状态", 80),
            ("task", "任务", 330),
            ("progress", "进度", 70),
            ("size", "大小", 130),
            ("message", "信息", 260),
        ):
            self.tree.heading(column, text=text)
            anchor = "w" if column in ("task", "message") else "center"
            self.tree.column(column, width=width, anchor=anchor)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        scroll = tk.Scrollbar(
            tree_frame,
            command=self.tree.yview,
            bg=CARD_ALT,
            troughcolor=INPUT,
            activebackground=BORDER,
            relief="flat",
            bd=0,
        )
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        for state, color in (
            ("queued", MUTED),
            ("running", BLUE),
            ("paused", YELLOW),
            ("cancelling", YELLOW),
            ("succeeded", ACCENT),
            ("failed", RED),
            ("cancelled", MUTED),
        ):
            self.tree.tag_configure(state, foreground=color)

        bottom = tk.Frame(outer, bg=BG)
        bottom.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        bottom.grid_columnconfigure(0, weight=1)
        self.pause_button = self.owner._button(
            bottom, "暂停", self._toggle_pause, width=8
        )
        self.pause_button.grid(row=0, column=1, padx=(6, 0))
        self.cancel_button = self.owner._button(
            bottom, "取消", self._cancel, width=8
        )
        self.cancel_button.grid(row=0, column=2, padx=(6, 0))
        self.remove_button = self.owner._button(
            bottom, "移除", self._remove, width=8
        )
        self.remove_button.grid(row=0, column=3, padx=(6, 0))
        self.clear_button = self.owner._button(
            bottom, "清空已结束", self._clear, width=11
        )
        self.clear_button.grid(row=0, column=4, padx=(6, 0))
        tk.Label(
            bottom,
            text="并发",
            bg=BG,
            fg=MUTED,
            font=(self.owner._font_family, 9),
        ).grid(row=0, column=5, padx=(14, 4))
        self.concurrency_var = tk.StringVar(
            value=str(self.owner.task_queue.max_concurrent)
        )
        self.concurrency_spin = self.owner._spinbox(
            bottom, self.concurrency_var, from_=1, to=3, width=3
        )
        self.concurrency_spin.grid(row=0, column=6, ipady=3)
        for sequence in ("<<Increment>>", "<<Decrement>>", "<Return>", "<FocusOut>"):
            self.concurrency_spin.bind(sequence, self._apply_concurrency)
        self.close_button = self.owner._button(
            bottom, "关闭", self._close, primary=True, width=8
        )
        self.close_button.grid(row=0, column=7, padx=(14, 0))

        self._update_buttons()

    def refresh(self) -> None:
        if self._closed:
            return
        snapshots = self.owner.task_queue.snapshots()
        rows = {item.id: item for item in snapshots}
        for task_id in list(self._rows):
            if task_id not in rows:
                try:
                    self.tree.delete(str(task_id))
                except tk.TclError:
                    pass
                self._rows.pop(task_id, None)
        for item in snapshots:
            size_text = "—"
            if item.total_bytes:
                size_text = (
                    f"{human_size(item.done_bytes)} / {human_size(item.total_bytes)}"
                )
            values = (
                state_label(item.state),
                item.title,
                f"{item.progress * 100:.1f}%",
                size_text,
                item.message or "",
            )
            if item.id in self._rows:
                self.tree.item(str(item.id), values=values, tags=(item.state,))
            else:
                self.tree.insert(
                    "",
                    "end",
                    iid=str(item.id),
                    values=values,
                    tags=(item.state,),
                )
                self._rows[item.id] = item
            self._rows[item.id] = item
        running = sum(1 for item in snapshots if item.state == "running")
        queued = sum(1 for item in snapshots if item.state in ("queued", "paused"))
        done = sum(
            1
            for item in snapshots
            if item.state in ("succeeded", "failed", "cancelled")
        )
        self.summary_var.set(
            f"并发上限 {self.owner.task_queue.max_concurrent} · "
            f"运行 {running} · 等待 {queued} · 结束 {done}"
        )
        self._update_buttons()

    def _tick(self) -> None:
        if self._closed:
            return
        self.refresh()
        self.after(600, self._tick)

    def _on_select(self, _event=None) -> None:
        selection = self.tree.selection()
        self._selected_id = int(selection[0]) if selection else None
        self._update_buttons()

    def _current(self) -> TaskSnapshot | None:
        if self._selected_id is None:
            return None
        return self.owner.task_queue.get(self._selected_id)

    def _update_buttons(self) -> None:
        snapshot = self._current()
        state = snapshot.state if snapshot is not None else ""
        self.pause_button.configure(
            state="normal" if state in ("queued", "running", "paused") else "disabled",
            text="继续" if state == "paused" else "暂停",
        )
        self.cancel_button.configure(
            state=(
                "normal"
                if state in ("queued", "running", "paused")
                else "disabled"
            )
        )
        self.remove_button.configure(
            state=(
                "normal"
                if state in ("succeeded", "failed", "cancelled")
                or (snapshot is not None and not snapshot.started_at)
                else "disabled"
            )
        )
        self.clear_button.configure(
            state=(
                "normal"
                if any(
                    item.state in ("succeeded", "failed", "cancelled")
                    for item in self.owner.task_queue.snapshots()
                )
                else "disabled"
            )
        )

    def _toggle_pause(self) -> None:
        snapshot = self._current()
        if snapshot is None:
            return
        if snapshot.state == "paused":
            self.owner.task_queue.resume(snapshot.id)
        elif snapshot.state in ("queued", "running"):
            self.owner.task_queue.pause(snapshot.id)
        self.refresh()

    def _cancel(self) -> None:
        snapshot = self._current()
        if snapshot is not None:
            self.owner.task_queue.cancel(snapshot.id)
            self.refresh()

    def _remove(self) -> None:
        snapshot = self._current()
        if snapshot is not None and self.owner.task_queue.remove(snapshot.id):
            self._selected_id = None
            self.refresh()

    def _clear(self) -> None:
        self.owner.task_queue.clear_finished()
        self._selected_id = None
        self.refresh()

    def _apply_concurrency(self, _event=None) -> None:
        try:
            value = int(str(self.concurrency_var.get()).strip())
        except (TypeError, ValueError):
            value = 1
        value = self.owner.task_queue.set_max_concurrent(value)
        self.concurrency_var.set(str(value))
        self.owner._config["queue_concurrency"] = value
        self.owner._save_config()
        self.refresh()

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.owner._on_queue_dialog_closed()
        self.destroy()


class _JavaLinksDialog(tk.Toplevel):
    """Read-only list of official/vendor Java download pages."""

    def __init__(self, parent: tk.Misc, app: "LauncherApp", links) -> None:
        super().__init__(parent)
        self.app = app
        self._links = list(links)
        self.title("手动下载 Java 的网址")
        self.configure(bg=BG)
        self.minsize(760, 420)
        self.transient(app)
        self.grab_set()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.update_idletasks()
        width, height = 940, 540
        x = app.winfo_rootx() + max(0, (app.winfo_width() - width) // 2)
        y = app.winfo_rooty() + max(0, (app.winfo_height() - height) // 2)
        x = max(8, min(x, self.winfo_screenwidth() - width - 8))
        y = max(8, min(y, self.winfo_screenheight() - height - 80))
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _build(self) -> None:
        owner = self.app
        header = tk.Frame(self, bg=BG, padx=16, pady=12)
        header.pack(fill="x")
        tk.Label(
            header,
            text="自行下载 Java（不经过启动器）",
            bg=BG,
            fg=TEXT,
            font=(owner._font_family, 13, "bold"),
        ).pack(side="left")

        tk.Label(
            self,
            text=(
                "推荐下载 Windows x64 的 JDK/JRE zip，解压后回到 Java 管理点击"
                "“导入已有 Java…”选择 bin\\java.exe。\n"
                "Mojang 清单适合核对组件或脚本下载；启动器的 Java 下载也只使用 Mojang 官方文件。"
            ),
            bg=BG,
            fg=MUTED,
            font=(owner._font_family, 8),
            justify="left",
            anchor="w",
            wraplength=900,
        ).pack(fill="x", padx=16)

        table = tk.Frame(self, bg=INPUT)
        table.pack(fill="both", expand=True, padx=16, pady=(10, 8))
        table.grid_rowconfigure(0, weight=1)
        table.grid_columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            table,
            columns=("label", "url"),
            show="headings",
            selectmode="browse",
            style="Dark.Treeview",
            height=10,
        )
        self.tree.heading("label", text="来源 / 说明")
        self.tree.heading("url", text="网址")
        self.tree.column("label", width=300, anchor="w")
        self.tree.column("url", width=600, anchor="w")
        for index, link in enumerate(self._links):
            self.tree.insert(
                "", "end", iid=str(index), values=(link.label, link.url)
            )
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<Double-1>", lambda _event: self._open())
        scroll = tk.Scrollbar(
            table,
            command=self.tree.yview,
            bg=CARD_ALT,
            troughcolor=INPUT,
            activebackground=BORDER,
            relief="flat",
            bd=0,
        )
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        hscroll = tk.Scrollbar(
            table,
            orient="horizontal",
            command=self.tree.xview,
            bg=CARD_ALT,
            troughcolor=INPUT,
            activebackground=BORDER,
            relief="flat",
            bd=0,
        )
        hscroll.grid(row=1, column=0, sticky="ew")
        self.tree.configure(xscrollcommand=hscroll.set)

        self.note_var = tk.StringVar(value="选择一行后打开或复制；双击可直接打开。")
        tk.Label(
            self,
            textvariable=self.note_var,
            bg=BG,
            fg=MUTED,
            font=(owner._font_family, 8),
            justify="left",
            anchor="w",
            wraplength=900,
        ).pack(fill="x", padx=16)

        buttons = tk.Frame(self, bg=BG, padx=16, pady=12)
        buttons.pack(fill="x")
        owner._button(buttons, "打开链接", self._open, width=10).pack(side="left")
        owner._button(buttons, "复制网址", self._copy, width=10).pack(
            side="left", padx=(8, 0)
        )
        owner._button(buttons, "复制全部", self._copy_all, width=10).pack(
            side="left", padx=(8, 0)
        )
        owner._button(buttons, "关闭", self._close, primary=True, width=8).pack(
            side="right"
        )

    def _selected(self) -> int | None:
        selection = self.tree.selection()
        if not selection:
            return None
        try:
            return int(selection[0])
        except (TypeError, ValueError):
            return None

    def _open(self) -> None:
        index = self._selected()
        if index is None:
            self.note_var.set("请先选择一行。")
            return
        link = self._links[index]
        try:
            webbrowser.open(link.url)
            self.note_var.set(f"已在浏览器中打开：{link.url}")
        except Exception as exc:  # noqa: BLE001 - opening a browser is best-effort
            messagebox.showerror(
                "无法打开浏览器",
                f"{link.url}\n\n{exc}\n\n可以点击“复制网址”手动粘贴到浏览器。",
                parent=self,
            )

    def _copy(self) -> None:
        index = self._selected()
        if index is None:
            self.note_var.set("请先选择一行。")
            return
        link = self._links[index]
        self.clipboard_clear()
        self.clipboard_append(link.url)
        self.note_var.set(f"已复制：{link.url}")
        if link.note:
            self.note_var.set(f"已复制：{link.url}\n{link.note}")

    def _copy_all(self) -> None:
        text = "\n".join(f"{link.label}\n{link.url}" for link in self._links)
        self.clipboard_clear()
        self.clipboard_append(text)
        self.note_var.set(f"已复制 {len(self._links)} 个网址。")

    def _close(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


class _JavaRuntimeDialog(tk.Toplevel):
    """Browse, consent to, install and repair Mojang Java runtimes."""

    def __init__(self, owner: "LauncherApp") -> None:
        super().__init__(owner)
        self.owner = owner
        self._catalog: JavaRuntimeCatalog | None = None
        self._components: list[JavaRuntimeComponent] = []
        self._by_id: dict[str, JavaRuntimeComponent] = {}
        self._installed = {}
        self._manifests = {}
        self._selected: JavaRuntimeComponent | None = None
        self._manifest = None
        self._busy = False
        self._cancel: threading.Event | None = None
        self._pause: PauseToken | None = None
        self._catalog_generation = 0
        self._manifest_generation = 0
        self._install_generation = 0
        self._preferred_component = str(owner._config.get("java_component", ""))
        self._setup_variables()
        self.title("Java 运行时管理")
        self.configure(bg=BG)
        self.minsize(980, 720)
        self.transient(owner)
        self.grab_set()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.update_idletasks()
        screen_height = self.winfo_screenheight()
        width = 1060
        height = min(1000, max(760, screen_height - 80))
        x = owner.winfo_rootx() + max(0, (owner.winfo_width() - width) // 2)
        y = owner.winfo_rooty() + max(0, (owner.winfo_height() - height) // 2)
        x = max(8, min(x, self.winfo_screenwidth() - width - 8))
        y = max(8, min(y, screen_height - height - 80))
        self.geometry(f"{width}x{height}+{x}+{y}")
        self._refresh_info()
        self._on_root_changed()
        self.after(80, self._load_catalog)

    # -- setup ------------------------------------------------------

    def _setup_variables(self) -> None:
        owner = self.owner
        configured_source = owner.dl_source_var.get()
        labels = [label for _sid, label in source_labels()]
        if configured_source not in labels:
            configured_source = get_source(DEFAULT_SOURCE_ID).name
        configured_root = str(owner._config.get("java_runtime_dir", "")).strip()
        if not configured_root:
            configured_root = str(
                default_install_root(
                    owner.root_info.root if owner.root_info is not None else None
                )
            )
        self.source_var = tk.StringVar(value=configured_source)
        self.path_var = tk.StringVar(value=configured_root)
        self.required_var = tk.StringVar(value="—")
        self.detected_var = tk.StringVar(value="—")
        self.current_var = tk.StringVar(value="—")
        self.detail_var = tk.StringVar(value="请选择下方组件。")
        self.size_var = tk.StringVar(value="选择组件后显示文件数与下载量")
        self.path_hint_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="准备获取 Mojang Java 运行时清单…")
        self.progress_var = tk.StringVar(value="只有勾选确认并点击下载后才会联网。")
        self.consent_var = tk.BooleanVar(value=False)
        self.use_after_var = tk.BooleanVar(value=True)

    def _build(self) -> None:
        owner = self.owner
        header = tk.Frame(self, bg=BG, padx=16, pady=10)
        header.pack(fill="x")
        tk.Label(
            header,
            text="Java 运行时管理",
            bg=BG,
            fg=TEXT,
            font=(owner._font_family, 14, "bold"),
        ).pack(side="left")
        self.status_label = tk.Label(
            header,
            textvariable=self.status_var,
            bg=BG,
            fg=BLUE,
            font=(owner._font_family, 9),
        )
        self.status_label.pack(side="left", padx=(14, 0))

        info = tk.Frame(self, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        info.pack(fill="x", padx=16)
        body = tk.Frame(info, bg=CARD, padx=14, pady=10)
        body.pack(fill="x")
        body.grid_columnconfigure(1, weight=1)
        for row, (label, variable) in enumerate(
            (
                ("当前版本", self.required_var),
                ("已检测 Java", self.detected_var),
                ("当前使用", self.current_var),
            )
        ):
            tk.Label(
                body,
                text=label,
                bg=CARD,
                fg=MUTED,
                font=(owner._font_family, 9),
            ).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=1)
            tk.Label(
                body,
                textvariable=variable,
                bg=CARD,
                fg=TEXT,
                font=(owner._font_family, 9),
                justify="left",
                anchor="w",
                wraplength=880,
            ).grid(row=row, column=1, sticky="w", pady=1)

        table_header = tk.Frame(self, bg=BG, padx=16)
        table_header.pack(fill="x", pady=(10, 3))
        tk.Label(
            table_header,
            text="Mojang 官方运行时组件",
            bg=BG,
            fg=TEXT,
            font=(owner._font_family, 10, "bold"),
        ).pack(side="left")
        tk.Label(
            table_header,
            text="选择一个组件才会获取其文件清单；不会自动下载",
            bg=BG,
            fg=MUTED,
            font=(owner._font_family, 8),
        ).pack(side="left", padx=(10, 0))

        table = tk.Frame(self, bg=INPUT)
        table.pack(fill="both", expand=True, padx=16)
        table.grid_rowconfigure(0, weight=1)
        table.grid_columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            table,
            columns=("component", "java", "version", "platform", "status"),
            show="headings",
            selectmode="browse",
            style="Dark.Treeview",
            height=4,
        )
        headings = {
            "component": ("组件", 260, "w"),
            "java": ("Java", 80, "center"),
            "version": ("版本", 170, "center"),
            "platform": ("平台", 150, "center"),
            "status": ("本机状态", 180, "center"),
        }
        for key, (text, width, anchor) in headings.items():
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor=anchor)
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._on_tree_select())
        scroll = tk.Scrollbar(
            table,
            command=self.tree.yview,
            bg=CARD_ALT,
            troughcolor=INPUT,
            activebackground=BORDER,
            relief="flat",
            bd=0,
        )
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.tag_configure("installed", foreground=ACCENT)

        tk.Label(
            self,
            textvariable=self.detail_var,
            bg=BG,
            fg=MUTED,
            font=(owner._font_family, 8),
            justify="left",
            anchor="w",
            wraplength=980,
        ).pack(fill="x", padx=16, pady=(5, 0))

        options = tk.Frame(self, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        options.pack(fill="x", padx=16, pady=(8, 0))
        options_body = tk.Frame(options, bg=CARD, padx=14, pady=10)
        options_body.pack(fill="x")
        options_body.grid_columnconfigure(1, weight=1)

        tk.Label(
            options_body,
            text="安装位置",
            bg=CARD,
            fg=TEXT,
            font=(owner._font_family, 9),
        ).grid(row=0, column=0, sticky="w", padx=(0, 12), pady=3)
        self.path_entry = self._entry_dark(options_body, self.path_var)
        self.path_entry.grid(row=0, column=1, sticky="ew", ipady=7, pady=3)
        self.path_entry.bind("<Return>", lambda _event: self._on_root_changed())
        self.path_entry.bind("<FocusOut>", lambda _event: self._on_root_changed())
        path_buttons = tk.Frame(options_body, bg=CARD)
        path_buttons.grid(row=0, column=2, sticky="e", padx=(8, 0), pady=3)
        self.browse_button = owner._button(
            path_buttons, "浏览…", self._browse_root, width=8
        )
        self.browse_button.pack(side="left")
        self.default_button = owner._button(
            path_buttons, "默认位置", self._use_default_root, width=9
        )
        self.default_button.pack(side="left", padx=(6, 0))

        tk.Label(
            options_body,
            textvariable=self.path_hint_var,
            bg=CARD,
            fg=MUTED,
            font=(owner._font_family, 8),
            justify="left",
            anchor="w",
            wraplength=880,
        ).grid(row=1, column=1, columnspan=2, sticky="w", pady=(0, 4))

        tk.Label(
            options_body,
            text="下载源",
            bg=CARD,
            fg=TEXT,
            font=(owner._font_family, 9),
        ).grid(row=2, column=0, sticky="w", padx=(0, 12), pady=3)
        self.source_combo = ttk.Combobox(
            options_body,
            textvariable=self.source_var,
            values=[label for _sid, label in source_labels()],
            state="readonly",
            style="Dark.TCombobox",
            font=(owner._font_family, 10),
        )
        self.source_combo.grid(row=2, column=1, sticky="ew", pady=3)
        self.source_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._on_source_change()
        )
        tk.Label(
            options_body,
            text="文件仍按 Mojang 清单的 SHA-1 校验",
            bg=CARD,
            fg=MUTED,
            font=(owner._font_family, 8),
        ).grid(row=2, column=2, sticky="w", padx=(8, 0))

        helper = tk.Frame(options_body, bg=CARD)
        helper.grid(row=3, column=1, columnspan=2, sticky="w", pady=(6, 0))
        self.links_button = owner._button(
            helper, "手动下载网址…", self._show_links, width=14
        )
        self.links_button.pack(side="left")
        self.import_button = owner._button(
            helper, "导入已有 Java…", self._import_java, width=14
        )
        self.import_button.pack(side="left", padx=(8, 0))
        self.open_button = owner._button(
            helper, "打开安装目录", self._open_install_dir, width=12
        )
        self.open_button.pack(side="left", padx=(8, 0))

        consent_card = tk.Frame(
            self, bg=CARD, highlightbackground=BORDER, highlightthickness=1
        )
        consent_card.pack(fill="x", padx=16, pady=(8, 0))
        consent_body = tk.Frame(consent_card, bg=CARD, padx=14, pady=10)
        consent_body.pack(fill="x")
        consent_body.grid_columnconfigure(0, weight=1)
        self.consent_check = tk.Checkbutton(
            consent_body,
            text="请选择组件并等待组件信息加载完成",
            variable=self.consent_var,
            command=self._on_consent_toggle,
            bg=CARD,
            fg=TEXT,
            activebackground=CARD,
            activeforeground=TEXT,
            selectcolor=INPUT,
            font=(owner._font_family, 9),
            cursor="hand2",
            bd=0,
            highlightthickness=0,
            justify="left",
            anchor="w",
            wraplength=900,
            state="disabled",
        )
        self.consent_check.grid(row=0, column=0, columnspan=2, sticky="w")
        tk.Label(
            consent_body,
            textvariable=self.size_var,
            bg=CARD,
            fg=MUTED,
            font=(owner._font_family, 8),
            justify="left",
            anchor="w",
            wraplength=820,
        ).grid(row=1, column=0, sticky="w", pady=(5, 0))
        action_row = tk.Frame(consent_body, bg=CARD)
        action_row.grid(row=1, column=1, sticky="e", pady=(5, 0))
        self.verify_button = owner._button(
            action_row, "校验选中安装", self._verify_selected, width=12
        )
        self.verify_button.pack(side="left")
        self.download_button = owner._button(
            action_row, "开始下载并安装", self._start_download, primary=True, width=16
        )
        self.download_button.pack(side="left", padx=(8, 0))
        self.use_after_check = tk.Checkbutton(
            consent_body,
            text="下载/校验完成后切换为该 Java",
            variable=self.use_after_var,
            bg=CARD,
            fg=MUTED,
            activebackground=CARD,
            activeforeground=TEXT,
            selectcolor=INPUT,
            font=(owner._font_family, 8),
            cursor="hand2",
            bd=0,
            highlightthickness=0,
        )
        self.use_after_check.grid(row=2, column=0, sticky="w", pady=(2, 0))

        progress_row = tk.Frame(self, bg=BG, padx=16)
        progress_row.pack(fill="x", pady=(8, 0))
        progress_row.grid_columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(
            progress_row,
            style="Accent.Horizontal.TProgressbar",
            maximum=1000,
            value=0,
        )
        self.progress.grid(row=0, column=0, sticky="ew")
        self.pause_button = owner._button(
            progress_row, "暂停", self._toggle_pause, width=7
        )
        self.pause_button.grid(row=0, column=1, sticky="e", padx=(8, 0))
        self.pause_button.configure(state="disabled")
        self.cancel_button = owner._button(
            progress_row, "取消", self._cancel_download, width=7
        )
        self.cancel_button.grid(row=0, column=2, sticky="e", padx=(8, 0))
        self.cancel_button.configure(state="disabled")
        tk.Label(
            progress_row,
            textvariable=self.progress_var,
            bg=BG,
            fg=MUTED,
            font=(owner._font_family, 8),
            justify="left",
            anchor="w",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(3, 0))

        log_card = tk.Frame(self, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        log_card.pack(fill="both", expand=False, padx=16, pady=(8, 14))
        log_header = tk.Frame(log_card, bg=CARD, padx=12, pady=6)
        log_header.pack(fill="x")
        tk.Label(
            log_header,
            text="操作日志",
            bg=CARD,
            fg=TEXT,
            font=(owner._font_family, 9, "bold"),
        ).pack(side="left")
        tk.Label(
            log_header,
            text="只有勾选确认并点击下载后才会写入文件；可随时取消，未完成分片会保留以便续传。",
            bg=CARD,
            fg=MUTED,
            font=(owner._font_family, 8),
        ).pack(side="left", padx=(10, 0))
        self.log_text = tk.Text(
            log_card,
            bg=INPUT,
            fg="#c9d1d9",
            insertbackground=TEXT,
            selectbackground="#23472c",
            selectforeground=TEXT,
            relief="flat",
            bd=0,
            wrap="word",
            font=("Consolas", 8),
            padx=8,
            pady=6,
            height=2,
            state="disabled",
        )
        self.log_text.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        self.log_text.tag_configure("muted", foreground="#6e7681")
        self.log_text.tag_configure("success", foreground=ACCENT)
        self.log_text.tag_configure("warning", foreground=YELLOW)
        self.log_text.tag_configure("error", foreground=RED)

    def _entry_dark(self, parent: tk.Misc, variable: tk.StringVar) -> tk.Entry:
        return tk.Entry(
            parent,
            textvariable=variable,
            bg=INPUT,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            font=(self.owner._font_family, 9),
            disabledbackground="#11151c",
            disabledforeground=MUTED,
        )

    # -- basic helpers ---------------------------------------------

    def _append_log(self, message: str, tag: str = "muted") -> None:
        if not hasattr(self, "log_text"):
            return
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {message}\n", tag)
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 500:
            self.log_text.delete("1.0", "100.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_status(self, text: str, color: str = BLUE) -> None:
        self.status_var.set(text)
        self.status_label.configure(fg=color)

    def _refresh_info(self) -> None:
        owner = self.owner
        summary = owner.selected_version
        if summary is None:
            self.required_var.set("未选择游戏版本（仍可安装 Java 供以后使用）")
        else:
            required = owner.required_java_major
            self.required_var.set(
                f"{summary.version_id} · "
                + (f"需要 Java {required}" if required else "该版本未声明 Java 要求")
            )
        runtimes = list(owner.java_runtimes)
        current = owner.java_var.get().strip()
        current_runtime = owner._java_runtime
        if (
            current_runtime is not None
            and self._same_path(current_runtime.path, current)
            and all(not self._same_path(item.path, current) for item in runtimes)
        ):
            runtimes.insert(0, current_runtime)
        if not runtimes:
            self.detected_var.set("未检测到 Java（可下载、手动指定或查看官方网址）")
        else:
            self.detected_var.set(
                "；".join(
                    f"Java {item.major}（{item.path}）" for item in runtimes[:6]
                )
                + (f" 等 {len(runtimes)} 个" if len(runtimes) > 6 else "")
            )
        self.current_var.set(current or "未选择")

    @staticmethod
    def _same_path(left, right) -> bool:
        if not left or not right:
            return False
        try:
            return Path(left).resolve() == Path(right).resolve()
        except OSError:
            return os.path.normcase(str(left)) == os.path.normcase(str(right))

    def _source_id(self) -> str:
        return source_by_label(self.source_var.get()).id

    def _effective_root(self):
        try:
            return validate_install_root(self.path_var.get()), None
        except LauncherError as exc:
            return None, str(exc)

    def _default_root_text(self) -> str:
        return str(
            default_install_root(
                self.owner.root_info.root
                if self.owner.root_info is not None
                else None
            )
        )

    def _scan_roots(self) -> list[Path]:
        roots: list[Path] = []
        for value in (
            self.path_var.get(),
            self.owner._config.get("java_runtime_dir", ""),
            self._default_root_text(),
        ):
            text = str(value or "").strip()
            if not text:
                continue
            try:
                roots.append(validate_install_root(text))
            except LauncherError:
                continue
        # Preserve order but remove duplicates.
        unique: list[Path] = []
        for item in roots:
            if item not in unique:
                unique.append(item)
        return unique

    # -- catalog / component list ----------------------------------

    def _load_catalog(self) -> None:
        if self._busy:
            return
        self._catalog_generation += 1
        generation = self._catalog_generation
        self._set_status("正在获取 Mojang Java 运行时清单…", BLUE)
        self._set_busy(True)
        source_id = self._source_id()
        scan_roots = self._scan_roots()

        def worker() -> None:
            try:
                source = get_source(source_id)
                catalog = fetch_catalog(
                    source,
                    logger=lambda message: self.owner._post(
                        "java_dialog_event", "log", message, "muted"
                    ),
                )
                installed = {
                    item.component_id: item
                    for item in scan_installed_runtimes(scan_roots)
                }
                self.owner._post(
                    "java_dialog_event", "catalog_loaded", generation, catalog, installed
                )
            except Exception as exc:  # noqa: BLE001 - reported in the dialog
                self.owner._post(
                    "java_dialog_event", "catalog_failed", generation, str(exc)
                )

        threading.Thread(target=worker, daemon=True, name="java-catalog").start()

    def _on_catalog_loaded(self, generation: int, catalog, installed) -> None:
        if generation != self._catalog_generation:
            return
        self._set_busy(False)
        self._catalog = catalog
        self._installed = installed
        self._components = catalog.components(default_platform())
        self._by_id = {item.id: item for item in self._components}
        self.tree.delete(*self.tree.get_children())
        for component in self._components:
            found = installed.get(component.id)
            if found is not None:
                status = f"已安装 {found.version or ''}".strip()
                tags = ("installed",)
            else:
                status = "未安装"
                tags = ()
            self.tree.insert(
                "",
                "end",
                iid=component.id,
                values=(
                    component.id,
                    f"Java {component.java_major}" if component.java_major else "Java ?",
                    component.version,
                    component.platform,
                    status,
                ),
                tags=tags,
            )
        if not self._components:
            self._set_status("没有可用的 Windows 组件", RED)
            self.detail_var.set("Mojang 清单没有 windows 平台组件，请查看手动下载网址。")
            self._update_consent(reset=True)
            return
        required = self.owner.required_java_major
        selector = self._preferred_component if required is None else None
        try:
            chosen = resolve_component(catalog, selector, required_major=required)
        except LauncherError as exc:
            chosen = self._components[0]
            self._append_log(str(exc), "warning")
        self._preferred_component = ""
        self.tree.selection_set(chosen.id)
        self.tree.see(chosen.id)
        self._select_component(chosen)
        cache_note = "（本地缓存）" if catalog.from_cache else "（在线）"
        self._set_status(f"已获取 {len(self._components)} 个组件{cache_note}", MUTED)
        self._append_log(
            f"Java 清单：{len(self._components)} 个组件{cache_note}；"
            "选择组件后才会获取文件清单，点击下载前不会写入任何文件。",
            "muted",
        )

    def _on_catalog_failed(self, generation: int, message: str) -> None:
        if generation != self._catalog_generation:
            return
        self._set_busy(False)
        self._set_status("获取 Java 清单失败", RED)
        self.detail_var.set(f"无法获取组件清单：{message}")
        self._append_log(message, "error")
        self._update_consent(reset=True)
        messagebox.showwarning(
            "无法获取 Java 组件清单",
            f"{message}\n\n"
            "你可以点击“手动下载网址…”查看 Mojang 清单和 Adoptium / Microsoft "
            "等发行版，或检查网络后在下方切换下载源重试。",
            parent=self,
        )

    def _on_tree_select(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        component = self._by_id.get(selection[0])
        if component is None or component is self._selected:
            return
        self._select_component(component)

    def _select_component(self, component: JavaRuntimeComponent) -> None:
        self._selected = component
        self._manifest = None
        self.consent_var.set(False)
        self.size_var.set("正在获取组件文件清单…")
        self._set_status(f"正在读取 {component.id} 的文件清单…", BLUE)
        self._update_consent(reset=True)
        self._load_manifest(component)

    def _load_manifest(self, component: JavaRuntimeComponent) -> None:
        self._manifest_generation += 1
        generation = self._manifest_generation
        source_id = self._source_id()
        key = f"{component.id}|{component.manifest_sha1}|{source_id}"
        cached = self._manifests.get(key)
        if cached is not None:
            self._apply_manifest(component, cached, from_cache=True)
            return

        def worker() -> None:
            try:
                manifest = fetch_component_manifest(
                    component,
                    get_source(source_id),
                    logger=lambda message: self.owner._post(
                        "java_dialog_event", "log", message, "muted"
                    ),
                )
                self.owner._post(
                    "java_dialog_event",
                    "manifest_loaded",
                    generation,
                    component.id,
                    key,
                    manifest,
                )
            except Exception as exc:  # noqa: BLE001 - reported in the dialog
                self.owner._post(
                    "java_dialog_event",
                    "manifest_failed",
                    generation,
                    component.id,
                    str(exc),
                )

        threading.Thread(target=worker, daemon=True, name="java-manifest").start()

    def _on_manifest_loaded(self, generation: int, component_id: str, key: str, manifest) -> None:
        if generation != self._manifest_generation:
            return
        if self._selected is None or self._selected.id != component_id:
            return
        self._manifests[key] = manifest
        self._apply_manifest(self._selected, manifest, from_cache=False)

    def _on_manifest_failed(self, generation: int, component_id: str, message: str) -> None:
        if generation != self._manifest_generation:
            return
        if self._selected is None or self._selected.id != component_id:
            return
        self._manifest = None
        self.size_var.set("无法获取该组件的文件清单")
        self._set_status("组件清单获取失败", RED)
        self._append_log(message, "error")
        self._update_consent(reset=True)

    def _apply_manifest(self, component, manifest, *, from_cache: bool) -> None:
        self._manifest = manifest
        root, error = self._effective_root()
        target = str(root / component.id) if root is not None else "（安装位置无效）"
        self.size_var.set(
            f"{manifest.file_count} 个文件 / {human_size(manifest.total_bytes)}"
            "（按 Mojang 清单 SHA-1 逐个校验）"
        )
        self.detail_var.set(
            f"{component.id} · Java {component.java_major} {component.version}："
            f"{manifest.file_count} 个文件，下载约 {human_size(manifest.total_bytes)}；"
            f"目标 {target}"
            + ("；组件清单来自本地缓存" if from_cache else "")
        )
        self._set_status(
            f"已获取 {component.id} 文件清单"
            + ("（缓存）" if from_cache else "（在线）"),
            MUTED,
        )
        self._refresh_installed_status(component)
        self._update_consent(reset=False)

    # -- path / source / consent -----------------------------------

    def _browse_root(self) -> None:
        initial = self.path_var.get().strip() or self._default_root_text()
        selected = filedialog.askdirectory(
            title="选择 Java 安装位置",
            initialdir=initial if Path(initial).is_dir() else None,
            mustexist=False,
            parent=self,
        )
        if selected:
            self.path_var.set(selected)
            self._on_root_changed()

    def _use_default_root(self) -> None:
        self.path_var.set(self._default_root_text())
        self._on_root_changed()

    def _on_root_changed(self) -> None:
        if self._busy:
            return
        root, error = self._effective_root()
        if error:
            self.path_hint_var.set(f"安装位置不可用：{error}")
            try:
                self.path_entry.configure(highlightbackground=RED, highlightcolor=RED)
            except tk.TclError:
                pass
        else:
            try:
                self.path_entry.configure(highlightbackground=BORDER, highlightcolor=ACCENT)
            except tk.TclError:
                pass
            if self._selected is not None:
                self.path_hint_var.set(
                    f"将安装到：{root / self._selected.id}（下载前仍可修改）"
                )
            else:
                self.path_hint_var.set(f"Java 将安装到：{root}")
        if self._components:
            for component in self._components:
                self._refresh_installed_status(component)
        elif self._selected is not None:
            self._refresh_installed_status(self._selected)
        self._update_consent(reset=True)

    def _refresh_installed_status(self, component: JavaRuntimeComponent) -> None:
        root, _error = self._effective_root()
        found = (
            find_installed_runtime(root, component.id)
            if root is not None
            else None
        )
        if found is None:
            self._installed.pop(component.id, None)
        else:
            self._installed[component.id] = found
        if self.tree.exists(component.id):
            status = (
                f"已安装 {found.version or ''}".strip() if found is not None else "未安装"
            )
            self.tree.set(component.id, "status", status)
            self.tree.item(
                component.id, tags=("installed",) if found is not None else ()
            )

    def _on_source_change(self) -> None:
        if self._busy:
            return
        if self._selected is not None:
            self._preferred_component = self._selected.id
        self._catalog = None
        self._components = []
        self._by_id = {}
        self._selected = None
        self._manifest = None
        self.tree.delete(*self.tree.get_children())
        self.consent_var.set(False)
        self._append_log(f"已切换下载源：{self.source_var.get()}", "muted")
        self._load_catalog()

    def _on_consent_toggle(self) -> None:
        self.download_button.configure(
            state=(
                "normal"
                if self.consent_var.get()
                and self._manifest is not None
                and self._selected is not None
                and not self._busy
                else "disabled"
            )
        )

    def _update_consent(self, *, reset: bool) -> None:
        if reset:
            self.consent_var.set(False)
        if self._busy:
            self.consent_check.configure(state="disabled")
            self.download_button.configure(state="disabled")
            return
        if self._selected is None or self._manifest is None:
            self.consent_check.configure(
                state="disabled",
                text="请选择组件，并等待组件文件清单加载完成",
            )
            self.download_button.configure(state="disabled")
            self.verify_button.configure(state="disabled")
            return
        root, error = self._effective_root()
        if error or root is None:
            self.consent_check.configure(
                state="disabled", text=f"安装位置不可用：{error}"
            )
            self.download_button.configure(state="disabled")
            self.verify_button.configure(state="disabled")
            return
        component = self._selected
        installed = find_installed_runtime(root, component.id)
        if installed is not None:
            self._installed[component.id] = installed
        action = "校验并补齐" if installed is not None else "下载并安装"
        target = root / component.id
        self.consent_check.configure(
            state="normal",
            text=(
                f"我确认{action} Java {component.java_major or '?'} "
                f"{component.version}（{component.id}，"
                f"{self._manifest.file_count} 个文件，约 "
                f"{human_size(self._manifest.total_bytes)}）到：{target}"
            ),
        )
        self.download_button.configure(
            text="校验并补齐" if installed is not None else "开始下载并安装",
            state="normal" if self.consent_var.get() else "disabled",
        )
        self.verify_button.configure(
            state="normal" if installed is not None else "disabled"
        )
        if self.path_hint_var.get() != f"将安装到：{target}（下载前仍可修改）":
            self.path_hint_var.set(f"将安装到：{target}（下载前仍可修改）")

    # -- download ---------------------------------------------------

    def _start_download(self) -> None:
        if self._busy or self._selected is None or self._manifest is None:
            return
        if not self.consent_var.get():
            messagebox.showinfo(
                "需要确认",
                "请先勾选确认框，明确允许下载并写入所选目录。",
                parent=self,
            )
            return
        root, error = self._effective_root()
        if error or root is None:
            messagebox.showerror("安装位置无效", error or "请选择安装位置", parent=self)
            return
        component = self._selected
        manifest = self._manifest
        source = get_source(self._source_id())
        target = root / component.id
        installed = find_installed_runtime(root, component.id)
        action = "校验并补齐" if installed is not None else "下载并安装"
        proceed = messagebox.askyesno(
            f"确认{action} Java",
            f"组件：{component.id}（Java {component.java_major} {component.version}）\n"
            f"文件：{manifest.file_count} 个，约 {human_size(manifest.total_bytes)}\n"
            f"来源：{source.name}\n"
            f"目标：{target}\n\n"
            "只会替换校验不通过或缺失的文件；已有完整文件会跳过。\n"
            "现在开始下载？",
            parent=self,
        )
        if not proceed:
            self._append_log("用户取消下载确认。", "muted")
            return
        try:
            plan = build_install_plan(component, manifest, root, source)
        except LauncherError as exc:
            messagebox.showerror("无法准备安装", str(exc), parent=self)
            return

        self._install_generation += 1
        generation = self._install_generation
        self._cancel = threading.Event()
        self._pause = PauseToken()
        self._busy = True
        self._set_busy(True)
        self.pause_button.configure(state="normal", text="暂停")
        self.progress.configure(value=0)
        self.progress_var.set("正在准备下载…")
        self._set_status("正在下载 Java 运行时…", BLUE)
        self._append_log(
            f"开始{action}：{component.id} → {plan.install_dir}", "muted"
        )
        cancel = self._cancel
        pause = self._pause
        workers = max(1, min(6, self.owner._threads_value()))

        def worker() -> None:
            client = HttpClient(
                source.allowed_hosts,
                strict_redirects=source.strict_redirects,
                logger=lambda message: self.owner._post(
                    "java_dialog_event", "log", message, "muted"
                ),
                segments=max(0, min(8, self.owner._segments_value())),
            )
            try:
                outcome = install_java_runtime(
                    plan,
                    client=client,
                    confirmed=True,
                    workers=workers,
                    progress=lambda progress: self.owner._post(
                        "java_dialog_event", "progress", progress
                    ),
                    log=lambda message, level="info": self.owner._post(
                        "java_dialog_event", "log", message, level
                    ),
                    cancel=cancel,
                    pause=pause,
                )
                self.owner._post(
                    "java_dialog_event", "installed", generation, outcome
                )
            except CancelledError:
                self.owner._post("java_dialog_event", "cancelled", generation)
            except Exception as exc:  # noqa: BLE001 - reported in the dialog
                self.owner._post(
                    "java_dialog_event", "install_failed", generation, str(exc)
                )

        threading.Thread(target=worker, daemon=True, name="java-install").start()

    def _on_progress(self, progress: RuntimeProgress) -> None:
        percent = (
            min(100.0, progress.done_bytes * 100.0 / progress.total_bytes)
            if progress.total_bytes
            else 0.0
        )
        self.progress.configure(value=int(percent * 10))
        current = progress.current or progress.label
        self._set_status(
            f"下载 {percent:.1f}% · {progress.done_files}/{progress.total_files} 个文件",
            BLUE,
        )
        self.progress_var.set(
            f"{human_size(progress.done_bytes)} / {human_size(progress.total_bytes)}"
            f" · {progress.label} · {current[:90]}"
        )
        if progress.done_files < progress.total_files:
            self.cancel_button.configure(state="normal")

    def _on_installed(self, generation: int, outcome: JavaRuntimeOutcome) -> None:
        if generation != self._install_generation:
            return
        self._busy = False
        self._cancel = None
        self._set_busy(False)
        self.progress.configure(value=1000)
        self.progress_var.set(
            f"完成：下载 {outcome.downloaded_files} 个文件 / "
            f"{human_size(outcome.downloaded_bytes)}，"
            f"跳过 {outcome.skipped_files} 个完整文件。"
        )
        self._set_status("Java 安装完成", ACCENT)
        self._append_log(
            f"安装完成：{outcome.plan.install_dir}；"
            f"下载 {outcome.downloaded_files} 个文件，"
            f"跳过 {outcome.skipped_files} 个完整文件。",
            "success",
        )
        root = outcome.plan.install_root
        self.path_var.set(str(root))
        self._refresh_installed_status(outcome.plan.component)
        self._preferred_component = outcome.plan.component.id
        self.owner._config["java_runtime_dir"] = str(root)
        self.owner._config["java_component"] = outcome.plan.component.id

        used = False
        if outcome.runtime is not None:
            if all(
                not self._same_path(item.path, outcome.java_path)
                for item in self.owner.java_runtimes
            ):
                self.owner.java_runtimes = sorted(
                    [*self.owner.java_runtimes, outcome.runtime],
                    key=lambda item: (item.major, item.version),
                    reverse=True,
                )
        if outcome.runtime is not None and self.use_after_var.get():
            self.owner.java_var.set(str(outcome.java_path))
            self.owner._java_manual = True
            self.owner._java_runtime = outcome.runtime
            self.owner._update_java_hint()
            self.owner._append_log(
                f"已切换到新安装的 Java {outcome.runtime.version}："
                f"{outcome.java_path}",
                "success",
            )
            used = True
        self.owner._save_config()
        self._refresh_info()
        self._update_consent(reset=True)

        if outcome.runtime is None:
            messagebox.showwarning(
                "Java 已安装，但无法运行",
                f"文件已写入：\n{outcome.plan.install_dir}\n\n"
                "但 java.exe 无法启动，可能被安全软件拦截或安装不完整。\n"
                "请尝试“校验选中安装”或换一个安装位置后重新下载。",
                parent=self,
            )
            return
        target_note = (
            "已设为当前 Java，可直接点击“启动游戏”。"
            if used
            else "可在启动页的 Java 一栏选择它，或重新打开本窗口后勾选“下载后使用”。"
        )
        messagebox.showinfo(
            "Java 安装完成",
            f"Java {outcome.runtime.version}\n{outcome.java_path}\n\n{target_note}",
            parent=self,
        )

    def _on_install_failed(self, generation: int, message: str) -> None:
        if generation != self._install_generation:
            return
        self._busy = False
        self._cancel = None
        self._set_busy(False)
        self._set_status("Java 下载失败", RED)
        self.progress_var.set("下载失败；未完成的分片会保留，可重试续传。")
        self._append_log(message, "error")
        messagebox.showerror(
            "Java 下载失败",
            f"{message}\n\n已下载的完整文件会保留；再次点击下载只会补全缺失/损坏部分。",
            parent=self,
        )

    def _on_cancelled(self, generation: int) -> None:
        if generation != self._install_generation:
            return
        self._busy = False
        self._cancel = None
        self._pause = None
        self._set_busy(False)
        self._set_status("已取消下载", YELLOW)
        self.progress_var.set("已取消；本次下载的文件和未完成分片已删除。")
        self._append_log(
            "下载已取消；本次下载的文件和未完成分片已删除。", "warning"
        )

    def _toggle_pause(self) -> None:
        if self._pause is None:
            return
        paused = self._pause.toggle()
        self.pause_button.configure(text="继续" if paused else "暂停")
        if paused:
            self._set_status("已暂停下载", YELLOW)
            self._append_log("Java 下载已暂停，未完成分片会保留。", "warning")
        else:
            self._set_status("正在继续下载…", BLUE)
            self._append_log("继续下载 Java。", "info")

    def _cancel_download(self) -> None:
        if self._cancel is None:
            return
        self._cancel.set()
        self.cancel_button.configure(state="disabled")
        self.pause_button.configure(state="disabled")
        self._set_status("正在取消…", YELLOW)
        self._append_log("正在取消下载…", "warning")

    # -- verify / import / links -----------------------------------

    def _verify_selected(self) -> None:
        if self._busy or self._selected is None:
            return
        root, error = self._effective_root()
        if error or root is None:
            messagebox.showerror("安装位置无效", error or "请选择安装位置", parent=self)
            return
        component = self._selected
        installed = find_installed_runtime(root, component.id)
        if installed is None:
            messagebox.showinfo(
                "尚未安装",
                f"在所选位置没有找到 {component.id}。\n请先确认并下载。",
                parent=self,
            )
            return
        self._set_busy(True)
        self._set_status(f"正在校验 {component.id}…", BLUE)
        self.progress_var.set("正在逐个校验 SHA-1，可能需要几秒…")

        def worker() -> None:
            try:
                report = verify_java_runtime(installed.install_dir)
                self.owner._post(
                    "java_dialog_event", "verify_done", component.id, report
                )
            except Exception as exc:  # noqa: BLE001 - reported in the dialog
                self.owner._post(
                    "java_dialog_event", "verify_failed", str(exc)
                )

        threading.Thread(target=worker, daemon=True, name="java-verify").start()

    def _on_verify_done(self, component_id: str, report) -> None:
        self._set_busy(False)
        text = format_runtime_report(report)
        self._append_log(text, "success" if report.healthy else "warning")
        self.progress_var.set(
            "校验通过。" if report.healthy else "发现缺失/损坏文件；点击“校验并补齐”可只下载这些文件。"
        )
        self._set_status(
            "Java 校验通过" if report.healthy else "Java 校验发现异常",
            ACCENT if report.healthy else RED,
        )
        self._update_consent(reset=True)
        if report.healthy:
            messagebox.showinfo("校验通过", text, parent=self)
        else:
            messagebox.showwarning(
                "Java 安装需要修复",
                text + "\n\n勾选确认后点击“校验并补齐”，只会下载缺失/损坏文件。",
                parent=self,
            )

    def _on_verify_failed(self, message: str) -> None:
        self._set_busy(False)
        self._set_status("Java 校验失败", RED)
        self._append_log(message, "error")
        messagebox.showerror("Java 校验失败", message, parent=self)

    def _import_java(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self,
            title="选择已有的 java.exe",
            filetypes=[("Java executable", "java.exe"), ("All files", "*.*")],
        )
        if not selected:
            return
        self._set_status("正在检测所选 Java…", BLUE)

        def worker() -> None:
            runtime = probe_java(selected)
            self.owner._post("java_dialog_event", "imported", selected, runtime)

        threading.Thread(target=worker, daemon=True, name="java-import").start()

    def _on_imported(self, path_text: str, runtime) -> None:
        if runtime is None:
            self._set_status("所选 Java 无法运行", RED)
            self._append_log(f"无法运行所选 Java：{path_text}", "error")
            messagebox.showerror(
                "无法使用该 Java",
                f"无法运行：\n{path_text}\n\n请选择 JDK/JRE 目录下的 bin\\java.exe。",
                parent=self,
            )
            return
        owner = self.owner
        owner.java_var.set(path_text)
        owner._java_manual = True
        owner._java_runtime = runtime
        owner._update_java_hint()
        owner._save_config()
        self._refresh_info()
        self._set_status(f"已使用 Java {runtime.version}", ACCENT)
        self._append_log(
            f"已导入 Java {runtime.version}：{path_text}", "success"
        )
        messagebox.showinfo(
            "已选择 Java",
            f"Java {runtime.version}\n{path_text}\n\n启动页会使用这个 Java。",
            parent=self,
        )

    def _open_install_dir(self) -> None:
        root, error = self._effective_root()
        if error or root is None:
            messagebox.showerror("安装位置无效", error or "请选择安装位置", parent=self)
            return
        target = root / self._selected.id if self._selected is not None else root
        if not target.is_dir():
            target = root
        if not target.is_dir():
            messagebox.showinfo(
                "目录尚未创建",
                f"还没有安装任何 Java：\n{target}\n\n确认下载并安装后会创建该目录。",
                parent=self,
            )
            return
        try:
            if os.name == "nt":
                os.startfile(str(target))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except OSError as exc:
            messagebox.showerror("无法打开文件夹", f"{target}\n{exc}", parent=self)

    def _show_links(self) -> None:
        links = manual_java_links(self.owner.required_java_major, self._selected)
        _JavaLinksDialog(self, self.owner, links)

    # -- busy / close ----------------------------------------------

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for widget in (
            self.path_entry,
            self.browse_button,
            self.default_button,
            self.import_button,
            self.verify_button,
        ):
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass
        self.source_combo.configure(state="disabled" if busy else "readonly")
        try:
            self.tree.state(["disabled"] if busy else ["!disabled"])
        except tk.TclError:
            pass
        self.consent_check.configure(state="disabled" if busy else "normal")
        self.download_button.configure(state="disabled")
        self.cancel_button.configure(state="normal" if busy else "disabled")
        self.pause_button.configure(
            state="normal" if busy else "disabled", text="暂停"
        )
        if not busy:
            self._pause = None
            self._update_consent(reset=False)

    def _close(self) -> None:
        if self._busy:
            proceed = messagebox.askyesno(
                "正在下载",
                "Java 正在下载。关闭窗口会取消下载，"
                "本次下载的文件和未完成分片会被删除。\n\n确定关闭？",
                parent=self,
            )
            if not proceed:
                return
            if self._cancel is not None:
                self._cancel.set()
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.owner._java_dialog = None
        self.destroy()

    # -- event dispatch -------------------------------------------

    def handle_event(self, kind: str, *payload) -> None:
        if kind == "log":
            self._append_log(payload[0], payload[1] if len(payload) > 1 else "muted")
        elif kind == "catalog_loaded":
            self._on_catalog_loaded(*payload)
        elif kind == "catalog_failed":
            self._on_catalog_failed(*payload)
        elif kind == "manifest_loaded":
            self._on_manifest_loaded(*payload)
        elif kind == "manifest_failed":
            self._on_manifest_failed(*payload)
        elif kind == "progress":
            self._on_progress(payload[0])
        elif kind == "installed":
            self._on_installed(*payload)
        elif kind == "install_failed":
            self._on_install_failed(*payload)
        elif kind == "cancelled":
            self._on_cancelled(*payload)
        elif kind == "verify_done":
            self._on_verify_done(*payload)
        elif kind == "verify_failed":
            self._on_verify_failed(*payload)
        elif kind == "imported":
            self._on_imported(*payload)


class _VersionManagerDialog(tk.Toplevel):
    """Local version inventory plus deletion of a single versions/<folder>."""

    def __init__(self, owner: "LauncherApp") -> None:
        super().__init__(owner)
        self.owner = owner
        self._details: list[VersionDetails] = []
        self.title("版本管理")
        self.configure(bg=BG)
        self.minsize(760, 400)
        self.transient(owner)
        self.grab_set()
        self._build()
        self.reload()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.update_idletasks()
        width, height = 900, 520
        x = owner.winfo_rootx() + max(0, (owner.winfo_width() - width) // 2)
        y = owner.winfo_rooty() + max(0, (owner.winfo_height() - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _build(self) -> None:
        owner = self.owner
        header = tk.Frame(self, bg=BG, padx=16, pady=12)
        header.pack(fill="x")
        tk.Label(
            header,
            text="版本管理",
            bg=BG,
            fg=TEXT,
            font=(owner._font_family, 13, "bold"),
        ).pack(side="left")
        self.hint_var = tk.StringVar(value="")
        tk.Label(
            header,
            textvariable=self.hint_var,
            bg=BG,
            fg=MUTED,
            font=(owner._font_family, 8),
        ).pack(side="left", padx=(12, 0))

        tree_frame = tk.Frame(self, bg=INPUT)
        tree_frame.pack(fill="both", expand=True, padx=16)
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            tree_frame,
            columns=("version", "type", "loader", "size", "modified", "isolated", "files"),
            show="headings",
            selectmode="browse",
            style="Dark.Treeview",
        )
        headings = {
            "version": "版本目录",
            "type": "类型",
            "loader": "加载器",
            "size": "占用",
            "modified": "修改时间",
            "isolated": "隔离",
            "files": "文件数",
        }
        widths = {
            "version": 300,
            "type": 80,
            "loader": 80,
            "size": 90,
            "modified": 140,
            "isolated": 50,
            "files": 70,
        }
        for key, text in headings.items():
            self.tree.heading(key, text=text)
            self.tree.column(
                key,
                width=widths[key],
                anchor="w" if key == "version" else "center",
            )
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._update_footer())
        self.tree.bind("<Double-1>", lambda _event: self._open_folder())
        scroll = tk.Scrollbar(
            tree_frame,
            command=self.tree.yview,
            bg=CARD_ALT,
            troughcolor=INPUT,
            activebackground=BORDER,
            relief="flat",
            bd=0,
        )
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.tag_configure("isolated", foreground=ACCENT)

        self.footer_var = tk.StringVar(value="")
        tk.Label(
            self,
            textvariable=self.footer_var,
            bg=BG,
            fg=MUTED,
            font=(owner._font_family, 8),
            anchor="w",
            justify="left",
        ).pack(fill="x", padx=16, pady=(8, 0))
        tk.Label(
            self,
            text=(
                "删除只移除 versions/<版本> 文件夹；共享的 libraries、assets、"
                "resources 与其他版本不受影响。\n"
                "该版本文件夹内的隔离存档和配置会一并删除，删除前请确认。"
            ),
            bg=BG,
            fg="#5c6674",
            font=(owner._font_family, 8),
            anchor="w",
            justify="left",
        ).pack(fill="x", padx=16, pady=(2, 0))

        buttons = tk.Frame(self, bg=BG, padx=16, pady=12)
        buttons.pack(fill="x")
        self.delete_button = owner._button(
            buttons, "删除版本", self._delete, width=10
        )
        self.delete_button.pack(side="left")
        self.open_button = owner._button(
            buttons, "打开版本文件夹", self._open_folder, width=14
        )
        self.open_button.pack(side="left", padx=(8, 0))
        owner._button(buttons, "刷新", self.reload, width=7).pack(
            side="left", padx=(8, 0)
        )
        owner._button(buttons, "关闭", self._close, primary=True, width=8).pack(
            side="right"
        )

    def reload(self) -> None:
        owner = self.owner
        if owner.root_info is None:
            self.hint_var.set("未选择游戏目录")
            return
        try:
            self._details = list_version_details(owner.root_info)
        except LauncherError as exc:
            messagebox.showerror("读取版本失败", str(exc), parent=self)
            return
        self.tree.delete(*self.tree.get_children())
        for item in self._details:
            self.tree.insert(
                "",
                "end",
                iid=item.folder_name,
                values=(
                    item.folder_name,
                    item.summary.version_type or "-",
                    item.loader,
                    human_size(item.size_bytes),
                    item.modified_text,
                    item.isolation_text,
                    item.file_count,
                ),
                tags=("isolated",) if item.isolated else (),
            )
        total = sum(item.size_bytes for item in self._details)
        self.hint_var.set(
            f"{len(self._details)} 个版本 · 合计 {human_size(total)} · "
            f"目录 {owner.root_info.versions_dir}"
        )
        if self._details:
            first = self._details[0].folder_name
            self.tree.selection_set(first)
            self.tree.focus(first)
            self.tree.see(first)
        self.delete_button.configure(state="normal" if self._details else "disabled")
        self.open_button.configure(state="normal" if self._details else "disabled")
        self._update_footer()

    def _selected(self) -> VersionDetails | None:
        selection = self.tree.selection()
        if not selection:
            return None
        return next(
            (item for item in self._details if item.folder_name == selection[0]),
            None,
        )

    def _update_footer(self) -> None:
        item = self._selected()
        if item is None:
            self.footer_var.set("请选择一个版本查看详情。")
            return
        inherited = f" · 继承自 {item.inherits_from}" if item.inherits_from else ""
        self.footer_var.set(
            f"{item.summary.folder} · {item.summary.version_type or '未知类型'} · "
            f"加载器 {item.loader} · {item.file_count} 个文件{inherited}"
        )

    def _delete(self) -> None:
        item = self._selected()
        if item is None:
            messagebox.showwarning("未选择版本", "请先在列表中选择一个版本。", parent=self)
            return
        owner = self.owner
        if owner.root_info is None:
            return
        protected: list[Path] = []
        if owner.process is not None and owner.selected_version is not None:
            protected.append(owner.selected_version.folder)
        running = bool(
            owner.process is not None
            and owner.selected_version is not None
            and owner.selected_version.folder.name == item.folder_name
        )
        children = [
            detail.folder_name
            for detail in self._details
            if detail.folder_name != item.folder_name
            and detail.inherits_from in (item.version_id, item.folder_name)
        ]
        child_note = ""
        if children:
            child_note = (
                "\n\n注意：以下版本继承自它，删除后可能无法启动：\n  "
                + "、".join(children[:6])
            )
        proceed = messagebox.askyesno(
            "删除版本",
            f"确定删除版本 {item.folder_name}？\n\n"
            f"类型：{item.summary.version_type or '未知'} · 加载器：{item.loader}\n"
            f"将释放约 {human_size(item.size_bytes)}（{item.file_count} 个文件）。\n\n"
            "只删除 versions/<版本> 文件夹；共享的 libraries、assets、resources "
            "和其他版本不受影响。\n"
            "该文件夹内的隔离存档和配置会一并删除。"
            + child_note,
            parent=self,
        )
        if not proceed:
            return
        if running:
            messagebox.showwarning(
                "版本正在运行",
                "该版本的游戏进程仍在运行，请先关闭游戏再删除。",
                parent=self,
            )
            return
        try:
            result = remove_version(
                owner.root_info, item.folder_name, protected_folders=protected
            )
        except LauncherError as exc:
            messagebox.showerror("删除失败", str(exc), parent=self)
            return
        owner._append_log(
            f"已删除版本 {result.folder_name}，释放 {human_size(result.freed_bytes)}。",
            "success",
        )
        messagebox.showinfo(
            "已删除",
            f"已删除 versions/{result.folder_name}，"
            f"释放 {human_size(result.freed_bytes)}。",
            parent=self,
        )
        if len(self._details) <= 1:
            self._close()
        else:
            self.reload()
        owner._refresh_versions()

    def _open_folder(self) -> None:
        item = self._selected()
        if item is None:
            return
        folder = item.summary.folder
        try:
            if os.name == "nt":
                os.startfile(str(folder))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except OSError as exc:
            messagebox.showerror(
                "无法打开文件夹", f"{folder}\n{exc}", parent=self
            )

    def _close(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.owner._version_dialog = None
        self.destroy()


class _CacheDialog(tk.Toplevel):
    """Size accounting and selective clean-up of known launcher caches."""

    def __init__(self, owner: "LauncherApp") -> None:
        super().__init__(owner)
        self.owner = owner
        self._usages = []
        self._vars: dict[str, tk.BooleanVar] = {}
        self.title("缓存清理")
        self.configure(bg=BG)
        self.minsize(720, 380)
        self.transient(owner)
        self.grab_set()
        self._build()
        self.reload()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.update_idletasks()
        width, height = 820, 500
        x = owner.winfo_rootx() + max(0, (owner.winfo_width() - width) // 2)
        y = owner.winfo_rooty() + max(0, (owner.winfo_height() - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _build(self) -> None:
        owner = self.owner
        header = tk.Frame(self, bg=BG, padx=16, pady=12)
        header.pack(fill="x")
        tk.Label(
            header,
            text="缓存与临时文件",
            bg=BG,
            fg=TEXT,
            font=(owner._font_family, 13, "bold"),
        ).pack(side="left")
        self.hint_var = tk.StringVar(value="")
        tk.Label(
            header,
            textvariable=self.hint_var,
            bg=BG,
            fg=MUTED,
            font=(owner._font_family, 8),
        ).pack(side="left", padx=(12, 0))

        table = tk.Frame(self, bg=INPUT)
        table.pack(fill="both", expand=True, padx=16)
        table.grid_rowconfigure(0, weight=1)
        table.grid_columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            table,
            columns=("category", "files", "size"),
            show="headings",
            style="Dark.Treeview",
            height=5,
        )
        self.tree.heading("category", text="类别")
        self.tree.heading("files", text="文件数")
        self.tree.heading("size", text="占用")
        self.tree.column("category", width=180, anchor="w")
        self.tree.column("files", width=90, anchor="center")
        self.tree.column("size", width=110, anchor="center")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = tk.Scrollbar(
            table,
            command=self.tree.yview,
            bg=CARD_ALT,
            troughcolor=INPUT,
            activebackground=BORDER,
            relief="flat",
            bd=0,
        )
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)

        checks = tk.Frame(self, bg=BG, padx=16, pady=10)
        checks.pack(fill="x")
        for category in CATEGORY_ORDER:
            variable = tk.BooleanVar(value=False)
            self._vars[category] = variable
            tk.Checkbutton(
                checks,
                text=CATEGORY_LABELS[category],
                variable=variable,
                bg=BG,
                fg=TEXT,
                activebackground=BG,
                activeforeground=TEXT,
                selectcolor=INPUT,
                font=(owner._font_family, 9),
                cursor="hand2",
                bd=0,
                highlightthickness=0,
            ).pack(side="left", padx=(0, 16))

        self.detail_var = tk.StringVar(value="")
        tk.Label(
            self,
            textvariable=self.detail_var,
            bg=BG,
            fg=MUTED,
            font=(owner._font_family, 8),
            justify="left",
            anchor="w",
            wraplength=760,
        ).pack(fill="x", padx=16, pady=(8, 0))
        tk.Label(
            self,
            text=(
                "只清理启动器元数据缓存、已下载的 Forge 安装器、临时 natives "
                "和游戏目录内未完成的 *.part 分片。\n"
                "不会删除 versions、libraries、assets、resources、存档或配置。"
            ),
            bg=BG,
            fg="#5c6674",
            font=(owner._font_family, 8),
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=16, pady=(2, 0))

        buttons = tk.Frame(self, bg=BG, padx=16, pady=12)
        buttons.pack(fill="x")
        owner._button(buttons, "清理选中", self._clean_selected, width=10).pack(
            side="left"
        )
        owner._button(buttons, "全部清理", self._clean_all, width=10).pack(
            side="left", padx=(8, 0)
        )
        owner._button(buttons, "重新统计", self.reload, width=9).pack(
            side="left", padx=(8, 0)
        )
        owner._button(buttons, "关闭", self._close, primary=True, width=8).pack(
            side="right"
        )

    def reload(self) -> None:
        self._usages = scan_cache(self.owner.root_info)
        self.tree.delete(*self.tree.get_children())
        for usage in self._usages:
            self.tree.insert(
                "",
                "end",
                iid=usage.category,
                values=(
                    usage.label,
                    usage.files,
                    human_size(usage.bytes),
                ),
            )
        self.hint_var.set(
            f"合计 {human_size(total_cache_bytes(self._usages))}"
            + (
                ""
                if self.owner.root_info is not None
                else "（未选择游戏目录，未统计 *.part）"
            )
        )
        self.detail_var.set(
            "\n".join(
                f"{usage.label}：{usage.description}" for usage in self._usages
            )
        )

    def _chosen(self, all_: bool) -> list[str]:
        if all_:
            return list(CATEGORY_ORDER)
        return [category for category, variable in self._vars.items() if variable.get()]

    def _clean_selected(self) -> None:
        self._clean(self._chosen(False), "清理选中缓存")

    def _clean_all(self) -> None:
        self._clean(self._chosen(True), "清理全部缓存")

    def _clean(self, categories: list[str], title: str) -> None:
        if not categories:
            messagebox.showwarning(
                "未选择类别", "请先勾选要清理的缓存类别。", parent=self
            )
            return
        labels = "、".join(CATEGORY_LABELS[item] for item in categories)
        estimate = sum(
            usage.bytes
            for usage in self._usages
            if usage.category in categories
        )
        proceed = messagebox.askyesno(
            title,
            f"将清理：{labels}\n预计释放约 {human_size(estimate)}。\n\n"
            "只删除启动器缓存、Forge 安装器、临时 natives 和未完成的 *.part "
            "分片；\n不会删除 versions、libraries、assets、resources、存档或配置。\n\n"
            "确定继续？",
            parent=self,
        )
        if not proceed:
            return
        report = clean_cache(
            categories,
            self.owner.root_info,
            logger=lambda message: self.owner._append_log(message, "muted"),
        )
        text = format_clean_report(report)
        self.owner._append_log(text, "success" if report.ok else "warning")
        if report.ok:
            messagebox.showinfo("清理完成", text, parent=self)
        else:
            messagebox.showwarning("部分清理失败", text, parent=self)
        self.reload()

    def _close(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.owner._cache_dialog = None
        self.destroy()


class _MicrosoftLoginDialog(tk.Toplevel):
    """Device-code login dialog; network work runs off the Tk thread."""

    def __init__(self, owner: "LauncherApp", initial_client_id: str = "") -> None:
        super().__init__(owner)
        self.owner = owner
        self._closed = False
        self._cancel = threading.Event()
        self._events: queue.Queue[tuple] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._verification_uri = ""
        self.title("微软登录")
        self.configure(bg=BG)
        self.transient(owner)
        self.resizable(False, False)
        width, height = 640, 520
        x = owner.winfo_rootx() + max(0, (owner.winfo_width() - width) // 2)
        y = owner.winfo_rooty() + max(0, (owner.winfo_height() - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.client_id_var = tk.StringVar(value=initial_client_id)
        self.status_var = tk.StringVar(value="")
        self.code_var = tk.StringVar(value="")
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.grab_set()
        self.after(120, self._poll)

    def _build(self) -> None:
        body = tk.Frame(self, bg=BG, padx=18, pady=14)
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(0, weight=1)

        tk.Label(
            body,
            text="微软账户登录",
            bg=BG,
            fg=TEXT,
            font=(self.owner._font_family, 14, "bold"),
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            body,
            text=(
                "使用 Microsoft 设备代码登录，浏览器完成验证后会自动返回。\n"
                "启动器不会保存密码；刷新令牌使用 Windows DPAPI 加密保存。"
            ),
            bg=BG,
            fg=MUTED,
            justify="left",
            wraplength=590,
            font=(self.owner._font_family, 9),
        ).grid(row=1, column=0, sticky="w", pady=(4, 12))

        tk.Label(
            body,
            text="Azure Application (client) ID",
            bg=BG,
            fg=TEXT,
            font=(self.owner._font_family, 9, "bold"),
        ).grid(row=2, column=0, sticky="w")
        self.client_id_entry = self.owner._entry(body, self.client_id_var)
        self.client_id_entry.grid(row=3, column=0, sticky="ew", ipady=7, pady=(3, 2))
        tk.Label(
            body,
            text=(
                "在 Entra ID → 应用注册中复制；应用需允许公共客户端流。"
                "只登录个人 Microsoft 账户时，应用类型选“仅个人 Microsoft 账户”。"
            ),
            bg=BG,
            fg=MUTED,
            justify="left",
            wraplength=590,
            font=(self.owner._font_family, 8),
        ).grid(row=4, column=0, sticky="w", pady=(0, 12))

        self.code_frame = tk.Frame(
            body,
            bg=CARD,
            highlightbackground=BORDER,
            highlightthickness=1,
            padx=14,
            pady=10,
        )
        self.code_frame.grid(row=5, column=0, sticky="ew", pady=(0, 10))
        self.code_frame.grid_columnconfigure(0, weight=1)
        tk.Label(
            self.code_frame,
            text="在浏览器中输入以下代码",
            bg=CARD,
            fg=MUTED,
            font=(self.owner._font_family, 8),
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            self.code_frame,
            textvariable=self.code_var,
            bg=CARD,
            fg=ACCENT,
            font=("Consolas", 22, "bold"),
        ).grid(row=1, column=0, sticky="w", pady=(2, 6))
        code_buttons = tk.Frame(self.code_frame, bg=CARD)
        code_buttons.grid(row=2, column=0, sticky="w")
        self.owner._button(
            code_buttons, "复制代码", self._copy_code, width=9
        ).pack(side="left")
        self.owner._button(
            code_buttons, "打开浏览器", self._open_browser, width=11
        ).pack(side="left", padx=(6, 0))
        self.code_frame.grid_remove()

        self.status_label = tk.Label(
            body,
            textvariable=self.status_var,
            bg=BG,
            fg=MUTED,
            justify="left",
            wraplength=590,
            font=(self.owner._font_family, 9),
        )
        self.status_label.grid(row=6, column=0, sticky="w", pady=(0, 10))

        buttons = tk.Frame(body, bg=BG)
        buttons.grid(row=7, column=0, sticky="ew")
        self.close_button = self.owner._button(
            buttons, "关闭", self._close, width=9
        )
        self.close_button.pack(side="right")
        self.start_button = self.owner._button(
            buttons, "开始登录", self._start, primary=True, width=11
        )
        self.start_button.pack(side="right", padx=(0, 8))

    def _set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.start_button.configure(state=state)
        self.client_id_entry.configure(state=state)
        self.close_button.configure(text="取消" if running else "关闭")

    def _start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        client_id = self.client_id_var.get().strip()
        try:
            validate_client_id(client_id)
        except AuthError as exc:
            messagebox.showerror("client ID 无效", str(exc), parent=self)
            return
        self._cancel.clear()
        self._set_running(True)
        self.status_var.set("正在向 Microsoft 请求设备代码…")
        self.code_frame.grid_remove()
        self._thread = threading.Thread(
            target=self._worker,
            args=(client_id,),
            daemon=True,
            name="msa-login",
        )
        self._thread.start()

    def _worker(self, client_id: str) -> None:
        transport = HttpAuthTransport()
        try:
            account = login_device_code(
                client_id,
                transport,
                on_code=lambda code: self._events.put(("code", code)),
                cancel=self._cancel,
            )
            self._events.put(("done", account, client_id))
        except AuthCancelled:
            self._events.put(("cancelled",))
        except AuthError as exc:
            self._events.put(("error", str(exc)))
        except Exception as exc:  # keep the dialog responsive
            self._events.put(("error", f"微软登录失败：{exc}"))

    def _poll(self) -> None:
        if self._closed:
            return
        try:
            while True:
                kind, *payload = self._events.get_nowait()
                if kind == "code":
                    self._show_code(payload[0])
                elif kind == "done":
                    self._done(payload[0], payload[1])
                elif kind == "cancelled":
                    self._close()
                    return
                elif kind == "error":
                    self._error(str(payload[0]))
        except queue.Empty:
            pass
        if not self._closed:
            self.after(120, self._poll)

    def _show_code(self, code) -> None:
        self._verification_uri = code.verification_uri
        self.code_var.set(code.user_code)
        self.code_frame.grid()
        self.status_var.set(
            f"请在浏览器中打开 {code.verification_uri}\n"
            f"输入代码后等待完成；代码约 {max(1, code.expires_in // 60)} 分钟内有效。"
        )
        try:
            webbrowser.open(code.verification_uri)
        except Exception:
            pass

    def _copy_code(self) -> None:
        value = self.code_var.get().strip()
        if value:
            self.clipboard_clear()
            self.clipboard_append(value)
            self.status_var.set("代码已复制到剪贴板。")

    def _open_browser(self) -> None:
        if self._verification_uri:
            try:
                webbrowser.open(self._verification_uri)
            except Exception:
                pass

    def _done(self, account: MicrosoftAccount, client_id: str) -> None:
        self._set_running(False)
        try:
            self.owner._on_ms_login_success(account, client_id)
        except Exception as exc:
            self.status_var.set(f"登录成功，但保存界面状态失败：{exc}")
            return
        self.status_var.set(f"登录成功：{account.name}")
        self.after(400, self._close)

    def _error(self, message: str) -> None:
        self._set_running(False)
        self.status_var.set("登录失败，可检查 client ID 后重试。")
        messagebox.showerror("微软登录失败", message, parent=self)

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._cancel.set()
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.owner._ms_dialog = None
        self.destroy()


def main() -> None:
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    app = LauncherApp()
    app.mainloop()
