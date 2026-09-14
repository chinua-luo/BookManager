from __future__ import annotations

import atexit
import multiprocessing
import os
import platform
import queue
import shutil
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk

from .crawler import crawl_into_library
from .metadata import extract_metadata
from .naming import (
    StructuredFileName,
    build_structured_file_name,
    infer_structured_file_name,
    normalize_structured_file_name,
    title_case,
)
from .rendering import render_preview
from .store import (
    CACHE_POLICIES,
    CACHE_POLICY_NAMES,
    CacheSettings,
    Folder,
    Item,
    LibraryStore,
    migrate_library_home,
    normalize_tag_names,
    safe_name,
    set_configured_library_home,
)

try:
    from tkinterdnd2 import COPY, DND_FILES, TkinterDnD  # type: ignore
except ImportError:
    COPY = "copy"
    DND_FILES = None
    TkinterDnD = None


TEXT_EXTENSIONS = {".txt", ".md", ".py", ".json", ".xml", ".html", ".htm", ".css", ".js", ".csv", ".log"}

BookManagerWindow = TkinterDnD.Tk if TkinterDnD is not None else tk.Tk


def _extract_metadata_in_worker(source_path: str, result_queue) -> None:
    """Run document parsing outside the Tk process so CPU-bound parsers cannot stall it."""
    try:
        result = extract_metadata(Path(source_path))
    except Exception as exc:
        result_queue.put((None, f"内容分析失败，已使用文件名候选：{exc}"))
        return
    result_queue.put((result, result.message))


def merge_name_candidates(
    file_name_parts: StructuredFileName,
    content_parts: StructuredFileName,
) -> StructuredFileName:
    """Use content-derived fields when available, with the filename as a fallback."""
    return StructuredFileName(
        series_abbr=content_parts.series_abbr or file_name_parts.series_abbr,
        number=content_parts.number or file_name_parts.number,
        main_title=content_parts.main_title or file_name_parts.main_title,
        subtitle=content_parts.subtitle or file_name_parts.subtitle,
        edition=content_parts.edition or file_name_parts.edition or "1",
        authors=content_parts.authors or file_name_parts.authors,
        extension=file_name_parts.extension or content_parts.extension,
        edition_language=content_parts.edition_language,
    )


def automatic_import_name_parts(source_path: Path) -> StructuredFileName:
    """Build structured fields for batch directory imports without a confirmation dialog."""
    source_path = Path(source_path)
    file_name_parts = infer_structured_file_name(source_path.name)
    try:
        metadata_parts = extract_metadata(source_path).parts
    except Exception:
        metadata_parts = file_name_parts
    parts = merge_name_candidates(file_name_parts, metadata_parts)

    try:
        build_structured_file_name(parts)
        return parts
    except ValueError:
        # Source names from non-Windows filesystems can contain disallowed field separators.
        title = safe_name(source_path.stem, "Untitled").replace(" - ", " ").replace(" _ ", " ")
        fallback = StructuredFileName(
            series_abbr="",
            number="",
            main_title=title,
            subtitle="",
            edition="1",
            authors="",
            extension=source_path.suffix.lower(),
        )
        build_structured_file_name(fallback)
        return fallback


def automatic_import_display_name(source_path: Path) -> str:
    return build_structured_file_name(automatic_import_name_parts(source_path))


def group_underlying_search_results(
    results: list[tuple[str, Item]],
) -> list[list[tuple[str, Item]]]:
    """Group all mirror items belonging to the same underlying document."""
    grouped: dict[int, list[tuple[str, Item]]] = {}
    for folder_path, item in results:
        grouped.setdefault(item.document_id, []).append((folder_path, item))

    groups = []
    for mirrors in grouped.values():
        groups.append(
            sorted(
                mirrors,
                key=lambda result: (formatted_display_name(result[1]).lower(), result[0].lower()),
            )
        )
    return sorted(
        groups,
        key=lambda mirrors: (formatted_display_name(mirrors[0][1]).lower(), mirrors[0][0].lower()),
    )


def item_sort_key(item: Item, mode: str) -> tuple[object, ...]:
    title_key = (display_title_for_item(item).casefold(), item.display_name.casefold())
    if mode != "序列":
        return title_key

    series_abbr = item.name_parts.series_abbr.strip().casefold()
    number = item.name_parts.number.strip()
    if number.isdecimal():
        number_key: tuple[object, ...] = (0, int(number))
    else:
        number_key = (1, number.casefold())
    return (not bool(series_abbr or number), series_abbr, *number_key, *title_key)


def infer_name_parts_from_pasted_text(text: str, fallback_extension: str) -> StructuredFileName:
    candidate = " ".join(line.strip() for line in text.splitlines() if line.strip())
    if not candidate:
        raise ValueError("请先粘贴要识别的名称文本")
    inferred = infer_structured_file_name(candidate)
    return normalize_structured_file_name(
        StructuredFileName(
            series_abbr=inferred.series_abbr,
            number=inferred.number,
            main_title=inferred.main_title,
            subtitle=inferred.subtitle,
            edition=inferred.edition,
            authors=inferred.authors,
            extension=fallback_extension,
            edition_language=inferred.edition_language,
        )
    )


