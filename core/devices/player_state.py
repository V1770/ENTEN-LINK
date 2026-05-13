"""Immutable snapshot of a single CDJ player's current state."""
from __future__ import annotations
from dataclasses import dataclass
from enum import IntEnum


class PlayStateRaw(IntEnum):
    """
    Detailed play-mode byte from CDJ status packet (offset 123, labeled P1).
    Values sourced from djl-analysis / beat-link documentation.
    """
    NO_DISC        = 0x00
    LOADING        = 0x02
    PLAYING        = 0x03   # normal forward playback
    LOOP           = 0x04   # actively playing a loop
    PAUSED         = 0x05   # paused anywhere other than cue point
    PAUSED_CUE     = 0x06   # paused at the cue point
    CUE_PLAY       = 0x07   # cue play (holding cue button)
    REVERSE        = 0x08   # cue scratch / reverse
    JOG_SEARCH     = 0x09   # searching forward or backward
    STOPPED        = 0x0E   # stopped / no-output
    STOPPED_CUE    = 0x0F   # stopped at cue
    END_OF_TRACK   = 0x11   # reached end and stopped
    EMERGENCY_LOOP = 0x12   # emergency loop active
    UNKNOWN        = 0xFF

    @classmethod
    def _missing_(cls, value: object) -> "PlayStateRaw":
        return cls.UNKNOWN


@dataclass(frozen=True)
class PlayerState:
    """
    All data known about one Pioneer player at a single point in time.
    Instances are frozen so they can be passed freely between threads.
    Phase-2 metadata fields (title, artist, key) are blank until the
    TCP metadata client is implemented.
    """
    player_number: int = 0
    name: str = ""
    ip_address: str = ""

    # Playback
    bpm: float = 0.0
    pitch: float = 0.0           # -1.0 … 0.0 … +1.0 (normalised)
    position_ms: int = 0
    beat_number: int = 0
    beat_in_bar: int = 0
    play_state_raw: PlayStateRaw = PlayStateRaw.STOPPED
    track_source_player: int = 0
    track_source_slot: int = 0   # 0=none 1=CD 2=SD 3=USB 4=rekordbox collection
    track_type: int = 0          # 0=none 1=rekordbox 2=unanalyzed 5=CD audio
    track_rekordbox_id: int = 0  # non-zero only for rekordbox tracks

    # Status flags
    is_playing: bool = False
    is_master: bool = False
    is_sync: bool = False
    is_on_air: bool = False
    loop_active: bool = False    # DJ has an active loop running
    master_tempo: bool = False   # Key Lock / Master Tempo on (CDJ-3000+ only)
    loop_start_ms: int = 0       # extended status packets (CDJ-3000+)
    loop_end_ms: int = 0         # extended status packets (CDJ-3000+)

    # Phase 2 — populated by TCP metadata client
    track_title: str = ""
    track_artist: str = ""
    track_duration_ms: int = 0
    track_key: str = ""

    # ── Convenience properties ────────────────────────────────────────
    @property
    def has_track(self) -> bool:
        return self.track_source_slot > 0

    @property
    def bpm_display(self) -> str:
        return f"{self.bpm:.2f}" if self.bpm > 0 else "---.--"

    @property
    def position_display(self) -> str:
        total_s = self.position_ms // 1000
        m, s = divmod(total_s, 60)
        return f"{m:02d}:{s:02d}"

    @property
    def play_icon(self) -> str:
        icons = {
            PlayStateRaw.PLAYING:      "▶",
            PlayStateRaw.LOOP:         "⟳",
            PlayStateRaw.CUE_PLAY:     "CUE",
            PlayStateRaw.REVERSE:      "◀",
            PlayStateRaw.PAUSED:       "⏸",
            PlayStateRaw.PAUSED_CUE:   "⏸",
            PlayStateRaw.END_OF_TRACK: "END",
        }
        return icons.get(self.play_state_raw, "■")
