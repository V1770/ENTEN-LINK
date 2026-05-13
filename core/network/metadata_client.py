"""
Async TCP client for the Pro DJ Link metadata server.

Connection sequence
───────────────────
1. Connect to port 12523, send DB_SERVER_QUERY → receive 2-byte port (usually 1051).
2. Connect to discovered port.
3. Send GREETING (5 bytes) → read echo.
4. Send context-setup message → verify RESP_SUCCESS.
5. Request track metadata → render-menu → collect items.
6. Request waveform preview blob.
7. Request waveform detail blob.

Thread model
------------
* __init__ and _on_state_updated run on the Qt main thread.
* run() is an asyncio coroutine scheduled inside NetworkWorker's event loop.
* Cross-thread hand-off uses queue.SimpleQueue (stdlib; thread-safe, no asyncio deps).
"""
from __future__ import annotations
import asyncio
import logging
import queue as stdlib_queue
import struct
from typing import Dict, Set

from core.network.constants import TrackSlot
from core.network.nfs_metadata import NfsMetadataFetcher
from core.network.metadata_protocol import (
    GREETING,
    DB_SERVER_QUERY, DB_SERVER_DEFAULT_PORT,
    make_setup_msg, make_metadata_request, make_render_menu,
    make_beat_grid_request,
    make_waveform_preview_request, make_waveform_detail_request, make_nxs2_waveform_detail_request,
    make_album_art_request,
    read_message,
    RESP_SUCCESS, RESP_MENU_ITEM, RESP_ALBUM_ART,
)

log = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 3.0
_READ_TIMEOUT    = 5.0
_SETUP_TIMEOUT   = 1.5

# Menu item type codes (low 16 bits of args[6] in each RESP_MENU_ITEM)
_ITEM_TITLE    = 0x0004
_ITEM_ARTIST   = 0x0007
_ITEM_ALBUM    = 0x0002
_ITEM_GENRE    = 0x0006
_ITEM_KEY      = 0x000f
_ITEM_COMMENT  = 0x0023
_ITEM_DATE     = 0x002E
_ITEM_RATING   = 0x000A
_ITEM_DURATION = 0x000b
_ITEM_TEMPO    = 0x000d
_NO_ITEMS      = 0xFFFFFFFF

# Track type candidates used in the DMST low byte (Tr).
_TRACK_TYPE_REKORDBOX = 1
_TRACK_TYPE_MEDIA     = 2
_TRACK_TYPE_CD_AUDIO  = 5

_PORT_SENTINELS = {0, 0xFFFF}
_METADATA_FLEXIBLE_DEVICES = {"CDJ-3000", "XDJ-AZ"}
_COLOR_ITEM_TYPES = {
    0x0013: "None",
    0x0014: "Pink",
    0x0015: "Red",
    0x0016: "Orange",
    0x0017: "Yellow",
    0x0018: "Green",
    0x0019: "Aqua",
    0x001A: "Blue",
    0x001B: "Purple",
}


def _metadata_requester_num(target_player: int) -> int:
    """Return the first player number 1-4 that is != the target CDJ."""
    for n in (1, 2, 3, 4):
        if n != target_player:
            return n
    return 1


def _default_metadata_requester_candidates(target_player: int) -> list[int]:
    """Return all legal requester player numbers (1-4) excluding target (fallback)."""
    return [n for n in (1, 2, 3, 4) if n != target_player]


