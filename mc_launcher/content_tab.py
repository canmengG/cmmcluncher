"""Tkinter "content / resources" tab for the download center and modpacks.

The tab lives in its own module to keep ``app.py`` focused on launching.  It
reuses the application's palette/style helpers, performs all network work on
daemon threads, and marshals results back through a queue polled by
``after()`` (Tk is not thread-safe).

Nothing is downloaded until the user presses "下载并安装" (or chooses a local
archive and confirms) and the same rule as the Java runtime manager applies.
"""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
import webbrowser
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .core.content import (
    CONTENT_TYPES,
    ContentError,
    ContentProject,
    ContentSearchResult,
    ContentVersion,
    ModrinthSource,
    SOURCE_CURSEFORGE,
    SOURCE_MODRINTH,
    SORT_OPTIONS,
    build_content_plan,
    content_instance_name,
    content_type_label,
    default_target_dir,
    get_content_source,
    install_content,
    list_worlds,
    read_mrpack,
    read_content_sidecar,
    select_best_version,
    validate_world_dir,
)
from .core.errors import LauncherError
from .core.mirrors import source_by_label
from .core.modpack import (
    build_instance_plan,
    download_and_install_launchable_modpack,
    format_instance_plan,
    install_instance,
    resolve_modpack_dependencies,
)
from .core.net import CancelledError, PauseToken, human_size
from .core.paths import normalize_game_root, version_game_dir
from .core.version import base_game_version

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

TYPE_LABELS = {
    "mod": "Mod",
    "modpack": "整合包",
    "datapack": "数据包",
    "resourcepack": "资源包",
    "shader": "光影包",
}
TYPE_BY_LABEL = {label: key for key, label in TYPE_LABELS.items()}
SOURCE_BY_LABEL = {"Modrinth": "modrinth", "CurseForge": "curseforge"}
LOADER_BY_LABEL = {
    "全部": "",
    "Fabric": "fabric",
    "Forge": "forge",
    "NeoForge": "neoforge",
    "Quilt": "quilt",
}
SORT_BY_LABEL = {label: key for key, label in SORT_OPTIONS}
ALL = "全部"


