"""
Full deck panel — one instance per CDJ player slot (1–4).

Layout (top → bottom):
  • Header row      — online dot · device name · IP
  • Separator
  • MetadataPanel   — title, artist, key, duration
  • TransportBar    — BPM, play state, position, pitch, MASTER/SYNC badges
  • WaveformView    — zoomed scrolling waveform (Phase 2)
  • OverviewStrip   — full-track waveform overview with playhead (Phase 2)
  • BeatIndicator   — 4 squares that flash on every beat
"""
from __future__ import annotations
import platform
import subprocess
import os
import time
import logging
from dataclasses import replace
from PyQt6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QLabel, QWidget,
)
from PyQt6.QtCore import Qt, QTimer

from ui.theme import (
    C_BG_WIDGET, C_BORDER, C_ACCENT, C_TEXT, C_TEXT_DIM, C_BEAT_1, C_BEAT_N,
    C_MASTER, C_SYNC, C_PLAY,
)
from ui.deck.metadata_panel import MetadataPanel
from ui.deck.transport_bar import TransportBar
from ui.waveform.waveform_view import WaveformView
from ui.waveform.overview_strip import OverviewStrip
from core.devices.player_state import PlayerState, PlayStateRaw
from core.analysis.track_metadata import TrackMetadata
from core.analysis.waveform_data import WaveformData
from core.analysis.sync_monitor import SyncStatus
from core.analysis.beat_grid import TrackBeatGrid
from core.analysis.playhead_tracker import PlayheadTracker

_DEFAULT_BEAT_FLASH_MS = 80   # how long a beat square stays lit
_PRECISE_ACTIVE_WINDOW_S = 0.60
_WAVEFORM_DETAIL_COL_RATE = 150.0
_WAVEFORM_PHASE_TRIM_COLS = 6
_WAVEFORM_MONO_EXTRA_TRIM_COLS = 14
_WAVEFORM_VISUAL_LEAD_MS = 8
_WAVEFORM_PHASE_NUDGE_COLS = 0.0

log = logging.getLogger(__name__)


