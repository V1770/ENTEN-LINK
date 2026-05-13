"""Application settings dialog for persisted UI and network preferences."""
from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QLabel,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QDoubleSpinBox,
)


class SettingsDialog(QDialog):
    def __init__(self, app_config, parent=None) -> None:
        super().__init__(parent)
        self._config = app_config
        self._slot_checks: dict[int, QCheckBox] = {}
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.resize(520, 480)
        self._build_ui()
        self._load_from_config()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        tabs = QTabWidget(self)
        tabs.addTab(self._build_ui_tab(), "Interface")
        tabs.addTab(self._build_network_tab(), "Network")
        root.addWidget(tabs)

        note = QLabel(
            "Interface settings apply immediately. Network defaults affect future runs or the next test-mode start."
        )
        note.setWordWrap(True)
        root.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _build_ui_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        # ── Layout ────────────────────────────────────────────────────
        layout_group = QGroupBox("Layout", page)
        layout_form = QFormLayout(layout_group)

        self._grid_columns = QComboBox(layout_group)
        self._grid_columns.addItem("Auto Fit", 0)
        for cols in range(1, 7):
            self._grid_columns.addItem(f"{cols}", cols)
        layout_form.addRow("Grid columns", self._grid_columns)

        self._hide_offline = QCheckBox("Hide offline players", layout_group)
        layout_form.addRow("Compact grid", self._hide_offline)

        self._beat_flash_ms = QSpinBox(layout_group)
        self._beat_flash_ms.setRange(20, 500)
        self._beat_flash_ms.setSuffix(" ms")
        layout_form.addRow("Beat flash", self._beat_flash_ms)

        layout.addWidget(layout_group)

        # ── Waveform appearance ────────────────────────────────────────
        waveform_group = QGroupBox("Waveform Appearance", page)
        wf_layout = QVBoxLayout(waveform_group)

        wf_form = QFormLayout()
        wf_form.setContentsMargins(0, 4, 0, 0)

        self._wave_detail_zoom = QComboBox(waveform_group)
        self._wave_detail_zoom.addItem("2 bars total (1 bar lookahead)", 2)
        self._wave_detail_zoom.addItem("4 bars total (2 bars lookahead)", 4)
        self._wave_detail_zoom.addItem("8 bars total (4 bars lookahead)", 8)
        self._wave_detail_zoom.addItem("16 bars total (8 bars lookahead)", 16)
        self._wave_detail_zoom.addItem("32 bars total (16 bars lookahead)", 32)
        wf_form.addRow("Detail zoom", self._wave_detail_zoom)

        self._wave_bass_alpha = QSpinBox(waveform_group)
        self._wave_bass_alpha.setRange(0, 255)
        self._wave_bass_alpha.setSuffix(" /255")
        wf_form.addRow("Bass opacity  (blue)", self._wave_bass_alpha)

        self._wave_mid_alpha = QSpinBox(waveform_group)
        self._wave_mid_alpha.setRange(0, 255)
        self._wave_mid_alpha.setSuffix(" /255")
        wf_form.addRow("Mid opacity  (yellow)", self._wave_mid_alpha)

        self._wave_high_alpha = QSpinBox(waveform_group)
        self._wave_high_alpha.setRange(0, 255)
        self._wave_high_alpha.setSuffix(" /255")
        wf_form.addRow("High opacity  (white)", self._wave_high_alpha)

        wf_layout.addLayout(wf_form)
        layout.addWidget(waveform_group)

        # ── Visible player slots ──────────────────────────────────────
        players_group = QGroupBox("Visible Player Slots", page)
        players_layout = QGridLayout(players_group)
        max_slots = max(6, int(getattr(self._config.ui, "max_player_slots", 6)))
        for index in range(max_slots):
            slot = index + 1
            check = QCheckBox(f"Player {slot}", players_group)
            self._slot_checks[slot] = check
            players_layout.addWidget(check, index // 4, index % 4)
        layout.addWidget(players_group)
        layout.addStretch(1)
        return page

    def _build_network_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        defaults_group = QGroupBox("Virtual CDJ", page)
        defaults_form = QFormLayout(defaults_group)

        self._vcdj_enabled = QCheckBox("Broadcast as Virtual CDJ", defaults_group)
        self._vcdj_enabled.setToolTip(
            "When enabled, this host appears on the DJ Link network as a "
            "player.  Required for dbserver/NFS metadata exchanges to succeed."
        )
        defaults_form.addRow("", self._vcdj_enabled)

        self._vcdj_player = QSpinBox(defaults_group)
        self._vcdj_player.setRange(1, 16)
        self._vcdj_player.setToolTip(
            "Player number (1–4 are real CDJ slots; 5–16 are virtual slots)."
        )
        defaults_form.addRow("Player number", self._vcdj_player)

        self._device_timeout = QDoubleSpinBox(defaults_group)
        self._device_timeout.setRange(1.0, 30.0)
        self._device_timeout.setDecimals(1)
        self._device_timeout.setSingleStep(0.5)
        self._device_timeout.setSuffix(" s")
        defaults_form.addRow("Device timeout", self._device_timeout)

        layout.addWidget(defaults_group)

        note = QLabel(
            "Changing the Virtual CDJ player number restarts the network "
            "stack so the new handshake propagates to every deck."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        layout.addStretch(1)
        return page

    def _load_from_config(self) -> None:
        ui_cfg = self._config.ui
        network_cfg = self._config.network

        grid_index = self._grid_columns.findData(int(getattr(ui_cfg, "grid_columns", 0) or 0))
        self._grid_columns.setCurrentIndex(max(0, grid_index))
        self._hide_offline.setChecked(bool(getattr(ui_cfg, "hide_offline_players", True)))
        self._beat_flash_ms.setValue(int(getattr(ui_cfg, "beat_flash_ms", 80)))
        self._wave_bass_alpha.setValue(int(getattr(ui_cfg, "waveform_bass_alpha", 165)))
        self._wave_mid_alpha.setValue(int(getattr(ui_cfg, "waveform_mid_alpha", 165)))
        self._wave_high_alpha.setValue(int(getattr(ui_cfg, "waveform_high_alpha", 145)))
        zoom_bars = int(getattr(ui_cfg, "waveform_detail_total_bars", 4))
        zoom_idx = self._wave_detail_zoom.findData(zoom_bars)
        self._wave_detail_zoom.setCurrentIndex(zoom_idx if zoom_idx >= 0 else 1)

        hidden_slots = set(getattr(ui_cfg, "hidden_slots", []))
        for slot, check in self._slot_checks.items():
            check.setChecked(slot not in hidden_slots)

        live_vp = int(getattr(network_cfg, "virtual_cdj_player", 0) or 0)
        self._vcdj_enabled.setChecked(live_vp > 0)
        self._vcdj_player.setValue(live_vp if live_vp > 0
                                   else int(getattr(network_cfg,
                                                   "default_virtual_cdj_player", 5)))
        self._vcdj_player.setEnabled(self._vcdj_enabled.isChecked())
        self._vcdj_enabled.toggled.connect(self._vcdj_player.setEnabled)
        self._device_timeout.setValue(
            float(getattr(network_cfg, "device_timeout_seconds", 5.0))
        )

    def apply_to_config(self) -> None:
        ui_cfg = self._config.ui
        network_cfg = self._config.network

        ui_cfg.grid_columns = int(self._grid_columns.currentData())
        ui_cfg.hide_offline_players = self._hide_offline.isChecked()
        ui_cfg.beat_flash_ms = int(self._beat_flash_ms.value())
        ui_cfg.waveform_bass_alpha = int(self._wave_bass_alpha.value())
        ui_cfg.waveform_mid_alpha = int(self._wave_mid_alpha.value())
        ui_cfg.waveform_high_alpha = int(self._wave_high_alpha.value())
        ui_cfg.waveform_detail_total_bars = int(self._wave_detail_zoom.currentData())
        ui_cfg.hidden_slots = [
            slot for slot, check in sorted(self._slot_checks.items()) if not check.isChecked()
        ]

        if self._vcdj_enabled.isChecked():
            vp = int(self._vcdj_player.value())
            network_cfg.virtual_cdj_player = vp
            network_cfg.default_virtual_cdj_player = vp
        else:
            network_cfg.virtual_cdj_player = 0
        network_cfg.device_timeout_seconds = float(self._device_timeout.value())
