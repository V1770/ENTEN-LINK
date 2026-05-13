#!/usr/bin/env python3
"""
Waveform playback test — visual validation of 3-band waveform rendering.

Simulates real-time playback of a synthetic or real audio track:
    • 4 500 waveform columns at 150 col/s (Pioneer color-detail sample rate)
    • 3-band display: blue (bass) / yellow (mid) / white (high)
    • Playhead scrolls left→right across the WaveformView
    • Overview strip QPixmap cached, only playhead strip repainted
    • Beat indicator flashes at 120 BPM
    • Live opacity controls (bass / mid / high)
    • Load a real WAV or AIFF file for frequency-accurate 3-band display

Run:  python tests/test_waveform_playback_gui.py
"""
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QLabel, QHBoxLayout,
    QPushButton, QSpinBox, QGroupBox, QFileDialog,
)
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QFont

from config import load_config
from core.event_bus import EventBus
from core.devices.player_state import PlayerState, PlayStateRaw
from core.analysis.waveform_data import WaveformData
from ui.deck.deck_widget import DeckWidget
from ui.theme import C_BG, C_TEXT, C_TEXT_DIM


class WaveformPlaybackTestWindow(QMainWindow):
    """
    Runs a 30-second real-time playback simulation against a single DeckWidget.
    Waveform data: WaveformData.synthetic(columns=4500) — 30 s at 150 col/s.
    """

    # Test parameters
    SLOT               = 1          # deck slot (1-4 CDJ, 5-6 rekordbox)
    BPM                = 120.0
    # 30-second track at Pioneer color-detail rate = 4500 columns
    TRACK_DURATION_MS  = 30_000
    COL_RATE           = 150        # columns per second (Pioneer standard)
    WAVEFORM_COLS      = int(TRACK_DURATION_MS / 1000 * COL_RATE)  # 4500
    UPDATE_INTERVAL_MS = 80         # ~12 Hz position updates (CDJ sends ~8 Hz)
    BEAT_INTERVAL_MS   = int(60_000 / BPM)   # 500 ms per beat at 120 BPM

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Waveform Playback Test")
        self.setGeometry(100, 100, 1100, 620)

        self._bus = EventBus.instance()

        # Load persisted opacity defaults
        try:
            cfg = load_config()
            self._bass_alpha = int(getattr(cfg.ui, "waveform_bass_alpha", 165))
            self._mid_alpha  = int(getattr(cfg.ui, "waveform_mid_alpha",  165))
            self._high_alpha = int(getattr(cfg.ui, "waveform_high_alpha", 145))
        except Exception:
            self._bass_alpha, self._mid_alpha, self._high_alpha = 165, 165, 145

        # Mutable track state (updated when a real track is loaded)
        self._track_duration_ms = self.TRACK_DURATION_MS
        self._play_bpm = self.BPM

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        central.setStyleSheet(f"background: {C_BG};")

        # ── Info label ────────────────────────────────────────────────
        self._info_label = QLabel()
        self._info_label.setFont(QFont("Monaco", 10))
        self._info_label.setStyleSheet(f"color: {C_TEXT};")
        layout.addWidget(self._info_label)

        # ── Deck widget ───────────────────────────────────────────────
        self._deck = DeckWidget(slot=self.SLOT, event_bus=self._bus)
        layout.addWidget(self._deck, 1)

        # ── Controls panel ────────────────────────────────────────────
        controls = QGroupBox("Waveform Controls")
        controls.setStyleSheet(f"color: {C_TEXT};")
        ctrl_v = QVBoxLayout(controls)
        ctrl_v.setContentsMargins(8, 8, 8, 8)
        ctrl_v.setSpacing(6)

        alpha_row = QHBoxLayout()
        alpha_row.setSpacing(10)
        for label_text, attr, default in (
            ("Bass (blue)",   "_spin_bass",  self._bass_alpha),
            ("Mid (yellow)",  "_spin_mid",   self._mid_alpha),
            ("High (white)",  "_spin_high",  self._high_alpha),
        ):
            lbl = QLabel(label_text)
            lbl.setStyleSheet(f"color: {C_TEXT_DIM};")
            spin = QSpinBox()
            spin.setRange(0, 255)
            spin.setValue(default)
            spin.valueChanged.connect(self._apply_opacity)
            alpha_row.addWidget(lbl)
            alpha_row.addWidget(spin)
            setattr(self, attr, spin)

        alpha_row.addStretch()

        btn_load = QPushButton("Load Track…")
        btn_load.clicked.connect(self._load_track)
        alpha_row.addWidget(btn_load)

        btn_synth = QPushButton("Synthetic")
        btn_synth.clicked.connect(self._load_synthetic)
        alpha_row.addWidget(btn_synth)

        ctrl_v.addLayout(alpha_row)

        self._status_label = QLabel()
        self._status_label.setStyleSheet(f"color: {C_TEXT_DIM};")
        ctrl_v.addWidget(self._status_label)

        layout.addWidget(controls)
        self.setCentralWidget(central)

        # ── Playback state ────────────────────────────────────────────
        self._position_ms  = 0
        self._beat_counter = 0
        self._elapsed_ms   = 0

        self._load_synthetic()   # sets waveform, duration, status label

        self._update_timer = QTimer(self)
        self._update_timer.timeout.connect(self._emit_state_update)
        self._update_timer.start(self.UPDATE_INTERVAL_MS)

        self._beat_timer = QTimer(self)
        self._beat_timer.timeout.connect(self._emit_beat)
        self._beat_timer.start(self.BEAT_INTERVAL_MS)

        self._close_timer = QTimer(self)
        self._close_timer.timeout.connect(self._on_test_complete)
        self._close_timer.start(self._track_duration_ms + 1_000)

    # ── Waveform helpers ──────────────────────────────────────────────

    def _apply_opacity(self) -> None:
        """Push current spinbox opacity values to the deck widgets."""
        self._deck.set_waveform_band_opacity(
            self._spin_bass.value(),
            self._spin_mid.value(),
            self._spin_high.value(),
        )

    def _set_waveform(self, wf: WaveformData, status: str, bpm: float | None = None) -> None:
        """Load *wf* into the deck and reset playback position."""
        self._track_duration_ms = int(wf.column_count / self.COL_RATE * 1000)
        self._play_bpm = float(bpm if bpm is not None else self.BPM)
        self._deck._waveform.set_waveform(wf)
        self._deck._overview.set_waveform(wf)
        self._deck.set_online(True)
        self._apply_opacity()
        self._elapsed_ms   = 0
        self._position_ms  = 0
        self._beat_counter = 0
        beat_interval = int(60_000 / max(1e-6, self._play_bpm))
        if hasattr(self, "_beat_timer"):
            self._beat_timer.start(max(60, beat_interval))
        if hasattr(self, "_close_timer"):
            self._close_timer.start(self._track_duration_ms + 1_000)
        self._status_label.setText(status)
        self._update_info_label()

    def _load_synthetic(self) -> None:
        """(Re)load the built-in synthetic waveform."""
        wf = WaveformData.synthetic(
            columns=self.WAVEFORM_COLS, bpm=self.BPM, seed=42
        )
        self._set_waveform(
            wf,
            f"Synthetic  ·  {self.BPM:.0f} BPM  ·  "
            f"{self.WAVEFORM_COLS:,} cols  ·  "
            f"{self.TRACK_DURATION_MS / 1000:.0f} s @ {self.COL_RATE} col/s",
            bpm=self.BPM,
        )

    def _load_track(self) -> None:
        """Open a file dialog and load a real WAV/AIFF track."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load Audio Track",
            str(Path.home()),
            "Audio Files (*.wav *.aif *.aiff);;All Files (*)",
        )
        if not path:
            return
        self._status_label.setText("Analysing 3-band spectrum…")
        QApplication.processEvents()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            wf = WaveformData.from_audio_file(path, col_rate=self.COL_RATE)
        except Exception as exc:
            self._status_label.setText(f"Error: {exc}")
            return
        finally:
            QApplication.restoreOverrideCursor()
        name    = Path(path).stem
        dur_s   = wf.column_count / self.COL_RATE
        bpm = float(wf.bpm) if getattr(wf, "bpm", 0.0) > 0.0 else self.BPM
        self._set_waveform(
            wf,
            f"{name}  ·  {bpm:.1f} BPM  ·  {wf.column_count:,} cols  ·  {dur_s:.1f} s",
            bpm=bpm,
        )

    def _emit_state_update(self) -> None:
        """Emit a player state update with incrementing position."""
        self._elapsed_ms  += self.UPDATE_INTERVAL_MS
        self._position_ms  = min(self._elapsed_ms, self._track_duration_ms)

        beat_num = self._beat_counter

        state = PlayerState(
            player_number=self.SLOT,
            name="Test Track",
            ip_address="127.0.0.1",
            bpm=self._play_bpm,
            pitch=0.0,
            position_ms=self._position_ms,
            beat_number=beat_num,
            beat_in_bar=((beat_num % 4) + 1),
            play_state_raw=PlayStateRaw.PLAYING,
            track_source_slot=1,
            is_playing=True,
            track_title="Test Track",
            track_artist="Test Artist",
            track_duration_ms=self._track_duration_ms,
            track_key="4A",
        )

        self._bus.player_state_updated.emit(self.SLOT, state)
        self._update_info_label()

    def _emit_beat(self) -> None:
        """Emit a beat detection event."""
        self._beat_counter += 1
        beat_in_bar = ((self._beat_counter - 1) % 4) + 1  # 1, 2, 3, 4
        self._bus.beat_detected.emit(self.SLOT, self._play_bpm, beat_in_bar, None)

    def _update_info_label(self) -> None:
        """Update info label with current playback position."""
        pos_sec   = self._position_ms / 1000
        total_sec = self._track_duration_ms / 1000
        pct       = 100 * self._position_ms / self._track_duration_ms if self._track_duration_ms > 0 else 0
        beat_display = (self._beat_counter % 4) + 1
        col_count = int(self._track_duration_ms / 1000 * self.COL_RATE)
        col       = int(self._position_ms / 1000 * self.COL_RATE)
        self._info_label.setText(
            f"Position: {pos_sec:.2f} s / {total_sec:.1f} s  ({pct:.1f}%)  "
            f"col {col}/{col_count}  ·  beat {beat_display}"
        )

    def _on_test_complete(self) -> None:
        """Called when test duration expires — window stays open for inspection."""
        print(
            f"\n[DONE] {self._beat_counter} beats emitted over "
            f"{self._elapsed_ms / 1000:.1f}s  —  "
            f"final position {100 * self._position_ms / self._track_duration_ms:.1f}%"
        )
        print("Window stays open for inspection. Close manually.\n")


def main():
    app = QApplication(sys.argv)
    window = WaveformPlaybackTestWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
