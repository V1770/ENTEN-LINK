"""
NFS-backed track-metadata + waveform fetcher.

Targets Pioneer CDJ-3000 networks where the legacy DB-server protocol on
port 1051/12523 is unavailable.  Reads the rekordbox export.pdb directly
from the USB-owning deck via NFSv2 and pulls the per-track ANLZ
analysis files (waveform, beat grid).

Concurrency model
─────────────────
* The public coroutine `fetch_track()` runs in NetworkWorker's asyncio
  loop, but every NFS read happens on a worker thread via
  `asyncio.to_thread()`.  NfsClient uses synchronous blocking sockets.
* `event_bus` signals are emitted from this loop thread and Qt queues
  them across to the GUI thread automatically.

Cache
─────
PDBs are large (often >1 MB).  We cache the parsed `PdbDatabase` keyed
by device IP.  The cache is invalidated implicitly when a device is
lost (callers should `forget_device(ip)`).
"""
from __future__ import annotations
import asyncio
import logging
import time
from typing import Optional

from core.analysis.beat_grid import TrackBeatGrid
from core.analysis.track_metadata import TrackMetadata
from core.network.anlz_parser import AnlzAssets, parse_anlz
from core.network.nfs_client import NfsClient
from core.network.pdb_parser import PdbDatabase, TrackRow, parse_pdb

log = logging.getLogger(__name__)

_PDB_PATH    = "/PIONEER/rekordbox/export.pdb"
_NFS_TIMEOUT = 30.0   # seconds for a single NFS download

# Synthesised 34-byte header so the existing waveform_color handler can
# reuse from_nxs2_detail_bytes() (which skips the first 34 bytes).
_PWV5_HEADER_PAD = b"\x00" * 34


