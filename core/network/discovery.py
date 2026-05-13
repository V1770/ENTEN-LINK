"""UDP 50000 — device announce / keepalive listener."""
from __future__ import annotations
import asyncio
import socket
import logging
from typing import Optional

from core.network.constants import PORT_ANNOUNCE
from core.network.packet_parser import PacketParser, PacketType

log = logging.getLogger(__name__)


def _local_ipv4_addresses() -> set[str]:
    """Best-effort set of local IPv4 addresses used for self-packet filtering.

    Uses hostname resolution, ifconfig (macOS/Linux), and a routing-trick
    UDP connect so that the set is populated correctly on all platforms,
    including Windows where ifconfig is not available.
    """
    import re, subprocess  # noqa: PLC0415
    result = {"127.0.0.1"}
    try:
        host = socket.gethostname()
        for family, _, _, _, sockaddr in socket.getaddrinfo(host, None):
            if family == socket.AF_INET and sockaddr:
                result.add(sockaddr[0])
    except OSError:
        pass
    try:
        out = subprocess.check_output(["ifconfig"], text=True, timeout=2)
        for m in re.finditer(r'\binet\s+(\d+\.\d+\.\d+\.\d+)', out):
            result.add(m.group(1))
    except Exception:
        pass
    # Routing-trick fallback: always gets the correct outgoing LAN IP even on
    # Windows (where ifconfig is absent) or when hostname resolution returns
    # only loopback / IPv6.  This guarantees the self-announce filter works.
    try:
        _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _s.connect(("8.8.8.8", 80))
        result.add(_s.getsockname()[0])
        _s.close()
    except OSError:
        pass
    return result


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(
        self,
        event_bus,
        parser: PacketParser,
        local_ips: set[str],
        ignore_virtual_player: Optional[int],
    ) -> None:
        self._bus = event_bus
        self._parser = parser
        self._local_ips = local_ips
        self._ignore_virtual_player = ignore_virtual_player
        self._debug_non_announce_packets = 0

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        pkt = self._parser.parse(data)
        if pkt is None:
            return
        if pkt.type != PacketType.DEVICE_ANNOUNCE:
            if self._debug_non_announce_packets < 8:
                log.debug(
                    "Ignoring UDP :50000 packet type=0x%02X len=%d from %s",
                    int(pkt.type), len(data), addr[0],
                )
                self._debug_non_announce_packets += 1
            return

        # Ignore our own virtual announcer broadcasts to avoid creating a
        # synthetic local "device" in DeviceManager/UI.
        if (
            self._ignore_virtual_player is not None
            and addr[0] in self._local_ips
            and pkt.device_number == self._ignore_virtual_player
            and pkt.device_name == "Pioneer DJ Link"
        ):
            log.debug(
                "Ignoring self-announce for virtual player #%d from %s",
                pkt.device_number,
                addr[0],
            )
            return

        ip = pkt.ip_address or addr[0]
        log.debug("Announce #%d '%s' from %s", pkt.device_number, pkt.device_name, ip)
        self._bus.device_discovered.emit(pkt.device_number, pkt.device_name, ip)

    def error_received(self, exc: Exception) -> None:
        log.warning("Discovery socket error: %s", exc)


class DiscoveryReceiver:
    def __init__(self, event_bus, ignore_virtual_player: Optional[int] = None) -> None:
        self._bus = event_bus
        self._parser = PacketParser()
        self._local_ips = _local_ipv4_addresses()
        self._ignore_virtual_player = ignore_virtual_player

    async def listen(self, stop_event: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass  # SO_REUSEPORT absent or unsupported on this platform/Python version
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", PORT_ANNOUNCE))

        transport, _ = await loop.create_datagram_endpoint(
            lambda: _DiscoveryProtocol(
                self._bus,
                self._parser,
                self._local_ips,
                self._ignore_virtual_player,
            ),
            sock=sock,
        )
        log.info("Listening for device announcements on UDP :%d", PORT_ANNOUNCE)
        try:
            await stop_event.wait()
        finally:
            transport.close()
