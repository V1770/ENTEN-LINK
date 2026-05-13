from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import threading
from typing import Optional

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui.theme import C_BG_WIDGET, C_BORDER, C_TEXT, C_TEXT_DIM

log = logging.getLogger(__name__)

# ── Category definitions ─────────────────────────────────────────────────────
# Each entry: (display_name, track_attr)
# track_attr=None means "all tracks" flat view.
_CATEGORIES = [
    ("TITLE",    None),
    ("ARTIST",   "artist"),
    ("ALBUM",    "album"),
    ("GENRE",    "genre"),
    ("KEY",      "key"),
    ("BPM",      "_bpm_bucket"),   # special: rounded
    ("RATING",   "_rating_stars"), # special: star display
    ("FORMAT",   "audio_format"),
    ("PLAYLIST", "_playlist_1"),   # special: each playlist membership
    ("FOLDER",   "_folder_name"),  # special: parent directory name
]

_TRACK_COLS = ["Title", "Artist", "Album", "Key", "BPM", "Rating", "Plays", "Playlists", "Folder"]


def _track_bpm_bucket(t) -> str:
    bpm = float(getattr(t, "bpm", 0.0) or 0.0)
    if bpm <= 0:
        return "—"
    return f"{int(bpm)}"


def _track_rating_stars(t) -> str:
    r = int(getattr(t, "rating", 0) or 0)
    return "★" * r if r > 0 else "—"


def _track_folder_name(t) -> str:
    """Return the immediate parent folder name of the track file."""
    path = str(getattr(t, "local_file_path", "") or "")
    return os.path.basename(os.path.dirname(path)) if path else ""


def _track_folder_path(t) -> str:
    """Return the full parent directory path."""
    path = str(getattr(t, "local_file_path", "") or "")
    return os.path.dirname(path) if path else ""


def _track_attr(t, attr: str | None) -> str:
    if attr is None:
        return ""
    if attr == "_bpm_bucket":
        return _track_bpm_bucket(t)
    if attr == "_rating_stars":
        return _track_rating_stars(t)
    if attr == "_folder_name":
        return _track_folder_name(t)
    if attr == "_playlist_1":
        # Each playlist is its own grouping key; handled specially in _populate_category
        names = getattr(t, "playlist_names", []) or []
        return names[0] if names else "—"
    return str(getattr(t, attr, "") or "")


# ── Filesystem helpers ────────────────────────────────────────────────────────

def reveal_in_finder(path: str) -> None:
    """Open the OS file manager and highlight the given file."""
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.Popen(["open", "-R", path])
        elif system == "Windows":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:  # Linux / other
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except Exception as exc:
        log.warning("reveal_in_finder failed: %s", exc)


def _copy_tracks(
    tracks: list,
    dest_dir: str,
    progress_cb=None,
) -> tuple[int, list[str]]:
    """
    Copy local files for a list of TrackMetadata to dest_dir.
    Returns (copied_count, error_messages).
    """
    copied = 0
    errors: list[str] = []
    total = len(tracks)
    for i, t in enumerate(tracks):
        src = getattr(t, "local_file_path", "") or ""
        if not src or not os.path.isfile(src):
            errors.append(f"Not found: {getattr(t, 'title', '?')!r}")
            continue
        try:
            shutil.copy2(src, dest_dir)
            copied += 1
        except Exception as exc:
            errors.append(f"{os.path.basename(src)}: {exc}")
        if progress_cb:
            progress_cb(i + 1, total)
    return copied, errors