class MetadataClient:
    def __init__(self, event_bus, virtual_player_number: int = 0) -> None:
        self._bus = event_bus
        self._virtual_player_number = virtual_player_number
        # {player_num: (ip, track_rekordbox_id)} — accessed only from Qt main thread
        self._last_track: Dict[int, tuple[str, int]] = {}
        # {player_num: reason} — dedupe frequent "skip" logs from ~8 Hz status stream
        self._last_skip_reason: Dict[int, str] = {}
        # {player_num: ip} — updated every status packet so we can route to source device
        self._player_ips: Dict[int, str] = {}
        # {player_num: device_name} — used for model-specific requester rules
        self._player_names: Dict[int, str] = {}
        # set[int] — known players discovered on network (for requester validation)
        self._known_players: Set[int] = set()
        # Thread-safe queue: Qt thread puts, asyncio loop gets
        self._pending: stdlib_queue.SimpleQueue = stdlib_queue.SimpleQueue()

        # Fallback path used when the legacy DB-server protocol is unavailable
        # (CDJ-3000 firmware doesn't expose it).  Reads export.pdb + ANLZ files
        # over NFSv2 from the USB-owning deck.
        self._nfs = NfsMetadataFetcher(event_bus)

        self._bus.player_state_updated.connect(self._on_state_updated)
        self._bus.device_discovered.connect(self._on_device_discovered)
        self._bus.device_lost.connect(self._on_device_lost)

    # ── Called from Qt main thread (queued cross-thread signal) ──────────────
    def _on_device_discovered(self, player_num: int, name: str, ip: str) -> None:
        """Cache every device's IP and mark as known (for requester validation)."""
        if ip:
            log.debug("Caching IP for player %d (%s): %s", player_num, name, ip)
            self._player_ips[player_num] = ip
            self._player_names[player_num] = name
            self._known_players.add(player_num)

    def _on_device_lost(self, player_num: int) -> None:
        """Clear the track cache so the next status packet triggers a fresh metadata fetch."""
        self._last_track.pop(player_num, None)
        self._last_skip_reason.pop(player_num, None)
        lost_ip = self._player_ips.pop(player_num, None)
        self._player_names.pop(player_num, None)
        self._known_players.discard(player_num)
        if lost_ip and lost_ip not in self._player_ips.values():
            # Drop cached PDB so a re-mounted USB will be re-read.
            self._nfs.forget_device(lost_ip)
        log.debug("Cleared metadata cache for lost player %d", player_num)

    def _on_state_updated(self, player_num: int, state) -> None:
        # Always track the IP and mark as known player
        if state.ip_address:
            self._player_ips[player_num] = state.ip_address
            self._known_players.add(player_num)  # Mark as discovered on network

        track_id = state.track_rekordbox_id
        if not state.ip_address:
            self._debug_skip(player_num, "missing player ip")
            return
        if track_id <= 0:
            self._debug_skip(player_num, "track has no rekordbox id")
            return
        if state.track_source_slot not in (TrackSlot.CD, TrackSlot.USB,
                                            TrackSlot.SD_CARD,
                                            TrackSlot.COLLECTION):
            self._debug_skip(
                player_num,
                f"unsupported track source slot={state.track_source_slot}",
            )
            return  # slot=0 (no track) carries no rekordbox ID
                    # TrackSlot.CD (0x01) included because CDJ-3000 reuses it for SD card

        key = (state.ip_address, track_id)
        if self._last_track.get(player_num) == key:
            return   # same track — nothing to do

        # Clear skip dedupe when the state is finally eligible.
        self._last_skip_reason.pop(player_num, None)

        self._last_track[player_num] = key

        # Query the reporting device first.
        # CDJ-3000 networks can report a shared/remote source player value that
        # does not always map to the correct metadata host for that deck.
        # Using the reporting deck IP as primary target is the most reliable.
        source_player = state.track_source_player or player_num
        query_player = player_num
        query_ip = state.ip_address
        if source_player != player_num:
            log.info(
                "Metadata source hint differs for player %d: source_player=%d "
                "source_slot=%d; prioritizing reporting player %d @ %s",
                player_num,
                source_player,
                state.track_source_slot,
                query_player,
                query_ip,
            )

        log.info(
            "Metadata routing: player %d track_id=%d  source_player=%d "
            "source_slot=%d  querying player %d @ %s",
            player_num, track_id,
            source_player, state.track_source_slot,
            query_player, query_ip,
        )

        self._pending.put_nowait((
            player_num,
            query_ip,
            query_player,
            track_id,
            state.track_source_slot,
            source_player,
            state.track_type,
        ))

    def _debug_skip(self, player_num: int, reason: str) -> None:
        previous = self._last_skip_reason.get(player_num)
        if previous == reason:
            return
        self._last_skip_reason[player_num] = reason
        log.debug("Skipping metadata fetch for player %d: %s", player_num, reason)

    # ── Asyncio loop (NetworkWorker thread) ───────────────────────────────────
    async def _query_db_port(self, ip: str) -> int:
        """
        Connect to port 12523, send the DB-Server discovery query, and return
        the 2-byte port the CDJ advertises (usually 1051).
        Falls back to DB_SERVER_DEFAULT_PORT on any failure.
        """
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, 12523),
                timeout=_CONNECT_TIMEOUT,
            )
            writer.write(DB_SERVER_QUERY)
            await writer.drain()
            resp = await asyncio.wait_for(reader.read(2), timeout=_READ_TIMEOUT)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            if len(resp) == 2:
                port = struct.unpack_from(">H", resp)[0]
                if port not in _PORT_SENTINELS:
                    log.debug("DB server port for %s: %d", ip, port)
                    return port
                log.debug(
                    "DB server query returned sentinel for %s: 0x%04X (%d)",
                    ip, port, port,
                )
        except Exception as exc:
            log.debug("Port query failed for %s: %s — using default %d",
                      ip, exc, DB_SERVER_DEFAULT_PORT)
        return DB_SERVER_DEFAULT_PORT

    def _candidate_db_ports(self, discovered: int) -> list[int]:
        """Return unique candidate DB ports to probe in priority order."""
        candidates: list[int] = []
        for port in (discovered, DB_SERVER_DEFAULT_PORT, 12523):
            if port in _PORT_SENTINELS:
                continue
            if port not in candidates:
                candidates.append(port)
        return candidates

    def _track_type_candidates(self, slot: int) -> list[int]:
        """
        Return likely track-type candidates for metadata/waveform queries.
        Try rekordbox first, then non-rekordbox media, and CD audio for slot 1.
        """
        candidates = [_TRACK_TYPE_REKORDBOX]
        if slot in (TrackSlot.CD, TrackSlot.SD_CARD, TrackSlot.USB):
            candidates.append(_TRACK_TYPE_MEDIA)
        if slot == TrackSlot.CD:
            candidates.append(_TRACK_TYPE_CD_AUDIO)
        deduped: list[int] = []
        for t in candidates:
            if t not in deduped:
                deduped.append(t)
        return deduped

    def _track_type_candidates_from_status(self, slot: int, track_type: int) -> list[int]:
        """Prefer the status-reported track type, then fall back to legacy guesses."""
        candidates: list[int] = []
        if track_type > 0:
            candidates.append(track_type)
        for candidate in self._track_type_candidates(slot):
            if candidate not in candidates:
                candidates.append(candidate)
        return candidates

    def _slot_candidates(self, slot: int) -> list[int]:
        """Return likely media-slot candidates for metadata queries."""
        candidates = [slot]
        for s in (TrackSlot.USB, TrackSlot.SD_CARD, TrackSlot.CD, TrackSlot.COLLECTION):
            if s not in candidates:
                candidates.append(s)
        return candidates

    def _menu_candidates(self) -> list[int]:
        """Try common menu-location values observed across player families."""
        return [1, 0]

    def _is_metadata_limited_player(self, player_num: int) -> bool:
        """Return true for older players that require a standard 1-4 requester."""
        name = self._player_names.get(player_num, "")
        return player_num < 7 and name not in _METADATA_FLEXIBLE_DEVICES

    def _metadata_requester_candidates(self, target_player: int) -> list[int]:
        """
        Return requester candidates (D values) using Beat Link-style rules.

        Older players require a standard requester 1-4 that is actually present
        on the network. Newer "metadata flexible" players can be queried while
        we pose as the target player itself when our virtual player number is
        outside 1-4.
        """
        virtual_player = self._virtual_player_number

        candidates: list[int] = []

        # Prefer an explicit virtual requester in the standard range first.
        if 1 <= virtual_player <= 4 and virtual_player != target_player:
            candidates.append(virtual_player)

        # Then try any known physical 1-4 requester slots.
        known_candidates = [
            n for n in (1, 2, 3, 4)
            if n != target_player and n in self._known_players and n not in candidates
        ]
        candidates.extend(known_candidates)

        # Metadata-flexible players (CDJ-3000/XDJ-AZ) can often be queried while
        # posing as themselves; keep this as an important fallback.
        if not self._is_metadata_limited_player(target_player) and target_player not in candidates:
            candidates.append(target_player)

        if candidates:
            return candidates

        if virtual_player > 4 and self._is_metadata_limited_player(target_player):
            log.warning(
                "No valid metadata requester available for player %d (%s): "
                "virtual player %d is outside the standard 1-4 range and no "
                "other standard players are available on the network; "
                "trying target player as fallback requester",
                target_player,
                self._player_names.get(target_player, "unknown"),
                virtual_player,
            )
            return [target_player]

        log.debug(
            "No known player candidates available for requester; "
            "falling back to untested candidates (known_players=%s)",
            sorted(self._known_players),
        )
        return _default_metadata_requester_candidates(target_player)

    async def run(self, stop_event: asyncio.Event) -> None:
        log.info("Metadata client running")
        while not stop_event.is_set():
            try:
                item = self._pending.get_nowait()
                await self._fetch(*item)
            except stdlib_queue.Empty:
                await asyncio.sleep(0.25)
            except Exception as exc:
                log.warning("Metadata client error: %s", exc, exc_info=True)
        log.info("Metadata client stopped")

    async def _fetch(
        self,
        player_num: int,
        ip: str,
        query_player: int,
        track_id: int,
        slot: int,
        source_player: int,
        status_track_type: int,
    ) -> None:
        log.info(
            "Fetching metadata: for player=%d track_id=%d  querying player=%d @ %s "
            "(source_player=%d, track_type=%d, known_players=%s)",
            player_num, track_id, query_player, ip, source_player, status_track_type,
            sorted(self._known_players),
        )
        self._bus.network_info.emit(
            f"Loading track info for player {player_num} (track {track_id})..."
        )

        if not ip:
            log.warning(
                "Cannot fetch metadata for player %d track_id=%d: query player %d has no IP",
                player_num, track_id, query_player,
            )
            return

        # ── Step 0: Validate target player is known ────────────────────────────
        # Beat Link-style validation before attempting connection
        if query_player not in self._known_players:
            log.warning(
                "Target query player %d not in known_players=%s; "
                "attempting fallback (player will retry if available)",
                query_player, sorted(self._known_players),
            )

        # ── Step 1: discover actual DB server port ────────────────────────────
        db_port = await self._query_db_port(ip)

        ports = self._candidate_db_ports(db_port)
        if not ports:
            log.debug("No valid DB port candidates for %s", ip)
            return

        for selected_port in ports:
            requester_candidates = self._metadata_requester_candidates(query_player)
            if not requester_candidates:
                continue
            got_any_data = False
            tx = 1

            for D in requester_candidates:
                # ── Step 2: connect to DB server (fresh session per D) ──────
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(ip, selected_port),
                        timeout=_CONNECT_TIMEOUT,
                    )
                except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as exc:
                    log.warning("Cannot reach DB server at %s:%d — %s", ip, selected_port, exc)
                    break

                try:
                    # ── Step 3: greeting exchange ─────────────────────────────
                    writer.write(GREETING)
                    await writer.drain()
                    greeting_echo = await asyncio.wait_for(reader.readexactly(5), timeout=_READ_TIMEOUT)
                    if greeting_echo != GREETING:
                        raise ValueError(
                            f"Unexpected greeting echo on {ip}:{selected_port}: {greeting_echo.hex()}"
                        )

                    log.debug("Using DB server %s:%d", ip, selected_port)

                    # ── Step 4: context setup ─────────────────────────────────
                    setup_msg = make_setup_msg(D)
                    log.debug(
                        "DB setup request: target_player=%d D=%d bytes=%s",
                        query_player, D, setup_msg.hex(),
                    )
                    writer.write(setup_msg)
                    await writer.drain()
                    try:
                        _, resp_type, _ = await asyncio.wait_for(
                            read_message(reader), timeout=_SETUP_TIMEOUT)
                    except asyncio.TimeoutError:
                        log.warning(
                            "Context setup timeout for player %d on %s:%d D=%d",
                            player_num, ip, selected_port, D,
                        )
                        continue
                    if resp_type != RESP_SUCCESS:
                        log.warning(
                            "Context setup rejected (type=0x%04X) for player %d on %s:%d D=%d",
                            resp_type, player_num, ip, selected_port, D,
                        )
                        continue

                    slot_candidates = self._slot_candidates(slot)

                    for menu_loc in self._menu_candidates():
                        if got_any_data:
                            break
                        for query_slot in slot_candidates:
                            if got_any_data:
                                break
                            track_types = self._track_type_candidates_from_status(
                                query_slot, status_track_type)
                            for track_type in track_types:
                                # ── Step 5: track metadata request ───────────────
                                metadata_msg = make_metadata_request(
                                    tx, D, query_slot, track_id, track_type, menu_loc)
                                log.debug(
                                    "Metadata request: tx=%d target_player=%d source_player=%d "
                                    "D=%d menu=%d slot=%d track_type=%d track_id=%d bytes=%s",
                                    tx, query_player, source_player,
                                    D, menu_loc, query_slot, track_type, track_id,
                                    metadata_msg.hex(),
                                )
                                writer.write(metadata_msg)
                                await writer.drain()
                                _, resp_type, args = await asyncio.wait_for(
                                    read_message(reader), timeout=_READ_TIMEOUT)
                                raw_items = args[1] if resp_type == RESP_SUCCESS and len(args) > 1 else 0
                                n_items = 0 if raw_items == _NO_ITEMS else int(raw_items)
                                if resp_type != RESP_SUCCESS:
                                    log.debug(
                                        "Metadata response non-success: tx=%d resp_type=0x%04X "
                                        "menu=%d slot=%d track_type=%d D=%d args=%s",
                                        tx, resp_type, menu_loc, query_slot, track_type, D, args,
                                    )
                                elif raw_items not in (_NO_ITEMS,) and n_items == 0:
                                    log.debug(
                                        "Metadata response empty list: tx=%d raw_items=%s menu=%d "
                                        "slot=%d track_type=%d D=%d",
                                        tx, raw_items, menu_loc, query_slot, track_type, D,
                                    )
                                tx += 1

                                if raw_items == _NO_ITEMS:
                                    log.info(
                                        "No metadata items: player %d track_id=%d menu=%d "
                                        "slot=%d track_type=%d  D=%d  queried=%s:%d  resp_type=0x%04X",
                                        player_num, track_id, menu_loc,
                                        query_slot, track_type,
                                        D, ip, selected_port, resp_type,
                                    )

                                if n_items > 0:
                                    log.info(
                                        "Metadata candidate hit: player=%d menu=%d slot=%d "
                                        "track_type=%d items=%d",
                                        player_num, menu_loc, query_slot, track_type, n_items,
                                    )

                                    # ── Step 6: render menu to collect metadata ─
                                    writer.write(make_render_menu(
                                        tx, D, query_slot, n_items, track_type, menu_loc))
                                    await writer.drain()
                                    menu_items: list = []
                                    for _ in range(n_items + 2):   # header + items + footer
                                        _, mtype, margs = await asyncio.wait_for(
                                            read_message(reader), timeout=_READ_TIMEOUT)
                                        if mtype == RESP_MENU_ITEM:
                                            menu_items.append(margs)
                                    meta = self._handle_metadata(player_num, track_id, menu_items)
                                    got_any_data = True
                                    tx += 1

                                    # ── Step 6b: artwork image (if track exposes artwork ID) ─
                                    if meta is not None and meta.artwork_id > 0:
                                        writer.write(make_album_art_request(
                                            tx,
                                            D,
                                            query_slot,
                                            meta.artwork_id,
                                            track_type,
                                            high_res=False,
                                        ))
                                        await writer.drain()
                                        _, resp_type, args = await asyncio.wait_for(
                                            read_message(reader), timeout=_READ_TIMEOUT)
                                        got_art = self._handle_album_art(player_num, resp_type, args)
                                        if got_art:
                                            self._bus.network_info.emit(
                                                f"Loaded artwork for player {player_num}"
                                            )
                                        tx += 1

                                    # ── Step 7: waveform preview ────────────────
                                    writer.write(make_waveform_preview_request(
                                        tx, D, query_slot, track_id, track_type))
                                    await writer.drain()
                                    _, _, args = await asyncio.wait_for(
                                        read_message(reader), timeout=_READ_TIMEOUT)
                                    got_preview = self._handle_waveform_preview(player_num, args)
                                    if got_preview:
                                        self._bus.network_info.emit(
                                            f"Loaded waveform preview for player {player_num}"
                                        )
                                    tx += 1

                                    # ── Step 8: waveform detail (Nxs2 color preferred, mono fallback) ─
                                    writer.write(make_nxs2_waveform_detail_request(
                                        tx, D, query_slot, track_id, track_type))
                                    await writer.drain()
                                    _, _, args = await asyncio.wait_for(
                                        read_message(reader), timeout=_READ_TIMEOUT)
                                    got_detail = self._handle_waveform_color(player_num, args)
                                    if got_detail:
                                        self._bus.network_info.emit(
                                            f"Loaded color waveform for player {player_num}"
                                        )
                                    tx += 1

                                    if not got_detail:
                                        # Fallback: request monochrome detail
                                        writer.write(make_waveform_detail_request(
                                            tx, D, query_slot, track_id, track_type))
                                        await writer.drain()
                                        _, _, args = await asyncio.wait_for(
                                            read_message(reader), timeout=_READ_TIMEOUT)
                                        got_detail = self._handle_waveform_detail(player_num, args)
                                        if got_detail:
                                            self._bus.network_info.emit(
                                                f"Loaded waveform detail for player {player_num}"
                                            )
                                        tx += 1

                                    # ── Step 9: beat grid ───────────────────────
                                    writer.write(make_beat_grid_request(
                                        tx, D, query_slot, track_id, track_type))
                                    await writer.drain()
                                    _, _, args = await asyncio.wait_for(
                                        read_message(reader), timeout=_READ_TIMEOUT)
                                    got_grid = self._handle_beat_grid(player_num, args)
                                    if got_grid:
                                        self._bus.network_info.emit(
                                            f"Loaded beat grid for player {player_num}"
                                        )
                                    tx += 1

                                    if got_preview or got_detail or got_grid:
                                        got_any_data = True
                                    break

                except (asyncio.TimeoutError, EOFError, OSError, ValueError) as exc:
                    log.warning(
                        "Metadata exchange failed for player %d on %s:%d D=%d: %s",
                        player_num, ip, selected_port, D, exc,
                    )
                finally:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:
                        pass

                if got_any_data:
                    return

            log.debug(
                "No metadata match on %s:%d after trying D candidates %s",
                ip, selected_port, requester_candidates,
            )

        log.warning(
            "Metadata fetch failed for player %d track_id=%d on all DB targets for %s: %s; "
            "falling through to NFS fallback",
            player_num,
            track_id,
            ip,
            ports,
        )

        # ── Fallback: read export.pdb + ANLZ over NFS from the USB-owning deck ──
        # The reporting deck and the USB-owning deck are usually different on
        # CDJ-3000 networks: status packets carry source_player to identify the
        # device that actually owns the media.  Prefer that deck's IP.
        nfs_ip = self._player_ips.get(source_player) or ip
        try:
            ok = await self._nfs.fetch_track(player_num, nfs_ip, track_id)
        except Exception as exc:
            log.warning(
                "NFS metadata fallback raised for player %d track_id=%d on %s: %s",
                player_num, track_id, nfs_ip, exc, exc_info=True,
            )
            ok = False

        if not ok:
            # Clear cache entry so the next status update retries this same track.
            self._last_track.pop(player_num, None)

    def _handle_metadata(self, player_num: int, track_id: int, menu_items: list):
        """Parse rendered menu items and emit a TrackMetadata signal."""
        from core.analysis.track_metadata import TrackMetadata

        title = artist = album = genre = key = ""
        comment = date_added = color = ""
        rating = 0
        audio_format = ""
        artwork_available = False
        artwork_id = 0
        duration_s = 0
        bpm = 0.0
        for item_args in menu_items:
            if len(item_args) < 7:
                continue
            # CDJ-3000 packs extra info in the high bytes; mask to low 16 bits.
            item_type = (item_args[6] & 0xFFFF) if isinstance(item_args[6], int) else 0
            label1 = item_args[3] if len(item_args) > 3 and isinstance(item_args[3], str) else ""
            if item_type == _ITEM_TITLE:
                title = label1
                artwork_id = (
                    item_args[8]
                    if len(item_args) > 8 and isinstance(item_args[8], int)
                    else 0
                )
                artwork_available = artwork_id > 0
            elif item_type == _ITEM_ARTIST:
                artist = label1
            elif item_type == _ITEM_ALBUM:
                album = label1
            elif item_type == _ITEM_GENRE:
                genre = label1
            elif item_type == _ITEM_KEY:
                key = label1
            elif item_type == _ITEM_COMMENT:
                comment = label1
            elif item_type == _ITEM_DATE:
                date_added = label1
            elif item_type == _ITEM_RATING:
                rating = (
                    item_args[1]
                    if len(item_args) > 1 and isinstance(item_args[1], int)
                    else 0
                )
            elif item_type == _ITEM_DURATION:
                duration_s = (
                    item_args[1]
                    if len(item_args) > 1 and isinstance(item_args[1], int)
                    else 0
                )
            elif item_type == _ITEM_TEMPO:
                bpm = (
                    (item_args[1] / 100.0)
                    if len(item_args) > 1 and isinstance(item_args[1], int) and item_args[1] > 0
                    else 0.0
                )
            elif item_type in _COLOR_ITEM_TYPES:
                color = label1 or _COLOR_ITEM_TYPES[item_type]

        # Heuristic: if title exposes a filename-like suffix, treat it as format.
        if title and "." in title:
            ext = title.rsplit(".", 1)[-1].strip().upper()
            if 1 < len(ext) <= 5:
                audio_format = ext

        meta = TrackMetadata(
            player_num=player_num,
            rekordbox_id=track_id,
            title=title,
            artist=artist,
            album=album,
            genre=genre,
            comment=comment,
            date_added=date_added,
            color=color,
            rating=rating,
            audio_format=audio_format,
            artwork_available=artwork_available,
            artwork_id=max(0, int(artwork_id)),
            key=key,
            duration_s=duration_s,
            bpm=bpm,
        )
        self._bus.metadata_received.emit(player_num, meta)
        log.info("Metadata: player=%d '%s' — %s", player_num, meta.title, meta.artist)
        self._bus.network_info.emit(
            f"Track metadata loaded for player {player_num}: {meta.title or 'Unknown Title'}"
        )
        return meta

    def _handle_album_art(self, player_num: int, resp_type: int, args: list) -> bool:
        if resp_type != RESP_ALBUM_ART:
            return False
        blob = args[3] if len(args) > 3 and isinstance(args[3], bytes) else b""
        if len(blob) < 32:
            log.debug("Album art missing/too small for player %d (blob=%d bytes)", player_num, len(blob))
            return False
        self._bus.album_art_received.emit(player_num, blob)
        log.info("Album art: player=%d (%d bytes)", player_num, len(blob))
        return True

    def _handle_waveform_preview(self, player_num: int, args: list) -> bool:
        # Response args: [echo, unknown, length, blob] — blob absent when length=0
        blob = args[3] if len(args) > 3 and isinstance(args[3], bytes) else b""
        if len(blob) >= 400:
            self._bus.waveform_preview_received.emit(player_num, blob)
            log.info("Waveform preview: player=%d (%d bytes)", player_num, len(blob))
            return True
        else:
            log.debug("Waveform preview not found for player %d (blob=%d bytes)",
                      player_num, len(blob))
            return False

    def _handle_waveform_color(self, player_num: int, args: list) -> bool:
        # Nxs2 0x4f02 response: 5 args — echo, 0, length, blob, 0
        # blob = raw ANLZ tag; waveform data begins at byte 34 inside it.
        blob = args[3] if len(args) > 3 and isinstance(args[3], bytes) else b""
        if len(blob) > 34 and len(blob[34:]) % 2 == 0:
            self._bus.waveform_color_received.emit(player_num, blob)
            log.info("Waveform color (Nxs2): player=%d (%d bytes, %d cols)",
                     player_num, len(blob), (len(blob) - 34) // 2)
            return True
        else:
            log.debug("Nxs2 color waveform not available for player %d (blob=%d bytes)",
                      player_num, len(blob))
            return False

    def _handle_waveform_detail(self, player_num: int, args: list) -> bool:
        # Response args: [echo, unknown, length, blob] — blob absent when length=0
        blob = args[3] if len(args) > 3 and isinstance(args[3], bytes) else b""
        if len(blob) >= 3:
            self._bus.waveform_detail_received.emit(player_num, blob)
            log.info("Waveform detail: player=%d (%d bytes)", player_num, len(blob))
            return True
        else:
            log.debug("Waveform detail not found for player %d (blob=%d bytes)",
                      player_num, len(blob))
            return False

    def _handle_beat_grid(self, player_num: int, args: list) -> bool:
        from core.analysis.beat_grid import TrackBeatGrid

        blob = args[3] if len(args) > 3 and isinstance(args[3], bytes) else b""
        if len(blob) < 20:
            log.debug("Beat grid not found for player %d (blob=%d bytes)", player_num, len(blob))
            return False

        beat_times: list[int] = []
        beat_within_bar: list[int] = []
        for base in range(20, len(blob) - 15, 16):
            beat_mark = int.from_bytes(blob[base:base + 2], "little", signed=False)
            if beat_mark < 1 or beat_mark > 4:
                beat_mark = 0
            beat_within_bar.append(beat_mark)
            # Beat times are little-endian and can be negative for pre-track beats.
            # Preserve signed values so waveform grid alignment remains correct when
            # tracks have leading silence before the first audible kick.
            beat_times.append(int.from_bytes(blob[base + 4:base + 8], "little", signed=True))

        if not beat_times:
            log.debug("Beat grid empty for player %d", player_num)
            return False

        grid = TrackBeatGrid(
            player_num=player_num,
            beat_times_ms=tuple(beat_times),
            beat_within_bar=tuple(beat_within_bar),
        )
        self._bus.beat_grid_received.emit(player_num, grid)
        log.info("Beat grid: player=%d (%d beats)", player_num, len(beat_times))
        return True
