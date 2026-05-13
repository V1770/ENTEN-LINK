"""
Main application window.

Customizable operator grid:
    - Auto-fit or fixed column count
    - Per-player visibility toggles
    - Optional offline compaction to avoid wasting panel space
"""
from __future__ import annotations
import logging

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QGridLayout, QStatusBar, QLabel, QPushButton,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QActionGroup

from config import save_config
from ui.deck.deck_widget import DeckWidget
from ui.settings_dialog import SettingsDialog
from ui.library.library_dialog import LibraryDialog
from ui.theme import C_TEXT_DIM, C_STOP, C_BORDER

log = logging.getLogger(__name__)

_OFFLINE_STYLE = f"QGroupBox {{ border-color: {C_BORDER}; }}"
_ONLINE_STYLE  = ""    # inherits theme accent border


class MainWindow(QMainWindow):
    def __init__(self, event_bus, device_manager, app_config, local_db=None,
                 restart_network=None) -> None:
        super().__init__()
        self._bus = event_bus
        self._dm = device_manager
        self._local_db = local_db
        self._app_config = app_config
        self._restart_network = restart_network
        self._ui_config = app_config.ui
        self._max_slots = max(1, int(getattr(self._ui_config, "max_player_slots", 6)))
        self._decks: dict[int, DeckWidget] = {}
        self._deck_visibility: dict[int, bool] = {
            slot: True for slot in range(1, self._max_slots + 1)
        }
        self._visibility_actions: dict[int, QAction] = {}
        self._hide_offline = bool(getattr(self._ui_config, "hide_offline_players", True))
        cfg_columns = int(getattr(self._ui_config, "grid_columns", 0) or 0)
        self._grid_columns: int | None = None if cfg_columns <= 0 else max(1, min(6, cfg_columns))
        self._grid_column_actions: dict[int | None, QAction] = {}
        self._hide_offline_action: QAction | None = None
        self._settings_action: QAction | None = None
        # Start in minimal deck view every launch (no cover art / track text).
        self._show_track_text = False
        self._track_text_button: QPushButton | None = None
        self._library_button: QPushButton | None = None
        self._grid: QGridLayout | None = None
        self._library_dialog: LibraryDialog | None = None

        for slot in getattr(self._ui_config, "hidden_slots", []):
            if isinstance(slot, int) and 1 <= slot <= self._max_slots:
                self._deck_visibility[slot] = False

        self.setWindowTitle("Pioneer DJ Link")
        self.setMinimumSize(900, 560)
        self.resize(1440, 780)

        self._build_ui()
        self._build_view_menu()
        self._apply_saved_ui_settings()
        self._connect_signals()

    # ── Layout ────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)

        self._grid = QGridLayout(central)
        self._grid.setContentsMargins(8, 8, 8, 8)
        self._grid.setSpacing(8)

        for slot in range(1, self._max_slots + 1):
            deck = DeckWidget(slot, self._bus)
            deck.setStyleSheet(_OFFLINE_STYLE)
            self._decks[slot] = deck

        # ── Separator above status bar ─────────────────────────────
        self._status_label = QLabel(
            "Listening on UDP 50000 · 50001 · 50002  —  "
            "Virtual CDJ active · change player number in Settings"
        )
        self._status_label.setStyleSheet(f"color: {C_TEXT_DIM};")

        self._player_count_label = QLabel("0 devices")
        self._player_count_label.setStyleSheet(f"color: {C_TEXT_DIM};")
        self._player_count_label.setAlignment(Qt.AlignmentFlag.AlignRight)

        self._track_text_button = QPushButton("", self)
        self._track_text_button.setFixedSize(14, 14)
        self._track_text_button.setToolTip("Toggle track text / artwork")
        self._track_text_button.setStyleSheet(
            "QPushButton { background: #1e1e1e; border: 1px solid #2a2a2a; border-radius: 3px; }"
            "QPushButton:checked { background: #222228; border-color: #2e2e38; }"
            "QPushButton:hover { background: #282828; }"
            "QPushButton:pressed { background: #303030; }"
        )
        self._track_text_button.setCheckable(True)
        self._track_text_button.setChecked(self._show_track_text)
        self._track_text_button.toggled.connect(self._set_show_track_text)
        self._update_track_text_button_label()

        self._library_button = QPushButton("", self)
        self._library_button.setFixedSize(14, 14)
        self._library_button.setToolTip("Open library")
        self._library_button.setStyleSheet(
            "QPushButton { background: #1e1e1e; border: 1px solid #2a2a2a; border-radius: 3px; }"
            "QPushButton:hover { background: #282828; }"
            "QPushButton:pressed { background: #303030; }"
            "QPushButton:disabled { background: #161616; border-color: #1e1e1e; }"
        )
        self._library_button.clicked.connect(self._open_library_dialog)
        self._library_button.setEnabled(bool(self._local_db and getattr(self._local_db, "ready", False)))

        bar = QStatusBar()
        bar.addWidget(self._status_label, stretch=1)
        bar.addPermanentWidget(self._library_button)
        bar.addPermanentWidget(self._track_text_button)
        bar.addPermanentWidget(self._player_count_label)
        self.setStatusBar(bar)

        self._apply_layout()

    def _build_view_menu(self) -> None:
        view_menu = self.menuBar().addMenu("&View")
        settings_menu = self.menuBar().addMenu("&Settings")

        columns_menu = view_menu.addMenu("Grid Columns")
        columns_group = QActionGroup(self)
        columns_group.setExclusive(True)

        for label, value in (
            ("Auto Fit", None),
            ("1 Column", 1),
            ("2 Columns", 2),
            ("3 Columns", 3),
            ("4 Columns", 4),
            ("5 Columns", 5),
            ("6 Columns", 6),
        ):
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(value == self._grid_columns)
            action.triggered.connect(
                lambda checked=False, cols=value: self._set_grid_columns(cols)
            )
            columns_group.addAction(action)
            columns_menu.addAction(action)
            self._grid_column_actions[value] = action

        self._hide_offline_action = QAction("Hide Offline Players", self)
        self._hide_offline_action.setCheckable(True)
        self._hide_offline_action.setChecked(self._hide_offline)
        self._hide_offline_action.toggled.connect(self._set_hide_offline)
        view_menu.addAction(self._hide_offline_action)

        players_menu = view_menu.addMenu("Players")
        for slot in range(1, self._max_slots + 1):
            action = QAction(f"Show Player {slot}", self)
            action.setCheckable(True)
            action.setChecked(self._deck_visibility.get(slot, True))
            action.triggered.connect(
                lambda checked=False, s=slot: self._set_slot_visible(s, checked)
            )
            self._visibility_actions[slot] = action
            players_menu.addAction(action)

        show_all = QAction("Show All Players", self)
        show_all.triggered.connect(self._show_all_slots)
        players_menu.addSeparator()
        players_menu.addAction(show_all)

        self._settings_action = QAction("Preferences...", self)
        self._settings_action.triggered.connect(self._open_settings_dialog)
        settings_menu.addAction(self._settings_action)

    def _connect_signals(self) -> None:
        self._bus.device_discovered.connect(self._on_device_discovered)
        self._bus.player_state_updated.connect(self._on_player_state_updated)
        self._bus.device_lost.connect(self._on_device_lost)
        self._bus.network_error.connect(self._on_network_error)
        self._bus.network_info.connect(self._on_network_info)

    # ── Helpers ───────────────────────────────────────────────────────
    def _configured_slots(self) -> list[int]:
        return [
            slot for slot in range(1, self._max_slots + 1)
            if self._deck_visibility.get(slot, False)
        ]

    def _slots_for_grid(self) -> list[int]:
        slots = self._configured_slots()
        if self._hide_offline:
            slots = [slot for slot in slots if self._decks[slot].is_online]
        return slots

    def _set_grid_columns(self, cols: int | None) -> None:
        self._grid_columns = cols
        self._ui_config.grid_columns = 0 if cols is None else int(cols)
        for value, action in self._grid_column_actions.items():
            action.blockSignals(True)
            action.setChecked(value == cols)
            action.blockSignals(False)
        self._apply_layout()
        save_config()

    def _set_hide_offline(self, enabled: bool) -> None:
        self._hide_offline = bool(enabled)
        self._ui_config.hide_offline_players = self._hide_offline
        self._apply_layout()
        save_config()

    def _set_slot_visible(self, slot: int, visible: bool) -> None:
        self._deck_visibility[slot] = bool(visible)
        self._ui_config.hidden_slots = [
            num for num, is_visible in sorted(self._deck_visibility.items()) if not is_visible
        ]
        self._apply_layout()
        save_config()

    def _show_all_slots(self) -> None:
        for slot in range(1, self._max_slots + 1):
            self._deck_visibility[slot] = True
            action = self._visibility_actions.get(slot)
            if action is not None:
                action.blockSignals(True)
                action.setChecked(True)
                action.blockSignals(False)
        self._ui_config.hidden_slots = []
        self._apply_layout()
        save_config()

    def _set_show_track_text(self, enabled: bool) -> None:
        self._show_track_text = bool(enabled)
        self._ui_config.show_track_text = self._show_track_text
        for deck in self._decks.values():
            deck.set_show_track_text(self._show_track_text)
            deck.set_show_artwork(self._show_track_text)
        self._update_track_text_button_label()
        save_config()

    def _update_track_text_button_label(self) -> None:
        pass  # buttons have no visible text — tooltip carries the meaning

    def _apply_saved_ui_settings(self) -> None:
        self._hide_offline = bool(getattr(self._ui_config, "hide_offline_players", True))
        # Force startup default OFF even if prior session saved it ON.
        self._show_track_text = False
        self._ui_config.show_track_text = False
        cfg_columns = int(getattr(self._ui_config, "grid_columns", 0) or 0)
        self._grid_columns = None if cfg_columns <= 0 else max(1, min(6, cfg_columns))

        hidden_slots = set(getattr(self._ui_config, "hidden_slots", []))
        for slot in range(1, self._max_slots + 1):
            self._deck_visibility[slot] = slot not in hidden_slots
            if slot in self._visibility_actions:
                self._visibility_actions[slot].blockSignals(True)
                self._visibility_actions[slot].setChecked(self._deck_visibility[slot])
                self._visibility_actions[slot].blockSignals(False)

        if self._hide_offline_action is not None:
            self._hide_offline_action.blockSignals(True)
            self._hide_offline_action.setChecked(self._hide_offline)
            self._hide_offline_action.blockSignals(False)

        flash_ms = int(getattr(self._ui_config, "beat_flash_ms", 80))
        bass_alpha = int(getattr(self._ui_config, "waveform_bass_alpha", 165))
        mid_alpha = int(getattr(self._ui_config, "waveform_mid_alpha", 165))
        high_alpha = int(getattr(self._ui_config, "waveform_high_alpha", 145))
        detail_zoom_bars = int(getattr(self._ui_config, "waveform_detail_total_bars", 4))
        for deck in self._decks.values():
            deck.set_beat_flash_ms(flash_ms)
            deck.set_waveform_band_opacity(bass_alpha, mid_alpha, high_alpha)
            deck.set_waveform_detail_total_bars(detail_zoom_bars)
            deck.set_show_track_text(self._show_track_text)
            deck.set_show_artwork(self._show_track_text)

        if self._track_text_button is not None:
            self._track_text_button.blockSignals(True)
            self._track_text_button.setChecked(self._show_track_text)
            self._track_text_button.blockSignals(False)
            self._update_track_text_button_label()

        for value, action in self._grid_column_actions.items():
            action.blockSignals(True)
            action.setChecked(value == self._grid_columns)
            action.blockSignals(False)

        self._apply_layout()

    def _open_settings_dialog(self) -> None:
        dialog = SettingsDialog(self._app_config, self)
        if dialog.exec() == 0:
            return

        prev_vp = int(getattr(self._app_config.network, "virtual_cdj_player", 0) or 0)
        dialog.apply_to_config()
        self._dm.set_timeout_seconds(self._app_config.network.device_timeout_seconds)
        self._apply_saved_ui_settings()
        save_config()

        new_vp = int(getattr(self._app_config.network, "virtual_cdj_player", 0) or 0)
        if new_vp != prev_vp and self._restart_network is not None:
            log.info("Virtual CDJ player changed %s → %s; restarting network",
                     prev_vp or "off", new_vp or "off")
            self._restart_network()

    def _open_library_dialog(self) -> None:
        if not self._local_db or not getattr(self._local_db, "ready", False):
            self._status_label.setStyleSheet(f"color: {C_TEXT_DIM};")
            self._status_label.setText("Local rekordbox library not available")
            return
        if self._library_dialog is None:
            self._library_dialog = LibraryDialog(self._local_db, self)
        self._library_dialog.refresh_tracks()
        self._library_dialog.show()
        self._library_dialog.raise_()
        self._library_dialog.activateWindow()

    def _clear_grid(self) -> None:
        if self._grid is None:
            return
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(self.centralWidget())

    def _reset_grid_stretches(self) -> None:
        if self._grid is None:
            return
        # Clear any previous stretch map so old multi-column layouts do not
        # reserve width when switching to 1-column view.
        for i in range(self._max_slots + 1):
            self._grid.setColumnStretch(i, 0)
            self._grid.setRowStretch(i, 0)

    def _compute_column_count(self, visible_count: int) -> int:
        if visible_count <= 1:
            return 1
        if self._grid_columns is not None:
            return max(1, min(self._grid_columns, visible_count))
        # Auto-fit to keep each deck readable while using available space.
        usable_width = max(1, self.centralWidget().width() - 24)
        target_deck_width = 430
        auto_cols = max(1, usable_width // target_deck_width)
        auto_cols = min(auto_cols, 6)
        return max(1, min(auto_cols, visible_count))

    def _apply_layout(self) -> None:
        if self._grid is None:
            return

        self._clear_grid()
        self._reset_grid_stretches()

        visible_slots = self._slots_for_grid()
        for slot in range(1, self._max_slots + 1):
            self._decks[slot].setVisible(slot in visible_slots)

        if not visible_slots:
            if hasattr(self, "_status_label"):
                self._status_label.setText(
                    "No visible players (enable players or disable 'Hide Offline Players')"
                )
            return

        assert self._grid is not None
        cols = self._compute_column_count(len(visible_slots))

        for index, slot in enumerate(visible_slots):
            row = index // cols
            col = index % cols
            self._grid.addWidget(self._decks[slot], row, col)
            self._grid.setRowStretch(row, 1)

        for col in range(cols):
            self._grid.setColumnStretch(col, 1)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_layout()

    def _update_player_count(self) -> None:
        active = sum(1 for d in self._decks.values() if d.is_online)
        noun   = "device" if active == 1 else "devices"
        self._player_count_label.setText(f"{active} {noun}")

    # ── Slots ─────────────────────────────────────────────────────────
    def _on_device_discovered(self, num: int, name: str, ip: str) -> None:
        self._status_label.setStyleSheet(f"color: {C_TEXT_DIM};")

        if num not in self._decks:
            # Slot outside configured display range (rare but possible).
            # Log it; no UI panel is created dynamically — Phase 3 can handle this.
            log.warning("Device on unhandled slot %d ('%s') — no deck panel", num, name)
            self._status_label.setText(
                f"Player {num} online — {name}  ({ip})  [slot outside display range]"
            )
            return

        self._decks[num].set_online(True)
        self._decks[num].setStyleSheet(_ONLINE_STYLE)
        label = "rekordbox" if num in {5, 6} else f"Player {num}"
        self._status_label.setText(f"{label} online — {name}  ({ip})")
        self._update_player_count()
        self._apply_layout()

    def _on_player_state_updated(self, num: int, _state) -> None:
        # Status packets may arrive before/without a visible announce event.
        # Re-apply layout so hidden-offline grids reveal decks immediately.
        if num in self._decks:
            if not self._decks[num].is_online:
                self._decks[num].set_online(True)
            self._decks[num].setStyleSheet(_ONLINE_STYLE)
            self._update_player_count()
            self._apply_layout()

    def _on_device_lost(self, num: int) -> None:
        if num in self._decks:
            self._decks[num].set_online(False)
            self._decks[num].setStyleSheet(_OFFLINE_STYLE)
        label = "rekordbox" if num in {5, 6} else f"Player {num}"
        self._status_label.setText(f"{label} offline")
        self._update_player_count()
        self._apply_layout()

    def _on_network_error(self, msg: str) -> None:
        self._status_label.setText(f"Network error: {msg}")
        self._status_label.setStyleSheet(f"color: {C_STOP};")

    def _on_network_info(self, msg: str) -> None:
        self._status_label.setStyleSheet(f"color: {C_TEXT_DIM};")
        self._status_label.setText(msg)
