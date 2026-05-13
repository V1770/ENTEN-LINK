"""
Phase 4: Local rekordbox database reader.

Opens the rekordbox 6 master.db (SQLite) and provides instant track lookup
by rekordbox ID.  Runs on the Qt main thread — lookups hit an in-memory cache
so there is no I/O latency during playback.

Emits event_bus.metadata_received when a track is resolved locally, using the
same signal as the TCP metadata client so the UI needs no changes.
"""
from __future__ import annotations

import logging
import os
from dataclasses import replace as _dc_replace
from pathlib import Path
from typing import Dict, Optional

from PyQt6.QtCore import QObject

from core.analysis.track_metadata import TrackMetadata

log = logging.getLogger(__name__)

# Default rekordbox library path (macOS).  Windows path checked as fallback.
_DEFAULT_PATHS = [
    Path.home() / "Library" / "Pioneer" / "rekordbox" / "master.db",
    Path.home() / "AppData" / "Roaming" / "Pioneer" / "rekordbox" / "master.db",
]

_FILETYPE_MAP = {
    1: "MP3",
    2: "M4A",
    3: "WAV",
    4: "AIFF",
    5: "FLAC",
    6: "ALAC",
    19: "STREAM",
}


def _find_db() -> Optional[Path]:
    for p in _DEFAULT_PATHS:
        if p.exists():
            return p
    return None