class _CurseForgeKeyDialog(tk.Toplevel):
    """Small modal dialog that stores the CurseForge API key locally."""

    def __init__(self, parent: tk.Misc, current: str = "") -> None:
        super().__init__(parent)
        self.result: str | None = None
        self.title("CurseForge API Key")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(parent)
        body = tk.Frame(self, bg=BG, padx=18, pady=14)
        body.pack(fill="both", expand=True)
        tk.Label(
            body,
            text="CurseForge 需要 API Key",
            bg=BG,
            fg=TEXT,
            font=("Microsoft YaHei UI", 13, "bold"),
        ).pack(anchor="w")
        tk.Label(
            body,
            text=(
                "在 https://console.curseforge.com/ 申请 Key 后粘贴到这里。\n"
                "Key 只保存在本机 config.json，不会写入日志或发布包。\n"
                "不配置 Key 时仍可正常使用 Modrinth。"
            ),
            bg=BG,
            fg=MUTED,
            justify="left",
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", pady=(6, 10))
        self.var = tk.StringVar(value=current)
        entry = tk.Entry(
            body,
            textvariable=self.var,
            show="*",
            bg=INPUT,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            width=52,
            font=("Consolas", 10),
        )
        entry.pack(fill="x", ipady=6)
        entry.focus_set()
        entry.bind("<Return>", lambda _event: self._save())
        buttons = tk.Frame(body, bg=BG)
        buttons.pack(fill="x", pady=(12, 0))
        tk.Button(
            buttons,
            text="打开申请页面",
            command=lambda: webbrowser.open("https://console.curseforge.com/"),
            bg=CARD_ALT,
            fg=TEXT,
            activebackground="#26303d",
            activeforeground=TEXT,
            relief="flat",
            padx=10,
            pady=6,
            cursor="hand2",
        ).pack(side="left")
        tk.Button(
            buttons,
            text="保存",
            command=self._save,
            bg=ACCENT,
            fg=ACCENT_DARK,
            activebackground=ACCENT_HOVER,
            activeforeground=ACCENT_DARK,
            relief="flat",
            padx=16,
            pady=6,
            cursor="hand2",
            font=("Microsoft YaHei UI", 9, "bold"),
        ).pack(side="right")
        tk.Button(
            buttons,
            text="取消",
            command=self._cancel,
            bg=CARD_ALT,
            fg=TEXT,
            activebackground="#26303d",
            activeforeground=TEXT,
            relief="flat",
            padx=12,
            pady=6,
            cursor="hand2",
        ).pack(side="right", padx=(0, 8))
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.grab_set()

    def _save(self) -> None:
        self.result = self.var.get().strip()
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


class ContentTab(tk.Frame):
    def __init__(self, master: tk.Misc, app) -> None:
        super().__init__(master, bg=BG, padx=16, pady=12)
        self.app = app
        self.font = app._font_family
        self._closed = False
        self._queue: queue.Queue[tuple[str, tuple]] = queue.Queue()
        self._busy = False
        self._installing = False
        self._search_gen = 0
        self._version_gen = 0
        self._install_gen = 0
        self._inspect_gen = 0
        self._search_after: str | None = None
        self._install_cancel: threading.Event | None = None
        self._install_pause: PauseToken | None = None
        self._projects: list[ContentProject] = []
        self._project_map: dict[str, ContentProject] = {}
        self._versions: list[ContentVersion] = []
        self._selected_project: ContentProject | None = None
        self._selected_version: ContentVersion | None = None
        self._sources: dict[tuple[str, str], object] = {}
        self._tags_loaded: set[str] = set()
        self._tags_loading: set[str] = set()
        self._game_versions: dict[str, list] = {}
        self._category_cache: dict[tuple[str, str], list] = {}
        self._category_loading: set[tuple[str, str]] = set()
        self._category_loaded: set[tuple[str, str]] = set()
        self._dep_titles: dict[str, str] = {}
        self._target_override: Path | None = None
        self._world_map: dict[str, Path] = {}
        self._installed_ids: set[str] = set()
        self._version_autoset = False
        self._build_variables()
        self._build_ui()
        self.after(80, self._poll)
        self.after(220, self._initial_refresh)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _label(self, parent: tk.Misc, text: str = "", **kwargs) -> tk.Label:
        options = {"bg": CARD, "fg": MUTED, "font": (self.font, 9)}
        options.update(kwargs)
        return tk.Label(parent, text=text, **options)

    def _entry(
        self, parent: tk.Misc, variable: tk.StringVar, *, state: str = "normal"
    ) -> tk.Entry:
        return tk.Entry(
            parent,
            textvariable=variable,
            state=state,
            readonlybackground=INPUT,
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
            font=(self.font, 10),
        )

    def _button(
        self, parent: tk.Misc, text: str, command, *, primary: bool = False, width: int | None = None
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
            padx=12,
            pady=6,
            cursor="hand2",
            font=(self.font, 9, "bold" if primary else "normal"),
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

    def _combo(
        self,
        parent: tk.Misc,
        variable: tk.StringVar,
        values,
        *,
        state: str = "readonly",
        width: int | None = None,
    ) -> ttk.Combobox:
        combo = ttk.Combobox(
            parent,
            textvariable=variable,
            values=list(values),
            state=state,
            style="Dark.TCombobox",
            font=(self.font, 10),
        )
        if width:
            combo.configure(width=width)
        return combo

    def _build_variables(self) -> None:
        configured_type = str(self.app._config.get("content_type", "Mod"))
        if configured_type not in TYPE_BY_LABEL:
            configured_type = "Mod"
        configured_source = str(self.app._config.get("content_source", "Modrinth"))
        if configured_source not in SOURCE_BY_LABEL:
            configured_source = "Modrinth"
        self.type_var = tk.StringVar(value=configured_type)
        self.source_var = tk.StringVar(value=configured_source)
        self.query_var = tk.StringVar()
        self.category_var = tk.StringVar(value=ALL)
        self.game_version_var = tk.StringVar(value=ALL)
        self.loader_var = tk.StringVar(value=ALL)
        self.sort_var = tk.StringVar(value="相关度")
        self.status_var = tk.StringVar(value="输入关键词，或留空后搜索全部内容")
        self.hint_var = tk.StringVar(
            value="Modrinth 公共 API，无需 Key；搜索在服务端按类型/版本/加载器筛选。"
        )
        self.target_var = tk.StringVar(value="—")
        self.world_var = tk.StringVar(value="（没有找到世界）")
        self.title_var = tk.StringVar(value="尚未选择项目")
        self.desc_var = tk.StringVar(
            value="在结果表中选择一个项目，即可查看版本、依赖和安装位置。"
        )
        self.meta_var = tk.StringVar(value="")
        self.deps_var = tk.StringVar(value="")
        self.progress_var = tk.StringVar(value="")
        self.version_var = tk.StringVar()
        preferred = ""
        if self.app.selected_version is not None:
            preferred = self.app.selected_version.version_id
        if preferred:
            self.game_version_var.set(preferred)
            self._version_autoset = True

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=3, minsize=130)
        self.grid_rowconfigure(3, weight=1, minsize=82)
        self._build_filters()
        self._build_results()
        self._build_detail()
        self._build_log()

    def _build_filters(self) -> None:
        card = tk.Frame(
            self, bg=CARD, highlightbackground=BORDER, highlightthickness=1
        )
        card.grid(row=0, column=0, sticky="ew")
        body = tk.Frame(card, bg=CARD, padx=14, pady=8)
        body.pack(fill="x")
        for column, weight in ((1, 2), (3, 1), (5, 1), (7, 1)):
            body.grid_columnconfigure(column, weight=weight)
        body.grid_columnconfigure(8, weight=1)

        self._label(body, "内容类型").grid(
            row=0, column=0, sticky="w", padx=(0, 8), pady=2
        )
        self.type_combo = self._combo(
            body,
            self.type_var,
            [TYPE_LABELS[key] for key in CONTENT_TYPES],
            width=10,
        )
        self.type_combo.grid(row=0, column=1, sticky="ew", padx=(0, 12), pady=2)
        self.type_combo.bind("<<ComboboxSelected>>", self._on_type_change)

        self._label(body, "来源").grid(row=0, column=2, sticky="w", padx=(0, 8), pady=2)
        self.source_combo = self._combo(
            body, self.source_var, list(SOURCE_BY_LABEL), width=12
        )
        self.source_combo.grid(row=0, column=3, sticky="ew", padx=(0, 12), pady=2)
        self.source_combo.bind("<<ComboboxSelected>>", self._on_source_change)

        self._label(body, "搜索").grid(row=0, column=4, sticky="w", padx=(0, 8), pady=2)
        self.search_entry = self._entry(body, self.query_var)
        self.search_entry.grid(
            row=0, column=5, columnspan=3, sticky="ew", ipady=4, pady=2
        )
        self.search_entry.bind("<Return>", self._on_search_enter)
        self.search_entry.bind("<KeyRelease>", self._on_query_key)
        self.search_button = self._button(
            body, "搜索", self._search, primary=True, width=8
        )
        self.search_button.grid(row=0, column=8, sticky="e", padx=(10, 0), pady=2)

        self._label(body, "分类").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=2)
        self.category_combo = self._combo(
            body, self.category_var, [ALL], width=14
        )
        self.category_combo.grid(row=1, column=1, sticky="ew", padx=(0, 12), pady=2)
        self.category_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._schedule_search(50)
        )

        self._label(body, "游戏版本").grid(
            row=1, column=2, sticky="w", padx=(0, 8), pady=2
        )
        self.game_version_combo = self._combo(
            body, self.game_version_var, [ALL], state="normal", width=12
        )
        self.game_version_combo.grid(row=1, column=3, sticky="ew", padx=(0, 12), pady=2)

        self._label(body, "加载器").grid(
            row=1, column=4, sticky="w", padx=(0, 8), pady=2
        )
        self.loader_combo = self._combo(
            body, self.loader_var, ["全部", "Fabric", "Forge", "NeoForge", "Quilt"], width=10
        )
        self.loader_combo.grid(row=1, column=5, sticky="ew", padx=(0, 12), pady=2)

        self._label(body, "排序").grid(row=1, column=6, sticky="w", padx=(0, 8), pady=2)
        self.sort_combo = self._combo(
            body, self.sort_var, [label for _key, label in SORT_OPTIONS], width=9
        )
        self.sort_combo.grid(row=1, column=7, sticky="ew", padx=(0, 12), pady=2)
        self.sort_combo.bind("<<ComboboxSelected>>", lambda _event: self._schedule_search(50))

        self.refresh_button = self._button(
            body, "刷新筛选", self._search, width=9
        )
        self.refresh_button.grid(row=1, column=8, sticky="e", pady=2)

        self._label(
            body,
            "",
            textvariable=self.hint_var,
            fg=MUTED,
            font=(self.font, 8),
            anchor="w",
            justify="left",
        ).grid(row=2, column=0, columnspan=8, sticky="w", pady=(5, 0))
        self.key_button = self._button(
            body, "CurseForge Key…", self._configure_key, width=15
        )
        self.key_button.grid(row=2, column=8, sticky="e", pady=(5, 0))

    def _build_results(self) -> None:
        card = tk.Frame(
            self, bg=CARD, highlightbackground=BORDER, highlightthickness=1
        )
        card.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        card.grid_rowconfigure(1, weight=1)
        card.grid_columnconfigure(0, weight=1)
        header = tk.Frame(card, bg=CARD)
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 4))
        header.grid_columnconfigure(0, weight=1)
        self.result_count_var = tk.StringVar(value="搜索结果")
        tk.Label(
            header,
            textvariable=self.result_count_var,
            bg=CARD,
            fg=TEXT,
            font=(self.font, 10, "bold"),
        ).grid(row=0, column=0, sticky="w")
        self._label(
            header,
            "选中项目后在下方查看版本；双击可直接打开项目页面",
            font=(self.font, 8),
        ).grid(row=0, column=1, sticky="e")
        self.archive_button = self._button(
            header, "从压缩包安装…", self._install_from_archive, width=16
        )
        self.archive_button.grid(row=0, column=2, sticky="e", padx=(10, 0))

        frame = tk.Frame(card, bg=INPUT)
        frame.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 10))
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            frame,
            columns=("title", "author", "type", "categories", "downloads", "updated", "installed"),
            show="headings",
            height=6,
            selectmode="browse",
            style="Dark.Treeview",
        )
        headings = {
            "title": ("名称", 250, "w"),
            "author": ("作者", 100, "w"),
            "type": ("类型", 70, "center"),
            "categories": ("分类", 170, "w"),
            "downloads": ("下载量", 90, "e"),
            "updated": ("最近更新", 90, "center"),
            "installed": ("已安装", 70, "center"),
        }
        for key, (text, width, anchor) in headings.items():
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor=anchor, stretch=key in ("title", "categories"))
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self._on_project_select)
        self.tree.bind("<Double-1>", self._on_project_open)
        scroll = tk.Scrollbar(
            frame,
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

    def _build_detail(self) -> None:
        card = tk.Frame(
            self, bg=CARD, highlightbackground=BORDER, highlightthickness=1
        )
        card.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        body = tk.Frame(card, bg=CARD, padx=14, pady=6)
        body.pack(fill="x")
        body.grid_columnconfigure(1, weight=1)

        tk.Label(
            body,
            textvariable=self.title_var,
            bg=CARD,
            fg=TEXT,
            font=(self.font, 12, "bold"),
            anchor="w",
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        self.link_button = self._button(
            body, "项目页面", self._open_project_page, width=9
        )
        self.link_button.grid(row=0, column=2, sticky="e", padx=(8, 0))
        self.copy_link_button = self._button(
            body, "复制链接", self._copy_project_link, width=9
        )
        self.copy_link_button.grid(row=0, column=3, sticky="e", padx=(8, 0))
        self.link_button.configure(state="disabled")
        self.copy_link_button.configure(state="disabled")

        tk.Label(
            body,
            textvariable=self.desc_var,
            bg=CARD,
            fg=MUTED,
            font=(self.font, 9),
            justify="left",
            anchor="w",
            wraplength=920,
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(3, 2))
        self._label(
            body,
            "",
            textvariable=self.meta_var,
            fg="#a5b4c4",
            font=(self.font, 8),
            anchor="w",
            justify="left",
            wraplength=920,
        ).grid(row=2, column=0, columnspan=4, sticky="w")

        self._label(body, "版本").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        self.version_combo = self._combo(body, self.version_var, [])
        self.version_combo.grid(
            row=3, column=1, columnspan=3, sticky="ew", pady=(4, 0)
        )
        self.version_combo.bind("<<ComboboxSelected>>", self._on_version_change)
        self.version_combo.configure(state="disabled")

        self.deps_label = self._label(
            body,
            "",
            textvariable=self.deps_var,
            fg=BLUE,
            font=(self.font, 8),
            anchor="w",
            justify="left",
            wraplength=920,
        )
        self.deps_label.grid(row=4, column=0, columnspan=4, sticky="w", pady=(2, 0))
        self.deps_label.grid_remove()

        self._label(body, "安装到").grid(
            row=5, column=0, sticky="w", padx=(0, 8), pady=(6, 0)
        )
        self.target_entry = self._entry(body, self.target_var, state="readonly")
        self.target_entry.grid(
            row=5, column=1, columnspan=2, sticky="ew", ipady=4, pady=(4, 0)
        )
        self.browse_target_button = self._button(
            body, "浏览…", self._browse_target, width=8
        )
        self.browse_target_button.grid(
            row=5, column=3, sticky="e", padx=(8, 0), pady=(4, 0)
        )

        self.world_label = self._label(body, "世界")
        self.world_label.grid(row=6, column=0, sticky="w", padx=(0, 8), pady=(4, 0))
        self.world_combo = self._combo(body, self.world_var, ["（没有找到世界）"])
        self.world_combo.grid(
            row=6, column=1, columnspan=2, sticky="ew", pady=(2, 0)
        )
        self.world_combo.bind("<<ComboboxSelected>>", self._on_world_change)
        self.world_browse_button = self._button(
            body, "浏览世界…", self._browse_world, width=10
        )
        self.world_browse_button.grid(
            row=6, column=3, sticky="e", padx=(8, 0), pady=(4, 0)
        )

        actions = tk.Frame(body, bg=CARD)
        actions.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        actions.grid_columnconfigure(0, weight=1)
        self.status_label = self._label(
            actions,
            "",
            textvariable=self.status_var,
            fg=MUTED,
            font=(self.font, 9),
            anchor="w",
        )
        self.status_label.grid(row=0, column=0, sticky="ew")
        self.pause_button = self._button(
            actions, "暂停", self._toggle_pause, width=8
        )
        self.pause_button.grid(row=0, column=1, padx=(8, 0))
        self.pause_button.configure(state="disabled")
        self.cancel_button = self._button(
            actions, "取消", self._cancel_install, width=8
        )
        self.cancel_button.grid(row=0, column=2, padx=(8, 0))
        self.cancel_button.configure(state="disabled")
        self.install_button = self._button(
            actions, "下载并安装", self._install, primary=True, width=13
        )
        self.install_button.grid(row=0, column=3, padx=(8, 0))
        self.install_button.configure(state="disabled")
        self.enqueue_button = self._button(
            actions, "加入队列", self._enqueue_selected, width=10
        )
        self.enqueue_button.grid(row=0, column=4, padx=(8, 0))
        self.enqueue_button.configure(state="disabled")

        self.progress_bar = ttk.Progressbar(
            actions,
            style="Accent.Horizontal.TProgressbar",
            maximum=1000,
            value=0,
        )
        self.progress_bar.grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=(5, 0)
        )
        self.progress_label = self._label(
            actions,
            "",
            textvariable=self.progress_var,
            fg=MUTED,
            font=(self.font, 8),
            anchor="e",
        )
        self.progress_label.grid(row=1, column=2, sticky="e", padx=(8, 0), pady=(5, 0))
        # Keep the detail card compact until a download actually starts.
        self.progress_bar.grid_remove()
        self.progress_label.grid_remove()

    def _build_log(self) -> None:
        card = tk.Frame(
            self, bg=CARD, highlightbackground=BORDER, highlightthickness=1
        )
        card.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        card.grid_rowconfigure(1, weight=1)
        card.grid_columnconfigure(0, weight=1)
        header = tk.Frame(card, bg=CARD)
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(7, 3))
        header.grid_columnconfigure(0, weight=1)
        tk.Label(
            header,
            text="内容日志",
            bg=CARD,
            fg=TEXT,
            font=(self.font, 10, "bold"),
        ).grid(row=0, column=0, sticky="w")
        self._button(header, "清空", self._clear_log, width=7).grid(
            row=0, column=1, sticky="e"
        )
        frame = tk.Frame(card, bg=INPUT)
        frame.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 8))
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)
        self.log_text = tk.Text(
            frame,
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
            pady=6,
            state="disabled",
            height=4,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = tk.Scrollbar(
            frame,
            command=self.log_text.yview,
            bg=CARD_ALT,
            troughcolor=INPUT,
            activebackground=BORDER,
            relief="flat",
            bd=0,
        )
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)
        for tag, color in (
            ("info", "#c9d1d9"),
            ("muted", "#6e7681"),
            ("success", ACCENT),
            ("warning", YELLOW),
            ("error", RED),
        ):
            self.log_text.tag_configure(tag, foreground=color)

    # ------------------------------------------------------------------
    # Queue and small UI helpers
    # ------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._busy

    def _post(self, event: str, *payload) -> None:
        self._queue.put((event, payload))

    def _poll(self) -> None:
        if self._closed:
            return
        try:
            while True:
                event, payload = self._queue.get_nowait()
                handler = getattr(self, f"_on_{event}", None)
                if handler is not None:
                    try:
                        handler(*payload)
                    except Exception as exc:  # keep the tab alive
                        self._log(f"界面事件处理失败：{exc}", "error")
        except queue.Empty:
            pass
        if not self._closed:
            try:
                self.after(80, self._poll)
            except tk.TclError:
                pass

    def _thread_log(self, message: str) -> None:
        self._post("log", str(message), "muted")

    def _on_log(self, message: str, tag: str = "muted") -> None:
        self._log(message, tag)

    def _log(self, message: str, tag: str = "info") -> None:
        if self._closed:
            return
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] ", "muted")
        self.log_text.insert("end", str(message).rstrip() + "\n", tag)
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 2000:
            self.log_text.delete("1.0", "1000.0")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _status_label_update(self, text: str, color: str = MUTED) -> None:
        self.status_var.set(text)
        if hasattr(self, "status_label"):
            self.status_label.configure(fg=color)

    def _show_error(self, title: str, message: str) -> None:
        messagebox.showerror(title, message, parent=self)

    def _set_busy(self, busy: bool, status: str | None = None) -> None:
        self._busy = busy
        self.search_button.configure(state="disabled" if busy else "normal")
        self.refresh_button.configure(state="disabled" if busy else "normal")
        self.type_combo.configure(state="disabled" if busy else "readonly")
        self.source_combo.configure(state="disabled" if busy else "readonly")
        if hasattr(self, "archive_button"):
            self.archive_button.configure(state="disabled" if busy else "normal")
        self.cancel_button.configure(
            state="normal" if busy and self._installing else "disabled"
        )
        if status:
            self._status_label_update(status, BLUE if busy else MUTED)
        self._update_install_state()

    # ------------------------------------------------------------------
    # Context / filters
    # ------------------------------------------------------------------

    def _type_id(self) -> str:
        return TYPE_BY_LABEL.get(self.type_var.get(), "mod")

    def _source_id(self) -> str:
        return SOURCE_BY_LABEL.get(self.source_var.get(), SOURCE_MODRINTH)

    def _game_version_filter(self) -> str:
        value = self.game_version_var.get().strip()
        return "" if value in ("", ALL) else value

    def _loader_filter(self) -> str:
        return LOADER_BY_LABEL.get(self.loader_var.get(), "")

    def _game_dir(self) -> Path:
        text = self.app.game_dir_var.get().strip()
        if not text:
            raise ContentError("请先在“启动游戏”页选择游戏目录。")
        root = normalize_game_root(text)
        if self.app.isolation_var.get() and self.app.selected_version is not None:
            return version_game_dir(self.app.selected_version)
        return root.root

    def _game_root_dir(self) -> Path:
        """The shared root used for new modpack instances.

        Modpacks must not be nested inside the currently selected version's
        isolated game directory, otherwise the new instance would be invisible
        to the version scanner and would share the wrong data.
        """
        text = self.app.game_dir_var.get().strip()
        if not text:
            raise ContentError("请先在“启动游戏”页选择游戏目录。")
        return normalize_game_root(text).root

    def _world_path(self) -> Path:
        name = self.world_var.get()
        path = self._world_map.get(name)
        if path is None:
            raise ContentError("安装数据包前必须选择世界文件夹（包含 level.dat）。")
        return validate_world_dir(path)

    def _target_dir(self) -> Path:
        content_type = self._type_id()
        game_dir = self._game_dir()
        if content_type == "datapack":
            return self._world_path() / "datapacks"
        if content_type == "modpack":
            return self._game_root_dir() / "versions"
        if self._target_override is not None:
            return self._target_override
        return default_target_dir(game_dir, content_type)

    def _target_text(self) -> str:
        try:
            content_type = self._type_id()
            if content_type == "modpack":
                project = self._selected_project
                version = self._selected_version
                root = self._game_root_dir()
                if project is None or version is None:
                    return str(root / "versions" / "<项目>-<版本>")
                name = content_instance_name(project.slug, version.version_number)
                return str(root / "versions" / name)
            return str(self._target_dir())
        except LauncherError as exc:
            return f"（{exc}）"

    def _update_target_text(self) -> None:
        self.target_var.set(self._target_text())

    def _refresh_installed(self) -> None:
        try:
            game_dir = self._game_dir()
        except LauncherError:
            self._installed_ids = set()
            return
        self._installed_ids = {
            str(entry.get("project_id"))
            for entry in read_content_sidecar(game_dir)
            if entry.get("project_id")
        }

    def _refresh_worlds(self) -> None:
        try:
            game_dir = self._game_dir()
            worlds = list_worlds(game_dir)
        except LauncherError:
            worlds = []
        self._world_map = {item.name: item.path for item in worlds}
        values = [item.name for item in worlds] or ["（没有找到世界）"]
        self.world_combo.configure(values=values)
        if self.world_var.get() not in self._world_map:
            self.world_var.set(values[0])

    def refresh_context(self) -> None:
        if self._closed:
            return
        if not self._version_autoset and self.app.selected_version is not None:
            if self.game_version_var.get() in ("", ALL):
                # A selected modpack/loader version id is not a game version;
                # follow inheritsFrom to the underlying vanilla version (e.g.
                # fabulously-optimized-14.1.1 -> 26.2).
                game_version = self.app.selected_version.version_id
                if self.app.root_info is not None:
                    game_version = base_game_version(
                        self.app.root_info, self.app.selected_version
                    )
                self.game_version_var.set(game_version)
                self._version_autoset = True
        self._refresh_installed()
        self._refresh_worlds()
        self._update_target_text()
        self._update_install_state()

    def _initial_refresh(self) -> None:
        self.refresh_context()
        self._sync_type_controls()
        self._ensure_tags()
        self._ensure_categories()
        self._update_hint()

    def _on_query_key(self, event) -> None:
        if event.keysym in ("Return", "Escape", "Up", "Down", "Tab"):
            return
        self._schedule_search(350)

    def _on_search_enter(self, _event=None) -> str:
        if self._search_after is not None:
            try:
                self.after_cancel(self._search_after)
            except tk.TclError:
                pass
            self._search_after = None
        self._search()
        return "break"

    def _schedule_search(self, delay: int = 350) -> None:
        if self._closed:
            return
        if self._search_after is not None:
            try:
                self.after_cancel(self._search_after)
            except tk.TclError:
                pass
        self._search_after = self.after(delay, self._search)

    def _sync_type_controls(self) -> None:
        content_type = self._type_id()
        if content_type in ("mod", "modpack"):
            values = ["全部", "Fabric", "Forge", "NeoForge", "Quilt"]
            self.loader_combo.configure(values=values, state="readonly")
            if self.loader_var.get() not in values:
                self.loader_var.set("全部")
        elif content_type == "datapack":
            self.loader_combo.configure(values=["datapack（固定）"], state="disabled")
            self.loader_var.set("datapack（固定）")
        else:
            self.loader_combo.configure(values=["不适用"], state="disabled")
            self.loader_var.set("不适用")
        if content_type == "datapack":
            self.world_label.grid()
            self.world_combo.grid()
            self.world_browse_button.grid()
            self._refresh_worlds()
        else:
            self.world_label.grid_remove()
            self.world_combo.grid_remove()
            self.world_browse_button.grid_remove()
        self.browse_target_button.configure(
            state="disabled" if content_type in ("modpack", "datapack") else "normal"
        )
        self._update_target_text()
        self._update_install_state()

    def _on_type_change(self, _event=None) -> None:
        self._target_override = None
        self._sync_type_controls()
        self._update_hint()
        self._ensure_categories()
        self._schedule_search(60)

    def _on_source_change(self, _event=None) -> None:
        self._update_hint()
        self._ensure_tags()
        self._ensure_categories()
        self._schedule_search(60)

    def _on_world_change(self, _event=None) -> None:
        self._update_target_text()
        self._update_install_state()

    def _on_tab_changed(self) -> None:
        self.refresh_context()

    def on_tab_shown(self) -> None:
        """Called by the main window when this tab becomes visible."""
        self.refresh_context()

    def _update_hint(self) -> None:
        source_id = self._source_id()
        content_type = self._type_id()
        if source_id == SOURCE_CURSEFORGE:
            key = str(self.app._config.get("curseforge_api_key", "")).strip()
            if key:
                source_hint = "CurseForge 已配置 API Key；下载链接可能由作者禁止第三方分发。"
            else:
                source_hint = "CurseForge 需要 API Key（本机保存）；不配置时请使用 Modrinth。"
        else:
            source_hint = "Modrinth 公共 API，无需 Key；搜索在服务端按类型/版本/加载器筛选。"
        type_hints = {
            "mod": "Mod 安装到 mods/；需要与游戏版本和加载器匹配。",
            "modpack": "整合包会安装为隔离版本：自动安装对应游戏 + Fabric/Forge，版本目录本身就是游戏目录（mods/config/saves 与版本 JSON 同级）；也可从本地 .mrpack/ZIP 安装。",
            "datapack": "数据包安装到所选世界的 datapacks/；请先选择包含 level.dat 的世界。",
            "resourcepack": "资源包安装到 resourcepacks/。",
            "shader": "光影包安装到 shaderpacks/（通常还需要 Iris/OptiFine）。",
        }
        self.hint_var.set(f"{source_hint}  {type_hints.get(content_type, '')}")

    # ------------------------------------------------------------------
    # Sources, tags, categories
    # ------------------------------------------------------------------

    def _source(self):
        source_id = self._source_id()
        key = str(self.app._config.get("curseforge_api_key", "")).strip()
        cache_key = (source_id, key)
        if cache_key not in self._sources:
            try:
                self._sources[cache_key] = get_content_source(
                    source_id,
                    api_key=key,
                    logger=self._thread_log,
                    segments=self.app._segments_value(),
                )
            except LauncherError as exc:
                raise ContentError(str(exc)) from exc
        return self._sources[cache_key]

    def _ensure_tags(self) -> None:
        source_id = self._source_id()
        if source_id in self._tags_loaded or source_id in self._tags_loading:
            return
        if source_id == SOURCE_CURSEFORGE and not str(
            self.app._config.get("curseforge_api_key", "")
        ).strip():
            return
        try:
            source = self._source()
        except LauncherError as exc:
            self._log(str(exc), "warning")
            return
        self._tags_loading.add(source_id)

        def worker() -> None:
            try:
                versions = source.game_versions()
            except Exception as exc:  # noqa: BLE001 - reported in the UI
                self._post("tags_failed", source_id, str(exc))
            else:
                self._post("tags_done", source_id, versions)

        threading.Thread(target=worker, daemon=True, name="content-tags").start()

    def _on_tags_done(self, source_id: str, versions) -> None:
        self._tags_loading.discard(source_id)
        self._tags_loaded.add(source_id)
        self._game_versions[source_id] = list(versions)
        if source_id != self._source_id():
            return
        values = [ALL] + [item.version for item in versions]
        current = self.game_version_var.get()
        self.game_version_combo.configure(values=values)
        if current not in values:
            self.game_version_var.set(ALL)
        self._log(f"已加载 {len(versions)} 个游戏版本供筛选。", "muted")

    def _on_tags_failed(self, source_id: str, message: str) -> None:
        self._tags_loading.discard(source_id)
        self._log(f"游戏版本列表加载失败：{message}", "warning")

    def _ensure_categories(self) -> None:
        source_id = self._source_id()
        content_type = self._type_id()
        key = (source_id, content_type)
        if key in self._category_loaded or key in self._category_loading:
            return
        if source_id == SOURCE_CURSEFORGE and not str(
            self.app._config.get("curseforge_api_key", "")
        ).strip():
            return
        try:
            source = self._source()
        except LauncherError as exc:
            self._log(str(exc), "warning")
            return
        self._category_loading.add(key)

        def worker() -> None:
            try:
                categories = source.categories(content_type)
            except Exception as exc:  # noqa: BLE001
                self._post("categories_failed", key, str(exc))
            else:
                self._post("categories_done", key, categories)

        threading.Thread(
            target=worker, daemon=True, name="content-categories"
        ).start()

    def _on_categories_done(self, key, categories) -> None:
        self._category_loading.discard(key)
        self._category_loaded.add(key)
        self._category_cache[key] = list(categories)
        if key != (self._source_id(), self._type_id()):
            return
        values = [ALL] + [item.name for item in categories]
        self.category_combo.configure(values=values)
        if self.category_var.get() not in values:
            self.category_var.set(ALL)

    def _on_categories_failed(self, key, message: str) -> None:
        self._category_loading.discard(key)
        self._log(f"分类列表加载失败：{message}", "warning")

    def _configure_key(self) -> None:
        current = str(self.app._config.get("curseforge_api_key", ""))
        dialog = _CurseForgeKeyDialog(self, current)
        try:
            self.wait_window(dialog)
        except tk.TclError:
            return
        if dialog.result is None:
            return
        self.app._config["curseforge_api_key"] = dialog.result
        try:
            self.app._save_config()
        except Exception:
            pass
        # Force the next request to use the new key.
        self._sources = {
            key: value for key, value in self._sources.items() if key[0] != SOURCE_CURSEFORGE
        }
        self._tags_loaded.discard(SOURCE_CURSEFORGE)
        self._tags_loading.discard(SOURCE_CURSEFORGE)
        self._category_loaded = {
            key for key in self._category_loaded if key[0] != SOURCE_CURSEFORGE
        }
        self._category_loading = {
            key for key in self._category_loading if key[0] != SOURCE_CURSEFORGE
        }
        if dialog.result:
            self._log("CurseForge API Key 已保存到本机配置。", "success")
        else:
            self._log("已清空 CurseForge API Key。", "warning")
        self._update_hint()
        self._ensure_tags()
        self._ensure_categories()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _search(self) -> None:
        self._search_after = None
        if self._closed or self._busy:
            return
        source_id = self._source_id()
        key = str(self.app._config.get("curseforge_api_key", "")).strip()
        if source_id == SOURCE_CURSEFORGE and not key:
            self._status_label_update("CurseForge 需要 API Key", YELLOW)
            if messagebox.askyesno(
                "需要 CurseForge API Key",
                "CurseForge 需要 API Key 才能搜索和下载。\n"
                "是否现在配置？不配置时仍可使用 Modrinth。",
                parent=self,
            ):
                self._configure_key()
            return
        try:
            source = self._source()
        except LauncherError as exc:
            self._show_error("内容来源不可用", str(exc))
            return
        self._search_gen += 1
        generation = self._search_gen
        self._set_busy(True, "正在搜索…")
        self.result_count_var.set("搜索结果（加载中…）")
        query = self.query_var.get().strip()
        content_type = self._type_id()
        game_version = self._game_version_filter()
        loader = self._loader_filter()
        category = self.category_var.get()
        if category == ALL:
            category = ""
        sort = SORT_BY_LABEL.get(self.sort_var.get(), "relevance")

        def worker() -> None:
            try:
                result = source.search(
                    query,
                    content_type,
                    game_version=game_version,
                    loader=loader,
                    category=category,
                    sort=sort,
                    offset=0,
                    limit=40,
                )
            except Exception as exc:  # noqa: BLE001
                self._post("search_failed", generation, str(exc))
            else:
                self._post("search_done", generation, result)

        threading.Thread(target=worker, daemon=True, name="content-search").start()

    def _on_search_done(self, generation: int, result: ContentSearchResult) -> None:
        if generation != self._search_gen:
            return
        self._projects = list(result.projects)
        self._project_map = {}
        self._selected_project = None
        self._selected_version = None
        self._versions = []
        self.tree.delete(*self.tree.get_children())
        for index, project in enumerate(result.projects):
            iid = f"p{index}"
            installed = "已安装" if project.id in self._installed_ids else ""
            values = (
                project.title[:100],
                project.author[:40],
                content_type_label(self._type_id()),
                project.category_text[:80],
                f"{project.downloads:,}",
                project.date_text,
                installed,
            )
            self.tree.insert(
                "", "end", iid=iid, values=values,
                tags=("installed",) if installed else (),
            )
            self._project_map[iid] = project
        self.result_count_var.set(
            f"搜索结果：共 {result.total:,} 个，显示 {len(result.projects)} 个"
        )
        self._status_label_update(
            f"搜索完成：{len(result.projects)} 个结果" if result.projects else "没有找到匹配内容",
            MUTED,
        )
        self.progress_bar.configure(value=0)
        self.progress_var.set("")
        self._set_busy(False)
        if result.projects:
            self._log(
                f"搜索“{self.query_var.get().strip() or '（空）'}”："
                f"{result.total:,} 个结果，显示 {len(result.projects)} 个。",
                "success",
            )
            first = self.tree.get_children()
            if first:
                self.tree.selection_set(first[0])
                self._on_project_select()
        else:
            self._log("没有找到匹配内容；可尝试清空筛选、切换来源或更换关键词。", "warning")
            self._selected_project = None
            self._selected_version = None
            self.title_var.set("没有匹配内容")
            self.desc_var.set("尝试清空分类/版本/加载器筛选，或切换来源。")
            self.meta_var.set("")
            self.deps_var.set("")
            self.version_combo.configure(values=[], state="disabled")
            self.version_var.set("")
            self._update_install_state()

    def _on_search_failed(self, generation: int, message: str) -> None:
        if generation != self._search_gen:
            return
        self._set_busy(False)
        self.result_count_var.set("搜索失败")
        self._status_label_update("搜索失败", RED)
        self._log(f"搜索失败：{message}", "error")
        self._show_error(
            "内容搜索失败",
            f"{message}\n\n可尝试：切换来源、清空筛选、检查网络，"
            "或稍后重试。",
        )

    # ------------------------------------------------------------------
    # Project / versions
    # ------------------------------------------------------------------

    def _on_project_select(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        project = self._project_map.get(selection[0])
        if project is None:
            return
        if self._selected_project is not None and self._selected_project.id == project.id:
            return
        self._selected_project = project
        self._selected_version = None
        self._versions = []
        self.title_var.set(project.title)
        self.desc_var.set(project.description or "（无简介）")
        self.meta_var.set(
            f"{content_type_label(self._type_id())} · 作者 {project.author} · "
            f"分类 {project.category_text or '—'} · 下载 {project.downloads:,} · "
            f"更新 {project.date_text}"
        )
        self.deps_var.set("")
        self.version_combo.configure(values=[], state="disabled")
        self.version_var.set("")
        self.link_button.configure(state="normal")
        self.copy_link_button.configure(state="normal")
        self._target_override = None
        self._update_target_text()
        self._update_install_state()
        self._load_versions(project)

    def _on_project_open(self, _event=None) -> None:
        project = self._selected_project
        if project is not None and project.page_url:
            webbrowser.open(project.page_url)

    def _open_project_page(self) -> None:
        project = self._selected_project
        if project and project.page_url:
            webbrowser.open(project.page_url)

    def _copy_project_link(self) -> None:
        project = self._selected_project
        if project and project.page_url:
            self.clipboard_clear()
            self.clipboard_append(project.page_url)
            self._status_label_update("项目链接已复制", ACCENT)

    def _load_versions(self, project: ContentProject) -> None:
        try:
            source = self._source()
        except LauncherError as exc:
            self._show_error("内容来源不可用", str(exc))
            return
        self._version_gen += 1
        generation = self._version_gen
        game_version = self._game_version_filter()
        loader = self._loader_filter()
        self._set_busy(True, "正在获取版本列表…")

        def worker() -> None:
            try:
                versions = source.versions(
                    project.id, game_version=game_version, loader=loader
                )
            except Exception as exc:  # noqa: BLE001
                self._post("versions_failed", generation, str(exc))
            else:
                self._post("versions_done", generation, project.id, versions)

        threading.Thread(target=worker, daemon=True, name="content-versions").start()

    def _on_versions_done(self, generation: int, project_id: str, versions) -> None:
        if generation != self._version_gen:
            return
        if self._selected_project is None or self._selected_project.id != project_id:
            return
        self._set_busy(False)
        self._versions = list(versions)
        if not self._versions:
            self.version_combo.configure(values=[], state="disabled")
            self.version_var.set("")
            self._selected_version = None
            self._status_label_update("没有匹配的版本", YELLOW)
            self.meta_var.set(
                "该游戏版本/加载器下没有可用版本；可清空筛选或切换版本。"
            )
            self.deps_var.set("")
            self._update_install_state()
            return
        shown = self._versions[:300]
        labels = [self._version_label(item) for item in shown]
        self.version_combo.configure(values=labels, state="readonly")
        best = select_best_version(
            self._versions,
            game_version=self._game_version_filter() or None,
            loader=self._loader_filter() or None,
        )
        index = 0
        if best is not None:
            try:
                index = self._versions.index(best)
            except ValueError:
                index = 0
        if index >= len(shown):
            index = 0
        self.version_combo.current(index)
        self._selected_version = shown[index] if shown else None
        self._status_label_update(
            f"共 {len(self._versions)} 个版本"
            + (f"，仅显示最新 {len(shown)} 个" if len(shown) < len(self._versions) else ""),
            MUTED,
        )
        self._log(
            f"{self._selected_project.title}：{len(self._versions)} 个版本。",
            "muted",
        )
        self._update_version_detail()
        self._load_dependency_titles(generation, self._selected_version)

    def _on_versions_failed(self, generation: int, message: str) -> None:
        if generation != self._version_gen:
            return
        self._set_busy(False)
        self._status_label_update("版本列表获取失败", RED)
        self._log(f"版本列表获取失败：{message}", "error")
        self._show_error("版本列表获取失败", message)

    def _version_label(self, version: ContentVersion) -> str:
        return (
            f"{version.version_number}  [{version.type_label}]  "
            f"{version.game_version_text}  {version.loader_text}  {version.date_text}"
        )

    def _on_version_change(self, _event=None) -> None:
        index = self.version_combo.current()
        if 0 <= index < len(self._versions):
            self._selected_version = self._versions[index]
            self._update_version_detail()
            self._load_dependency_titles(self._version_gen, self._selected_version)

    def _update_version_detail(self) -> None:
        version = self._selected_version
        if version is None:
            self.meta_var.set("")
            self.deps_var.set("")
            self._update_install_state()
            return
        file = version.primary_file
        if file is None:
            self.meta_var.set("该版本没有可下载文件。")
        else:
            sha = f"{file.sha1[:12]}…" if file.sha1 else "缺失"
            self.meta_var.set(
                f"文件 {file.filename} · {human_size(file.size)} · "
                f"下载 {version.downloads:,} · 发布 {version.date_text} · "
                f"SHA-1 {sha}"
            )
        self._update_deps_label(version)
        self._update_target_text()
        self._update_install_state()

    def _update_deps_label(self, version: ContentVersion) -> None:
        if not version.dependencies:
            self.deps_var.set("")
            self.deps_label.grid_remove()
            return
        parts = []
        for dependency in version.dependencies[:8]:
            title = self._dep_titles.get(dependency.project_id, dependency.project_id)
            parts.append(f"{dependency.type_label} {title}")
        suffix = " …" if len(version.dependencies) > 8 else ""
        self.deps_var.set(
            "依赖：" + "，".join(parts) + suffix
            + "（只展示依赖，不自动递归下载）"
        )
        self.deps_label.grid()

    def _load_dependency_titles(self, generation: int, version: ContentVersion | None) -> None:
        if version is None or not version.dependencies:
            return
        if self._source_id() != SOURCE_MODRINTH:
            return
        ids = [
            dependency.project_id
            for dependency in version.dependencies[:12]
            if dependency.project_id not in self._dep_titles
        ]
        if not ids:
            return
        try:
            source = self._source()
        except LauncherError:
            return
        if not isinstance(source, ModrinthSource):
            return
        target_version = version

        def worker() -> None:
            try:
                projects = source.projects(ids)
            except Exception:
                return
            mapping = {project.id: project.title for project in projects}
            self._post("dep_titles_done", generation, mapping, target_version.id)

        threading.Thread(target=worker, daemon=True, name="content-deps").start()

    def _on_dep_titles_done(self, generation: int, mapping: dict, version_id: str) -> None:
        self._dep_titles.update(mapping)
        if (
            generation == self._version_gen
            and self._selected_version is not None
            and self._selected_version.id == version_id
        ):
            self._update_deps_label(self._selected_version)

    # ------------------------------------------------------------------
    # Target directory
    # ------------------------------------------------------------------

    def _browse_target(self) -> None:
        content_type = self._type_id()
        if content_type == "modpack":
            return
        initial = self._target_text()
        path = filedialog.askdirectory(
            parent=self,
            title="选择安装目录",
            initialdir=initial if Path(initial).is_dir() else str(Path.home()),
        )
        if not path:
            return
        self._target_override = Path(path)
        self._update_target_text()
        self._update_install_state()

    def _browse_world(self) -> None:
        path = filedialog.askdirectory(
            parent=self, title="选择 Minecraft 世界文件夹（包含 level.dat）"
        )
        if not path:
            return
        try:
            world = validate_world_dir(Path(path))
        except LauncherError as exc:
            self._show_error("世界目录无效", str(exc))
            return
        name = world.name
        base = name
        counter = 2
        while name in self._world_map and self._world_map[name] != world:
            name = f"{base} ({counter})"
            counter += 1
        self._world_map[name] = world
        self.world_combo.configure(values=list(self._world_map))
        self.world_var.set(name)
        self._update_target_text()
        self._update_install_state()

    def _update_install_state(self) -> None:
        if not hasattr(self, "install_button"):
            return
        enabled = (
            not self._busy
            and self._selected_project is not None
            and self._selected_version is not None
            and self._selected_version.primary_file is not None
        )
        if enabled:
            file = self._selected_version.primary_file
            if file is None or not file.url or not file.sha1:
                enabled = False
        if enabled and self._type_id() == "datapack":
            try:
                self._world_path()
            except LauncherError:
                enabled = False
        self.install_button.configure(state="normal" if enabled else "disabled")
        if hasattr(self, "enqueue_button"):
            self.enqueue_button.configure(state="normal" if enabled else "disabled")
        if not enabled:
            self.cancel_button.configure(state="disabled")

    # ------------------------------------------------------------------
    # Install
    # ------------------------------------------------------------------

    def _confirm(self, title: str, message: str) -> bool:
        return messagebox.askyesno(title, message, parent=self)

    def _install(self) -> None:
        if self._closed or self._busy:
            return
        project = self._selected_project
        version = self._selected_version
        if project is None or version is None:
            return
        file = version.primary_file
        if file is None:
            self._show_error("无法安装", "该版本没有可下载文件。")
            return
        try:
            game_root = self._game_root_dir()
        except LauncherError as exc:
            self._show_error("游戏目录不可用", str(exc))
            return
        content_type = self._type_id()
        label = content_type_label(content_type)
        if content_type == "modpack":
            instance = content_instance_name(project.slug, version.version_number)
            message = (
                f"项目：{project.title}\n"
                f"版本：{version.version_number}\n"
                f"文件：{file.filename}（{human_size(file.size)}）\n"
                f"游戏版本：{version.game_version_text or '以压缩包声明为准'}\n"
                f"加载器：{version.loader_text or '以压缩包声明为准'}\n"
                f"实例目录：{game_root / 'versions' / instance}\n\n"
                "将下载 .mrpack、校验 SHA-1，并自动安装对应的 Minecraft + "
                "Fabric/Forge/NeoForge/Quilt，创建独立版本；"
                "版本目录本身就是隔离游戏目录。\n\n"
                "是否继续？"
            )
            if not self._confirm("确认下载并安装整合包", message):
                return
            self._start_modpack_install(project, version, file, game_root)
            return

        world_dir: Path | None = None
        try:
            if content_type == "datapack":
                world_dir = self._world_path()
            target = self._target_dir()
            plan = build_content_plan(
                project,
                version,
                file,
                game_dir,
                content_type,
                target_dir=target,
                world_dir=world_dir,
            )
            if plan.dest.exists():
                if not self._confirm(
                    "文件已存在",
                    f"目标文件已存在：\n{plan.dest}\n\n确定覆盖吗？",
                ):
                    return
                plan = build_content_plan(
                    project,
                    version,
                    file,
                    game_dir,
                    content_type,
                    target_dir=target,
                    world_dir=world_dir,
                    overwrite=True,
                )
        except LauncherError as exc:
            self._show_error("无法安装", str(exc))
            return
        message = (
            f"项目：{project.title}\n"
            f"版本：{version.version_number}\n"
            f"类型：{label}\n"
            f"文件：{plan.filename}（{human_size(plan.size)}）\n"
            f"目标：{plan.dest}\n"
            f"来源：{project.source}\n\n"
            "下载后会校验 SHA-1，并记录到 .mc-launcher-content.json。\n"
            "是否继续？"
        )
        if not self._confirm("确认下载并安装", message):
            return
        self._start_content_install(plan)

    def _enqueue_selected(self) -> None:
        """Add the currently selected project/version to the download queue."""
        if self._closed or self._busy:
            return
        project = self._selected_project
        version = self._selected_version
        if project is None or version is None:
            return
        file = version.primary_file
        if file is None:
            self._show_error("无法加入队列", "该版本没有可下载文件。")
            return
        try:
            game_root = self._game_root_dir()
        except LauncherError as exc:
            self._show_error("游戏目录不可用", str(exc))
            return
        content_type = self._type_id()
        label = content_type_label(content_type)
        workers = self.app._threads_value()
        deep = bool(self.app.deep_verify_var.get())
        if content_type == "modpack":
            instance = content_instance_name(project.slug, version.version_number)
            message = (
                f"项目：{project.title}\n"
                f"版本：{version.version_number}\n"
                f"文件：{file.filename}（{human_size(file.size)}）\n"
                f"实例目录：{game_root / 'versions' / instance}\n\n"
                "将加入下载队列，在后台依次下载 .mrpack 并安装对应的 "
                "Minecraft + 加载器。队列可在“下载队列…”中查看、暂停或取消。\n\n"
                "是否加入队列？"
            )
            if not self._confirm("加入下载队列", message):
                return
            self.app.enqueue_modpack_install(
                project,
                version,
                file,
                game_root,
                source=self._modpack_source(),
                client=self._modpack_client(),
                java_path=self._selected_java_path(),
                deep=deep,
                workers=workers,
            )
            self._status_label_update("已加入下载队列", ACCENT)
            self._log(f"已加入下载队列：{project.title} / {version.version_number}", "success")
            return

        world_dir: Path | None = None
        try:
            if content_type == "datapack":
                world_dir = self._world_path()
            target = self._target_dir()
            plan = build_content_plan(
                project,
                version,
                file,
                game_dir,
                content_type,
                target_dir=target,
                world_dir=world_dir,
            )
            if plan.dest.exists():
                if not self._confirm(
                    "文件已存在",
                    f"目标文件已存在：\n{plan.dest}\n\n确定覆盖吗？",
                ):
                    return
                plan = build_content_plan(
                    project,
                    version,
                    file,
                    game_dir,
                    content_type,
                    target_dir=target,
                    world_dir=world_dir,
                    overwrite=True,
                )
        except LauncherError as exc:
            self._show_error("无法加入队列", str(exc))
            return
        message = (
            f"项目：{project.title}\n"
            f"版本：{version.version_number}\n"
            f"类型：{label}\n"
            f"文件：{plan.filename}（{human_size(plan.size)}）\n"
            f"目标：{plan.dest}\n\n"
            "将加入下载队列，在后台依次下载并校验 SHA-1。\n"
            "队列可在“下载队列…”中查看、暂停或取消。\n\n是否加入队列？"
        )
        if not self._confirm("加入下载队列", message):
            return
        try:
            client = self._source().client
        except LauncherError:
            client = None
        self.app.enqueue_content_install(
            plan,
            client=client,
            title=f"下载 {label} {project.title} / {version.version_number}",
            detail=str(plan.dest),
        )
        self._status_label_update("已加入下载队列", ACCENT)
        self._log(f"已加入下载队列：{plan.filename}", "success")

    def _start_content_install(self, plan) -> None:
        self._install_gen += 1
        generation = self._install_gen
        self._installing = True
        self._install_cancel = threading.Event()
        self._install_pause = PauseToken()
        self._set_busy(True, "正在下载…")
        self.cancel_button.configure(state="normal")
        self.pause_button.configure(state="normal", text="暂停")
        self.progress_bar.grid()
        self.progress_label.grid()
        self.progress_bar.configure(value=0)
        self.progress_var.set("0 B")
        try:
            client = self._source().client  # type: ignore[attr-defined]
        except LauncherError:
            client = None
        target = plan.dest
        pause = self._install_pause
        self._log(f"开始下载：{plan.project.title} / {plan.filename}", "info")

        def on_progress(value: int) -> None:
            self._post(
                "install_progress", generation, int(value), int(plan.size), plan.filename
            )

        def worker() -> None:
            try:
                outcome = install_content(
                    plan,
                    client=client,
                    progress=on_progress,
                    cancel=self._install_cancel,
                    pause=pause,
                    confirmed=True,
                    logger=self._thread_log,
                )
            except CancelledError:
                self._post("install_cancelled", generation)
            except Exception as exc:  # noqa: BLE001
                self._post("install_failed", generation, str(exc))
            else:
                self._post(
                    "install_done",
                    generation,
                    outcome.message,
                    str(plan.dest),
                    plan.project.id,
                )

        threading.Thread(target=worker, daemon=True, name="content-install").start()

    def _modpack_client(self):
        try:
            return self._source().client  # type: ignore[attr-defined]
        except LauncherError:
            return None

    def _modpack_source(self):
        return source_by_label(self.app.dl_source_var.get())

    def _selected_java_path(self) -> Path | None:
        text = self.app.java_var.get().strip()
        path = Path(text) if text else None
        return path if path is not None and path.is_file() else None

    def _open_modpack_progress(self, status: str) -> None:
        self._installing = True
        self._install_cancel = threading.Event()
        self._install_pause = PauseToken()
        self._set_busy(True, status)
        self.cancel_button.configure(state="normal")
        self.pause_button.configure(state="normal", text="暂停")
        self.progress_bar.grid()
        self.progress_label.grid()
        self.progress_bar.configure(value=0)
        self.progress_var.set("0 B")

    def _start_modpack_install(self, project, version, file, game_root: Path) -> None:
        self._install_gen += 1
        generation = self._install_gen
        self._open_modpack_progress("正在安装整合包…")
        client = self._modpack_client()
        source = self._modpack_source()
        java_path = self._selected_java_path()
        pause = self._install_pause
        self._log(f"开始下载整合包：{project.title} / {file.filename}", "info")

        def on_progress(progress) -> None:
            self._post(
                "install_progress",
                generation,
                int(progress.downloaded),
                int(progress.total),
                str(progress.current or progress.phase),
            )

        def worker() -> None:
            try:
                outcome = download_and_install_launchable_modpack(
                    project,
                    version,
                    file,
                    game_root,
                    slug=project.slug,
                    client=client,
                    source=source,
                    java_path=java_path,
                    progress=on_progress,
                    cancel=self._install_cancel,
                    pause=pause,
                    confirmed=True,
                    logger=self._thread_log,
                )
            except CancelledError:
                self._post("install_cancelled", generation)
            except Exception as exc:  # noqa: BLE001
                self._post("install_failed", generation, str(exc))
            else:
                self._post(
                    "modpack_done",
                    generation,
                    outcome.message,
                    str(outcome.instance_dir),
                    project.id,
                    str(outcome.version_id or ""),
                    bool(outcome.launchable),
                )

        threading.Thread(target=worker, daemon=True, name="content-modpack").start()

    def _install_from_archive(self) -> None:
        if self._closed or self._busy:
            return
        path = filedialog.askopenfilename(
            parent=self,
            title="选择整合包压缩包（.mrpack / .zip）",
            filetypes=(
                ("Modrinth 整合包", "*.mrpack"),
                ("ZIP 压缩包", "*.zip"),
                ("所有文件", "*.*"),
            ),
        )
        if not path:
            return
        try:
            game_root = self._game_root_dir()
        except LauncherError as exc:
            self._show_error("游戏目录不可用", str(exc))
            return
        self._inspect_gen += 1
        generation = self._inspect_gen
        self._set_busy(True, "正在检查压缩包…")
        self._log(f"正在检查整合包压缩包：{path}", "info")

        def worker() -> None:
            try:
                index = read_mrpack(Path(path))
                dependencies = resolve_modpack_dependencies(index)
                plan = build_instance_plan(index, game_root, launch=True)
            except Exception as exc:  # noqa: BLE001
                self._post("archive_failed", generation, str(exc))
            else:
                self._post(
                    "archive_ready", generation, str(path), plan, dependencies
                )

        threading.Thread(target=worker, daemon=True, name="modpack-inspect").start()

    def _on_archive_failed(self, generation: int, message: str) -> None:
        if generation != self._inspect_gen:
            return
        self._set_busy(False)
        self._status_label_update("压缩包无效", RED)
        self._log(f"整合包压缩包检查失败：{message}", "error")
        self._show_error("无法安装该压缩包", message)

    def _on_archive_ready(self, generation: int, path: str, plan, dependencies) -> None:
        if generation != self._inspect_gen:
            return
        self._set_busy(False)
        detail = format_instance_plan(plan)
        if not dependencies.launchable and dependencies.reason:
            detail += f"\n\n注意：{dependencies.reason}"
        if not self._confirm(
            "确认安装本地整合包",
            f"{detail}\n\n压缩包：{path}\n\n"
            "将校验 ZIP、下载缺失内容并创建隔离实例。是否继续？",
        ):
            self._status_label_update("已取消", MUTED)
            return

        overwrite = False
        if plan.version_dir.exists() and any(plan.version_dir.iterdir()):
            if messagebox.askyesno(
                "实例已存在",
                f"实例版本目录已存在：\n{plan.version_dir}\n\n"
                "是否覆盖同名文件并更新这个实例？\n"
                "选择“否”将只补齐缺失文件，遇到不同内容的同名文件会报错。",
                parent=self,
            ):
                overwrite = True
        if overwrite:
            plan = replace(plan, overwrite=True)
        if messagebox.askyesno(
            "加入下载队列",
            "是否把本地整合包加入下载队列？\n\n"
            "选择“是”会在后台依次安装，可在“下载队列…”中查看进度。\n"
            "选择“否”将立即开始安装。",
            parent=self,
        ):
            self.app.enqueue_archive_install(
                plan,
                source=self._modpack_source(),
                client=self._modpack_client(),
                java_path=self._selected_java_path(),
                deep=bool(self.app.deep_verify_var.get()),
                workers=self.app._threads_value(),
            )
            self._status_label_update("已加入下载队列", ACCENT)
            self._log(f"已加入下载队列：{plan.index.name}", "success")
            return
        self._start_archive_install(plan)

    def _start_archive_install(self, plan) -> None:
        self._install_gen += 1
        generation = self._install_gen
        self._open_modpack_progress("正在安装本地整合包…")
        client = self._modpack_client()
        source = self._modpack_source()
        java_path = self._selected_java_path()
        pause = self._install_pause
        self._log(f"开始安装本地整合包：{plan.index.archive}", "info")

        def on_progress(progress) -> None:
            self._post(
                "install_progress",
                generation,
                int(progress.downloaded),
                int(progress.total),
                str(progress.current or progress.phase),
            )

        def worker() -> None:
            try:
                outcome = install_instance(
                    plan,
                    confirmed=True,
                    source=source,
                    client=client,
                    java_path=java_path,
                    progress=on_progress,
                    cancel=self._install_cancel,
                    pause=pause,
                    logger=self._thread_log,
                )
            except CancelledError:
                self._post("install_cancelled", generation)
            except Exception as exc:  # noqa: BLE001
                self._post("install_failed", generation, str(exc))
            else:
                self._post(
                    "modpack_done",
                    generation,
                    outcome.message,
                    str(outcome.instance_dir),
                    "",
                    str(outcome.version_id or ""),
                    bool(outcome.launchable),
                )

        threading.Thread(target=worker, daemon=True, name="modpack-install").start()

    def _on_install_progress(
        self, generation: int, downloaded: int, total: int, current: str
    ) -> None:
        if generation != self._install_gen:
            return
        fraction = 0.0 if total <= 0 else max(0.0, min(1.0, downloaded / total))
        self.progress_bar.configure(value=fraction * 1000)
        if total > 0:
            self.progress_var.set(f"{human_size(downloaded)} / {human_size(total)}")
        else:
            self.progress_var.set(human_size(downloaded))
        if current:
            self._status_label_update(f"正在下载：{current}", BLUE)

    def _finish_install(self) -> None:
        self._installing = False
        self._install_cancel = None
        self._install_pause = None
        self._set_busy(False)
        self.cancel_button.configure(state="disabled")
        self.pause_button.configure(state="disabled", text="暂停")
        self.progress_bar.grid_remove()
        self.progress_label.grid_remove()
        self._refresh_installed()

    def _on_install_done(
        self, generation: int, message: str, dest: str, project_id: str
    ) -> None:
        if generation != self._install_gen:
            return
        self._finish_install()
        self.progress_bar.configure(value=1000)
        self.progress_var.set("完成")
        self._status_label_update("安装完成", ACCENT)
        self._installed_ids.add(str(project_id))
        for iid, project in self._project_map.items():
            if project.id == str(project_id) and self.tree.exists(iid):
                values = list(self.tree.item(iid, "values"))
                if values:
                    values[-1] = "已安装"
                    self.tree.item(iid, values=values, tags=("installed",))
        self._log(f"{message}\n目标：{dest}", "success")
        messagebox.showinfo("安装完成", f"{message}\n\n{dest}", parent=self)

    def _on_modpack_done(
        self,
        generation: int,
        message: str,
        instance: str,
        project_id: str,
        version_id: str = "",
        launchable: bool = False,
    ) -> None:
        if generation != self._install_gen:
            return
        self._finish_install()
        self.progress_bar.configure(value=1000)
        self.progress_var.set("完成")
        if launchable and version_id:
            self._status_label_update("整合包已可启动", ACCENT)
        else:
            self._status_label_update("整合包内容已整理", YELLOW)
        if project_id:
            self._installed_ids.add(str(project_id))
            for iid, project in self._project_map.items():
                if project.id == str(project_id) and self.tree.exists(iid):
                    values = list(self.tree.item(iid, "values"))
                    if values:
                        values[-1] = "已安装"
                        self.tree.item(iid, values=values, tags=("installed",))
        self._log(f"{message}\n实例目录：{instance}", "success")
        if launchable and version_id:
            self.app._preferred_version = version_id
            self.app._refresh_versions()
            messagebox.showinfo(
                "整合包安装完成",
                f"{message}\n\n"
                f"已创建可启动版本：{version_id}\n"
                f"隔离游戏目录：{instance}\n\n"
                "启动器会刷新版本列表并自动选中该实例。",
                parent=self,
            )
        else:
            messagebox.showinfo(
                "整合包内容已整理",
                f"{message}\n\n实例目录：{instance}\n\n"
                "该整合包未创建可启动版本。NeoForge / Quilt 自动安装将在 0.6.0 提供。",
                parent=self,
            )

    def _on_install_failed(self, generation: int, message: str) -> None:
        if generation != self._install_gen:
            return
        self._finish_install()
        self.progress_var.set("失败")
        self._status_label_update("安装失败", RED)
        self._log(f"安装失败：{message}", "error")
        self._show_error("安装失败", message)

    def _on_install_cancelled(self, generation: int) -> None:
        if generation != self._install_gen:
            return
        self._finish_install()
        self.progress_var.set("已取消")
        self._status_label_update("已取消", YELLOW)
        self._log(
            "下载已取消；本次下载的文件和未完成分片已删除。", "warning"
        )

    def _toggle_pause(self) -> None:
        if self._install_pause is None:
            return
        paused = self._install_pause.toggle()
        self.pause_button.configure(text="继续" if paused else "暂停")
        if paused:
            self._status_label_update("已暂停；点击“继续”恢复下载", YELLOW)
            self._log("下载已暂停，未完成分片会保留。", "warning")
        else:
            self._status_label_update("正在继续下载…", BLUE)
            self._log("继续下载。", "info")

    def _cancel_install(self) -> None:
        if self._install_cancel is not None:
            self._install_cancel.set()
            self._status_label_update("正在取消…", YELLOW)
            self.cancel_button.configure(state="disabled")
            self.pause_button.configure(state="disabled")

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        self._closed = True
        if self._install_cancel is not None:
            self._install_cancel.set()
        if self._search_after is not None:
            try:
                self.after_cancel(self._search_after)
            except tk.TclError:
                pass
            self._search_after = None