class NfsMetadataFetcher:
    """Per-NetworkWorker singleton; cache + fetch coordinator."""

    def __init__(self, event_bus) -> None:
        self._bus = event_bus
        self._pdb_cache: dict[str, PdbDatabase] = {}
        self._pdb_locks: dict[str, asyncio.Lock] = {}
        # Per-track guard so concurrent status packets don't race ANLZ reads.
        self._track_locks: dict[tuple[str, int], asyncio.Lock] = {}

    # ── Public API ─────────────────────────────────────────────────
    def forget_device(self, ip: str) -> None:
        """Drop cached PDB for `ip` (call when the device disappears)."""
        self._pdb_cache.pop(ip, None)
        self._pdb_locks.pop(ip, None)
        log.debug("NFS cache cleared for %s", ip)

    async def fetch_track(self, player_num: int, target_ip: str,
                          track_id: int) -> bool:
        """
        Resolve `track_id` against the rekordbox PDB on `target_ip`.

        Emits TrackMetadata on success and (when the analysis file is
        reachable) waveform / beat-grid signals.

        Returns True if any data was successfully delivered.
        """
        if not target_ip or track_id <= 0:
            return False

        track_lock = self._track_locks.setdefault((target_ip, track_id), asyncio.Lock())
        async with track_lock:
            try:
                pdb = await self._get_pdb(target_ip)
            except Exception as exc:
                log.warning("NFS PDB fetch failed for %s: %s", target_ip, exc)
                return False

            track = pdb.tracks.get(track_id)
            if track is None:
                log.warning("NFS PDB on %s has no track id=%d (have %d tracks)",
                            target_ip, track_id, len(pdb.tracks))
                return False

            # ── 1. Emit text metadata immediately ─────────────────────
            meta = self._build_metadata(player_num, pdb, track)
            self._bus.metadata_received.emit(player_num, meta)
            self._bus.network_info.emit(
                f"NFS metadata loaded for player {player_num}: "
                f"{meta.title or 'Unknown Title'}"
            )
            log.info("NFS metadata: player=%d '%s' — %s", player_num,
                     meta.title, meta.artist)

            # ── 2. Fetch & parse ANLZ files (waveform + beat grid) ────
            await self._fetch_anlz(player_num, target_ip, track)

            return True

    # ── Internal: PDB download + cache ─────────────────────────────
    async def _get_pdb(self, ip: str) -> PdbDatabase:
        cached = self._pdb_cache.get(ip)
        if cached is not None:
            return cached
        lock = self._pdb_locks.setdefault(ip, asyncio.Lock())
        async with lock:
            cached = self._pdb_cache.get(ip)
            if cached is not None:
                return cached
            self._bus.network_info.emit(f"Reading rekordbox database from {ip}...")
            t0 = time.monotonic()
            data = await asyncio.wait_for(
                asyncio.to_thread(_blocking_read, ip, _PDB_PATH),
                timeout=_NFS_TIMEOUT * 2,
            )
            log.info("NFS: fetched export.pdb from %s — %d bytes in %.2fs",
                     ip, len(data), time.monotonic() - t0)
            db = await asyncio.to_thread(parse_pdb, data)
            self._pdb_cache[ip] = db
            self._bus.network_info.emit(
                f"rekordbox database loaded ({len(db.tracks)} tracks) from {ip}"
            )
            return db

    # ── Internal: ANLZ download + emit ─────────────────────────────
    async def _fetch_anlz(self, player_num: int, ip: str, track: TrackRow) -> None:
        analyze_path = (track.analyze_path or "").strip()
        if not analyze_path:
            log.debug("Track %d has no analyze_path — skipping waveform fetch", track.id)
            return

        # Primary: ANLZ0000.DAT (PWAV preview, PQTZ beat grid).
        try:
            dat_bytes = await asyncio.wait_for(
                asyncio.to_thread(_blocking_read, ip, analyze_path),
                timeout=_NFS_TIMEOUT,
            )
        except Exception as exc:
            log.warning("NFS ANLZ.DAT fetch failed for %s on %s: %s",
                        analyze_path, ip, exc)
            dat_bytes = b""

        if dat_bytes:
            assets = await asyncio.to_thread(parse_anlz, dat_bytes)
            self._emit_anlz_dat(player_num, assets)

        # Extended: same path with .DAT → .EXT (PWV3 mono detail, PWV5 colour detail).
        ext_path = _swap_anlz_extension(analyze_path, ".EXT")
        if ext_path:
            try:
                ext_bytes = await asyncio.wait_for(
                    asyncio.to_thread(_blocking_read, ip, ext_path),
                    timeout=_NFS_TIMEOUT,
                )
            except Exception as exc:
                log.debug("NFS ANLZ.EXT fetch failed for %s on %s: %s",
                          ext_path, ip, exc)
                ext_bytes = b""

            if ext_bytes:
                ext_assets = await asyncio.to_thread(parse_anlz, ext_bytes)
                self._emit_anlz_ext(player_num, ext_assets)

    # ── Helpers: build TrackMetadata and emit waveform signals ──────
    def _build_metadata(self, player_num: int, pdb: PdbDatabase,
                        track: TrackRow) -> TrackMetadata:
        artwork_path = pdb.artwork.get(track.artwork_id, "") if track.artwork_id else ""
        meta = TrackMetadata(
            player_num=player_num,
            rekordbox_id=track.id,
            title=track.title,
            artist=pdb.artists.get(track.artist_id, ""),
            album=pdb.albums.get(track.album_id, ""),
            genre=pdb.genres.get(track.genre_id, ""),
            comment=track.comment,
            date_added=track.date_added,
            color=pdb.colors.get(track.color_id, ""),
            rating=track.rating,
            artwork_available=track.artwork_id > 0,
            artwork_id=track.artwork_id,
            artwork_path=artwork_path,
            key=pdb.keys.get(track.key_id, ""),
            duration_s=track.duration,
            bpm=(track.tempo / 100.0) if track.tempo else 0.0,
            play_count=track.play_count,
        )
        # Heuristic: derive an audio_format from the filename extension.
        name = track.filename or track.title
        if name and "." in name:
            ext = name.rsplit(".", 1)[-1].strip().upper()
            if 1 < len(ext) <= 5:
                meta.audio_format = ext
        return meta

    def _emit_anlz_dat(self, player_num: int, assets: AnlzAssets) -> None:
        if assets.preview:
            self._bus.waveform_preview_received.emit(player_num, assets.preview)
            self._bus.network_info.emit(
                f"NFS waveform preview loaded for player {player_num}"
            )
            log.info("NFS waveform preview: player=%d (%d bytes)",
                     player_num, len(assets.preview))
        if assets.beat_grid:
            beat_times: list[int] = []
            beat_marks: list[int] = []
            for bn, _tempo, ts in assets.beat_grid:
                beat_times.append(int(ts))
                beat_marks.append(bn if 1 <= bn <= 4 else 0)
            grid = TrackBeatGrid(
                player_num=player_num,
                beat_times_ms=tuple(beat_times),
                beat_within_bar=tuple(beat_marks),
            )
            self._bus.beat_grid_received.emit(player_num, grid)
            self._bus.network_info.emit(
                f"NFS beat grid loaded for player {player_num}"
            )
            log.info("NFS beat grid: player=%d (%d beats)",
                     player_num, len(beat_times))

    def _emit_anlz_ext(self, player_num: int, assets: AnlzAssets) -> None:
        # Prefer Nxs2 colour detail (PWV5) when available.
        if assets.color_detail and len(assets.color_detail) >= 2:
            blob = _PWV5_HEADER_PAD + assets.color_detail
            self._bus.waveform_color_received.emit(player_num, blob)
            self._bus.network_info.emit(
                f"NFS colour waveform loaded for player {player_num}"
            )
            log.info("NFS colour waveform: player=%d (%d cols)",
                     player_num, len(assets.color_detail) // 2)
        elif assets.detail:
            self._bus.waveform_detail_received.emit(player_num, assets.detail)
            self._bus.network_info.emit(
                f"NFS mono waveform loaded for player {player_num}"
            )
            log.info("NFS mono waveform: player=%d (%d bytes)",
                     player_num, len(assets.detail))


# ─────────────────────────────────────────────────────────────────────────
# Helpers (module level so they pickle / asyncio.to_thread cleanly)
# ─────────────────────────────────────────────────────────────────────────
def _blocking_read(ip: str, path: str) -> bytes:
    """Fetch `path` from the Pioneer NFS export at `ip`.  Blocking."""
    with NfsClient(ip) as client:
        return client.read_file(path)


def _swap_anlz_extension(path: str, new_ext: str) -> Optional[str]:
    """Replace the .DAT (or .dat) extension with `new_ext`.  Returns None on mismatch."""
    if not path or "." not in path:
        return None
    stem, _, ext = path.rpartition(".")
    if ext.upper() != "DAT":
        return None
    return f"{stem}{new_ext}"