class LibraryDialog(QDialog):
    """Pioneer-style two-panel library browser: category tree + track list."""

    def __init__(self, local_db, parent=None) -> None:
        super().__init__(parent)
        self._local_db = local_db
        self._all_tracks: list = []
        self._current_tracks: list = []   # tracks currently shown in table
        self.setWindowTitle("Browse Library")
        self.resize(1100, 660)
        self._build()
        self.refresh_tracks()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(6)

        # ── Search bar ────────────────────────────────────────────────
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        top.addWidget(QLabel("Search:"))
        self._search = QLineEdit(self)
        self._search.setPlaceholderText("title, artist, album, key, format ...")
        self._search.textChanged.connect(self._on_search_changed)
        top.addWidget(self._search, 1)
        self._count = QLabel("0 tracks")
        self._count.setStyleSheet(f"color: {C_TEXT_DIM};")
        top.addWidget(self._count)
        btn_copy = QPushButton("Copy to…")
        btn_copy.setToolTip("Copy selected (or all shown) tracks to a folder")
        btn_copy.clicked.connect(self._on_copy_to)
        top.addWidget(btn_copy)
        btn_refresh = QPushButton("Refresh")
        btn_refresh.clicked.connect(self.refresh_tracks)
        top.addWidget(btn_refresh)
        root.addLayout(top)

        # ── Stacked widget: loading page / content page ───────────────
        self._stack = QStackedWidget()

        # Page 0: loading indicator
        loading_page = QWidget()
        loading_layout = QVBoxLayout(loading_page)
        loading_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._loading_label = QLabel("Loading library\u2026")
        loading_font = QFont()
        loading_font.setPointSize(18)
        self._loading_label.setFont(loading_font)
        self._loading_label.setStyleSheet(f"color: {C_TEXT_DIM};")
        self._loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        loading_layout.addWidget(self._loading_label)
        self._stack.addWidget(loading_page)   # index 0

        # Page 1: content
        content_page = QWidget()
        content_layout = QVBoxLayout(content_page)
        content_layout.setContentsMargins(0, 0, 0, 0)

        # ── Splitter: category tree | track table ─────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: category tree
        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setColumnCount(1)
        self._tree.setMinimumWidth(200)
        self._tree.setMaximumWidth(320)
        self._tree.setStyleSheet(
            f"QTreeWidget {{ background: {C_BG_WIDGET}; border: 1px solid {C_BORDER}; color: {C_TEXT}; }}"
        )
        self._tree.itemExpanded.connect(self._on_item_expanded)
        self._tree.itemClicked.connect(self._on_item_clicked)
        splitter.addWidget(self._tree)

        # Right: track table
        self._table = QTableWidget()
        self._table.setColumnCount(len(_TRACK_COLS))
        self._table.setHorizontalHeaderLabels(_TRACK_COLS)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setStyleSheet(
            f"QTableWidget {{ background: {C_BG_WIDGET}; border: 1px solid {C_BORDER}; color: {C_TEXT}; }}"
        )
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_table_context_menu)
        splitter.addWidget(self._table)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        content_layout.addWidget(splitter, 1)
        self._stack.addWidget(content_page)   # index 1

        root.addWidget(self._stack, 1)

    # ── Data loading ──────────────────────────────────────────────────────────
    def refresh_tracks(self) -> None:
        # Show loading screen, then schedule the actual load on the next event loop tick
        # so Qt has time to paint the loading page before the DB call blocks.
        self._stack.setCurrentIndex(0)
        self._loading_label.setText("Loading library\u2026")
        QTimer.singleShot(0, self._do_load)

    def _do_load(self) -> None:
        if not self._local_db or not getattr(self._local_db, "ready", False):
            self._all_tracks = []
        else:
            self._all_tracks = sorted(
                self._local_db.all_tracks(),
                key=lambda t: (
                    (t.artist or "").lower(),
                    (t.title or "").lower(),
                ),
            )
        self._rebuild_tree()
        self._populate_table(self._all_tracks)
        self._stack.setCurrentIndex(1)

    def _rebuild_tree(self) -> None:
        self._tree.clear()
        for cat_name, attr in _CATEGORIES:
            root_item = QTreeWidgetItem([cat_name])
            root_item.setData(0, Qt.ItemDataRole.UserRole, ("category", attr))
            if attr is not None:
                # Add a placeholder child so the expand arrow shows.
                placeholder = QTreeWidgetItem([""])
                placeholder.setData(0, Qt.ItemDataRole.UserRole, ("placeholder",))
                root_item.addChild(placeholder)
            self._tree.addTopLevelItem(root_item)

    # ── Tree interactions ─────────────────────────────────────────────────────
    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data or data[0] != "category":
            return
        attr = data[1]
        if attr is None:
            return
        # Check if already populated (first child is not a placeholder).
        if item.childCount() == 1:
            child = item.child(0)
            child_data = child.data(0, Qt.ItemDataRole.UserRole)
            if child_data and child_data[0] == "placeholder":
                item.removeChild(child)
                self._populate_category(item, attr)

    def _populate_category(self, parent: QTreeWidgetItem, attr: str) -> None:
        """Build sub-items for a category (grouped by attribute value)."""
        if attr == "_playlist_1":
            # Special: a track can belong to multiple playlists — fan out.
            groups: dict[str, list] = {}
            for t in self._all_tracks:
                names = getattr(t, "playlist_names", []) or []
                if not names:
                    groups.setdefault("(none)", []).append(t)
                else:
                    for name in names:
                        groups.setdefault(name, []).append(t)
        else:
            groups = {}
            for t in self._all_tracks:
                key = _track_attr(t, attr) or "—"
                groups.setdefault(key, []).append(t)
        for key in sorted(groups.keys(), key=lambda s: s.lower()):
            tracks = groups[key]
            child = QTreeWidgetItem([f"{key}  ({len(tracks)})"])
            child.setData(0, Qt.ItemDataRole.UserRole, ("group", tracks))
            parent.addChild(child)

    def _on_item_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        kind = data[0]
        if kind == "category":
            attr = data[1]
            if attr is None:
                # TITLE — show all tracks
                self._populate_table(self._all_tracks)
        elif kind == "group":
            self._populate_table(data[1])

    # ── Search ────────────────────────────────────────────────────────────────
    def _on_search_changed(self, text: str) -> None:
        q = text.strip().lower()
        if not q:
            filtered = self._all_tracks
        else:
            filtered = [
                t for t in self._all_tracks
                if q in " ".join([
                    t.title or "", t.artist or "", t.album or "",
                    t.key or "", t.audio_format or "",
                    _track_bpm_bucket(t),
                    _track_folder_name(t),
                    ", ".join(getattr(t, "playlist_names", []) or []),
                ]).lower()
            ]
        self._populate_table(filtered)

    # ── Track table ───────────────────────────────────────────────────────────
    def _populate_table(self, tracks: list) -> None:
        self._current_tracks = list(tracks)
        self._table.setRowCount(len(tracks))
        for row, t in enumerate(tracks):
            bpm = float(getattr(t, "bpm", 0.0) or 0.0)
            rating = int(getattr(t, "rating", 0) or 0)
            play_count = int(getattr(t, "play_count", 0) or 0)
            playlists = ", ".join(getattr(t, "playlist_names", []) or [])
            folder_name = _track_folder_name(t)
            folder_full = _track_folder_path(t)

            values = [
                t.title or "",
                t.artist or "",
                t.album or "",
                t.key or "",
                (f"{bpm:.1f}" if bpm > 0 else ""),
                ("★" * rating if rating > 0 else ""),
                (str(play_count) if play_count > 0 else ""),
                playlists,
                folder_name,
            ]
            tooltips = [None] * len(values)
            tooltips[8] = folder_full or None   # full path tooltip on Folder cell

            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, t)
                if tooltips[col]:
                    item.setToolTip(tooltips[col])
                self._table.setItem(row, col, item)
        self._table.resizeColumnsToContents()
        self._count.setText(f"{len(tracks)} tracks")

    def _selected_track(self) -> object | None:
        """Return the TrackMetadata for the currently selected table row, or None."""
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return None
        row = rows[0].row()
        item = self._table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    # ── Context menu ──────────────────────────────────────────────────────────
    def _on_table_context_menu(self, pos) -> None:
        track = self._selected_track()
        menu = QMenu(self)

        act_show = menu.addAction("Show in Finder" if platform.system() == "Darwin" else "Show in Explorer")
        act_show.setEnabled(bool(track and getattr(track, "local_file_path", "")))

        menu.addSeparator()

        act_copy_one = menu.addAction("Copy to…")
        act_copy_one.setEnabled(bool(track and getattr(track, "local_file_path", "")))

        act_copy_all = menu.addAction(f"Copy all {len(self._current_tracks)} shown tracks to…")
        act_copy_all.setEnabled(bool(self._current_tracks))

        chosen = menu.exec(self._table.viewport().mapToGlobal(pos))
        if chosen == act_show and track:
            reveal_in_finder(track.local_file_path)
        elif chosen == act_copy_one and track:
            self._copy_tracks_dialog([track])
        elif chosen == act_copy_all:
            self._copy_tracks_dialog(self._current_tracks)

    # ── Copy to folder ────────────────────────────────────────────────────────
    def _on_copy_to(self) -> None:
        """Toolbar 'Copy to…' button — copies selected row or all shown tracks."""
        track = self._selected_track()
        if track and getattr(track, "local_file_path", ""):
            self._copy_tracks_dialog([track])
        else:
            self._copy_tracks_dialog(self._current_tracks)

    def _copy_tracks_dialog(self, tracks: list) -> None:
        if not tracks:
            QMessageBox.information(self, "Copy to…", "No tracks to copy.")
            return

        dest_dir = QFileDialog.getExistingDirectory(
            self, "Choose Destination Folder", os.path.expanduser("~")
        )
        if not dest_dir:
            return

        progress = QProgressDialog(
            f"Copying {len(tracks)} track(s)…", "Cancel", 0, len(tracks), self
        )
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(300)
        progress.setValue(0)

        cancelled = [False]

        def on_progress(done, total):
            if progress.wasCanceled():
                cancelled[0] = True
            progress.setValue(done)

        def do_copy():
            copied, errors = _copy_tracks(tracks, dest_dir, on_progress)
            return copied, errors

        # Run on a thread so the progress dialog stays responsive.
        result: list = []

        def worker():
            result.extend(do_copy())

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        while t.is_alive():
            from PyQt6.QtWidgets import QApplication
            QApplication.processEvents()
            t.join(timeout=0.05)
            if progress.wasCanceled():
                break

        progress.close()
        copied = result[0] if result else 0
        errors = result[1] if len(result) > 1 else []

        if errors:
            QMessageBox.warning(
                self,
                "Copy complete with errors",
                f"Copied {copied} track(s).\n\nErrors:\n" + "\n".join(errors[:10]),
            )
        else:
            QMessageBox.information(
                self, "Copy complete", f"Copied {copied} track(s) to:\n{dest_dir}"
            )