class BookManagerApp(BookManagerWindow):
    def __init__(self) -> None:
        super().__init__()
        self.title("BookManager")
        self.geometry("1180x760")
        self.minsize(980, 620)
        self.window_icon = tk.PhotoImage(file=str(Path(__file__).resolve().parent.parent / "book.png"))
        self.iconphoto(True, self.window_icon)

        self.store = LibraryStore()
        self.store.cleanup_caches("startup")
        self._closing = False
        atexit.register(self._cleanup_caches_on_process_exit)
        self.current_folder_id = 1
        self.current_item: Item | None = None
        self.preview_image: tk.PhotoImage | None = None
        self.preview_images: list[tk.PhotoImage] = []
        self.folder_item_icon: tk.PhotoImage | None = None
        self.item_icons: dict[str, tk.PhotoImage] = {}
        self.folder_drag_source_id: int | None = None
        self.folder_drag_start: tuple[int, int] | None = None
        self.folder_drag_active = False
        self.resize_hint_after_id: str | None = None
        self.resize_hint_visible = False
        self.resize_hint_previous_status = ""
        self.shortcut_values: dict[str, str] = {}
        self.shortcut_binding_ids: dict[str, str] = {}
        self._content_marquee_text: dict[str, str] = {}
        self._content_marquee_offsets: dict[str, int] = {}
        self._content_marquee_visible: dict[str, str] = {}
        self._content_marquee_after_id: str | None = None
        self._preview_title_text = "预览"
        self._preview_title_offset = 0
        self._preview_title_after_id: str | None = None
        self._search_file_group_labels: dict[str, str] = {}
        self.folder_sort_modes: dict[int, str] = {}
        self.content_sort_column: str | None = None
        self.content_sort_descending = False
        self._open_cache_sequence = 0
        self._active_drop_signatures: set[tuple[int, tuple[str, ...]]] = set()
        self._recent_drop_signatures: dict[tuple[int, tuple[str, ...]], float] = {}
        migration_note = "；已迁移原有资料库" if self.store.migrated_legacy_library else ""
        self.status_var = tk.StringVar(value=f"Library: {self.store.home}{migration_note}")

        self._configure_style()
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()
        self._load_shortcuts()

        self.refresh_folders()
        self.select_folder(1)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind_all("<Command-q>", self._quit_application)

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names() and platform.system() == "Windows":
            style.theme_use("vista")
        style.configure("Toolbar.TButton", padding=(8, 4))
        style.configure("Treeview", rowheight=24)

    def _build_toolbar(self) -> None:
        toolbar_view = ttk.Frame(self, padding=(8, 6, 8, 0))
        toolbar_view.pack(side=tk.TOP, fill=tk.X)
        self.toolbar_canvas = tk.Canvas(toolbar_view, height=38, highlightthickness=0, borderwidth=0)
        toolbar_scroll = ttk.Scrollbar(toolbar_view, orient=tk.HORIZONTAL, command=self.toolbar_canvas.xview)
        self.toolbar_canvas.configure(xscrollcommand=toolbar_scroll.set)
        self.toolbar_canvas.pack(side=tk.TOP, fill=tk.X, expand=True)
        toolbar_scroll.pack(side=tk.BOTTOM, fill=tk.X)

        toolbar = ttk.Frame(self.toolbar_canvas, padding=(0, 2))
        self.toolbar_content = toolbar
        self._toolbar_sync_pending = False
        self.toolbar_window = self.toolbar_canvas.create_window((0, 0), window=toolbar, anchor=tk.NW)
        toolbar.bind("<Configure>", self._update_toolbar_scroll_region)
        self.toolbar_canvas.bind("<Configure>", self._update_toolbar_scroll_region)
        self.toolbar_canvas.bind("<Shift-MouseWheel>", self._scroll_toolbar_horizontally)
        self.toolbar_canvas.bind("<Shift-Button-4>", self._scroll_toolbar_horizontally)
        self.toolbar_canvas.bind("<Shift-Button-5>", self._scroll_toolbar_horizontally)

        self.add_menu = tk.Menu(toolbar, tearoff=False)
        self.add_menu.add_command(label="导入文件", command=self.import_file)
        self.add_menu.add_command(label="添加文件夹", command=self.import_folder)
        add_button = ttk.Menubutton(toolbar, text="添加", menu=self.add_menu, style="Toolbar.TButton")
        add_button.pack(side=tk.LEFT, padx=(0, 6))

        buttons = [
            ("新建文件夹", self.create_folder),
        ]
        for text, command in buttons:
            ttk.Button(toolbar, text=text, command=command, style="Toolbar.TButton").pack(side=tk.LEFT, padx=(0, 6))

        self.open_menu = tk.Menu(toolbar, tearoff=False)
        self.open_menu.add_command(label="打开文件", command=self.open_selected_file_default)
        self.open_menu.add_command(label="打开底层", command=self.open_blob_location)
        open_button = ttk.Menubutton(toolbar, text="打开", menu=self.open_menu, style="Toolbar.TButton")
        open_button.pack(side=tk.LEFT, padx=(0, 6))

        buttons = [
            ("按规则重命名", self.rename_selected_item),
            ("保存文本改动", self.save_text_preview),
            ("爬取系列", self.open_crawler_dialog),
        ]
        for text, command in buttons:
            ttk.Button(toolbar, text=text, command=command, style="Toolbar.TButton").pack(side=tk.LEFT, padx=(0, 6))

        self.settings_menu = tk.Menu(toolbar, tearoff=False)
        self.settings_menu.add_command(label="数据位置", command=self.open_data_location_dialog)
        self.settings_menu.add_command(label="缓存管理", command=self.open_cache_management_dialog)
        ttk.Menubutton(toolbar, text="设置", menu=self.settings_menu, style="Toolbar.TButton").pack(side=tk.LEFT, padx=(0, 6))

        self.help_menu = tk.Menu(toolbar, tearoff=False)
        self.help_menu.add_command(label="功能介绍", command=self.open_help_dialog)
        self.help_menu.add_command(label="快捷键", command=self.open_shortcut_dialog)
        help_button = ttk.Menubutton(toolbar, text="帮助", menu=self.help_menu, style="Toolbar.TButton")
        help_button.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Frame(toolbar).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(toolbar, text="搜索", command=self.open_search_dialog, style="Toolbar.TButton").pack(side=tk.RIGHT)

    def _update_toolbar_scroll_region(self, _event=None) -> None:
        if self._toolbar_sync_pending:
            return
        self._toolbar_sync_pending = True
        self.after_idle(self._sync_toolbar_scroll_region)

    def _sync_toolbar_scroll_region(self) -> None:
        self._toolbar_sync_pending = False
        content_width = max(self.toolbar_canvas.winfo_width(), self.toolbar_content.winfo_reqwidth())
        self.toolbar_canvas.itemconfigure(self.toolbar_window, width=content_width)
        self.toolbar_canvas.configure(scrollregion=(0, 0, content_width, self.toolbar_content.winfo_reqheight()))

    def _scroll_toolbar_horizontally(self, event) -> str:
        if getattr(event, "num", None) == 4:
            amount = -3
        elif getattr(event, "num", None) == 5:
            amount = 3
        else:
            delta = getattr(event, "delta", 0)
            amount = -1 if delta > 0 else 1
        self.toolbar_canvas.xview_scroll(amount, "units")
        return "break"

    def _build_body(self) -> None:
        self.main_pane = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        self.main_pane.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.main_pane.bind("<Motion>", self.on_main_pane_motion)
        self.main_pane.bind("<Leave>", self.hide_resize_hint)
        self.main_pane.bind("<ButtonPress-1>", self.on_main_pane_button_press)

        left = ttk.Panedwindow(self.main_pane, orient=tk.VERTICAL)
        self.main_pane.add(left, weight=38)

        folder_frame = ttk.Frame(left, padding=(8, 8, 4, 4))
        ttk.Label(folder_frame, text="文件夹层级").pack(anchor=tk.W)
        self.folder_tree = ttk.Treeview(folder_frame, show="tree", selectmode="browse")
        folder_scroll = ttk.Scrollbar(folder_frame, orient=tk.VERTICAL, command=self.folder_tree.yview)
        self.folder_tree.configure(yscrollcommand=folder_scroll.set)
        self.folder_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        folder_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.folder_tree.bind("<<TreeviewSelect>>", self.on_folder_select)
        self.folder_tree.bind("<Button-3>", self.show_folder_menu)
        self.folder_tree.bind("<Button-2>", self.show_folder_menu)
        self.folder_tree.bind("<ButtonPress-1>", self.on_folder_drag_start, add="+")
        self.folder_tree.bind("<B1-Motion>", self.on_folder_drag_motion, add="+")
        self.folder_tree.bind("<ButtonRelease-1>", self.on_folder_drag_release, add="+")
        self._enable_file_drop(self.folder_tree, "folder_tree")
        left.add(folder_frame, weight=46)

        item_frame = ttk.Frame(left, padding=(8, 4, 4, 8))
        ttk.Label(item_frame, text="本文件夹内容").pack(anchor=tk.W)
        self.folder_item_icon, self.item_icons = self._create_item_icons()
        self.item_tree = ttk.Treeview(
            item_frame,
            columns=("size", "type"),
            show="tree headings",
            selectmode="browse",
        )
        self.item_tree.heading("#0", text="名称", command=lambda: self.sort_content_by("name"))
        self.item_tree.heading("size", text="大小", command=lambda: self.sort_content_by("size"))
        self.item_tree.heading("type", text="类型")
        self.item_tree.column("#0", width=260, minwidth=180, anchor=tk.CENTER, stretch=True)
        self.item_tree.column("size", width=86, minwidth=76, anchor=tk.CENTER, stretch=False)
        self.item_tree.column("type", width=64, minwidth=58, anchor=tk.CENTER, stretch=False)
        item_scroll = ttk.Scrollbar(item_frame, orient=tk.VERTICAL, command=self.item_tree.yview)
        self.item_tree.configure(yscrollcommand=item_scroll.set)
        self.item_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        item_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.item_tree.bind("<<TreeviewSelect>>", self.on_item_select)
        self.item_tree.bind("<Button-3>", self.show_item_menu)
        self.item_tree.bind("<Button-2>", self.show_item_menu)
        self.item_tree.bind("<Double-Button-1>", self.on_content_double_click)
        self.item_tree.bind("<Configure>", lambda _event: self._start_content_marquee())
        self._enable_file_drop(self.item_tree, "item_tree")
        left.add(item_frame, weight=54)

        preview_frame = ttk.Frame(self.main_pane, padding=(4, 8, 8, 8))
        header = ttk.Frame(preview_frame)
        header.pack(side=tk.TOP, fill=tk.X)
        self.preview_meta = ttk.Label(header, text="", foreground="#555")
        self.preview_meta.pack(side=tk.RIGHT)
        title_holder = ttk.Frame(header)
        title_holder.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.preview_title_viewport = ttk.Frame(title_holder, height=26)
        self.preview_title_viewport.pack(fill=tk.X, expand=True)
        self.preview_title_viewport.pack_propagate(False)
        self.preview_title = ttk.Label(self.preview_title_viewport, text="预览", font=("", 12, "bold"), anchor=tk.W)
        self.preview_title.place(x=0, y=0)
        self.preview_title_viewport.bind("<Configure>", self._on_preview_title_configure)

        self.preview_container = ttk.Frame(preview_frame)
        self.preview_container.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(8, 0))
        self.main_pane.add(preview_frame, weight=62)
        self._enable_file_drop(self, "root")

    def _build_statusbar(self) -> None:
        status = ttk.Label(self, textvariable=self.status_var, anchor=tk.W, padding=(8, 4))
        status.pack(side=tk.BOTTOM, fill=tk.X)

    def on_main_pane_motion(self, event) -> None:
        if self.is_main_sash_hit(event.x, event.y):
            if self.resize_hint_after_id is None and not self.resize_hint_visible:
                self.resize_hint_after_id = self.after(350, self.show_resize_hint)
            return
        self.hide_resize_hint()

    def on_main_pane_button_press(self, event) -> None:
        if self.is_main_sash_hit(event.x, event.y):
            self.show_resize_hint()

    def is_main_sash_hit(self, x: int, y: int) -> bool:
        try:
            sash_x = int(self.main_pane.sashpos(0))
            if abs(x - sash_x) <= 8:
                return True
        except tk.TclError:
            pass
        try:
            identity = self.main_pane.identify(x, y)
        except tk.TclError:
            return False
        return "sash" in str(identity).lower()

    def show_resize_hint(self) -> None:
        self.resize_hint_after_id = None
        if self.resize_hint_visible:
            return
        self.resize_hint_visible = True
        self.resize_hint_previous_status = self.status_var.get()
        self.main_pane.configure(cursor="sb_h_double_arrow")
        self.status_var.set("按住左右分隔条并拖动，可以调整左右界面宽度。")

    def hide_resize_hint(self, _event=None) -> None:
        if self.resize_hint_after_id is not None:
            self.after_cancel(self.resize_hint_after_id)
            self.resize_hint_after_id = None
        if not self.resize_hint_visible:
            return
        self.resize_hint_visible = False
        self.main_pane.configure(cursor="")
        self.status_var.set(self.resize_hint_previous_status)

    def refresh_folders(self) -> None:
        selected_folder_id = self.current_folder_id
        self.folder_tree.delete(*self.folder_tree.get_children())
        for folder in self.store.folders(None):
            self._insert_folder("", folder)
        selected_node = f"folder:{selected_folder_id}"
        if self.folder_tree.exists(selected_node):
            self.folder_tree.selection_set(selected_node)
            self.folder_tree.focus(selected_node)
            self._open_folder_ancestors(selected_folder_id)
        else:
            self.folder_tree.selection_set("folder:1")
            self.folder_tree.focus("folder:1")
        self.folder_tree.item("folder:1", open=True)

    def _insert_folder(self, parent_node: str, folder: Folder) -> None:
        node = f"folder:{folder.id}"
        self.folder_tree.insert(parent_node, tk.END, iid=node, text=folder.name, open=folder.id == 1)
        for child in self.store.folders(folder.id):
            self._insert_folder(node, child)

    def _open_folder_ancestors(self, folder_id: int) -> None:
        folder = self.store.get_folder(folder_id)
        while folder.parent_id is not None:
            parent_node = f"folder:{folder.parent_id}"
            if self.folder_tree.exists(parent_node):
                self.folder_tree.item(parent_node, open=True)
            folder = self.store.get_folder(folder.parent_id)

    def on_folder_select(self, _event=None) -> None:
        selection = self.folder_tree.selection()
        if not selection:
            return
        folder_id = int(selection[0].split(":", 1)[1])
        self.select_folder(folder_id)

    def on_folder_drag_start(self, event) -> None:
        node = self.folder_tree.identify_row(event.y)
        self.folder_drag_source_id = int(node.split(":", 1)[1]) if node.startswith("folder:") else None
        self.folder_drag_start = (event.x, event.y)
        self.folder_drag_active = False

    def on_folder_drag_motion(self, event) -> None:
        if self.folder_drag_source_id is None or self.folder_drag_start is None:
            return
        start_x, start_y = self.folder_drag_start
        if not self.folder_drag_active and max(abs(event.x - start_x), abs(event.y - start_y)) < 6:
            return
        self.folder_drag_active = True
        target_node = self.folder_tree.identify_row(event.y)
        if target_node.startswith("folder:"):
            self.folder_tree.focus(target_node)
            self.status_var.set("松开鼠标可将文件夹移动到当前目标文件夹。")

    def on_folder_drag_release(self, event) -> None:
        source_id = self.folder_drag_source_id
        was_dragging = self.folder_drag_active
        self.folder_drag_source_id = None
        self.folder_drag_start = None
        self.folder_drag_active = False
        if source_id is None or not was_dragging:
            return
        target_node = self.folder_tree.identify_row(event.y)
        if not target_node.startswith("folder:"):
            return
        target_id = int(target_node.split(":", 1)[1])
        self._move_folder_to_target(source_id, target_id)

    def select_folder(self, folder_id: int) -> None:
        self.current_folder_id = folder_id
        self.current_item = None
        self._stop_content_marquee()
        self.item_tree.delete(*self.item_tree.get_children())
        child_folders = self.store.folders(folder_id)
        folder_entries = [(folder, self.store.folder_stats(folder.id)) for folder in child_folders]
        items = sorted(
            self.store.list_items(folder_id),
            key=lambda item: item_sort_key(item, self._folder_sort_mode_for(folder_id)),
        )
        if self.content_sort_column == "name":
            folder_entries.sort(key=lambda entry: entry[0].name.casefold(), reverse=self.content_sort_descending)
            items.sort(key=lambda item: display_title_for_item(item).casefold(), reverse=self.content_sort_descending)
        elif self.content_sort_column == "size":
            folder_entries.sort(key=lambda entry: entry[1].unique_size, reverse=self.content_sort_descending)
            items.sort(key=lambda item: item.size, reverse=self.content_sort_descending)

        for folder, stats in folder_entries:
            self.item_tree.insert(
                "",
                tk.END,
                iid=f"content-folder:{folder.id}",
                text=folder.name,
                image=self.folder_item_icon,
                values=(human_size(stats.unique_size), "文件夹"),
            )
        for item in items:
            node = f"item:{item.id}"
            display_title = display_title_for_item(item)
            self.item_tree.insert(
                "",
                tk.END,
                iid=node,
                text=display_title,
                image=self._item_icon_for(item),
                values=(human_size(item.size), display_type_for_item(item)),
            )
            self._content_marquee_text[node] = display_title
        self.preview_folder(folder_id)
        self._start_content_marquee()

    def sort_content_by(self, column: str) -> None:
        selected_nodes = self.item_tree.selection()
        if self.content_sort_column == column:
            self.content_sort_descending = not self.content_sort_descending
        else:
            self.content_sort_column = column
            self.content_sort_descending = False
        self._update_content_sort_headings()
        self.select_folder(self.current_folder_id)
        if not selected_nodes:
            return
        selected_node = selected_nodes[0]
        if not self.item_tree.exists(selected_node):
            return
        self.item_tree.selection_set(selected_node)
        self.item_tree.focus(selected_node)
        self.on_item_select()

    def _update_content_sort_headings(self) -> None:
        for column, label in (("name", "名称"), ("size", "大小")):
            if self.content_sort_column == column:
                direction = "↓" if self.content_sort_descending else "↑"
                label = f"{label} {direction}"
            tree_column = "#0" if column == "name" else column
            self.item_tree.heading(tree_column, text=label, command=lambda column=column: self.sort_content_by(column))

    def _folder_sort_mode_for(self, folder_id: int) -> str:
        folder = self.store.get_folder(folder_id)
        while True:
            mode = self.folder_sort_modes.get(folder.id)
            if mode is not None:
                return mode
            if folder.parent_id is None:
                return "标准"
            folder = self.store.get_folder(folder.parent_id)

    def set_folder_sort_mode(self, folder_id: int, mode: str) -> None:
        for descendant_id in self._folder_subtree_ids(folder_id):
            self.folder_sort_modes.pop(descendant_id, None)
        self.folder_sort_modes[folder_id] = mode
        self.select_folder(self.current_folder_id)
        folder_path = self.store.folder_path(folder_id)
        self.status_var.set(f"已将“{folder_path}”及其子文件夹设为{mode}排序。")

    def _stop_content_marquee(self) -> None:
        if self._content_marquee_after_id is not None:
            self.after_cancel(self._content_marquee_after_id)
            self._content_marquee_after_id = None
        self._content_marquee_text.clear()
        self._content_marquee_offsets.clear()
        self._content_marquee_visible.clear()

    def _start_content_marquee(self) -> None:
        if self._content_marquee_text and self._content_marquee_after_id is None:
            self._content_marquee_after_id = self.after(450, self._scroll_content_marquee)

    def _scroll_content_marquee(self) -> None:
        self._content_marquee_after_id = None
        if not self._content_marquee_text:
            return
        font = self._treeview_font()
        has_overflow = False
        for node, full_text in self._content_marquee_text.items():
            if not self.item_tree.exists(node):
                self._content_marquee_offsets.pop(node, None)
                self._content_marquee_visible.pop(node, None)
                continue
            bounds = self.item_tree.bbox(node, "#0")
            if not bounds:
                continue
            available_width = max(bounds[2] - 26, 1)
            if font.measure(full_text) <= available_width:
                self._set_content_marquee_text(node, full_text)
                self._content_marquee_offsets[node] = 0
                continue
            has_overflow = True
            offset = self._content_marquee_offsets.get(node, 0)
            self._set_content_marquee_text(
                node,
                self._marquee_window(full_text, offset, available_width, font),
            )
            self._content_marquee_offsets[node] = offset + 1
        delay = 220 if has_overflow else 700
        self._content_marquee_after_id = self.after(delay, self._scroll_content_marquee)

    def _set_content_marquee_text(self, node: str, text: str) -> None:
        if self._content_marquee_visible.get(node) == text:
            return
        self.item_tree.item(node, text=text)
        self._content_marquee_visible[node] = text

    def _treeview_font(self) -> tkfont.Font:
        font_name = ttk.Style(self).lookup("Treeview", "font") or "TkDefaultFont"
        return self._measurement_font(font_name)

    def _measurement_font(self, font_spec: str) -> tkfont.Font:
        """Return a font usable for measuring both named fonts and Tk font descriptions."""
        try:
            return tkfont.nametofont(font_spec)
        except tk.TclError:
            return tkfont.Font(self, font=font_spec)

    @staticmethod
    def _marquee_window(text: str, offset: int, available_width: int, font: tkfont.Font) -> str:
        if not text or font.measure(text) <= available_width:
            return text
        cycle = text + "     "
        start = offset % len(cycle)
        visible = ""
        for character in (cycle + cycle)[start:]:
            candidate = visible + character
            if visible and font.measure(candidate) > available_width:
                break
            visible = candidate
        return visible or text[:1]

    def _set_preview_title(self, text: str) -> None:
        self._preview_title_text = text
        self._preview_title_offset = 0
        self._render_preview_title()
        self._schedule_preview_title_marquee(500)

    def _on_preview_title_configure(self, _event=None) -> None:
        self._render_preview_title()
        self._schedule_preview_title_marquee(250)

    def _render_preview_title(self) -> bool:
        if not hasattr(self, "preview_title"):
            return False
        font = self._measurement_font(self.preview_title.cget("font") or "TkDefaultFont")
        available_width = max(self.preview_title_viewport.winfo_width() - 4, 1)
        if font.measure(self._preview_title_text) <= available_width:
            self.preview_title.configure(text=self._preview_title_text)
            self.preview_title.place(x=0, y=0)
            self._preview_title_offset = 0
            return False
        gap = "     "
        cycle_width = font.measure(self._preview_title_text + gap)
        self.preview_title.configure(text=self._preview_title_text + gap + self._preview_title_text)
        self.preview_title.place(x=-(self._preview_title_offset % cycle_width), y=0)
        return True

    def _schedule_preview_title_marquee(self, delay: int) -> None:
        if self._preview_title_after_id is None and self._preview_title_text:
            self._preview_title_after_id = self.after(delay, self._scroll_preview_title)

    def _scroll_preview_title(self) -> None:
        self._preview_title_after_id = None
        if not self._render_preview_title():
            return
        self._preview_title_offset += 1
        self._preview_title_after_id = self.after(32, self._scroll_preview_title)

    def _create_item_icons(self) -> tuple[tk.PhotoImage, dict[str, tk.PhotoImage]]:
        folder = tk.PhotoImage(width=16, height=16)
        folder.put("#d3a32d", to=(1, 4, 15, 14))
        folder.put("#f0c653", to=(2, 3, 9, 6))
        folder.put("#8e6b1d", to=(1, 13, 15, 15))

        def document_icon(color: str) -> tk.PhotoImage:
            icon = tk.PhotoImage(width=16, height=16)
            icon.put(color, to=(3, 1, 12, 15))
            icon.put("#f8fbff", to=(4, 2, 11, 14))
            icon.put(color, to=(5, 5, 10, 6))
            icon.put(color, to=(5, 8, 10, 9))
            icon.put(color, to=(5, 11, 9, 12))
            return icon

        return folder, {
            "file": document_icon("#4e73a8"),
            "pdf": document_icon("#bb3c38"),
            "epub": document_icon("#33875b"),
            "djvu": document_icon("#7358a0"),
        }

    def _item_icon_for(self, item: Item) -> tk.PhotoImage:
        suffix = (item.name_parts.extension or item.stored_path.suffix).lower()
        icon_key = "djvu" if suffix in {".djvu", ".djv"} else suffix.lstrip(".")
        return self.item_icons.get(icon_key, self.item_icons["file"])

    def open_help_dialog(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("功能介绍")
        dialog.transient(self)
        dialog.geometry("720x470")
        dialog.minsize(560, 340)

        ttk.Label(dialog, text="功能介绍", font=("", 12, "bold")).pack(anchor=tk.W, padx=12, pady=(12, 6))
        table_frame = ttk.Frame(dialog, padding=(12, 0, 12, 12))
        table_frame.pack(fill=tk.BOTH, expand=True)
        table = ttk.Treeview(table_frame, columns=("description",), show="tree headings", selectmode="none")
        table.heading("#0", text="功能")
        table.heading("description", text="功能说明")
        table.column("#0", anchor=tk.W, stretch=False, width=180, minwidth=150)
        table.column("description", anchor=tk.W, stretch=True, width=620)
        scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=table.yview)
        table.configure(yscrollcommand=scrollbar.set)
        table.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        add_node = table.insert("", tk.END, text="添加", values=("展开导入文件与添加文件夹操作。",), open=True)
        table.insert(add_node, tk.END, text="导入文件", values=("选择本地文件，识别或填写命名字段、备注和标签后导入底层 Hash 文件库。",))
        table.insert(add_node, tk.END, text="添加文件夹", values=("选择本地目录后，可为该目录内全部导入文件选择共同标签，再递归导入结构与文件。",))
        table.insert("", tk.END, text="新建文件夹", values=("在当前文件夹下创建一个可自主命名的虚拟文件夹。",))
        settings_node = table.insert("", tk.END, text="设置", values=("管理软件的数据位置。",), open=True)
        table.insert(settings_node, tk.END, text="数据位置", values=("默认使用软件安装目录下的 BookManagerData；可选择空文件夹并迁移全部书籍数据。",))
        table.insert(settings_node, tk.END, text="缓存管理", values=("可选择五种清理方案，修改保留天数、容量上限与回收目标，并手动清理打开或渲染缓存。",))
        open_node = table.insert("", tk.END, text="打开", values=("展开打开文件与打开底层操作。",), open=True)
        table.insert(open_node, tk.END, text="打开文件", values=("按文件扩展名调用系统默认应用程序打开当前文件。",))
        table.insert(open_node, tk.END, text="打开底层", values=("在系统文件管理器中定位当前文件对应的 Hash 底层文件。",))
        folder_tree_node = table.insert("", tk.END, text="左侧文件夹层级", values=("显示虚拟文件夹层级。",), open=True)
        table.insert(folder_tree_node, tk.END, text="拖动移动", values=("把非根文件夹拖到另一文件夹中以调整层级。",))
        table.insert(folder_tree_node, tk.END, text="右键菜单", values=("可新建子文件夹、重命名、移动或删除虚拟文件夹。",))
        table.insert(folder_tree_node, tk.END, text="排序方式", values=("在文件夹右键菜单选择；标准按主标题排序，序列按系列缩写和编号排序，并应用到当前文件夹及其子文件夹。",))
        content_node = table.insert("", tk.END, text="左侧本文件夹内容", values=("文件夹在前，文件按当前排序方式排列。",), open=True)
        table.insert(content_node, tk.END, text="双击", values=("双击文件夹进入该文件夹；双击文件按系统默认方式打开。",))
        table.insert(content_node, tk.END, text="右键菜单", values=("文件夹可打开、新建、重命名、移动或删除；文件可打开、复制名称、移动、镜像、重命名、替换底层内容、删除镜像或永久删除底层文件。",))
        table.insert(content_node, tk.END, text="外部拖入", values=("将文件拖到目标文件夹或内容列表以导入，并确认命名字段。",))
        for name, description in [
            ("按规则重命名", "可填写系列、标题、版本、作者、备注与标签；主、副标题可选驼峰化或原样，备注和标签不加入文件名。"),
            ("article 标签规则", "带有 article 标签的文件会自动镜像到根文件夹下的 Articles；目录不存在时自动创建。"),
            ("保存文本改动", "仅用于右侧可编辑的纯文本预览；保存后会替换底层文本内容并同步所有镜像。"),
            ("爬取系列", "输入网页地址或 Springer 系列编号，将抓取结果导入新文件夹。"),
            ("帮助", "展开功能介绍和快捷键设置。"),
            ("搜索", "按主标题、副标题、作者搜索去重后的底层文件和全部命中的镜像文件，也可单独按标签搜索镜像文件；镜像结果可右键使用与左侧文件相同的菜单。"),
        ]:
            table.insert("", tk.END, text=name, values=(description,))

        ttk.Button(dialog, text="关闭", command=dialog.destroy).pack(anchor=tk.E, padx=12, pady=(0, 12))
        dialog.bind("<Escape>", lambda _event: dialog.destroy())

    def _shortcut_actions(self) -> list[tuple[str, str, object]]:
        return [
            ("import_file", "导入文件", self.import_file),
            ("create_folder", "新建文件夹", self.create_folder),
            ("rename_folder", "重命名当前文件夹", self.rename_current_folder),
            ("delete_folder", "删除当前文件夹", lambda: self.delete_folder_by_id(self.current_folder_id)),
            ("open_file", "打开文件", self.open_selected_file_default),
            ("open_underlying", "打开底层位置", self.open_blob_location),
            ("mirror_file", "镜像到文件夹", self.mirror_selected_item),
            ("rename_file", "按规则重命名", self.rename_selected_item),
            ("move_file", "移动文件", self.move_selected_item),
            ("delete_mirror", "删除镜像文件", self.delete_selected_item),
            ("delete_underlying", "删除底层文件", self.delete_selected_underlying_file),
            ("copy_file_name", "复制文件名", self.copy_selected_file_name),
            ("replace_content", "替换底层文件", self.replace_selected_content),
            ("save_text", "保存文本改动", self.save_text_preview),
            ("crawl_series", "爬取系列", self.open_crawler_dialog),
            ("search", "搜索", self.open_search_dialog),
            ("function_intro", "功能介绍", self.open_help_dialog),
            ("shortcut_settings", "快捷键设置", self.open_shortcut_dialog),
        ]

    def _load_shortcuts(self) -> None:
        self.shortcut_values = self.store.shortcuts()
        self._apply_shortcuts()

    def _apply_shortcuts(self) -> None:
        for accelerator, binding_id in self.shortcut_binding_ids.items():
            sequence = shortcut_sequence(accelerator)
            if sequence:
                self.unbind(sequence, binding_id)
        self.shortcut_binding_ids = {}

        commands = {action_id: command for action_id, _label, command in self._shortcut_actions()}
        occupied: set[str] = set()
        for action_id, accelerator in self.shortcut_values.items():
            sequence = shortcut_sequence(accelerator)
            command = commands.get(action_id)
            if not sequence or command is None or accelerator in occupied:
                continue
            occupied.add(accelerator)
            binding_id = self.bind(
                sequence,
                lambda _event, command=command: self._run_shortcut(command),
                add="+",
            )
            if binding_id:
                self.shortcut_binding_ids[accelerator] = binding_id

    def _run_shortcut(self, command) -> str:
        command()
        return "break"

    def open_shortcut_dialog(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("快捷键")
        dialog.transient(self)
        dialog.geometry("680x560")
        dialog.minsize(540, 380)

        ttk.Label(dialog, text="快捷键", font=("", 12, "bold")).pack(anchor=tk.W, padx=12, pady=(12, 2))
        ttk.Label(dialog, text="点击右侧按钮后按下组合键；未设置的功能不会占用任何快捷键。", foreground="#555").pack(
            anchor=tk.W, padx=12, pady=(0, 8)
        )

        outer = ttk.Frame(dialog, padding=(12, 0, 12, 8))
        outer.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        content = ttk.Frame(canvas)
        content_window = canvas.create_window((0, 0), window=content, anchor=tk.NW)

        def sync_scroll(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def resize_content(event) -> None:
            canvas.itemconfigure(content_window, width=event.width)

        def scroll(event) -> str:
            if getattr(event, "num", None) == 4:
                canvas.yview_scroll(-3, "units")
            elif getattr(event, "num", None) == 5:
                canvas.yview_scroll(3, "units")
            elif getattr(event, "delta", 0):
                canvas.yview_scroll(int(-event.delta / 120), "units")
            return "break"

        content.bind("<Configure>", sync_scroll)
        canvas.bind("<Configure>", resize_content)
        dialog.bind("<MouseWheel>", scroll)
        dialog.bind("<Button-4>", scroll)
        dialog.bind("<Button-5>", scroll)

        ttk.Label(content, text="功能", anchor=tk.W).grid(row=0, column=0, padx=(6, 12), pady=(4, 6), sticky=tk.EW)
        ttk.Label(content, text="快捷键", anchor=tk.W).grid(row=0, column=1, padx=(6, 6), pady=(4, 6), sticky=tk.EW)
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=1)
        for row, (action_id, label, _command) in enumerate(self._shortcut_actions(), start=1):
            value = tk.StringVar(value=self.shortcut_values.get(action_id, "未设置"))
            ttk.Label(content, text=label, anchor=tk.W).grid(row=row, column=0, padx=(6, 12), pady=3, sticky=tk.EW)
            ttk.Button(
                content,
                textvariable=value,
                command=lambda action_id=action_id, value=value: self.open_shortcut_capture(dialog, action_id, value),
            ).grid(row=row, column=1, padx=(6, 6), pady=3, sticky=tk.EW)

        ttk.Button(dialog, text="关闭", command=dialog.destroy).pack(anchor=tk.E, padx=12, pady=(0, 12))
        dialog.bind("<Escape>", lambda _event: dialog.destroy())

    def open_shortcut_capture(self, parent: tk.Toplevel, action_id: str, value: tk.StringVar) -> None:
        labels = {identifier: label for identifier, label, _command in self._shortcut_actions()}
        dialog = tk.Toplevel(parent)
        dialog.title("设置快捷键")
        dialog.transient(parent)
        dialog.grab_set()
        dialog.resizable(False, False)

        message = tk.StringVar(value="请按下 Ctrl、Alt 或 Shift 组合键，或直接按 F 功能键。")
        ttk.Label(dialog, text=f"功能：{labels[action_id]}").pack(anchor=tk.W, padx=12, pady=(12, 4))
        ttk.Label(dialog, textvariable=message, foreground="#555").pack(anchor=tk.W, padx=12, pady=(0, 8))

        def clear() -> None:
            self.store.set_shortcut(action_id, "")
            self.shortcut_values.pop(action_id, None)
            self._apply_shortcuts()
            value.set("未设置")
            dialog.destroy()

        def capture(event) -> str:
            accelerator = accelerator_from_event(event)
            if not accelerator:
                message.set("请使用 Ctrl、Alt 或 Shift 组合键，或按 F 功能键。")
                return "break"
            conflict = next(
                (identifier for identifier, existing in self.shortcut_values.items() if existing == accelerator and identifier != action_id),
                None,
            )
            if conflict is not None:
                message.set(f"{accelerator} 已分配给“{labels.get(conflict, conflict)}”。")
                return "break"
            self.store.set_shortcut(action_id, accelerator)
            self.shortcut_values[action_id] = accelerator
            self._apply_shortcuts()
            value.set(accelerator)
            dialog.destroy()
            return "break"

        buttons = ttk.Frame(dialog)
        buttons.pack(fill=tk.X, padx=12, pady=(4, 12))
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="清除", command=clear).pack(side=tk.RIGHT, padx=(0, 8))
        dialog.bind("<KeyPress>", capture)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.focus_set()

    def on_item_select(self, _event=None) -> None:
        selection = self.item_tree.selection()
        if not selection:
            return
        node = selection[0]
        if node.startswith("content-folder:"):
            self.current_item = None
            self.preview_folder(int(node.split(":", 1)[1]))
            return
        if not node.startswith("item:"):
            return
        item_id = int(node.split(":", 1)[1])
        self.current_item = self.store.get_item(item_id)
        self.preview_item(self.current_item)

    def on_content_double_click(self, _event=None) -> None:
        selection = self.item_tree.selection()
        if not selection:
            return
        node = selection[0]
        if node.startswith("content-folder:"):
            folder_id = int(node.split(":", 1)[1])
            self.folder_tree.selection_set(f"folder:{folder_id}")
            self.folder_tree.focus(f"folder:{folder_id}")
            self.select_folder(folder_id)
        elif node.startswith("item:"):
            self.open_selected_file_default()

    def create_folder(self) -> None:
        self.create_folder_under(self.current_folder_id)

    def create_folder_under(self, parent_folder_id: int) -> None:
        name = simpledialog.askstring("新建文件夹", "文件夹名称：", parent=self)
        if not name:
            return
        folder_id = self.store.create_folder(parent_folder_id, name)
        self.refresh_folders()
        self.folder_tree.selection_set(f"folder:{folder_id}")
        self.select_folder(folder_id)

    def rename_current_folder(self) -> None:
        self.rename_folder_by_id(self.current_folder_id)

    def rename_folder_by_id(self, folder_id: int) -> None:
        folder = self.store.get_folder(folder_id)
        name = simpledialog.askstring("重命名文件夹", "新的文件夹名称：", initialvalue=folder.name, parent=self)
        if not name:
            return
        self.store.rename_folder(folder.id, name)
        self.refresh_folders()
        self.select_folder(folder.id)
        self.status_var.set("文件夹已重命名。")

    def delete_folder_by_id(self, folder_id: int) -> None:
        folder = self.store.get_folder(folder_id)
        if folder.parent_id is None:
            messagebox.showwarning("删除文件夹", "根文件夹不能删除。", parent=self)
            return
        stats = self.store.folder_stats(folder_id)
        confirmed = messagebox.askyesno(
            "删除文件夹",
            f"删除文件夹“{folder.name}”及其 {stats.folder_count} 个子文件夹、{stats.item_count} 个镜像文件？\n\n"
            "底层 Hash 文件不会被删除。",
            parent=self,
        )
        if not confirmed:
            return
        for descendant_id in self._folder_subtree_ids(folder_id):
            self.folder_sort_modes.pop(descendant_id, None)
        self.store.delete_folder(folder_id)
        self.current_folder_id = folder.parent_id
        self.refresh_folders()
        self.folder_tree.selection_set(f"folder:{self.current_folder_id}")
        self.folder_tree.focus(f"folder:{self.current_folder_id}")
        self.select_folder(self.current_folder_id)
        self.status_var.set("文件夹及其镜像文件已删除；底层文件仍保留。")

    def move_folder_by_id(self, folder_id: int) -> None:
        folder = self.store.get_folder(folder_id)
        if folder.parent_id is None:
            messagebox.showwarning("移动文件夹", "根文件夹不能移动。", parent=self)
            return
        target_folder_id = self.choose_folder(
            "选择移动目标文件夹",
            confirm_label="确认移动",
            excluded_folder_ids=self._folder_subtree_ids(folder_id),
        )
        if target_folder_id is not None:
            self._move_folder_to_target(folder_id, target_folder_id)

    def _folder_subtree_ids(self, folder_id: int) -> set[int]:
        folder_ids = {folder_id}
        for child in self.store.folders(folder_id):
            folder_ids.update(self._folder_subtree_ids(child.id))
        return folder_ids

    def _move_folder_to_target(self, folder_id: int, target_folder_id: int) -> bool:
        try:
            self.store.move_folder(folder_id, target_folder_id)
        except ValueError as exc:
            messagebox.showwarning("移动文件夹", str(exc), parent=self)
            return False
        self.current_folder_id = folder_id
        self.refresh_folders()
        self.folder_tree.selection_set(f"folder:{folder_id}")
        self.folder_tree.focus(f"folder:{folder_id}")
        self.select_folder(folder_id)
        self.status_var.set(f"已移动文件夹到：{self.store.folder_path(target_folder_id)}")
        return True

    def import_file(self) -> None:
        path = filedialog.askopenfilename(title="选择要导入的文件")
        if not path:
            return
        self.import_path_into_folder(Path(path), self.current_folder_id)

    def import_folder(self) -> None:
        path = filedialog.askdirectory(title="选择要添加的文件夹", mustexist=True)
        if not path:
            return

        source_dir = Path(path)
        if not source_dir.is_dir():
            messagebox.showwarning("添加文件夹", f"无法读取文件夹：{source_dir}", parent=self)
            return

        folder_tags = TagPickerDialog(
            self,
            self.store,
            title="为文件夹内所有书籍添加标签",
        ).show()
        if folder_tags is None:
            return

        previous_cursor = self.cget("cursor")
        self.configure(cursor="watch")
        self.status_var.set("正在添加文件夹及其内容，请稍候...")
        self.update_idletasks()
        try:
            root_folder_id, folder_count, file_count, errors = self._import_folder_tree(
                source_dir,
                self.current_folder_id,
                folder_tags,
            )
        except OSError as exc:
            messagebox.showerror("添加文件夹", f"读取文件夹失败：{exc}", parent=self)
            return
        finally:
            self.configure(cursor=previous_cursor)

        self.current_folder_id = root_folder_id
        self.refresh_folders()
        self.folder_tree.selection_set(f"folder:{root_folder_id}")
        self.folder_tree.focus(f"folder:{root_folder_id}")
        self.select_folder(root_folder_id)
        summary = f"已添加文件夹：{source_dir.name}\n导入文件：{file_count}\n创建文件夹：{folder_count}"
        if errors:
            summary += f"\n跳过文件：{len(errors)}"
        messagebox.showinfo("添加文件夹", summary, parent=self)
        if errors:
            messagebox.showwarning("添加文件夹", "部分文件未能导入：\n" + "\n".join(errors[:8]), parent=self)
        self.status_var.set(f"已添加文件夹：{source_dir.name}，导入 {file_count} 个文件。")

    def open_data_location_dialog(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("数据位置")
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)

        content = ttk.Frame(dialog, padding=12)
        content.pack(fill=tk.BOTH, expand=True)
        ttk.Label(content, text="当前数据位置").pack(anchor=tk.W)
        location = ttk.Entry(content, width=70)
        location.insert(0, str(self.store.home))
        location.configure(state="readonly")
        location.pack(fill=tk.X, pady=(4, 12))

        buttons = ttk.Frame(content)
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="关闭", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(
            buttons,
            text="选择新位置并迁移",
            command=lambda: self.select_new_data_location(dialog),
        ).pack(side=tk.RIGHT, padx=(0, 8))

        dialog.bind("<Escape>", lambda _event: dialog.destroy())

    def select_new_data_location(self, dialog: tk.Toplevel) -> None:
        path = filedialog.askdirectory(title="选择新的数据文件夹", parent=dialog, mustexist=True)
        if not path:
            return
        if self.migrate_data_location(Path(path)):
            dialog.destroy()

    def open_cache_management_dialog(self) -> None:
        current = self.store.cache_settings()
        dialog = tk.Toplevel(self)
        dialog.title("缓存管理")
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)

        content = ttk.Frame(dialog, padding=12)
        content.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            content,
            text="缓存只保存打开副本和后端渲染结果；清理前会将已修改的打开副本保存回底层文件。",
            foreground="#555",
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 10))

        policy_values = (*CACHE_POLICY_NAMES, "自定义")
        policy_var = tk.StringVar(value=current.policy if current.policy in policy_values else "自定义")
        open_days_var = tk.StringVar(value=str(current.open_days))
        render_days_var = tk.StringVar(value=str(current.render_days))
        max_gb_var = tk.StringVar(value=self._cache_gb_text(current.max_bytes))
        target_gb_var = tk.StringVar(value=self._cache_gb_text(current.target_bytes))
        clear_on_exit_var = tk.BooleanVar(value=current.clear_open_on_exit)
        usage_var = tk.StringVar()

        ttk.Label(content, text="清理方案").grid(row=1, column=0, sticky=tk.W, pady=3)
        policy_box = ttk.Combobox(content, textvariable=policy_var, values=policy_values, state="readonly", width=22)
        policy_box.grid(row=1, column=1, sticky=tk.EW, pady=3)

        fields = [
            ("打开缓存保留天数", open_days_var),
            ("渲染缓存保留天数", render_days_var),
            ("总缓存上限（GB）", max_gb_var),
            ("回收目标容量（GB）", target_gb_var),
        ]
        for row, (label, variable) in enumerate(fields, start=2):
            ttk.Label(content, text=label).grid(row=row, column=0, sticky=tk.W, pady=3)
            ttk.Entry(content, textvariable=variable, width=24).grid(row=row, column=1, sticky=tk.EW, pady=3)

        ttk.Label(content, text="天数或容量填 0 表示不启用该项限制。", foreground="#555").grid(
            row=6, column=0, columnspan=2, sticky=tk.W, pady=(2, 4)
        )
        ttk.Checkbutton(
            content,
            text="退出软件时清理打开缓存",
            variable=clear_on_exit_var,
        ).grid(row=7, column=0, columnspan=2, sticky=tk.W, pady=(0, 8))

        ttk.Separator(content).grid(row=8, column=0, columnspan=2, sticky=tk.EW, pady=(0, 8))
        ttk.Label(content, textvariable=usage_var).grid(row=9, column=0, columnspan=2, sticky=tk.W, pady=(0, 8))

        def refresh_usage() -> None:
            usage = self.store.cache_usage()
            usage_var.set(
                f"当前占用：打开缓存 {human_size(usage['open_cache'])}；"
                f"渲染缓存 {human_size(usage['render_cache'])}"
            )

        def apply_policy(_event=None) -> None:
            preset = CACHE_POLICIES.get(policy_var.get())
            if preset is None:
                return
            open_days_var.set(str(preset["open_days"]))
            render_days_var.set(str(preset["render_days"]))
            max_gb_var.set(self._cache_gb_text(preset["max_bytes"]))
            target_gb_var.set(self._cache_gb_text(preset["target_bytes"]))
            clear_on_exit_var.set(bool(preset["clear_open_on_exit"]))

        def collect_settings() -> CacheSettings:
            try:
                open_days = int(open_days_var.get().strip())
                render_days = int(render_days_var.get().strip())
                max_bytes = self._cache_gb_to_bytes(max_gb_var.get())
                target_bytes = self._cache_gb_to_bytes(target_gb_var.get())
            except ValueError as exc:
                raise ValueError("天数须为非负整数；容量须为非负数字。") from exc
            policy = policy_var.get()
            selected_preset = CACHE_POLICIES.get(policy)
            current_values = {
                "open_days": open_days,
                "render_days": render_days,
                "max_bytes": max_bytes,
                "target_bytes": target_bytes,
                "clear_open_on_exit": clear_on_exit_var.get(),
            }
            if selected_preset is not None and current_values != selected_preset:
                policy = "自定义"
            return CacheSettings(policy, **current_values)

        def save_settings() -> None:
            try:
                settings = collect_settings()
                self.store.set_cache_settings(settings)
            except ValueError as exc:
                messagebox.showerror("缓存管理", str(exc), parent=dialog)
                return
            reclaimed = self.store.cleanup_caches("after_write")
            policy_var.set(settings.policy)
            refresh_usage()
            self.status_var.set(f"已保存缓存策略：{settings.policy}，本次释放 {human_size(reclaimed)}。")

        def clean_cache(cache_name: str, title: str) -> None:
            if not messagebox.askyesno(
                "清理缓存",
                f"确定清理全部{title}吗？已修改的打开副本会先保存回底层文件。",
                parent=dialog,
            ):
                return
            reclaimed = self.store.clear_cache(cache_name)
            refresh_usage()
            self.status_var.set(f"已清理{title}，释放 {human_size(reclaimed)}。")

        policy_box.bind("<<ComboboxSelected>>", apply_policy)
        content.columnconfigure(1, weight=1)
        refresh_usage()

        actions = ttk.Frame(content)
        actions.grid(row=10, column=0, columnspan=2, sticky=tk.EW, pady=(0, 8))
        ttk.Button(actions, text="清理打开缓存", command=lambda: clean_cache("open_cache", "打开缓存")).pack(side=tk.LEFT)
        ttk.Button(actions, text="清理渲染缓存", command=lambda: clean_cache("render_cache", "渲染缓存")).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(actions, text="保存设置", command=save_settings).pack(side=tk.RIGHT)
        ttk.Button(actions, text="关闭", command=dialog.destroy).pack(side=tk.RIGHT, padx=(0, 8))
        dialog.bind("<Escape>", lambda _event: dialog.destroy())

    @staticmethod
    def _cache_gb_text(size: int) -> str:
        if size <= 0:
            return "0"
        return f"{size / 1024**3:.2f}".rstrip("0").rstrip(".")

    @staticmethod
    def _cache_gb_to_bytes(value: str) -> int:
        size_gb = float(value.strip())
        if size_gb < 0:
            raise ValueError("容量不能小于零")
        return int(size_gb * 1024**3)

    def migrate_data_location(self, target: Path) -> bool:
        source = self.store.home.expanduser().resolve()
        target = target.expanduser().resolve()
        if target == source:
            messagebox.showinfo("数据位置", "选择的位置已是当前数据位置。", parent=self)
            return False
        if target.exists() and any(target.iterdir()):
            messagebox.showerror("数据位置", "新的数据文件夹必须为空。", parent=self)
            return False
        confirmed = messagebox.askyesno(
            "迁移数据",
            f"将全部书籍数据迁移到：\n{target}\n\n迁移完成后将使用新位置。",
            parent=self,
        )
        if not confirmed:
            return False

        try:
            set_configured_library_home(target)
        except OSError as exc:
            messagebox.showerror("数据位置", f"无法保存数据位置设置：{exc}", parent=self)
            return False

        selected_folder_id = self.current_folder_id
        self.store.close()
        try:
            migrate_library_home(source, target)
        except OSError as exc:
            set_configured_library_home(source)
            self.store = LibraryStore(source)
            messagebox.showerror("迁移数据", str(exc), parent=self)
            return False

        self.store = LibraryStore(target)
        self.current_item = None
        self.refresh_folders()
        if not self.folder_tree.exists(f"folder:{selected_folder_id}"):
            selected_folder_id = 1
        self.select_folder(selected_folder_id)
        self._load_shortcuts()
        self.status_var.set(f"数据已迁移到：{self.store.home}")
        return True

    def _import_folder_tree(
        self,
        source_dir: Path,
        parent_folder_id: int,
        tags: tuple[str, ...],
    ) -> tuple[int, int, int, list[str]]:
        root_folder_id = self.store.create_folder(parent_folder_id, source_dir.name)
        folder_ids = {source_dir: root_folder_id}
        folder_count = 1
        file_count = 0
        errors: list[str] = []

        for directory_text, directory_names, file_names in os.walk(source_dir, topdown=True, followlinks=False):
            directory = Path(directory_text)
            target_folder_id = folder_ids[directory]
            directory_names[:] = sorted(
                name for name in directory_names if not (directory / name).is_symlink()
            )
            for directory_name in directory_names:
                child_source = directory / directory_name
                folder_ids[child_source] = self.store.create_folder(target_folder_id, directory_name)
                folder_count += 1

            for file_name in sorted(file_names):
                source_file = directory / file_name
                if source_file.is_symlink() or not source_file.is_file():
                    continue
                try:
                    name_parts = automatic_import_name_parts(source_file)
                    self.store.add_local_file(source_file, target_folder_id, name_parts, tags=tags)
                    file_count += 1
                except (OSError, ValueError) as exc:
                    errors.append(f"{source_file.name}: {exc}")

        return root_folder_id, folder_count, file_count, errors

    def import_path_into_folder(self, source_path: Path, folder_id: int) -> bool:
        source_path = Path(source_path)
        if not source_path.is_file():
            messagebox.showwarning("导入文件", f"无法读取文件：{source_path}", parent=self)
            return False
        file_name_parts = infer_structured_file_name(source_path.name)
        name_dialog = StructuredNameDialog(self, self.store, source_path.name, file_name_parts)
        name_result = name_dialog.show(metadata_path=source_path)
        if name_result is None:
            return False
        name_parts, note, tags = name_result
        try:
            self.store.add_local_file(source_path, folder_id, name_parts, note, tags)
        except ValueError as exc:
            messagebox.showerror("文件名不符合规则", str(exc), parent=self)
            return False
        self.refresh_folders()
        if folder_id == self.current_folder_id:
            self.select_folder(self.current_folder_id)
        self.status_var.set(f"已导入文件：{build_structured_file_name(name_parts)}；{name_dialog.metadata_message}")
        return True

    def _enable_file_drop(self, widget, target: str) -> None:
        if DND_FILES is None:
            return
        widget.drop_target_register(DND_FILES)
        widget.dnd_bind("<<DropEnter>>", lambda _event: COPY)
        widget.dnd_bind("<<DropPosition>>", lambda _event: COPY)
        widget.dnd_bind("<<Drop>>", lambda event, target=target: self.on_file_drop(event, target))

    def on_file_drop(self, event, target: str) -> str:
        folder_id = self._drop_target_folder_id(event, target)
        try:
            source_paths = [Path(path) for path in self.tk.splitlist(event.data)]
        except tk.TclError:
            source_paths = []
        if not source_paths:
            self.status_var.set("未识别到可导入的拖放文件。")
            return COPY
        signature = (folder_id, tuple(str(path.expanduser().resolve()) for path in source_paths))
        now = time.monotonic()
        if signature in self._active_drop_signatures or now - self._recent_drop_signatures.get(signature, 0) < 2:
            return COPY
        self._active_drop_signatures.add(signature)
        try:
            for source_path in source_paths:
                if not source_path.is_file():
                    self.status_var.set(f"拖放项不是可读取的文件：{source_path}")
                    continue
                if not self.import_path_into_folder(source_path, folder_id):
                    break
        finally:
            self._active_drop_signatures.discard(signature)
            self._recent_drop_signatures[signature] = time.monotonic()
            self.after(2000, lambda: self._recent_drop_signatures.pop(signature, None))
        return COPY

    def _drop_target_folder_id(self, event, target: str) -> int:
        if target == "root":
            if self._drop_is_over_widget(event, self.folder_tree):
                target = "folder_tree"
            elif self._drop_is_over_widget(event, self.item_tree):
                target = "item_tree"
            else:
                return self.current_folder_id
        if target == "folder_tree":
            node = self.folder_tree.identify_row(event.y_root - self.folder_tree.winfo_rooty())
            if node.startswith("folder:"):
                return int(node.split(":", 1)[1])
            return self.current_folder_id
        node = self.item_tree.identify_row(event.y_root - self.item_tree.winfo_rooty())
        if node.startswith("content-folder:"):
            return int(node.split(":", 1)[1])
        return self.current_folder_id

    def _drop_is_over_widget(self, event, widget) -> bool:
        return (
            widget.winfo_rootx() <= event.x_root < widget.winfo_rootx() + widget.winfo_width()
            and widget.winfo_rooty() <= event.y_root < widget.winfo_rooty() + widget.winfo_height()
        )

    def mirror_selected_item(self) -> None:
        item = self.require_item()
        if item is None:
            return
        target_folder_id = self.choose_folder("选择镜像目标文件夹")
        if target_folder_id is None:
            return
        try:
            self.store.mirror_item(item.id, target_folder_id)
        except ValueError as exc:
            messagebox.showerror("文件名不符合规则", str(exc))
            return
        if target_folder_id == self.current_folder_id:
            self.select_folder(self.current_folder_id)
        self.status_var.set("镜像已创建；两个位置指向同一个底层文档。")

    def rename_selected_item(self) -> None:
        item = self.require_item()
        if item is None:
            return
        name_result = StructuredNameDialog(
            self,
            self.store,
            formatted_display_name(item),
            item.name_parts,
            item.note,
            item.tags,
        ).show()
        if name_result is None:
            return
        name_parts, note, tags = name_result
        try:
            self.store.rename_item(item.id, name_parts, note, tags)
        except ValueError as exc:
            messagebox.showerror("文件名不符合规则", str(exc))
            return
        self.select_folder(self.current_folder_id)
        self.status_var.set("已重命名。底层文件未复制或改名。")

    def copy_selected_file_name(self) -> None:
        item = self.require_item()
        if item is None:
            return
        file_name_without_extension = Path(formatted_display_name(item)).stem
        self.clipboard_clear()
        self.clipboard_append(file_name_without_extension)
        self.update()
        self.status_var.set(f"已复制文件名：{file_name_without_extension}")

    def delete_selected_item(self) -> None:
        item = self.require_item()
        if item is None:
            return
        confirmed = messagebox.askyesno(
            "删除文件",
            f"删除当前文件夹中的这个镜像文件？\n\n{item.display_name}\n\n底层 hash 文件不会被删除。",
            parent=self,
        )
        if not confirmed:
            return
        self.store.delete_item(item.id)
        self.current_item = None
        self.select_folder(self.current_folder_id)
        self.status_var.set("已删除当前文件夹中的镜像文件。")

    def delete_selected_underlying_file(self) -> None:
        item = self.require_item()
        if item is None:
            return
        confirmed = messagebox.askyesno(
            "删除底层文件",
            "确定删除这个底层文件吗？\n\n"
            f"{formatted_display_name(item)}\n\n"
            "所有引用该底层文件的镜像都会被删除，此操作无法撤回。",
            icon=messagebox.WARNING,
            parent=self,
        )
        if not confirmed:
            return
        try:
            removed_item_count = self.store.delete_underlying_file(item.id)
        except ValueError as exc:
            messagebox.showerror("删除底层文件", str(exc), parent=self)
            return
        self.current_item = None
        self.select_folder(self.current_folder_id)
        self.status_var.set(f"已永久删除底层文件及 {removed_item_count} 个镜像文件。")

    def move_selected_item(self) -> None:
        item = self.require_item()
        if item is None:
            return
        target_folder_id = self.choose_folder("选择移动目标文件夹", confirm_label="确认移动")
        if target_folder_id is None:
            return
        self.store.move_item(item.id, target_folder_id)
        self.current_item = None
        self.select_folder(self.current_folder_id)
        self.status_var.set(f"已移动到：{self.store.folder_path(target_folder_id)}")

    def replace_selected_content(self) -> None:
        item = self.require_item()
        if item is None:
            return
        path = filedialog.askopenfilename(title="选择新的底层文件")
        if not path:
            return
        self.store.replace_document_content(item.document_id, Path(path))
        self.select_folder(self.current_folder_id)
        self.status_var.set("底层文档已替换；所有镜像位置会同时显示新内容。")

    def save_text_preview(self) -> None:
        item = self.require_item()
        if item is None:
            return
        text_widget = getattr(self, "text_preview", None)
        if text_widget is None or not isinstance(text_widget, tk.Text):
            messagebox.showinfo("保存文本改动", "当前预览不是可编辑文本。")
            return
        content = text_widget.get("1.0", tk.END)
        self.store.replace_document_text(item.document_id, content, formatted_display_name(item))
        self.select_folder(self.current_folder_id)
        self.status_var.set("文本已保存为新的 hash 版本；所有镜像位置同步更新。")

    def open_blob_location(self) -> None:
        item = self.require_item()
        if item is None:
            return
        if platform.system() == "Windows":
            subprocess.Popen(["explorer", "/select,", str(item.stored_path)])
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", "-R", str(item.stored_path)])
        else:
            subprocess.Popen(["xdg-open", str(item.stored_path.parent)])

    def open_selected_file_default(self) -> None:
        item = self.require_item()
        if item is None:
            return
        self.open_item_default(item)

    def open_item_default(self, item: Item) -> None:
        open_path = self.prepare_mirror_open_path(item)
        open_path_with_default_app(open_path)
        self.status_var.set(f"已用系统默认方式打开：{formatted_display_name(item)}")

    def prepare_mirror_open_path(self, item: Item) -> Path:
        # Keep open copies distinct while this process is running.  The counter
        # deliberately resets at the next launch after the open cache is cleared.
        self._open_cache_sequence += 1
        cache_dir = self.store.prepare_cache_directory(
            "open_cache", f"open-{self._open_cache_sequence:04d}"
        )
        file_name = safe_name(formatted_display_name(item))
        if not Path(file_name).suffix and item.stored_path.suffix:
            file_name += item.stored_path.suffix
        open_path = cache_dir / file_name
        shutil.copy2(item.stored_path, open_path)
        self.store.register_open_cache_copy(
            cache_dir,
            item.document_id,
            item.sha256,
            open_path.name,
        )
        self.store.touch_cache_directory(cache_dir)
        self.store.cleanup_caches("after_write")
        return open_path

    def show_folder_menu(self, event) -> None:
        node = self.folder_tree.identify_row(event.y)
        if not node:
            return
        self.folder_tree.selection_set(node)
        self.folder_tree.focus(node)
        self.current_folder_id = int(node.split(":", 1)[1])
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="新建子文件夹", command=self.create_folder)
        menu.add_command(label="重命名文件夹", command=self.rename_current_folder)
        menu.add_command(label="移动", command=lambda folder_id=self.current_folder_id: self.move_folder_by_id(folder_id))
        self.add_folder_sort_menu(menu, self.current_folder_id)
        menu.add_separator()
        menu.add_command(label="删除文件夹", command=lambda folder_id=self.current_folder_id: self.delete_folder_by_id(folder_id))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def show_item_menu(self, event) -> None:
        node = self.item_tree.identify_row(event.y)
        if not node:
            return
        self.item_tree.selection_set(node)
        self.item_tree.focus(node)
        if node.startswith("content-folder:"):
            self.current_item = None
            folder_id = int(node.split(":", 1)[1])
            menu = tk.Menu(self, tearoff=False)
            menu.add_command(label="打开文件夹", command=lambda folder_id=folder_id: self.open_content_folder(folder_id))
            menu.add_command(label="新建子文件夹", command=lambda folder_id=folder_id: self.create_folder_under(folder_id))
            menu.add_command(label="重命名文件夹", command=lambda folder_id=folder_id: self.rename_folder_by_id(folder_id))
            menu.add_command(label="移动", command=lambda folder_id=folder_id: self.move_folder_by_id(folder_id))
            self.add_folder_sort_menu(menu, folder_id)
            menu.add_separator()
            menu.add_command(label="删除文件夹", command=lambda folder_id=folder_id: self.delete_folder_by_id(folder_id))
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
            return
        if not node.startswith("item:"):
            return
        self.show_file_menu(event, self.store.get_item(int(node.split(":", 1)[1])))

    def show_file_menu(self, event, item: Item) -> None:
        self.current_item = item
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="用系统默认方式打开", command=self.open_selected_file_default)
        menu.add_command(label="打开底层位置", command=self.open_blob_location)
        menu.add_command(label="复制文件名", command=self.copy_selected_file_name)
        menu.add_separator()
        menu.add_command(label="删除", command=self.delete_selected_item)
        menu.add_command(label="删除底层文件", command=self.delete_selected_underlying_file)
        menu.add_command(label="重命名", command=self.rename_selected_item)
        menu.add_command(label="移动", command=self.move_selected_item)
        menu.add_separator()
        menu.add_command(label="镜像到文件夹", command=self.mirror_selected_item)
        menu.add_command(label="替换底层文件", command=self.replace_selected_content)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def add_folder_sort_menu(self, menu: tk.Menu, folder_id: int) -> None:
        sort_menu = tk.Menu(menu, tearoff=False)
        selected_mode = tk.StringVar(master=sort_menu, value=self._folder_sort_mode_for(folder_id))
        sort_menu._selected_mode = selected_mode  # Keep the Tk variable alive while the menu is open.
        for mode in ("标准", "序列"):
            sort_menu.add_radiobutton(
                label=mode,
                variable=selected_mode,
                value=mode,
                command=lambda selected_mode=mode, folder_id=folder_id: self.set_folder_sort_mode(folder_id, selected_mode),
            )
        menu.add_cascade(label="排序方式", menu=sort_menu)

    def open_content_folder(self, folder_id: int) -> None:
        node = f"folder:{folder_id}"
        if self.folder_tree.exists(node):
            self.folder_tree.selection_set(node)
            self.folder_tree.focus(node)
        self.select_folder(folder_id)

    def open_crawler_dialog(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("爬取网络系列数据")
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)

        source_var = tk.StringVar()
        ttk.Label(dialog, text="输入网址或 Springer 系列编号：").grid(row=0, column=0, padx=12, pady=(12, 4), sticky=tk.W)
        entry = ttk.Entry(dialog, textvariable=source_var, width=56)
        entry.grid(row=1, column=0, padx=12, pady=4, sticky=tk.EW)
        ttk.Label(dialog, text="爬取结果会新建一个以系列全称命名的文件夹。", foreground="#555").grid(
            row=2, column=0, padx=12, pady=(0, 8), sticky=tk.W
        )

        buttons = ttk.Frame(dialog)
        buttons.grid(row=3, column=0, padx=12, pady=(4, 12), sticky=tk.E)
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(
            buttons,
            text="开始爬取",
            command=lambda: self.start_crawl(dialog, source_var.get()),
        ).pack(side=tk.RIGHT, padx=(0, 8))
        entry.focus_set()

    def start_crawl(self, dialog: tk.Toplevel, source: str) -> None:
        if not source.strip():
            messagebox.showwarning("爬取系列", "请输入网址或 Springer 系列编号。")
            return
        dialog.destroy()
        parent_folder_id = self.current_folder_id
        self.status_var.set("正在爬取，请稍候...")

        def worker() -> None:
            worker_store = LibraryStore(self.store.home)
            try:
                result = crawl_into_library(worker_store, source, parent_folder_id)
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("爬取失败", str(exc)))
                self.after(0, lambda: self.status_var.set("爬取失败。"))
                return
            finally:
                worker_store.close()

            def done() -> None:
                self.refresh_folders()
                self.folder_tree.selection_set(f"folder:{result.folder_id}")
                self.select_folder(result.folder_id)
                details = "\n".join(result.messages[-6:])
                messagebox.showinfo("爬取完成", f"文件夹：{result.folder_name}\n导入文件：{result.downloaded}\n\n{details}")
                self.status_var.set(f"爬取完成：{result.folder_name}，导入 {result.downloaded} 个文件。")

            self.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    def open_search_dialog(self) -> None:
        criteria = StructuredSearchDialog(self).show()
        if criteria is None:
            return
        self.run_structured_search(criteria)

    def run_structured_search(self, criteria: dict[str, dict[str, str]]) -> None:
        file_filters = criteria["files"]
        mirror_filters = criteria["mirrors"]
        tag_query = criteria["tags"]["query"]
        has_file_filters = any(file_filters.values())
        has_mirror_filters = any(mirror_filters.values())
        has_tag_query = bool(tag_query)
        if sum((has_file_filters, has_mirror_filters, has_tag_query)) != 1:
            messagebox.showinfo("搜索", "请仅填写“底层文件”、“镜像文件”或“标签搜索”其中一项。")
            return

        if has_file_filters:
            file_results = self.store.search_unique_documents(
                file_filters["main_title"],
                file_filters["subtitle"],
                file_filters["authors"],
            )
            document_count = len({item.document_id for _, item in file_results})
            self._set_preview_title("底层文件搜索结果")
            self.preview_meta.configure(text=f"{document_count} 个底层文件 | {len(file_results)} 个镜像文件")
            self._show_search_results(file_results=file_results)
            self.status_var.set(f"搜索完成：{document_count} 个底层文件，显示 {len(file_results)} 个镜像文件。")
            return

        if has_tag_query:
            tagged_results = self.store.search_tagged_items(tag_query)
            self._set_preview_title("标签搜索结果")
            self.preview_meta.configure(text=f"标签“{tag_query}”匹配 {len(tagged_results)} 个镜像文件")
            self._show_search_results(mirror_results=tagged_results)
            self.status_var.set(f"标签搜索完成：{len(tagged_results)} 个镜像文件。")
            return

        mirror_results = self.store.search_mirror_items(
            mirror_filters["main_title"],
            mirror_filters["subtitle"],
            mirror_filters["authors"],
        )
        self._set_preview_title("镜像文件搜索结果")
        self.preview_meta.configure(text=f"{len(mirror_results)} 个镜像文件")
        self._show_search_results(mirror_results=mirror_results)
        self.status_var.set(f"搜索完成：{len(mirror_results)} 个镜像文件。")

    def _show_search_results(
        self,
        file_results: list[tuple[str, Item]] | None = None,
        mirror_results: list[tuple[str, Item]] | None = None,
    ) -> None:
        self._clear_preview()
        if file_results is not None:
            file_frame = ttk.LabelFrame(self.preview_container, text="底层唯一文件的全部镜像", padding=(8, 6))
            file_frame.pack(fill=tk.BOTH, expand=True)
            self.search_file_tree = ttk.Treeview(
                file_frame,
                columns=("folder", "size", "type", "note"),
                show="tree headings",
                selectmode="browse",
            )
            self.search_file_tree.heading("#0", text="镜像文件")
            self.search_file_tree.heading("folder", text="所在目录")
            self.search_file_tree.heading("size", text="大小")
            self.search_file_tree.heading("type", text="类型")
            self.search_file_tree.heading("note", text="备注")
            self.search_file_tree.column("#0", width=280, minwidth=180, stretch=True)
            self.search_file_tree.column("folder", width=220, minwidth=150, stretch=True)
            self.search_file_tree.column("size", width=86, anchor=tk.E, stretch=False)
            self.search_file_tree.column("type", width=130, stretch=False)
            self.search_file_tree.column("note", width=180, minwidth=100, stretch=True)
            file_scroll = ttk.Scrollbar(file_frame, orient=tk.VERTICAL, command=self.search_file_tree.yview)
            self.search_file_tree.configure(yscrollcommand=file_scroll.set)
            self.search_file_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            file_scroll.pack(side=tk.RIGHT, fill=tk.Y)
            self.search_file_tree.bind("<Double-Button-1>", self.open_search_file_result)
            self.search_file_tree.bind("<Return>", self.open_search_file_result)
            self.search_file_tree.bind("<Button-1>", self.toggle_search_file_group_from_click, add="+")
            self._search_file_group_labels = {}
            for mirrors in group_underlying_search_results(file_results):
                if len(mirrors) == 1:
                    folder_path, item = mirrors[0]
                    self.search_file_tree.insert(
                        "",
                        tk.END,
                        iid=f"search-item:{item.id}",
                        text=formatted_display_name(item),
                        values=(folder_path, human_size(item.size), item.mime, item.note),
                    )
                    continue

                _, first_item = mirrors[0]
                group_iid = f"search-document:{first_item.document_id}"
                group_label = f"{formatted_display_name(first_item)}（{len(mirrors)} 个镜像）"
                self._search_file_group_labels[group_iid] = group_label
                self.search_file_tree.insert(
                    "",
                    tk.END,
                    iid=group_iid,
                    text=group_label,
                    values=("", f"{len(mirrors)} 个镜像", "底层文件", ""),
                    open=False,
                )
                for folder_path, item in mirrors:
                    self.search_file_tree.insert(
                        group_iid,
                        tk.END,
                        iid=f"search-item:{item.id}",
                        text=formatted_display_name(item),
                        values=(folder_path, human_size(item.size), item.mime, item.note),
                    )
            if not file_results:
                self.search_file_tree.insert("", tk.END, text="没有匹配的底层唯一文件。", values=("", "", "", ""))
            return

        if mirror_results is None:
            return

        mirror_frame = ttk.LabelFrame(self.preview_container, text="镜像文件", padding=(8, 6))
        mirror_frame.pack(fill=tk.BOTH, expand=True)
        self.search_mirror_tree = ttk.Treeview(
            mirror_frame,
            columns=("folder", "size", "type"),
            show="tree headings",
            selectmode="browse",
        )
        self.search_mirror_tree.heading("#0", text="镜像文件")
        self.search_mirror_tree.heading("folder", text="所在目录")
        self.search_mirror_tree.heading("size", text="大小")
        self.search_mirror_tree.heading("type", text="类型")
        self.search_mirror_tree.column("#0", width=280, minwidth=180, stretch=True)
        self.search_mirror_tree.column("folder", width=220, minwidth=150, stretch=True)
        self.search_mirror_tree.column("size", width=86, anchor=tk.E, stretch=False)
        self.search_mirror_tree.column("type", width=100, stretch=False)
        mirror_scroll = ttk.Scrollbar(mirror_frame, orient=tk.VERTICAL, command=self.search_mirror_tree.yview)
        self.search_mirror_tree.configure(yscrollcommand=mirror_scroll.set)
        self.search_mirror_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        mirror_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.search_mirror_tree.bind("<Double-Button-1>", self.open_search_mirror_result)
        self.search_mirror_tree.bind("<Return>", self.open_search_mirror_result)
        self.search_mirror_tree.bind("<Button-3>", self.show_search_mirror_menu)
        self.search_mirror_tree.bind("<Button-2>", self.show_search_mirror_menu)
        for folder_path, item in mirror_results:
            self.search_mirror_tree.insert(
                "",
                tk.END,
                iid=f"search-mirror:{item.id}",
                text=formatted_display_name(item),
                values=(folder_path, human_size(item.size), display_type_for_item(item)),
            )
        if not mirror_results:
            self.search_mirror_tree.insert("", tk.END, text="没有匹配的镜像文件。", values=("", "", ""))

    def open_search_file_result(self, _event=None) -> None:
        tree = getattr(self, "search_file_tree", None)
        if tree is None:
            return
        selection = tree.selection()
        if not selection:
            return
        selected_iid = selection[0]
        if selected_iid.startswith("search-document:"):
            tree.item(selected_iid, open=not bool(tree.item(selected_iid, "open")))
            return
        if not selected_iid.startswith("search-item:"):
            return
        item = self.store.get_item(int(selected_iid.split(":", 1)[1]))
        self.current_item = item
        self.open_item_default(item)

    def toggle_search_file_group_from_click(self, event) -> None:
        tree = event.widget
        group_iid = tree.identify_row(event.y)
        if group_iid not in self._search_file_group_labels:
            return
        element = tree.identify_element(event.x, event.y)
        if "indicator" in element.lower() or tree.identify_region(event.x, event.y) != "tree":
            return
        self.after_idle(lambda: self._toggle_search_file_group(tree, group_iid))

    def _toggle_search_file_group(self, tree: ttk.Treeview, group_iid: str) -> None:
        if not tree.winfo_exists() or not tree.exists(group_iid):
            return
        tree.item(group_iid, open=not bool(tree.item(group_iid, "open")))

    def open_search_mirror_result(self, _event=None) -> None:
        tree = getattr(self, "search_mirror_tree", None)
        if tree is None:
            return
        selection = tree.selection()
        if not selection or not selection[0].startswith("search-mirror:"):
            return
        item = self.store.get_item(int(selection[0].split(":", 1)[1]))
        self.current_item = item
        self.open_item_default(item)

    def show_search_mirror_menu(self, event) -> None:
        tree = getattr(self, "search_mirror_tree", None)
        if tree is None:
            return
        node = tree.identify_row(event.y)
        if not node.startswith("search-mirror:"):
            return
        tree.selection_set(node)
        tree.focus(node)
        item = self.store.get_item(int(node.split(":", 1)[1]))
        self.show_file_menu(event, item)

    def preview_item(self, item: Item) -> None:
        self._set_preview_title(formatted_display_name(item))
        self.preview_meta.configure(text="")
        suffix = item.name_parts.extension.lower() or item.stored_path.suffix.lower()
        if item.mime.startswith("text/") or suffix in TEXT_EXTENSIONS:
            self._show_text_file(item)
        elif item.mime.startswith("image/") or suffix in {".png", ".gif", ".ppm", ".pgm"}:
            self._show_image_file(item)
        elif suffix in {".pdf", ".djvu", ".djv", ".epub"}:
            self._show_rendered_document_preview(item, suffix)
        elif suffix == ".pdf" or item.mime == "application/pdf":
            self._show_pdf_summary(item)
        else:
            self._show_binary_summary(item)

    def preview_folder(self, folder_id: int) -> None:
        folder = self.store.get_folder(folder_id)
        stats = self.store.folder_stats(folder_id)
        self._set_preview_title(folder.name)
        self.preview_meta.configure(text="")
        lines = [
            "文件夹信息",
            "",
            f"名称: {folder.name}",
            f"目录: {self.store.folder_path(folder_id)}",
            f"占用内存: {human_size(stats.unique_size)}",
            f"内部文件数量: {stats.item_count}",
            f"底层唯一文件数量: {stats.unique_document_count}",
            f"子文件夹数量: {stats.folder_count}",
        ]
        self._show_message("\n".join(lines))

    def _clear_preview(self) -> None:
        for child in self.preview_container.winfo_children():
            child.destroy()
        self.preview_image = None
        self.preview_images = []
        self.text_preview = None

    def _show_message(self, message: str) -> None:
        self._clear_preview()
        text = tk.Text(self.preview_container, wrap=tk.WORD, borderwidth=0, padx=10, pady=10)
        text.insert("1.0", message)
        text.configure(state=tk.DISABLED)
        text.pack(fill=tk.BOTH, expand=True)

    def _show_text_file(self, item: Item) -> None:
        self._clear_preview()
        text = tk.Text(self.preview_container, wrap=tk.WORD, undo=True, padx=10, pady=10)
        content = item.stored_path.read_text(encoding="utf-8", errors="replace")
        text.insert("1.0", content)
        text.pack(fill=tk.BOTH, expand=True)
        self.text_preview = text

    def _show_image_file(self, item: Item) -> None:
        self._clear_preview()
        try:
            self.preview_image = tk.PhotoImage(file=str(item.stored_path))
        except tk.TclError:
            self._show_binary_summary(item, "当前 Tk 运行环境不能直接预览这种图片格式，可使用“打开底层位置”查看。")
            return
        canvas = tk.Canvas(self.preview_container, background="#f6f6f6", highlightthickness=0)
        canvas.pack(fill=tk.BOTH, expand=True)

        def redraw(_event=None) -> None:
            if not self.preview_image:
                return
            canvas.delete("all")
            width = canvas.winfo_width()
            height = canvas.winfo_height()
            image_width = self.preview_image.width()
            image_height = self.preview_image.height()
            canvas.create_image(
                max((width - image_width) // 2, 0),
                max((height - image_height) // 2, 0),
                anchor=tk.NW,
                image=self.preview_image,
            )

        canvas.bind("<Configure>", redraw)
        redraw()

    def _show_rendered_document_preview(self, item: Item, suffix: str) -> None:
        cache_dir = self.store.prepare_cache_directory("render_cache", item.sha256)
        result = render_preview(item.stored_path, cache_dir, pages=2)
        self.store.touch_cache_directory(cache_dir)
        self.store.cleanup_caches("after_write")
        if result.kind == "images" and result.paths:
            self._show_image_pages(result.paths)
            return
        if result.kind == "text" and result.text:
            self._show_readonly_text(result.text)
            return
        if suffix == ".pdf":
            self._show_pdf_summary(item, result.message)
        else:
            self._show_binary_summary(item, result.message)

    def _show_image_pages(self, paths: list[Path]) -> None:
        self._clear_preview()
        outer = ttk.Frame(self.preview_container)
        outer.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(outer, background="#f6f6f6", highlightthickness=0)
        y_scroll = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=y_scroll.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        y_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        def redraw(_event=None) -> None:
            canvas.delete("all")
            self.preview_images = []
            available_width = max(canvas.winfo_width() - 28, 1)
            y = 14
            max_width = 0
            for path in paths:
                try:
                    source = tk.PhotoImage(file=str(path))
                except tk.TclError:
                    continue
                factor = max(1, (source.width() + available_width - 1) // available_width)
                image = source.subsample(factor, factor) if factor > 1 else source
                self.preview_images.extend([source, image])
                x = max((canvas.winfo_width() - image.width()) // 2, 0)
                canvas.create_image(x, y, anchor=tk.NW, image=image)
                y += image.height() + 18
                max_width = max(max_width, image.width())
            canvas.configure(scrollregion=(0, 0, min(max_width + 28, canvas.winfo_width()), y))

        def on_mousewheel(event) -> str:
            if getattr(event, "num", None) == 4:
                canvas.yview_scroll(-3, "units")
            elif getattr(event, "num", None) == 5:
                canvas.yview_scroll(3, "units")
            else:
                delta = getattr(event, "delta", 0)
                if delta:
                    canvas.yview_scroll(int(-1 * (delta / 120)), "units")
            return "break"

        canvas.bind("<Configure>", redraw)
        canvas.bind("<MouseWheel>", on_mousewheel)
        canvas.bind("<Button-4>", on_mousewheel)
        canvas.bind("<Button-5>", on_mousewheel)
        outer.bind("<MouseWheel>", on_mousewheel)
        outer.bind("<Button-4>", on_mousewheel)
        outer.bind("<Button-5>", on_mousewheel)
        self.preview_container.bind("<MouseWheel>", on_mousewheel)
        self.preview_container.bind("<Button-4>", on_mousewheel)
        self.preview_container.bind("<Button-5>", on_mousewheel)
        redraw()

    def _show_readonly_text(self, content: str) -> None:
        self._clear_preview()
        text = tk.Text(self.preview_container, wrap=tk.WORD, padx=10, pady=10)
        text.insert("1.0", content)
        text.configure(state=tk.DISABLED)
        text.pack(fill=tk.BOTH, expand=True)

    def _show_pdf_summary(self, item: Item, note: str | None = None) -> None:
        head = item.stored_path.read_bytes()[:2048]
        text = [
            "PDF 文件",
            "",
            f"显示名: {formatted_display_name(item)}",
            f"底层 hash: {item.sha256}",
            f"大小: {human_size(item.size)}",
            "",
            note or "当前环境未能内嵌渲染 PDF。",
            "可以通过“打开底层位置”在系统 PDF 阅读器中预览；替换底层文件后，所有镜像会同步到新 PDF。",
            "",
            "文件头:",
            head.decode("latin-1", errors="replace")[:1000],
        ]
        self._show_message("\n".join(text))

    def _show_binary_summary(self, item: Item, note: str | None = None) -> None:
        text = [
            note or "二进制文件预览",
            "",
            f"显示名: {formatted_display_name(item)}",
            f"底层 hash: {item.sha256}",
            f"大小: {human_size(item.size)}",
            f"类型: {item.mime}",
            f"底层路径: {item.stored_path}",
        ]
        self._show_message("\n".join(text))

    def choose_folder(
        self,
        title: str,
        confirm_label: str = "选择",
        excluded_folder_ids: set[int] | None = None,
    ) -> int | None:
        return FolderBrowserDialog(self, self.store, title, confirm_label, excluded_folder_ids).show()

    def require_item(self) -> Item | None:
        if self.current_item is None:
            messagebox.showinfo("需要选择文件", "请先在左下角选择一个文件。")
            return None
        return self.store.get_item(self.current_item.id)

    def on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._stop_content_marquee()
        if self._preview_title_after_id is not None:
            self.after_cancel(self._preview_title_after_id)
        self._cleanup_caches_before_exit()
        self.store.close()
        self.destroy()

    def _quit_application(self, _event=None) -> str:
        self.on_close()
        return "break"

    def _cleanup_caches_before_exit(self) -> None:
        try:
            self.store.cleanup_caches("exit")
            self.store.cleanup_unreferenced_blobs()
            return
        except Exception:
            pass
        try:
            fallback_store = LibraryStore(self.store.home)
            try:
                fallback_store.cleanup_caches("exit")
                fallback_store.cleanup_unreferenced_blobs()
            finally:
                fallback_store.close()
        except Exception:
            pass

    def _cleanup_caches_on_process_exit(self) -> None:
        if not self._closing:
            self._cleanup_caches_before_exit()


class FolderBrowserDialog:
    """Navigate the virtual folder tree and select a folder as an operation target."""

    def __init__(
        self,
        parent: tk.Misc,
        store: LibraryStore,
        title: str,
        confirm_label: str,
        excluded_folder_ids: set[int] | None = None,
    ) -> None:
        self.parent = parent
        self.store = store
        self.title = title
        self.confirm_label = confirm_label
        self.excluded_folder_ids = excluded_folder_ids or set()
        self.result: int | None = None
        self.current_folder_id = 1
        self.selected_folder_id: int | None = None
        self.target_var = tk.StringVar(value="目标文件夹：尚未选择")

    def show(self) -> int | None:
        dialog = tk.Toplevel(self.parent)
        self.dialog = dialog
        dialog.title(self.title)
        dialog.transient(self.parent)
        dialog.grab_set()
        dialog.geometry("560x440")
        dialog.minsize(440, 320)

        container = ttk.Frame(dialog, padding=12)
        container.pack(fill=tk.BOTH, expand=True)

        self.breadcrumb = ttk.Frame(container)
        self.breadcrumb.pack(fill=tk.X, pady=(0, 8))

        list_frame = ttk.Frame(container)
        list_frame.pack(fill=tk.BOTH, expand=True)
        self.folder_list = ttk.Treeview(list_frame, show="tree", selectmode="browse")
        self.folder_list.heading("#0", text="文件夹")
        folder_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.folder_list.yview)
        self.folder_list.configure(yscrollcommand=folder_scroll.set)
        self.folder_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        folder_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.folder_list.bind("<<TreeviewSelect>>", self._select_folder)
        self.folder_list.bind("<Double-Button-1>", self._open_selected_folder)

        ttk.Label(container, textvariable=self.target_var, anchor=tk.W).pack(fill=tk.X, pady=(8, 4))
        buttons = ttk.Frame(container)
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side=tk.RIGHT)
        self.confirm_button = ttk.Button(buttons, text=self.confirm_label, command=self._accept, state=tk.DISABLED)
        self.confirm_button.pack(side=tk.RIGHT, padx=(0, 8))

        dialog.bind("<Return>", lambda _event: self._accept())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        self._show_folder(1)
        self.parent.wait_window(dialog)
        return self.result

    def _folder_ancestry(self, folder_id: int) -> list[Folder]:
        folders: list[Folder] = []
        current = self.store.get_folder(folder_id)
        while True:
            folders.append(current)
            if current.parent_id is None:
                break
            current = self.store.get_folder(current.parent_id)
        return list(reversed(folders))

    def _render_breadcrumb(self) -> None:
        for child in self.breadcrumb.winfo_children():
            child.destroy()
        for index, folder in enumerate(self._folder_ancestry(self.current_folder_id)):
            if index:
                ttk.Label(self.breadcrumb, text=" / ").pack(side=tk.LEFT)
            label = ttk.Label(self.breadcrumb, text=folder.name, cursor="hand2")
            label.pack(side=tk.LEFT)
            label.bind("<Double-Button-1>", lambda _event, folder_id=folder.id: self._show_folder(folder_id))

    def _show_folder(self, folder_id: int) -> None:
        self.current_folder_id = folder_id
        self.selected_folder_id = None
        self._render_breadcrumb()
        self.folder_list.delete(*self.folder_list.get_children())

        current = self.store.get_folder(folder_id)
        self.folder_list.insert("", tk.END, iid=f"current:{current.id}", text=f"当前文件夹：{current.name}")
        for child in self.store.folders(folder_id):
            if child.id in self.excluded_folder_ids:
                continue
            self.folder_list.insert("", tk.END, iid=f"folder:{child.id}", text=child.name)

        self.target_var.set("目标文件夹：尚未选择")
        self.confirm_button.configure(state=tk.DISABLED)

    def _select_folder(self, _event=None) -> None:
        selection = self.folder_list.selection()
        if not selection:
            return
        selected_iid = selection[0]
        if selected_iid.startswith("current:"):
            self.selected_folder_id = int(selected_iid.split(":", 1)[1])
        elif selected_iid.startswith("folder:"):
            self.selected_folder_id = int(selected_iid.split(":", 1)[1])
        else:
            return
        self.target_var.set(f"目标文件夹：{self.store.folder_path(self.selected_folder_id)}")
        self.confirm_button.configure(state=tk.NORMAL)

    def _open_selected_folder(self, event) -> str:
        selected_iid = self.folder_list.identify_row(event.y)
        if not selected_iid.startswith("folder:"):
            return "break"
        self._show_folder(int(selected_iid.split(":", 1)[1]))
        return "break"

    def _accept(self) -> None:
        if self.selected_folder_id is None:
            return
        self.result = self.selected_folder_id
        self.dialog.destroy()


class StructuredSearchDialog:
    def __init__(self, parent: tk.Tk) -> None:
        self.parent = parent
        self.result: dict[str, dict[str, str]] | None = None
        self.values = {
            "files": {
                "main_title": tk.StringVar(),
                "subtitle": tk.StringVar(),
                "authors": tk.StringVar(),
            },
            "mirrors": {
                "main_title": tk.StringVar(),
                "subtitle": tk.StringVar(),
                "authors": tk.StringVar(),
            },
            "tags": {
                "query": tk.StringVar(),
            },
        }

    def show(self) -> dict[str, dict[str, str]] | None:
        dialog = tk.Toplevel(self.parent)
        self.dialog = dialog
        dialog.title("搜索")
        dialog.transient(self.parent)
        dialog.grab_set()
        dialog.resizable(False, False)

        container = ttk.Frame(dialog, padding=(12, 12))
        container.pack(fill=tk.BOTH, expand=True)

        headings = ["匹配主标题", "匹配副标题", "匹配作者名"]
        row_configs = [
            ("底层文件", "files"),
            ("镜像文件", "mirrors"),
        ]
        first_entry: ttk.Entry | None = None
        for row_index, (row_label, key) in enumerate(row_configs):
            ttk.Label(container, text=row_label).grid(row=row_index * 2, column=0, columnspan=3, sticky=tk.W, pady=(0, 4))
            for column, (heading, field_key) in enumerate(zip(headings, ("main_title", "subtitle", "authors"))):
                box = ttk.LabelFrame(container, text=heading, padding=(8, 6))
                box.grid(row=row_index * 2 + 1, column=column, padx=(0 if column == 0 else 8, 0), pady=(0, 12), sticky=tk.EW)
                entry = ttk.Entry(box, textvariable=self.values[key][field_key], width=26)
                entry.pack(fill=tk.X)
                if first_entry is None:
                    first_entry = entry

        tag_box = ttk.LabelFrame(container, text="标签搜索", padding=(8, 6))
        tag_box.grid(row=4, column=0, columnspan=3, pady=(0, 12), sticky=tk.EW)
        ttk.Label(tag_box, text="匹配标签").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Entry(tag_box, textvariable=self.values["tags"]["query"], width=72).pack(side=tk.LEFT, fill=tk.X, expand=True)

        buttons = ttk.Frame(container)
        buttons.grid(row=5, column=0, columnspan=3, sticky=tk.E)
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="搜索", command=self.accept).pack(side=tk.RIGHT, padx=(0, 8))

        dialog.bind("<Return>", lambda _event: self.accept())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        if first_entry is not None:
            first_entry.focus_set()
        self.parent.wait_window(dialog)
        return self.result

    def accept(self) -> None:
        filled_rows = sum(
            any(var.get().strip() for var in fields.values())
            for fields in self.values.values()
        )
        if filled_rows != 1:
            messagebox.showinfo("搜索", "请仅填写“底层文件”、“镜像文件”或“标签搜索”其中一项。", parent=self.dialog)
            return
        self.result = {
            row_key: {
                field_key: var.get().strip()
                for field_key, var in fields.items()
            }
            for row_key, fields in self.values.items()
        }
        self.dialog.destroy()


class TagPickerDialog:
    def __init__(
        self,
        parent: tk.Misc,
        store: LibraryStore,
        initial_tags: tuple[str, ...] = (),
        title: str = "选择标签",
    ) -> None:
        self.parent = parent
        self.store = store
        self.initial_tags = normalize_tag_names(initial_tags)
        self.title = title
        self.result: tuple[str, ...] | None = None

    def show(self) -> tuple[str, ...] | None:
        dialog = tk.Toplevel(self.parent)
        self.dialog = dialog
        dialog.title(self.title)
        dialog.transient(self.parent)
        dialog.grab_set()
        dialog.geometry("620x400")
        dialog.minsize(500, 320)

        content = ttk.Frame(dialog, padding=12)
        content.pack(fill=tk.BOTH, expand=True)
        ttk.Label(content, text="选择已有标签，或在下方输入新标签。常用标签至少已用于 3 个镜像文件。", foreground="#555").pack(
            anchor=tk.W, pady=(0, 8)
        )

        lists = ttk.Frame(content)
        lists.pack(fill=tk.BOTH, expand=True)
        common, general = self.store.tags_by_frequency()
        self._tag_names_by_list: dict[tk.Listbox, list[str]] = {}
        selected_keys = {tag.casefold() for tag in self.initial_tags}
        for column, (label, tags) in enumerate((("常用标签", common), ("一般标签", general))):
            frame = ttk.LabelFrame(lists, text=label, padding=(6, 6))
            frame.grid(row=0, column=column, padx=(0 if column == 0 else 8, 0), sticky=tk.NSEW)
            listbox = tk.Listbox(frame, selectmode=tk.MULTIPLE, exportselection=False, height=12)
            scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=listbox.yview)
            listbox.configure(yscrollcommand=scrollbar.set)
            listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
            names = [tag.name for tag in tags]
            self._tag_names_by_list[listbox] = names
            for index, tag in enumerate(tags):
                listbox.insert(tk.END, f"{tag.name} ({tag.usage_count})")
                if tag.name.casefold() in selected_keys:
                    listbox.selection_set(index)
        lists.columnconfigure(0, weight=1)
        lists.columnconfigure(1, weight=1)
        lists.rowconfigure(0, weight=1)

        new_tags_var = tk.StringVar()
        new_tags = ttk.LabelFrame(content, text="新增或补充标签（用逗号分隔）", padding=(8, 6))
        new_tags.pack(fill=tk.X, pady=(10, 0))
        ttk.Entry(new_tags, textvariable=new_tags_var).pack(fill=tk.X)

        def accept() -> None:
            visible_keys = {
                name.casefold()
                for names in self._tag_names_by_list.values()
                for name in names
            }
            selected = [tag for tag in self.initial_tags if tag.casefold() not in visible_keys]
            for listbox, names in self._tag_names_by_list.items():
                selected.extend(names[index] for index in listbox.curselection())
            selected.append(new_tags_var.get())
            self.result = normalize_tag_names(selected)
            dialog.destroy()

        buttons = ttk.Frame(content)
        buttons.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="确定", command=accept).pack(side=tk.RIGHT, padx=(0, 8))
        dialog.bind("<Return>", lambda _event: accept())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        self.parent.wait_window(dialog)
        return self.result


