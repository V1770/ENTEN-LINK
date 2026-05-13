"""
Central Qt signal hub.
All network threads emit through here; UI widgets connect to it.
PyQt6 signals are inherently thread-safe (queued cross-thread delivery).
"""
from __future__ import annotations
from PyQt6.QtCore import QObject, pyqtSignal


class EventBus(QObject):
    # ── Device lifecycle ───────────────────────────────────────────────
    device_discovered = pyqtSignal(int, str, str)   # player_num, name, ip
    device_lost = pyqtSignal(int)                    # player_num

    # ── Status updates (~8 Hz per device) ─────────────────────────────
    player_state_updated = pyqtSignal(int, object)  # player_num, PlayerState

    # ── Beat events (one per beat per device) ─────────────────────────
    beat_detected = pyqtSignal(int, float, int, object)  # player_num, bpm, beat_in_bar, timing payload
    precise_position_received = pyqtSignal(int, int, int, float, float)  # player_num, pos_ms, track_len_ms, bpm, pitch

    # ── Phase 3 sync monitoring ────────────────────────────────────────
    sync_state_updated = pyqtSignal(int, object)    # player_num, SyncStatus

    # ── Metadata (Phase 2 — TCP port 12523) ───────────────────────────
    metadata_received         = pyqtSignal(int, object)  # player_num, TrackMetadata
    waveform_preview_received = pyqtSignal(int, bytes)   # player_num, 400-byte blob
    waveform_detail_received  = pyqtSignal(int, bytes)   # player_num, 1-byte/col monochrome blob
    waveform_color_received   = pyqtSignal(int, bytes)   # player_num, 3-byte/col color blob (blue/yellow/gray)
    beat_grid_received        = pyqtSignal(int, object)  # player_num, TrackBeatGrid
    album_art_received        = pyqtSignal(int, bytes)   # player_num, encoded image bytes (jpeg/png)
    network_info = pyqtSignal(str)                       # transient human-readable progress

    # ── Network health ─────────────────────────────────────────────────
    network_error = pyqtSignal(str)                 # human-readable error

    _instance: "EventBus | None" = None

    @classmethod
    def instance(cls) -> "EventBus":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