class LocalDB(QObject):
    """
    Read-only wrapper around the rekordbox 6 SQLite library.

    Call open() once at startup.  After that, every call to lookup(rekordbox_id)
    returns a TrackMetadata (or None) synchronously from the cache.

    The database is opened in read-only WAL mode so it does not conflict with
    a concurrently running rekordbox instance.
    """

    def __init__(self, event_bus, db_path: Path | str | None = None) -> None:
        super().__init__()
        self._bus = event_bus
        self._db_path: Path | None = Path(db_path) if db_path else _find_db()
        self._db = None                          # Rekordbox6Database | None
        self._cache: Dict[int, TrackMetadata] = {}
        self._key_map: Dict[int, str] = {}
        self._artist_map: Dict[int, str] = {}
        self._content_playlists: Dict[int, list] = {}   # content_id → [playlist_name, ...]
        self._seen: set[int] = set()             # avoid duplicate emits
        self._ready = False

        self._bus.player_state_updated.connect(self._on_state_updated)

    # ── Public API ────────────────────────────────────────────────────

    def open(self) -> bool:
        """
        Open the database and build lookup caches.
        Returns True on success, False if the DB was not found or could not
        be opened (non-fatal — the app continues without local metadata).
        """
        if self._db_path is None or not self._db_path.exists():
            log.warning(
                "rekordbox database not found (tried %s). "
                "Local track metadata will not be available.",
                [str(p) for p in _DEFAULT_PATHS],
            )
            return False

        try:
            import logging as _logging
            _logging.getLogger("pyrekordbox").setLevel(_logging.ERROR)
            from pyrekordbox.db6 import Rekordbox6Database
            self._db = Rekordbox6Database(str(self._db_path))
            self._build_caches()
            self._ready = True
            log.info(
                "rekordbox DB opened: %s  (%d tracks, %d artists, %d keys)",
                self._db_path,
                len(self._cache),
                len(self._artist_map),
                len(self._key_map),
            )
            return True
        except Exception as exc:
            log.warning("Could not open rekordbox database: %s", exc)
            return False

    def lookup(self, rekordbox_id: int) -> Optional[TrackMetadata]:
        """Return cached TrackMetadata for a rekordbox ID, or None."""
        return self._cache.get(rekordbox_id)

    def all_tracks(self) -> list[TrackMetadata]:
        """Return all cached tracks for library browsing."""
        return list(self._cache.values())

    @property
    def ready(self) -> bool:
        return self._ready

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except Exception:
                pass
            self._db = None
        self._ready = False

    # ── Internal ──────────────────────────────────────────────────────

    def _build_caches(self) -> None:
        """Load all tracks, artists, keys, and playlist memberships into memory."""
        # Build key ScaleName map
        for key in self._db.get_key():
            self._key_map[int(key.ID)] = key.ScaleName or ""

        # Build artist name map
        for artist in self._db.get_artist():
            self._artist_map[int(artist.ID)] = artist.Name or ""

        # Build playlist membership map: content_id → [playlist_name, ...]
        try:
            pl_name_map: Dict[int, str] = {}
            for pl in self._db.get_playlist():
                pl_name_map[int(pl.ID)] = pl.Name or ""
            for song_pl in self._db.get_playlist_song():
                cid = int(getattr(song_pl, "ContentID", 0) or 0)
                pid = int(getattr(song_pl, "PlaylistID", 0) or 0)
                name = pl_name_map.get(pid, "")
                if cid and name:
                    self._content_playlists.setdefault(cid, []).append(name)
        except Exception as exc:
            log.debug("Could not load playlist data: %s", exc)

        # Build track cache by ID
        for track in self._db.get_content():
            meta = self._track_to_metadata(track)
            if meta is not None:
                self._cache[int(track.ID)] = meta

    def _track_to_metadata(self, track) -> Optional[TrackMetadata]:
        try:
            bpm_raw = track.BPM or 0
            # rekordbox stores BPM × 100
            bpm = bpm_raw / 100.0

            artist = self._artist_map.get(int(track.ArtistID or 0), "")
            key    = self._key_map.get(int(track.KeyID or 0), "")
            duration_s = int(track.Length or 0)
            audio_format = self._infer_audio_format(track)
            artwork_path = str(getattr(track, "ImagePath", "") or "")
            artwork_available = bool(artwork_path)
            artwork_id = int(getattr(track, "ImageID", 0) or 0)

            # Full absolute path on this machine stored by rekordbox.
            local_file_path = str(getattr(track, "FolderPath", "") or "") or \
                              str(getattr(track, "OrgFolderPath", "") or "")

            track_id = int(track.ID or 0)
            playlist_names = list(self._content_playlists.get(track_id, []))
            play_count = int(getattr(track, "PlayCount", 0) or 0)

            return TrackMetadata(
                player_num=0,       # filled in when emitting
                rekordbox_id=track_id,
                title=track.Title or "",
                artist=artist,
                key=key,
                duration_s=duration_s,
                bpm=bpm,
                audio_format=audio_format,
                artwork_available=artwork_available,
                artwork_id=artwork_id,
                artwork_path=artwork_path,
                local_file_path=local_file_path,
                playlist_names=playlist_names,
                play_count=play_count,
            )
        except Exception as exc:
            log.debug("Skipping malformed track row: %s", exc)
            return None

    def _on_state_updated(self, player_num: int, state) -> None:
        """When a deck loads a new track, look it up and emit metadata."""
        if not self._ready:
            return
        track_id = int(state.track_rekordbox_id)
        if track_id <= 0:
            return
        # Only emit once per (player, track_id) combination
        key = (player_num, track_id)
        if key in self._seen:
            return

        meta = self._cache.get(track_id)
        if meta is None:
            return

        # Stamp the player number and emit
        self._seen.add(key)
        stamped = _dc_replace(meta, player_num=player_num)
        self._bus.metadata_received.emit(player_num, stamped)
        art_bytes = self._load_artwork_bytes(stamped)
        if art_bytes is not None:
            self._bus.album_art_received.emit(player_num, art_bytes)
        log.info(
            "LocalDB → P%d  '%s'  %s  %s  %ds",
            player_num, stamped.title, stamped.artist, stamped.key, stamped.duration_s,
        )

    def reset_seen(self, player_num: int) -> None:
        """Call when a player goes offline to allow re-emit on reconnect."""
        self._seen = {k for k in self._seen if k[0] != player_num}

    def _infer_audio_format(self, track) -> str:
        file_type = int(getattr(track, "FileType", 0) or 0)
        mapped = _FILETYPE_MAP.get(file_type)
        if mapped:
            return mapped

        for field in ("FileNameL", "FileNameS", "FolderPath", "OrgFolderPath"):
            value = str(getattr(track, field, "") or "")
            if "." in value:
                ext = value.rsplit(".", 1)[-1].strip().upper()
                if 1 < len(ext) <= 5:
                    return ext
        return ""

    def _load_artwork_bytes(self, meta: TrackMetadata) -> bytes | None:
        path = str(getattr(meta, "artwork_path", "") or "")
        if not path:
            return None
        try:
            p = Path(path)
            if p.exists() and p.is_file():
                data = p.read_bytes()
                return data if data else None
        except Exception as exc:
            log.debug("Could not load artwork '%s': %s", path, exc)
        return None