class StructuredNameDialog:
    def __init__(
        self,
        parent: tk.Tk,
        store: LibraryStore,
        current_name: str,
        initial_parts: StructuredFileName | None = None,
        initial_note: str = "",
        initial_tags: tuple[str, ...] = (),
    ) -> None:
        self.parent = parent
        self.store = store
        self.current_name = current_name
        self.result: tuple[StructuredFileName, str, tuple[str, ...]] | None = None
        parsed = initial_parts or StructuredFileName(
            series_abbr="",
            number="",
            main_title=Path(current_name).stem,
            subtitle="",
            edition="1",
            authors="",
            extension=Path(current_name).suffix or ".pdf",
        )
        parsed = normalize_structured_file_name(parsed)
        self.metadata_base_parts = parsed
        self.metadata_message = "已使用文件名识别候选字段。"
        self._metadata_queue = None
        self._metadata_process: multiprocessing.Process | None = None
        self._metadata_poll_after_id: str | None = None
        self._updating_fields = False
        self._edited_fields: set[str] = set()

        self.values = {
            "series_abbr": tk.StringVar(value=parsed.series_abbr),
            "number": tk.StringVar(value=parsed.number),
            "main_title": tk.StringVar(value=parsed.main_title),
            "subtitle": tk.StringVar(value=parsed.subtitle),
            "edition": tk.StringVar(value=parsed.edition),
            "authors": tk.StringVar(value=parsed.authors),
            "extension": tk.StringVar(value=parsed.extension or ".pdf"),
        }
        self.note_var = tk.StringVar(value=initial_note)
        self.tags_var = tk.StringVar(value=", ".join(normalize_tag_names(initial_tags)))
        self.title_case_mode = tk.StringVar(value="驼峰化")
        self.edition_language = tk.StringVar(value=parsed.edition_language)
        self.preview_var = tk.StringVar()

    def show(self, metadata_path: Path | None = None) -> tuple[StructuredFileName, str, tuple[str, ...]] | None:
        dialog = tk.Toplevel(self.parent)
        self.dialog = dialog
        dialog.title("按规则重命名文件")
        dialog.transient(self.parent)
        dialog.grab_set()
        dialog.resizable(False, False)

        ttk.Label(
            dialog,
            text="文件名结构：系列缩写编号 主标题名 - 副标题名 [- 版本信息] _ 作者信息.扩展名；各字段可空但合成文件名不能为空。",
        ).grid(row=1, column=0, padx=12, pady=(4, 6), sticky=tk.W)

        pasted_name = ttk.LabelFrame(dialog, text="粘贴名称文本", padding=(8, 6))
        pasted_name.grid(row=0, column=0, padx=12, pady=(12, 0), sticky=tk.EW)
        self.pasted_name_text = tk.Text(pasted_name, height=3, wrap=tk.WORD)
        self.pasted_name_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ttk.Button(pasted_name, text="确认识别", command=self.apply_pasted_name).pack(side=tk.RIGHT, padx=(8, 0))

        form = ttk.Frame(dialog)
        form.grid(row=2, column=0, padx=12, pady=4, sticky=tk.EW)

        fields = [
            ("系列缩写", "series_abbr"),
            ("编号", "number"),
            ("主标题名", "main_title"),
            ("副标题名", "subtitle"),
            ("版本号", "edition"),
            ("作者信息", "authors"),
            ("扩展名", "extension"),
        ]
        for index, (label, key) in enumerate(fields):
            box = ttk.LabelFrame(form, text=label, padding=(8, 6))
            box.grid(row=index // 2, column=index % 2, padx=4, pady=4, sticky=tk.EW)
            entry = ttk.Entry(box, textvariable=self.values[key], width=36)
            entry.pack(fill=tk.X)
            self.values[key].trace_add("write", lambda *_args, key=key: self._on_field_changed(key))
            if key == "edition":
                languages = ttk.Frame(box)
                languages.pack(fill=tk.X, pady=(5, 0))
                ttk.Radiobutton(
                    languages,
                    text="英文",
                    value="英文",
                    variable=self.edition_language,
                    command=self.update_preview,
                ).pack(side=tk.LEFT)
                ttk.Radiobutton(
                    languages,
                    text="中文",
                    value="中文",
                    variable=self.edition_language,
                    command=self.update_preview,
                ).pack(side=tk.LEFT, padx=(12, 0))

        title_case_box = ttk.LabelFrame(form, text="主、副标题格式", padding=(8, 6))
        title_case_box.grid(row=4, column=0, columnspan=2, padx=4, pady=4, sticky=tk.EW)
        ttk.Radiobutton(
            title_case_box,
            text="驼峰化",
            value="驼峰化",
            variable=self.title_case_mode,
            command=self.update_preview,
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            title_case_box,
            text="原样",
            value="原样",
            variable=self.title_case_mode,
            command=self.update_preview,
        ).pack(side=tk.LEFT, padx=(16, 0))

        note_box = ttk.LabelFrame(form, text="备注（不加入文件名）", padding=(8, 6))
        note_box.grid(row=5, column=0, columnspan=2, padx=4, pady=4, sticky=tk.EW)
        ttk.Entry(note_box, textvariable=self.note_var, width=78).pack(fill=tk.X)

        tag_box = ttk.LabelFrame(form, text="标签（不加入文件名）", padding=(8, 6))
        tag_box.grid(row=6, column=0, columnspan=2, padx=4, pady=4, sticky=tk.EW)
        ttk.Entry(tag_box, textvariable=self.tags_var, width=64).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(tag_box, text="选择标签", command=self.choose_tags).pack(side=tk.RIGHT, padx=(8, 0))

        preview = ttk.LabelFrame(dialog, text="生成的文件名", padding=(8, 6))
        preview.grid(row=3, column=0, padx=12, pady=(4, 8), sticky=tk.EW)
        ttk.Label(preview, textvariable=self.preview_var, wraplength=760).pack(fill=tk.X)

        buttons = ttk.Frame(dialog)
        buttons.grid(row=4, column=0, padx=12, pady=(0, 12), sticky=tk.E)
        ttk.Button(buttons, text="取消", command=self.cancel).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="确定", command=self.accept).pack(side=tk.RIGHT, padx=(0, 8))

        self.metadata_status_var = tk.StringVar()
        ttk.Label(dialog, textvariable=self.metadata_status_var, foreground="#555", wraplength=760).grid(
            row=5, column=0, padx=12, pady=(0, 10), sticky=tk.W
        )

        dialog.bind("<Return>", lambda _event: self.accept())
        dialog.bind("<Escape>", lambda _event: self.cancel())
        dialog.protocol("WM_DELETE_WINDOW", self.cancel)
        self.update_preview()
        if metadata_path is not None:
            self._start_metadata_analysis(metadata_path)
        self.pasted_name_text.focus_set()
        self.parent.wait_window(dialog)
        if self.metadata_message == "正在后台分析前几页内容…":
            self.metadata_message = "已按当前字段导入；前几页内容分析未在确认前完成。"
        return self.result

    def _on_field_changed(self, key: str) -> None:
        if not self._updating_fields:
            self._edited_fields.add(key)
        self.update_preview()

    def _start_metadata_analysis(self, source_path: Path) -> None:
        self.metadata_message = "正在后台分析前几页内容…"
        self.metadata_status_var.set(self.metadata_message)
        context = multiprocessing.get_context("spawn")
        self._metadata_queue = context.Queue(maxsize=1)
        self._metadata_process = context.Process(
            target=_extract_metadata_in_worker,
            args=(str(source_path), self._metadata_queue),
            daemon=True,
        )
        self._metadata_process.start()
        self._poll_metadata_analysis()

    def _poll_metadata_analysis(self) -> None:
        if self._metadata_queue is None or not self.dialog.winfo_exists():
            return
        try:
            result, message = self._metadata_queue.get_nowait()
        except queue.Empty:
            if self._metadata_process is not None and self._metadata_process.exitcode is not None:
                self.metadata_message = "内容分析进程已结束，未返回结果；已使用文件名候选。"
                self.metadata_status_var.set(self.metadata_message)
                self._stop_metadata_analysis()
                return
            self._metadata_poll_after_id = self.dialog.after(60, self._poll_metadata_analysis)
            return
        self._stop_metadata_analysis()
        self.metadata_message = message
        self.metadata_status_var.set(message)
        if result is None or not result.extracted:
            return
        candidates = merge_name_candidates(self.metadata_base_parts, result.parts)
        self._updating_fields = True
        try:
            for key in self.values:
                if key not in self._edited_fields:
                    self.values[key].set(getattr(candidates, key))
            if "edition" not in self._edited_fields:
                self.edition_language.set(candidates.edition_language)
        finally:
            self._updating_fields = False
        self.update_preview()

    def _stop_metadata_analysis(self) -> None:
        if self._metadata_poll_after_id is not None:
            try:
                self.dialog.after_cancel(self._metadata_poll_after_id)
            except tk.TclError:
                pass
            self._metadata_poll_after_id = None
        if self._metadata_process is not None:
            if self._metadata_process.is_alive():
                self._metadata_process.terminate()
            self._metadata_process.join(timeout=0.2)
            self._metadata_process = None
        if self._metadata_queue is not None:
            self._metadata_queue.close()
            self._metadata_queue.cancel_join_thread()
            self._metadata_queue = None

    def cancel(self) -> None:
        self._stop_metadata_analysis()
        self.dialog.destroy()

    def apply_pasted_name(self) -> None:
        text = self.pasted_name_text.get("1.0", tk.END)
        try:
            parts = infer_name_parts_from_pasted_text(text, self.values["extension"].get())
        except ValueError as exc:
            messagebox.showerror("名称识别", str(exc), parent=self.dialog)
            return
        for key in self.values:
            self.values[key].set(getattr(parts, key))
        self.edition_language.set(parts.edition_language)
        self.update_preview()

    def choose_tags(self) -> None:
        tags = TagPickerDialog(
            self.dialog,
            self.store,
            normalize_tag_names(self.tags_var.get()),
        ).show()
        if tags is not None:
            self.tags_var.set(", ".join(tags))

    def accept(self) -> None:
        try:
            parts = self.build_parts()
            build_structured_file_name(parts)
            self.result = (parts, self.note_var.get().strip(), normalize_tag_names(self.tags_var.get()))
        except ValueError as exc:
            messagebox.showerror("文件名不符合规则", str(exc), parent=self.dialog)
            return
        self._stop_metadata_analysis()
        self.dialog.destroy()

    def update_preview(self) -> None:
        try:
            self.preview_var.set(build_structured_file_name(self.build_parts()))
        except ValueError as exc:
            self.preview_var.set(f"待补全：{exc}")

    def build_parts(self) -> StructuredFileName:
        main_title = self.values["main_title"].get()
        subtitle = self.values["subtitle"].get()
        if self.title_case_mode.get() == "驼峰化":
            main_title = title_case(main_title)
            subtitle = title_case(subtitle)
        return StructuredFileName(
            series_abbr=self.values["series_abbr"].get(),
            number=self.values["number"].get(),
            main_title=main_title,
            subtitle=subtitle,
            edition=self.values["edition"].get(),
            authors=self.values["authors"].get(),
            extension=self.values["extension"].get(),
            edition_language=self.edition_language.get(),
        )


