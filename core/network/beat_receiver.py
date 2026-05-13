"""UDP 50001 — beat packet listener."""
from __future__ import annotations
import asyncio
import socket
import logging

from core.network.constants import PORT_BEAT
from core.network.packet_parser import PacketParser, PacketType

log = logging.getLogger(__name__)


class _BeatProtocol(asyncio.DatagramProtocol):
    def __init__(self, event_bus, parser: PacketParser) -> None:
        self._bus = event_bus
        self._parser = parser
        self._debug_any_packets = 0
        self._debug_parse_fail_packets = 0
        self._debug_non_beat_packets = 0
        self._debug_precise_packets = 0

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

        if pkt.type != PacketType.BEAT:
            if self._debug_non_beat_packets < 5:
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
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass
        sock.bind(("", PORT_BEAT))

        transport, _ = await loop.create_datagram_endpoint(
            lambda: _BeatProtocol(self._bus, self._parser),
            sock=sock,
        )
        log.info("Listening for beat packets on UDP :%d", PORT_BEAT)
        try:
            await stop_event.wait()
        finally:
            transport.close()
