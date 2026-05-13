"""Track metadata returned by the TCP metadata server."""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class TrackMetadata:
    player_num: int
    rekordbox_id: int = 0
    title: str = ""
    artist: str = ""
    album: str = ""
    genre: str = ""
    comment: str = ""
    date_added: str = ""
    color: str = ""
    rating: int = 0
    audio_format: str = ""
    artwork_available: bool = False
    artwork_id: int = 0
    artwork_path: str = ""
    key: str = ""
    duration_s: int = 0    # seconds
    bpm: float = 0.0
    local_file_path: str = ""   # absolute path on this machine (from local rekordbox DB)
    playlist_names: list = field(default_factory=list)  # rekordbox playlist memberships
    play_count: int = 0                                 # total play count from rekordbox

    @property
    def duration_display(self) -> str:
        m, s = divmod(self.duration_s, 60)
        return f"{m}:{s:02d}"

    @property
    def duration_ms(self) -> int:
        return self.duration_s * 1000