def human_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def display_title_for_item(item: Item) -> str:
    if item.name_parts.main_title.strip():
        main_title = item.name_parts.main_title.strip()
        authors = item.name_parts.authors.strip()
        return f"{main_title} _ {authors}" if authors else main_title
    stem = Path(item.display_name).stem
    return stem or item.display_name


def formatted_display_name(item: Item) -> str:
    try:
        return build_structured_file_name(item.name_parts)
    except ValueError:
        return item.display_name


def display_type_for_item(item: Item) -> str:
    suffix = (item.name_parts.extension or item.stored_path.suffix).lower()
    if suffix == ".pdf":
        return "PDF"
    if suffix == ".epub":
        return "EPUB"
    if suffix in {".djvu", ".djv"}:
        return "DJVU"
    if suffix:
        return suffix[1:].upper()
    return "文件"


def accelerator_from_event(event) -> str:
    key = getattr(event, "keysym", "")
    if key in {"Control_L", "Control_R", "Alt_L", "Alt_R", "Shift_L", "Shift_R", "Caps_Lock"}:
        return ""
    state = getattr(event, "state", 0)
    modifiers: list[str] = []
    if state & 0x0004:
        modifiers.append("Ctrl")
    if state & (0x0008 | 0x20000):
        modifiers.append("Alt")
    if state & 0x0001:
        modifiers.append("Shift")
    if not modifiers and not key.upper().startswith("F"):
        return ""
    labels = {"Return": "Enter", "Escape": "Esc", "space": "Space"}
    if len(key) == 1:
        key = key.upper()
    else:
        key = labels.get(key, key)
    return "+".join([*modifiers, key])


def shortcut_sequence(accelerator: str) -> str:
    if not accelerator:
        return ""
    parts = accelerator.split("+")
    if not parts:
        return ""
    key = {"Enter": "Return", "Esc": "Escape", "Space": "space"}.get(parts[-1], parts[-1])
    if len(key) == 1:
        key = key.lower()
    modifier_names = {"Ctrl": "Control", "Alt": "Alt", "Shift": "Shift"}
    modifiers = [modifier_names[part] for part in parts[:-1] if part in modifier_names]
    return "<" + "-".join([*modifiers, key]) + ">"


def open_path_with_default_app(path: Path) -> None:
    if platform.system() == "Windows":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif platform.system() == "Darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def main() -> None:
    app = BookManagerApp()
    app.mainloop()
