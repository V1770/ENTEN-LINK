"""UDP 50002 — CDJ player status packet listener."""
from __future__ import annotations
import asyncio
import socket
import logging

from core.network.constants import PORT_STATUS
from core.network.packet_parser import PacketParser, PacketType
from core.devices.player_state import PlayerState, PlayStateRaw

log = logging.getLogger(__name__)


class _StatusProtocol(asyncio.DatagramProtocol):
    def __init__(self, event_bus, parser: PacketParser) -> None:
        self._bus = event_bus
        self._parser = parser
        self._debug_any_packets = 0
        self._debug_parse_fail_packets = 0
        self._debug_non_status_packets = 0
        self._last_status_summary: dict[int, tuple] = {}

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        if self._debug_any_packets < 8:
            log.debug("UDP :50002 packet len=%d from %s", len(data), addr[0])
            self._debug_any_packets += 1

        pkt = self._parser.parse(data)
        if pkt is None:
            if self._debug_parse_fail_packets < 5:
                preview = data[:10].hex()
                log.debug(
                    "Ignoring UDP :50002 unparsed packet len=%d from %s first10=%s",
                    len(data), addr[0], preview,
                )
                self._debug_parse_fail_packets += 1
            return
        if pkt.type != PacketType.CDJ_STATUS:
            # Low-noise diagnostics: log only first few non-status packets on :50002.
            if self._debug_non_status_packets < 5:
                log.debug(
                    "Ignoring UDP :50002 packet type=0x%02X len=%d from %s",
                    int(pkt.type), len(data), addr[0],
                )
                self._debug_non_status_packets += 1
            return

        if log.isEnabledFor(logging.DEBUG):
            summary = (
                round(pkt.bpm, 2),
                pkt.beat_number,
                pkt.beat_in_bar,
                pkt.play_state_byte,
                pkt.is_playing,
                round(pkt.pitch, 4),
                pkt.track_source_slot,
                pkt.track_type,
                pkt.track_rekordbox_id,
            )
            if self._last_status_summary.get(pkt.device_number) != summary:
                self._last_status_summary[pkt.device_number] = summary
                log.debug(
                    "CDJ status packet from %s slot=%d bpm=%.2f beat_num=%d beat=%d play=0x%02X is_playing=%s pitch=%+.4f source_slot=%d track_type=%d rb_id=%d",
                    addr[0],
                    pkt.device_number,
                    pkt.bpm,
                    pkt.beat_number,
                    pkt.beat_in_bar,
                    pkt.play_state_byte,
                    pkt.is_playing,
                    pkt.pitch,
                    pkt.track_source_slot,
                    pkt.track_type,
                    pkt.track_rekordbox_id,
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

    def error_received(self, exc: Exception) -> None:
        log.warning("Status socket error: %s", exc)


class StatusReceiver:
    def __init__(self, event_bus) -> None:
        self._bus = event_bus
        self._parser = PacketParser()

    async def listen(self, stop_event: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass  # SO_REUSEPORT absent or unsupported on this platform/Python version
        sock.bind(("", PORT_STATUS))

        transport, _ = await loop.create_datagram_endpoint(
            lambda: _StatusProtocol(self._bus, self._parser),
            sock=sock,
        )
        log.info("Listening for CDJ status on UDP :%d", PORT_STATUS)
        try:
            await stop_event.wait()
        finally:
            transport.close()
