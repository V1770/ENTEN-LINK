"""UDP 50001 — beat packet listener."""
from __future__ import annotations
import asyncio
import socket
import logging

from core.network.constants import PORT_BEAT
from core.network.packet_parser import PacketParser, PacketType
from core.devices.player_state import PlayerState, PlayStateRaw

log = logging.getLogger(__name__)


class _BeatProtocol(asyncio.DatagramProtocol):
    def __init__(self, event_bus, parser: PacketParser) -> None:
        self._bus = event_bus
        self._parser = parser
        self._debug_any_packets = 0
        self._debug_parse_fail_packets = 0
        self._debug_non_beat_packets = 0
        self._debug_precise_packets = 0
        # CDJ-3000 sends CDJ_STATUS on 50001 (not 50002).  Track per-device to
        # avoid duplicate INFO messages after the first one per player.
        self._seen_cdj_status_players: set[int] = set()

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        if self._debug_any_packets < 8:
            log.debug("UDP :50001 packet len=%d from %s", len(data), addr[0])
            self._debug_any_packets += 1

        pkt = self._parser.parse(data)
        if pkt is None:
            if self._debug_parse_fail_packets < 5:
                preview = data[:10].hex()
                log.debug(
                    "Ignoring UDP :50001 unparsed packet len=%d from %s first10=%s",
                    len(data), addr[0], preview,
                )
                self._debug_parse_fail_packets += 1
            return

        if pkt.type == PacketType.PRECISE_POSITION:
            if self._debug_precise_packets < 8:
                log.debug(
                    "Precise position packet from %s slot=%d pos=%dms len=%dms bpm=%.2f pitch=%+.3f",
                    addr[0], pkt.device_number, pkt.position_ms, pkt.track_length_ms, pkt.bpm, pkt.pitch,
                )
                self._debug_precise_packets += 1
            self._bus.precise_position_received.emit(
                pkt.device_number,
                int(pkt.position_ms),
                int(pkt.track_length_ms),
                float(pkt.bpm),
                float(pkt.pitch),
            )
            return

        if pkt.type == PacketType.CDJ_STATUS:
            # CDJ-3000 sends CDJ_STATUS on UDP 50001 rather than 50002.
            # Handle it here so track metadata is available on CDJ-3000 networks.
            if pkt.device_number not in self._seen_cdj_status_players:
                self._seen_cdj_status_players.add(pkt.device_number)
                log.info(
                    "CDJ status (on :50001) from player %d @ %s track_id=%d slot=%d",
                    pkt.device_number, addr[0],
                    pkt.track_rekordbox_id, pkt.track_source_slot,
                )
            try:
                raw_state = PlayStateRaw(pkt.play_state_byte)
            except ValueError:
                raw_state = PlayStateRaw.UNKNOWN
            state = PlayerState(
                player_number=pkt.device_number,
                name=pkt.device_name,
                ip_address=addr[0],
                bpm=pkt.bpm,
                pitch=pkt.pitch,
                position_ms=pkt.position_ms,
                beat_number=pkt.beat_number,
                beat_in_bar=pkt.beat_in_bar,
                play_state_raw=raw_state,
                is_playing=pkt.is_playing,
                is_master=pkt.is_master,
                is_sync=pkt.is_sync,
                is_on_air=pkt.is_on_air,
                loop_active=pkt.loop_active,
                master_tempo=pkt.master_tempo,
                loop_start_ms=pkt.loop_start_ms,
                loop_end_ms=pkt.loop_end_ms,
                track_source_slot=pkt.track_source_slot,
                track_source_player=pkt.track_source_player,
                track_type=pkt.track_type,
                track_rekordbox_id=pkt.track_rekordbox_id,
            )
            self._bus.player_state_updated.emit(pkt.device_number, state)
            return

        if pkt.type != PacketType.BEAT:
            if self._debug_non_beat_packets < 3:
                log.debug(
                    "Ignoring UDP :50001 packet type=0x%02X len=%d from %s",
                    int(pkt.type), len(data), addr[0],
                )
                self._debug_non_beat_packets += 1
            return

        # BPM 0xFFFFFFFF/100 ≈ 42949672.95 and beat_in_bar=255 are CDJ idle
        # sentinels meaning "no track loaded".  Filter both before emitting.
        if not (0 < pkt.bpm < 500 and 1 <= pkt.beat_in_bar <= 4):
            log.debug(
                "Idle beat sentinel from %s slot=%d bpm=%.2f beat=%d — ignored",
                addr[0], pkt.device_number, pkt.bpm, pkt.beat_in_bar,
            )
            return

        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "Beat packet from %s slot=%d bpm=%.2f eff=%.2f pitch=%+.3f next=%dms second=%dms bar=%dms beat=%d",
                addr[0],
                pkt.device_number,
                pkt.bpm,
                pkt.effective_bpm,
                pkt.pitch,
                pkt.next_beat_ms,
                pkt.second_beat_ms,
                pkt.next_bar_ms,
                pkt.beat_in_bar,
            )
        bpm_out = pkt.effective_bpm if pkt.effective_bpm > 0 else pkt.bpm
        timing = {
            "next_beat_ms": int(pkt.next_beat_ms),
            "second_beat_ms": int(pkt.second_beat_ms),
            "next_bar_ms": int(pkt.next_bar_ms),
            "fourth_beat_ms": int(pkt.fourth_beat_ms),
            "second_bar_ms": int(pkt.second_bar_ms),
            "eighth_beat_ms": int(pkt.eighth_beat_ms),
        }
        self._bus.beat_detected.emit(pkt.device_number, bpm_out, pkt.beat_in_bar, timing)

    def error_received(self, exc: Exception) -> None:
        log.warning("Beat socket error: %s", exc)


class BeatReceiver:
    def __init__(self, event_bus) -> None:
        self._bus = event_bus
        self._parser = PacketParser()

    async def listen(self, stop_event: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()

        # Retry binding up to 3 times — port 50001 may be briefly held by a
        # previous instance or the Windows dynamic port allocator.
        sock = None
        for attempt in range(3):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass
            try:
                s.bind(("", PORT_BEAT))
                sock = s
                break
            except OSError as exc:
                s.close()
                if attempt < 2:
                    log.warning(
                        "Beat receiver: UDP :%d bind failed (attempt %d/3): %s — retrying in 2s",
                        PORT_BEAT, attempt + 1, exc,
                    )
                    await asyncio.sleep(2.0)
                else:
                    log.error(
                        "Beat receiver could not bind to UDP :%d — %s. "
                        "Check Windows Firewall or run the installer to add firewall rules. "
                        "Beat/position packets will not be received.",
                        PORT_BEAT, exc,
                    )
                    raise

        transport, _ = await loop.create_datagram_endpoint(
            lambda: _BeatProtocol(self._bus, self._parser),
            sock=sock,
        )
        log.info("Listening for beat packets on UDP :%d", PORT_BEAT)
        try:
            await stop_event.wait()
        finally:
            transport.close()