class BeatIndicator(QWidget):
    """Four squares: downbeat flashes white, beats 2-4 flash cyan."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._flash_ms = _DEFAULT_BEAT_FLASH_MS
        self.setFixedHeight(14)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self._dots: list[QLabel] = []
        for _ in range(4):
            d = QLabel()
            d.setFixedSize(10, 10)
            d.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._set_dim(d)
            layout.addWidget(d)
            self._dots.append(d)
        layout.addStretch()

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._dim_all)

    def flash(self, beat_in_bar: int) -> None:
        idx = max(1, min(4, beat_in_bar)) - 1
        for i, d in enumerate(self._dots):
            if i == idx:
                colour = C_BEAT_1 if idx == 0 else C_BEAT_N
                d.setStyleSheet(f"background: {colour}; border-radius: 3px;")
            else:
                self._set_dim(d)
        self._timer.start(self._flash_ms)

    def set_flash_ms(self, flash_ms: int) -> None:
        self._flash_ms = max(20, int(flash_ms))

    def _dim_all(self) -> None:
        for d in self._dots:
            self._set_dim(d)

    @staticmethod
    def _set_dim(dot: QLabel) -> None:
        dot.setStyleSheet(
            f"background: {C_BG_WIDGET}; border: 1px solid {C_BORDER}; border-radius: 3px;"
        )


class DeckWidget(QGroupBox):
    """Complete UI panel for one Pioneer CDJ player slot."""

    # Slots 5+ are rekordbox / software players
    _SOFTWARE_SLOTS = {5, 6}

    def __init__(self, slot: int, event_bus, parent=None) -> None:
        super().__init__(parent)
        self._slot = slot
        self._bus = event_bus
        self._is_online = False
        self._tracker = PlayheadTracker()
        self._last_state: PlayerState | None = None
        self._last_meta: TrackMetadata | None = None
        self._last_precise_pos_ms: int | None = None
        self._last_precise_t: float | None = None
        self._waveform_timeline_duration_ms: int | None = None
        self._waveform_trim_offset_ms: int = 0
        self._waveform_time_offset_ms: int = 0
        self._last_beat_grid: TrackBeatGrid | None = None
        self._build()
        self._connect()
        self._interp_timer = QTimer(self)
        self._interp_timer.timeout.connect(self._tick_interpolated_playhead)
        self._interp_timer.start(16)
        self.set_online(False)

    # ── Build ─────────────────────────────────────────────────────────
    def _build(self) -> None:
        self.setTitle("")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 5)
        layout.setSpacing(3)

        # Header row: big deck number, online marker, compact identity line.
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(16)
        self._deck_id = QLabel(str(self._slot))
        self._deck_id.setStyleSheet(
            f"color: {C_ACCENT}; font-size: 30px; font-weight: 900;"
        )
        self._deck_id.setFixedWidth(34)
        self._deck_id.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter)

        identity = QVBoxLayout()
        identity.setContentsMargins(0, 0, 0, 0)
        identity.setSpacing(0)

        self._online_dot = QLabel("●")
        self._online_dot.setStyleSheet(f"color: {C_BORDER}; font-size: 11px;")
        self._name_label = QLabel("—")
        self._name_label.setStyleSheet(
            f"color: {C_TEXT}; font-size: 12px; font-weight: bold;"
        )
        self._ip_label = QLabel("")
        self._ip_label.setStyleSheet(f"color: #a8b7c2; font-size: 10px; font-weight: 500;")

        source_text = "rekordbox" if self._slot in self._SOFTWARE_SLOTS else "cdj"
        self._slot_role = QLabel(source_text.upper())
        self._slot_role.setStyleSheet(
            f"color: {C_ACCENT}; font-size: 10px; font-weight: bold; letter-spacing: 0.4px;"
        )
        self._slot_role.setContentsMargins(0, 0, 0, 0)

        self._header_bpm = QLabel("---.--")
        self._header_bpm.setStyleSheet(
            f"color: {C_TEXT}; font-size: 13px; font-weight: bold;"
        )
        self._header_bpm.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)

        self._bottom_pitch = QLabel("")
        self._bottom_pitch.setStyleSheet(
            f"color: {C_ACCENT}; font-size: 12px; font-weight: bold;"
        )

        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.setSpacing(4)
        name_row.addWidget(self._online_dot)
        name_row.addWidget(self._name_label)
        name_row.addStretch()

        identity.addLayout(name_row)
        identity.addWidget(self._ip_label)

        bottom_info = QHBoxLayout()
        bottom_info.setContentsMargins(0, 0, 0, 0)
        bottom_info.setSpacing(8)

        header.addWidget(self._deck_id)
        header.addLayout(identity)

        self._metadata  = MetadataPanel()
        self._transport = TransportBar()
        self._waveform  = WaveformView()
        self._overview  = OverviewStrip()
        self._beat_ind  = BeatIndicator()

        header.addWidget(self._metadata, 1)
        header.addWidget(self._slot_role, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)

        layout.addLayout(header)
        layout.addWidget(self._transport)
        layout.addWidget(self._waveform, 1)
        layout.addWidget(self._overview)
        bottom_info.addWidget(self._beat_ind)
        bottom_info.addWidget(self._header_bpm)
        bottom_info.addWidget(self._bottom_pitch)
        bottom_info.addStretch(1)
        layout.addLayout(bottom_info)

    def _connect(self) -> None:
        self._bus.player_state_updated.connect(self._on_state)
        self._bus.sync_state_updated.connect(self._on_sync_state)
        self._bus.beat_detected.connect(self._on_beat)
        self._bus.device_lost.connect(self._on_lost)
        self._bus.metadata_received.connect(self._on_metadata)
        self._bus.album_art_received.connect(self._on_album_art)
        self._bus.waveform_preview_received.connect(self._on_waveform_preview)
        self._bus.waveform_detail_received.connect(self._on_waveform_detail)
        self._bus.waveform_color_received.connect(self._on_waveform_color)
        self._bus.beat_grid_received.connect(self._on_beat_grid)
        self._bus.precise_position_received.connect(self._on_precise_position)

    # ── Public ────────────────────────────────────────────────────────
    @property
    def is_online(self) -> bool:
        return self._is_online

    def set_online(self, online: bool) -> None:
        self._is_online = online
        if online:
            self._online_dot.setStyleSheet(f"color: {C_ACCENT};")
            self._deck_id.setStyleSheet(
                f"color: {C_ACCENT}; font-size: 30px; font-weight: 900;"
            )
        else:
            self._online_dot.setStyleSheet(f"color: {C_BORDER};")
            self._deck_id.setStyleSheet(
                f"color: {C_BORDER}; font-size: 30px; font-weight: 900;"
            )
            self._name_label.setText("—")
            self._ip_label.setText("")
            self._header_bpm.setText("---.--")
            self._bottom_pitch.setText("")
            self._metadata.set_artwork_bytes(None)
            self._waveform.clear()
            self._overview.clear()
            self._tracker.reset()
            self._last_state = None
            self._last_meta = None
            self._last_precise_pos_ms = None
            self._last_precise_t = None
            self._waveform_timeline_duration_ms = None
            self._waveform_trim_offset_ms = 0
            self._waveform_time_offset_ms = 0
            self._last_beat_grid = None
            self._transport.update_jog_delta(None)
            self._transport.set_precise_active(False)

    def set_beat_flash_ms(self, flash_ms: int) -> None:
        self._beat_ind.set_flash_ms(flash_ms)

    def set_waveform_band_opacity(self, bass_alpha: int, mid_alpha: int, high_alpha: int) -> None:
        self._waveform.set_band_opacity(bass_alpha, mid_alpha, high_alpha)
        self._overview.set_band_opacity(bass_alpha, mid_alpha, high_alpha)

    def set_waveform_detail_total_bars(self, total_bars: int) -> None:
        self._waveform.set_zoom_total_bars(total_bars)

    def set_show_track_text(self, enabled: bool) -> None:
        self._metadata.set_show_track_text(enabled)

    def set_show_artwork(self, enabled: bool) -> None:
        self._metadata.set_show_artwork(enabled)

    def reveal_playing_track(self) -> None:
        """Open the OS file manager and highlight the currently playing track file."""
        meta = self._last_meta
        path = str(getattr(meta, "local_file_path", "") or "") if meta else ""
        if not path:
            return
        system = platform.system()
        try:
            if system == "Darwin":
                subprocess.Popen(["open", "-R", path])
            elif system == "Windows":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except Exception as exc:
            log.warning("reveal_playing_track failed: %s", exc)

    # ── Slots ─────────────────────────────────────────────────────────
    def _on_state(self, player_num: int, state: PlayerState) -> None:
        if player_num != self._slot:
            return
        state = self._merge_state_with_metadata(state)
        self._last_state = state
        self.set_online(True)
        self._name_label.setText(state.name)
        ip_text = state.ip_address if state.ip_address else ""
        self._ip_label.setText(ip_text)
        pos_ms = self._tracker.ingest_state(state, time.monotonic())
        duration_ms = self._tracker.duration_ms or int(state.track_duration_ms)
        beat_num = self._tracker.current_beat_number
        display_state = replace(
            state,
            position_ms=pos_ms,
            beat_number=beat_num,
            track_duration_ms=duration_ms,
        )

        self._metadata.update_state(display_state)
        self._transport.update_state(display_state)
        self._update_header_bpm(display_state)
        self._update_bottom_info(display_state)

        # Update waveform playhead position from track position / duration
        if display_state.track_duration_ms > 0:
            timeline_duration_ms = self._waveform_timeline_ms(display_state.track_duration_ms)
            visual_duration_ms = self._waveform_visual_duration_ms(timeline_duration_ms)
            waveform_pos_ms = self._waveform_display_pos_ms(
                display_state.position_ms,
                timeline_duration_ms,
            )
            frac = waveform_pos_ms / max(1, visual_duration_ms)
            self._waveform.set_position(frac)
            self._overview.set_position(frac)
            effective_bpm = self._tracker.effective_bpm
            transport_pos_ms = self._waveform_transport_pos_ms(
                display_state.position_ms,
                timeline_duration_ms,
            )
            self._waveform.set_transport(
                effective_bpm,
                transport_pos_ms,
                beat_num,
                visual_duration_ms,
            )
        self._update_cue_marker()

    def _on_metadata(self, player_num: int, meta) -> None:
        if player_num != self._slot:
            return
        self._last_meta = meta
        self._metadata.update_from_metadata(meta)
        if self._last_state is not None:
            merged = self._merge_state_with_metadata(self._last_state)
            pos_ms = self._tracker.ingest_state(merged, time.monotonic())
            duration_ms = self._tracker.duration_ms or int(merged.track_duration_ms)
            beat_num = self._tracker.current_beat_number
            merged = replace(
                merged,
                position_ms=pos_ms,
                beat_number=beat_num,
                track_duration_ms=duration_ms,
            )
            self._metadata.update_state(merged)
            self._transport.update_state(merged)
            self._update_header_bpm(merged)
            self._update_bottom_info(merged)
            if merged.track_duration_ms > 0:
                timeline_duration_ms = self._waveform_timeline_ms(merged.track_duration_ms)
                visual_duration_ms = self._waveform_visual_duration_ms(timeline_duration_ms)
                waveform_pos_ms = self._waveform_display_pos_ms(
                    merged.position_ms,
                    timeline_duration_ms,
                )
                frac = waveform_pos_ms / max(1, visual_duration_ms)
                self._waveform.set_position(frac)
                self._overview.set_position(frac)
                effective_bpm = self._tracker.effective_bpm
                transport_pos_ms = self._waveform_transport_pos_ms(
                    merged.position_ms,
                    timeline_duration_ms,
                )
                self._waveform.set_transport(
                    effective_bpm,
                    transport_pos_ms,
                    beat_num,
                    visual_duration_ms,
                )
        else:
            self._metadata.update_from_metadata(meta)

    def _on_album_art(self, player_num: int, image_bytes: bytes) -> None:
        if player_num != self._slot:
            return
        self._metadata.set_artwork_bytes(image_bytes)

    def _on_waveform_preview(self, player_num: int, data: bytes) -> None:
        if player_num != self._slot:
            return
        self._waveform.clear_beat_grid()
        self._waveform_timeline_duration_ms = None
        self._waveform_trim_offset_ms = 0
        self._waveform_time_offset_ms = 0
        self._waveform.set_waveform_time_offset_ms(0)
        wf = self._trim_waveform_leading_cols(
            WaveformData.from_preview_bytes(data),
            _WAVEFORM_PHASE_TRIM_COLS,
        )
        self._waveform.set_waveform(wf)
        self._overview.set_waveform(wf)
        self._reapply_last_beat_grid()

    def _on_waveform_color(self, player_num: int, data: bytes) -> None:
        """Nxs2 color waveform (PWV5 tag, 0x2c04): true-color, preferred over monochrome."""
        if player_num != self._slot:
            return
        self._waveform.clear_beat_grid()
        wf = self._trim_waveform_leading_cols(
            WaveformData.from_nxs2_detail_bytes(data),
            _WAVEFORM_PHASE_TRIM_COLS,
        )
        self._waveform_trim_offset_ms = int(round((_WAVEFORM_PHASE_TRIM_COLS * 1000.0) / _WAVEFORM_DETAIL_COL_RATE))
        self._apply_waveform_time_offset()
        self._waveform_timeline_duration_ms = (
            int(round((wf.column_count * 1000.0) / _WAVEFORM_DETAIL_COL_RATE))
            if wf.column_count > 0
            else None
        )
        self._waveform.set_waveform(wf)
        self._overview.set_waveform(wf)
        self._reapply_last_beat_grid()

    def _on_waveform_detail(self, player_num: int, data: bytes) -> None:
        if player_num != self._slot:
            return
        # Monochrome fallback (0x2904): synthesise 3 bands from height+whiteness.
        self._waveform.clear_beat_grid()
        wf = self._trim_waveform_leading_cols(
            WaveformData.from_detail_bytes(data),
            _WAVEFORM_PHASE_TRIM_COLS + _WAVEFORM_MONO_EXTRA_TRIM_COLS,
        )
        mono_trim_cols = _WAVEFORM_PHASE_TRIM_COLS + _WAVEFORM_MONO_EXTRA_TRIM_COLS
        self._waveform_trim_offset_ms = int(round((mono_trim_cols * 1000.0) / _WAVEFORM_DETAIL_COL_RATE))
        self._apply_waveform_time_offset()
        self._waveform_timeline_duration_ms = (
            int(round((wf.column_count * 1000.0) / _WAVEFORM_DETAIL_COL_RATE))
            if wf.column_count > 0
            else None
        )
        self._waveform.set_waveform(wf)
        self._overview.set_waveform(wf)
        self._reapply_last_beat_grid()

    def _reapply_last_beat_grid(self) -> None:
        """Restore beat grid after waveform refresh if one was already loaded."""
        if self._last_beat_grid is None:
            return
        self._waveform.set_beat_grid(
            self._last_beat_grid.beat_times_ms,
            self._last_beat_grid.beat_within_bar,
        )

    def _trim_waveform_leading_cols(self, wf: WaveformData, trim_cols: int) -> WaveformData:
        """Apply a fixed leading-column trim so waveform onset aligns with playhead/grid."""
        trim = max(0, int(trim_cols))
        if trim <= 0 or wf.column_count <= trim:
            return wf
        return WaveformData(
            low_h=wf.low_h[trim:],
            mid_h=wf.mid_h[trim:],
            high_h=wf.high_h[trim:],
            bpm=wf.bpm,
            raw_colors=(wf.raw_colors[trim:] if wf.raw_colors is not None else None),
            heights=(wf.heights[trim:] if wf.heights is not None else None),
        )

    def _apply_waveform_time_offset(self) -> None:
        """Apply trim offset used to compensate leading waveform columns.

        Robust rule:
        - If a real beat grid is available, do not apply trim-based time offset.
          This keeps grid timing tied directly to analyzed beat timestamps for
          both zero-start and later-start tracks.
        - If no beat grid is available, use trim compensation for fallback BPM grid.
        """
        has_grid = self._last_beat_grid is not None and self._last_beat_grid.beat_count > 0
        effective = 0 if has_grid else max(0, int(self._waveform_trim_offset_ms))
        self._waveform_time_offset_ms = effective
        self._waveform.set_waveform_time_offset_ms(self._waveform_time_offset_ms)

    def _on_beat_grid(self, player_num: int, beat_grid: TrackBeatGrid) -> None:
        if player_num != self._slot:
            return
        self._last_beat_grid = beat_grid
        self._tracker.set_beat_grid(beat_grid)
        self._apply_waveform_time_offset()
        self._waveform.set_beat_grid(beat_grid.beat_times_ms, beat_grid.beat_within_bar)
        if self._last_state is not None:
            merged = self._merge_state_with_metadata(self._last_state)
            pos_ms = self._tracker.ingest_state(merged, time.monotonic())
            duration_ms = self._tracker.duration_ms or int(merged.track_duration_ms)
            beat_num = self._tracker.current_beat_number
            merged = replace(
                merged,
                position_ms=pos_ms,
                beat_number=beat_num,
                track_duration_ms=duration_ms,
            )
            self._metadata.update_state(merged)
            self._transport.update_state(merged)
            self._update_header_bpm(merged)
            self._update_bottom_info(merged)
            if merged.track_duration_ms > 0:
                timeline_duration_ms = self._waveform_timeline_ms(merged.track_duration_ms)
                visual_duration_ms = self._waveform_visual_duration_ms(timeline_duration_ms)
                waveform_pos_ms = self._waveform_display_pos_ms(
                    merged.position_ms,
                    timeline_duration_ms,
                )
                frac = waveform_pos_ms / max(1, visual_duration_ms)
                self._waveform.set_position(frac)
                self._overview.set_position(frac)
                effective_bpm = self._tracker.effective_bpm
                transport_pos_ms = self._waveform_transport_pos_ms(
                    merged.position_ms,
                    timeline_duration_ms,
                )
                self._waveform.set_transport(
                    effective_bpm,
                    transport_pos_ms,
                    beat_num,
                    visual_duration_ms,
                )
        self._update_cue_marker()

    def _on_precise_position(
        self,
        player_num: int,
        position_ms: int,
        track_length_ms: int,
        bpm: float,
        pitch: float,
    ) -> None:
        if player_num != self._slot:
            return

        now_t = time.monotonic()
        jog_delta: int | None = None
        if self._last_precise_pos_ms is not None and self._last_precise_t is not None:
            dt_ms = max(1.0, (now_t - self._last_precise_t) * 1000.0)
            raw_delta = int(position_ms - self._last_precise_pos_ms)

            playing = self._last_state.is_playing if self._last_state is not None else False
            reverse = (self._last_state.play_state_raw == PlayStateRaw.REVERSE) if self._last_state is not None else False
            pitch_mult = max(0.0, 1.0 + float(pitch))
            expected = int((-dt_ms if reverse else dt_ms) * pitch_mult) if playing else 0
            residual = raw_delta - expected

            # Show meaningful platter/search movement only.
            if (not playing and abs(raw_delta) >= 2) or abs(residual) >= 18:
                jog_delta = residual if playing else raw_delta

        self._last_precise_pos_ms = int(position_ms)
        self._last_precise_t = now_t
        self._transport.update_jog_delta(jog_delta)
        self._transport.set_precise_active(True)

        playing = self._last_state.is_playing if self._last_state is not None else False
        reverse = (self._last_state.play_state_raw == PlayStateRaw.REVERSE) if self._last_state is not None else False
        pitch_mult = max(0.0, 1.0 + float(pitch))
        pos_ms = self._tracker.ingest_precise_position(
            position_ms=position_ms,
            track_duration_ms=track_length_ms,
            pitch_mult=pitch_mult,
            playing=playing,
            reverse=reverse,
            now=now_t,
        )
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "Deck %d PPOS packet=%dms applied=%dms playing=%s reverse=%s",
                self._slot,
                position_ms,
                pos_ms,
                playing,
                reverse,
            )

        duration_ms = self._tracker.duration_ms or int(track_length_ms)
        if duration_ms > 0:
            timeline_duration_ms = self._waveform_timeline_ms(duration_ms)
            visual_duration_ms = self._waveform_visual_duration_ms(timeline_duration_ms)
            waveform_pos_ms = self._waveform_display_pos_ms(pos_ms, timeline_duration_ms)
            frac = waveform_pos_ms / max(1, visual_duration_ms)
            self._waveform.set_position(frac)
            self._overview.set_position(frac)
            beat_num = self._tracker.current_beat_number
            transport_pos_ms = self._waveform_transport_pos_ms(pos_ms, timeline_duration_ms)
            self._waveform.set_transport(
                bpm if bpm > 0 else self._tracker.effective_bpm,
                transport_pos_ms,
                beat_num,
                visual_duration_ms,
            )

        if self._last_state is not None:
            display_state = self._merge_state_with_metadata(self._last_state)
            display_state = replace(
                display_state,
                bpm=(bpm if bpm > 0 else display_state.bpm),
                pitch=pitch,
                position_ms=pos_ms,
                beat_number=self._tracker.current_beat_number,
                track_duration_ms=duration_ms,
            )
            self._transport.update_state(display_state)
            self._update_header_bpm(display_state)
            self._update_bottom_info(display_state)

    def _on_beat(self, player_num: int, bpm: float, beat_in_bar: int, timing: object | None = None) -> None:
        if player_num != self._slot:
            return
        self._beat_ind.flash(beat_in_bar)
        # Feed beat packet to tracker as a definitive anchor (mirrors beat-link beatListener).
        # The beat number is derived internally from the last anchor — not from the status
        # packet which may already reflect the new beat (race with status updates).
        if self._last_state is not None:
            pitch_mult = max(0.0, 1.0 + float(self._last_state.pitch))
            next_beat_ms = None
            second_beat_ms = None
            if isinstance(timing, dict):
                next_beat_ms = timing.get("next_beat_ms")
                second_beat_ms = timing.get("second_beat_ms")
            self._tracker.ingest_beat(
                pitch_mult,
                time.monotonic(),
                next_beat_ms=next_beat_ms,
                second_beat_ms=second_beat_ms,
                effective_bpm=bpm if bpm > 0 else None,
            )

    def _on_lost(self, player_num: int) -> None:
        if player_num == self._slot:
            self.set_online(False)
            self._transport.update_sync_status(None)

    def _on_sync_state(self, player_num: int, sync_status: SyncStatus) -> None:
        if player_num != self._slot:
            return
        self._transport.update_sync_status(sync_status)

    def _tick_interpolated_playhead(self) -> None:
        """Interpolate playhead between status packets for smoother low-latency UI."""
        if self._last_precise_t is not None:
            precise_fresh = (time.monotonic() - self._last_precise_t) <= _PRECISE_ACTIVE_WINDOW_S
            self._transport.set_precise_active(precise_fresh)

        predicted_ms = self._tracker.tick(time.monotonic())
        if predicted_ms is None:
            return
        duration_ms = self._tracker.duration_ms
        if duration_ms <= 0:
            return
        timeline_duration_ms = self._waveform_timeline_ms(duration_ms)
        visual_duration_ms = self._waveform_visual_duration_ms(timeline_duration_ms)
        waveform_pos_ms = self._waveform_display_pos_ms(predicted_ms, timeline_duration_ms)
        frac = waveform_pos_ms / max(1, visual_duration_ms)
        self._waveform.set_position(frac)
        self._overview.set_position(frac)
        beat_num = self._tracker.current_beat_number
        transport_pos_ms = self._waveform_transport_pos_ms(predicted_ms, timeline_duration_ms)
        self._waveform.set_transport(self._tracker.effective_bpm, transport_pos_ms, beat_num, visual_duration_ms)
        if self._last_state is not None:
            display_state = self._merge_state_with_metadata(self._last_state)
            display_state = replace(
                display_state,
                position_ms=predicted_ms,
                beat_number=beat_num,
                track_duration_ms=duration_ms,
            )
            self._transport.update_state(display_state)

    def _update_cue_marker(self) -> None:
        """Push the tracker's cue point to waveform/overview as a display fraction."""
        cue_ms = self._tracker.cue_point_ms
        duration_ms = self._tracker.duration_ms
        if cue_ms is None or duration_ms <= 0:
            self._waveform.set_cue_marker(None)
            self._overview.set_cue_marker(None)
            return
        timeline_duration_ms = self._waveform_timeline_ms(duration_ms)
        visual_duration_ms = self._waveform_visual_duration_ms(timeline_duration_ms)
        cue_display_ms = self._waveform_display_pos_ms(int(cue_ms), timeline_duration_ms)
        frac = cue_display_ms / max(1, visual_duration_ms)
        self._waveform.set_cue_marker(frac)
        self._overview.set_cue_marker(frac)

    def _waveform_display_pos_ms(self, position_ms: int, timeline_duration_ms: int) -> int:
        """Return timeline position for waveform drawing.

        Keep beat-grid alignment exact: when a real beat grid is present we do
        not apply visual lead compensation, otherwise playhead can appear off-grid.
        """
        shifted = self._waveform_transport_pos_ms(position_ms, timeline_duration_ms)
        return max(0, shifted - self._waveform_time_offset_ms)

    def _waveform_transport_pos_ms(self, position_ms: int, timeline_duration_ms: int) -> int:
        """Position basis shared by waveform center and fallback BPM grid."""
        lead_ms = 0
        has_grid = self._last_beat_grid is not None and self._last_beat_grid.beat_count > 0
        if self._last_state is not None and self._last_state.is_playing and not has_grid:
            lead_ms = _WAVEFORM_VISUAL_LEAD_MS
        phase_nudge_ms = int(round((_WAVEFORM_PHASE_NUDGE_COLS * 1000.0) / _WAVEFORM_DETAIL_COL_RATE))
        shifted = max(0, int(position_ms) + lead_ms - phase_nudge_ms)
        return min(shifted, max(0, int(timeline_duration_ms)))

    def _waveform_visual_duration_ms(self, timeline_duration_ms: int) -> int:
        """Duration represented by the waveform currently being drawn.

        If waveform-derived timeline is present, it already reflects trimmed
        columns, so do not subtract offset again. Only subtract when falling
        back to full track duration.
        """
        if self._waveform_timeline_duration_ms is not None and self._waveform_timeline_duration_ms > 0:
            return max(1, int(timeline_duration_ms))
        return max(1, int(timeline_duration_ms) - int(self._waveform_time_offset_ms))

    def _waveform_timeline_ms(self, fallback_duration_ms: int) -> int:
        """Timeline duration used for waveform/grid scaling.

        Prefer waveform-derived duration when available so grid spacing matches
        the actual waveform column geometry.
        """
        if self._waveform_timeline_duration_ms is not None and self._waveform_timeline_duration_ms > 0:
            return int(self._waveform_timeline_duration_ms)
        return max(1, int(fallback_duration_ms))

    def _update_header_bpm(self, state: PlayerState) -> None:
        effective_bpm = state.bpm * (1.0 + state.pitch) if state.bpm > 0 else 0.0
        self._header_bpm.setText(f"{effective_bpm:.2f}" if effective_bpm > 0 else "---.--")

    def _update_bottom_info(self, state: PlayerState) -> None:
        if state.pitch != 0.0:
            sign = "+" if state.pitch > 0 else ""
            self._bottom_pitch.setText(f"{sign}{state.pitch * 100:.1f}%")
        else:
            self._bottom_pitch.setText("0.0%")

    def _merge_state_with_metadata(self, state: PlayerState) -> PlayerState:
        meta = self._last_meta
        merged = state if meta is None else replace(
            state,
            bpm=state.bpm if state.bpm > 0 else float(meta.bpm or 0.0),
            track_title=state.track_title or meta.title,
            track_artist=state.track_artist or meta.artist,
            track_duration_ms=state.track_duration_ms or meta.duration_ms,
            track_key=state.track_key or meta.key,
        )
        return merged

