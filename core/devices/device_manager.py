"""
Registry of active Pioneer devices.
Runs on the Qt main thread; reacts to EventBus signals and emits
device_lost when a device stops sending packets.
"""
from __future__ import annotations
import logging
import time
from typing import Dict, Optional

from PyQt6.QtCore import QObject, QTimer

from core.devices.player_state import PlayerState

log = logging.getLogger(__name__)

_TIMEOUT_S = 5.0   # seconds without a packet before a device is considered lost


class DeviceManager(QObject):
    def __init__(self, event_bus, network_config=None) -> None:
        super().__init__()
        self._bus = event_bus
        self._states: Dict[int, PlayerState] = {}
        self._last_seen: Dict[int, float] = {}
        self._device_timeout_s = float(
            getattr(network_config, "device_timeout_seconds", _TIMEOUT_S)
        )

        self._bus.device_discovered.connect(self._on_device_discovered)
        self._bus.player_state_updated.connect(self._on_state_updated)
        self._bus.precise_position_received.connect(self._on_precise_position)

        # Watchdog: check for stale devices every second
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(1_000)
        self._watchdog.timeout.connect(self._check_timeouts)
        self._watchdog.start()

    # ── Public API ────────────────────────────────────────────────────
    def get_state(self, player_number: int) -> Optional[PlayerState]:
        return self._states.get(player_number)

    def all_states(self) -> Dict[int, PlayerState]:
        return dict(self._states)

    def active_players(self) -> list[int]:
        return sorted(self._states.keys())

    def set_timeout_seconds(self, timeout_seconds: float) -> None:
        self._device_timeout_s = max(1.0, float(timeout_seconds))

    # ── Internal slots ────────────────────────────────────────────────
    def _on_device_discovered(self, player_num: int, name: str, ip: str) -> None:
        self._last_seen[player_num] = time.monotonic()
        if player_num not in self._states:
            log.info("New device: player #%d '%s' at %s", player_num, name, ip)
            self._states[player_num] = PlayerState(
                player_number=player_num, name=name, ip_address=ip
            )

    def _on_state_updated(self, player_num: int, state: PlayerState) -> None:
        self._states[player_num] = state
        self._last_seen[player_num] = time.monotonic()

    def _on_precise_position(self, player_num: int, position_ms: int, track_length_ms: int, bpm: float, pitch: float) -> None:
        """Keep CDJ-3000 devices alive by resetting timeout on precise position packets."""
        self._last_seen[player_num] = time.monotonic()

    def _check_timeouts(self) -> None:
        now = time.monotonic()
        lost = [n for n, t in self._last_seen.items() if now - t > self._device_timeout_s]
        for n in lost:
            log.info("Device #%d timed out", n)
            self._states.pop(n, None)
            self._last_seen.pop(n, None)
            self._bus.device_lost.emit(n)
