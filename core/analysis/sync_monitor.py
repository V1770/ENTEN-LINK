"""Phase 3: monitor deck sync drift against the current tempo master."""
from __future__ import annotations

from dataclasses import dataclass
import time

from PyQt6.QtCore import QObject

from core.devices.player_state import PlayerState


@dataclass(frozen=True)
class SyncStatus:
    player_number: int
    master_player: int = 0
    has_reference: bool = False
    is_master: bool = False
    phase_offset_ms: float = 0.0
    beat_offset: float = 0.0
    bpm_delta: float = 0.0
    quality: str = "none"   # none | master | tight | good | drift


class SyncMonitor(QObject):
    """
    Maintains the latest PlayerState per deck and estimates phase alignment
    for each deck relative to the current tempo reference.
    """

    _FRESH_WINDOW_S = 2.5
    _TIGHT_MS = 20.0
    _GOOD_MS = 60.0

    def __init__(self, event_bus) -> None:
        super().__init__()
        self._bus = event_bus
        self._states: dict[int, PlayerState] = {}
        self._last_seen: dict[int, float] = {}

        self._bus.player_state_updated.connect(self._on_state_updated)
        self._bus.device_lost.connect(self._on_device_lost)

    def _on_state_updated(self, player_num: int, state: PlayerState) -> None:
        self._states[player_num] = state
        self._last_seen[player_num] = time.monotonic()
        self._emit_all()

    def _on_device_lost(self, player_num: int) -> None:
        self._states.pop(player_num, None)
        self._last_seen.pop(player_num, None)
        self._emit_all()

    def _emit_all(self) -> None:
        self._purge_stale()
        reference = self._select_reference()
        for player_num, state in self._states.items():
            sync = self._build_status(state, reference)
            self._bus.sync_state_updated.emit(player_num, sync)

    def _purge_stale(self) -> None:
        now = time.monotonic()
        stale = [
            player_num
            for player_num, seen in self._last_seen.items()
            if now - seen > self._FRESH_WINDOW_S
        ]
        for player_num in stale:
            self._states.pop(player_num, None)
            self._last_seen.pop(player_num, None)

    def _select_reference(self) -> PlayerState | None:
        candidates = [
            state for state in self._states.values()
            if state.bpm > 0 and state.is_playing
        ]
        if not candidates:
            return None

        explicit_master = [state for state in candidates if state.is_master]
        if explicit_master:
            return min(explicit_master, key=lambda s: s.player_number)

        return min(candidates, key=lambda s: s.player_number)

    def _build_status(self, state: PlayerState, ref: PlayerState | None) -> SyncStatus:
        if ref is None or ref.bpm <= 0:
            return SyncStatus(player_number=state.player_number)

        if state.player_number == ref.player_number:
            return SyncStatus(
                player_number=state.player_number,
                master_player=ref.player_number,
                has_reference=True,
                is_master=True,
                quality="master",
            )

        beat_ms = 60_000.0 / ref.bpm
        if beat_ms <= 0:
            return SyncStatus(player_number=state.player_number)

        # Wrap into [-beat/2, +beat/2] so the phase drift is easy to read.
        raw_delta = float(state.position_ms - ref.position_ms)
        phase_offset_ms = ((raw_delta + beat_ms / 2.0) % beat_ms) - beat_ms / 2.0
        beat_offset = phase_offset_ms / beat_ms
        bpm_delta = state.bpm - ref.bpm
        abs_phase = abs(phase_offset_ms)
        if abs_phase <= self._TIGHT_MS:
            quality = "tight"
        elif abs_phase <= self._GOOD_MS:
            quality = "good"
        else:
            quality = "drift"

        return SyncStatus(
            player_number=state.player_number,
            master_player=ref.player_number,
            has_reference=True,
            is_master=False,
            phase_offset_ms=phase_offset_ms,
            beat_offset=beat_offset,
            bpm_delta=bpm_delta,
            quality=quality,
        )