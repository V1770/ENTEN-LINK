"""
Read-only transport indicator row.
Displays play state, timing, and status badges.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

from PyQt6.QtWidgets import QWidget, QHBoxLayout, QLabel
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

from ui.theme import (
    C_TEXT_DIM, C_PLAY, C_PAUSE, C_STOP, C_ACCENT,
    C_MASTER, C_SYNC,
)
from core.devices.player_state import PlayerState, PlayStateRaw

if TYPE_CHECKING:
    from core.analysis.sync_monitor import SyncStatus


_STATE_COLOR: dict[PlayStateRaw, str] = {
    PlayStateRaw.PLAYING:      C_PLAY,
    PlayStateRaw.LOOP:         C_PLAY,
    PlayStateRaw.CUE_PLAY:     C_PAUSE,
    PlayStateRaw.REVERSE:      C_ACCENT,
    PlayStateRaw.PAUSED:       C_PAUSE,
    PlayStateRaw.PAUSED_CUE:   C_PAUSE,
    PlayStateRaw.END_OF_TRACK: C_STOP,
    PlayStateRaw.STOPPED:      C_TEXT_DIM,
    PlayStateRaw.STOPPED_CUE:  C_TEXT_DIM,
    PlayStateRaw.NO_DISC:      C_TEXT_DIM,
    PlayStateRaw.LOADING:      C_TEXT_DIM,
}

_STATE_LABEL: dict[PlayStateRaw, str] = {
    PlayStateRaw.PLAYING:      "▶  PLAYING",
    PlayStateRaw.LOOP:         "⟳  LOOP",
    PlayStateRaw.CUE_PLAY:     "CUE▶",
    PlayStateRaw.REVERSE:      "◀  REVERSE",
    PlayStateRaw.PAUSED:       "⏸  PAUSED",
    PlayStateRaw.PAUSED_CUE:   "⏸  CUE",
    PlayStateRaw.END_OF_TRACK: "END",
    PlayStateRaw.STOPPED:      "■  STOPPED",
    PlayStateRaw.STOPPED_CUE:  "■  CUE",
    PlayStateRaw.NO_DISC:      "NO TRACK",
    PlayStateRaw.LOADING:      "LOADING…",
    PlayStateRaw.UNKNOWN:      "UNKNOWN",
}

_BADGE_OFF_BG   = "#2a2a2a"
_BADGE_OFF_TEXT = "#555"
_BADGE_STYLE    = "border-radius: 3px; padding: 1px 6px; font-size: 10px; font-weight: bold;"

_SYNC_QUALITY_COLOR = {
    "none": C_TEXT_DIM,
    "master": C_MASTER,
    "tight": C_ACCENT,
    "good": C_MASTER,
    "drift": C_TEXT_DIM,
}


def _badge(text: str, bg: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setStyleSheet(
        f"background: {_BADGE_OFF_BG}; color: {_BADGE_OFF_TEXT}; {_BADGE_STYLE}"
    )
    lbl.setProperty("active_bg", bg)
    return lbl


def _set_badge(badge: QLabel, active: bool) -> None:
    if active:
        bg = badge.property("active_bg")
        badge.setStyleSheet(f"background: {bg}; color: #000; {_BADGE_STYLE}")
    else:
        badge.setStyleSheet(
            f"background: {_BADGE_OFF_BG}; color: {_BADGE_OFF_TEXT}; {_BADGE_STYLE}"
        )


class TransportBar(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._build()

    def _build(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 1, 2, 1)
        layout.setSpacing(8)
        self.setFixedHeight(28)

        self._jog_label = QLabel("")
        self._jog_label.setStyleSheet(f"color: {C_ACCENT}; font-size: 11px; font-weight: bold;")
        self._jog_label.setMinimumWidth(72)

        self._state_label = QLabel("NO TRACK")
        self._state_label.setStyleSheet(
            f"color: {C_TEXT_DIM}; font-size: 11px; font-weight: bold;"
        )

        self._pos_label = QLabel("00:00.000")
        self._pos_label.setStyleSheet(
            f"color: {C_MASTER}; font-size: 13px; font-weight: bold;"
        )

        self._left_label = QLabel("-00:00.000")
        self._left_label.setStyleSheet(
            f"color: {C_TEXT_DIM}; font-size: 11px; font-weight: bold;"
        )

        self._master_badge = _badge("MASTER", C_MASTER)
        self._sync_badge   = _badge("SYNC",   C_SYNC)
        self._ppos_badge   = _badge("PPOS",   C_ACCENT)
        self._cue_badge    = _badge("CUE",    C_PAUSE)
        self._loop_badge   = _badge("LOOP",   C_PLAY)
        self._mt_badge     = _badge("MT",     C_ACCENT)

        layout.addStretch()
        layout.addWidget(self._state_label)
        layout.addWidget(self._jog_label)
        layout.addWidget(self._pos_label)
        layout.addWidget(self._left_label)
        layout.addWidget(self._cue_badge)
        layout.addWidget(self._loop_badge)
        layout.addWidget(self._mt_badge)
        layout.addWidget(self._ppos_badge)
        layout.addWidget(self._sync_badge)
        layout.addWidget(self._master_badge)

    def update_state(self, state: PlayerState) -> None:
        raw = state.play_state_raw
        label = _STATE_LABEL.get(raw, f"0x{state.play_state_raw:02X}")
        color = _STATE_COLOR.get(raw, C_TEXT_DIM)
        self._state_label.setText(label)
        self._state_label.setStyleSheet(
            f"color: {color}; font-size: 11px; font-weight: bold;"
        )

        self._pos_label.setText(self._format_time_ms(state.position_ms))

        if state.track_duration_ms > 0:
            left_ms = max(0, state.track_duration_ms - state.position_ms)
            self._left_label.setText(f"-{self._format_time_ms(left_ms)}")
        else:
            self._left_label.setText("-00:00.000")

        cue_active = raw in (PlayStateRaw.PAUSED_CUE, PlayStateRaw.CUE_PLAY)
        _set_badge(self._cue_badge, cue_active)
        _set_badge(self._master_badge, state.is_master)
        _set_badge(self._sync_badge, state.is_sync)
        _set_badge(self._loop_badge, state.loop_active)
        _set_badge(self._mt_badge, state.master_tempo)

    def update_sync_status(self, sync_status: SyncStatus | None) -> None:
        return

    def update_jog_delta(self, delta_ms: int | None) -> None:
        if delta_ms is None:
            self._jog_label.setText("")
            return
        sign = "+" if delta_ms > 0 else ""
        self._jog_label.setText(f"JOG {sign}{delta_ms}ms")

    def set_precise_active(self, active: bool) -> None:
        _set_badge(self._ppos_badge, bool(active))

    @staticmethod
    def _format_time_ms(total_ms: int) -> str:
        ms = max(0, int(total_ms))
        total_s, milli = divmod(ms, 1000)
        minutes, seconds = divmod(total_s, 60)
        return f"{minutes:02d}:{seconds:02d}.{milli:03d}"
