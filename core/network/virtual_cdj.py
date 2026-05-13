"""
VirtualCDJAnnouncer — makes this app visible as a Pro DJ Link device.

rekordbox (and physical CDJs) only emit full status packets when they detect
another Pro DJ Link device on the network.  This announcer sends the full
4-phase device-number claim handshake that rekordbox requires before it
starts emitting status and beat packets.

Startup sequence (each phase sends 3 packets at 300 ms intervals):
  Phase 0 — Hello         (type 0x0A, 38 bytes)
  Phase 1 — Stage-1 claim (type 0x00, 44 bytes)
  Phase 2 — Stage-2 claim (type 0x02, 50 bytes)
  Phase 3 — Final claim   (type 0x04, 38 bytes)
  Ongoing — Keep-alive    (type 0x06, 54 bytes, every 1.5 s)

Packet layout exactly matches beat-link VirtualCdj.java (CDJ-3000-compatible
template).  Key offsets:
  DEVICE_NAME_OFFSET   = 0x0c (12)  — 20 bytes
  DEVICE_NUMBER_OFFSET = 0x24 (36)
  MAC_ADDRESS_OFFSET   = 0x26 (38)  — 6 bytes
  IP_ADDRESS_OFFSET    = 0x2c (44)  — 4 bytes

References:
  https://djl-analysis.deepsymmetry.org/djl-analysis/startup.html
  https://github.com/Deep-Symmetry/beat-link (VirtualCdj.java)
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import struct
import subprocess

from core.network.constants import MAGIC, PORT_ANNOUNCE

log = logging.getLogger(__name__)

# ── Protocol layout constants (beat-link exact offsets) ──────────────────────
_NAME_OFFSET = 0x0c   # 12 — DEVICE_NAME_OFFSET
_NAME_LEN    = 0x14   # 20 — DEVICE_NAME_LENGTH
_NUM_OFFSET  = 0x24   # 36 — DEVICE_NUMBER_OFFSET
_MAC_OFFSET  = 0x26   # 38 — MAC_ADDRESS_OFFSET
_IP_OFFSET   = 0x2c   # 44 — IP address

_CLAIM_INTERVAL     = 0.30   # between each claim packet (seconds)
_KEEPALIVE_INTERVAL = 1.50   # keepalive period (seconds)

_DEVICE_NAME = "Pioneer DJ Link"



# ── Network interface detection ───────────────────────────────────────────────

def _parse_ifconfig_interfaces() -> list[tuple[str, str, str, bytes, bool]]:
    """Parse ifconfig (macOS/Linux) or ipconfig /all (Windows).

    Returns (iface, ip, netmask_hex, mac_bytes, is_active).
    """
    import sys as _sys
    if _sys.platform == "win32":
        return _parse_ipconfig_interfaces()
    try:
        out = subprocess.check_output(["ifconfig"], text=True, timeout=2)
    except Exception as exc:
        log.debug("Could not run ifconfig: %s", exc)
        return []

    interfaces: list[tuple[str, str, str, bytes, bool]] = []
    for block in re.split(r"\n(?=\S)", out):
        header = re.match(r"^(\S+):", block)
        if not header:
            continue
        inet = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)", block)
        if not inet:
            continue
        ip = inet.group(1)
        if ip.startswith("127."):
            continue
        mask_m = re.search(r"\bnetmask\s+(0x[0-9a-fA-F]+)", block)
        mask_hex = mask_m.group(1) if mask_m else "0xffffff00"
        mac_m = re.search(r"\bether\s+([0-9a-f]{2}(?::[0-9a-f]{2}){5})", block)
        mac = (bytes(int(x, 16) for x in mac_m.group(1).split(":"))
               if mac_m else b"\x00" * 6)
        interfaces.append((header.group(1), ip, mask_hex, mac, "status: active" in block))
    return interfaces


def _parse_ipconfig_interfaces() -> list[tuple[str, str, str, bytes, bool]]:
    """Windows equivalent of _parse_ifconfig_interfaces using 'ipconfig /all'."""
    try:
        # STARTUPINFO hides the console window without breaking stdout capture.
        # CREATE_NO_WINDOW (0x08000000) prevents stdout capture in windowless
        # PyInstaller GUI builds, causing check_output to return None.
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        out = subprocess.check_output(
            ["ipconfig", "/all"], text=True, timeout=4,
            stderr=subprocess.DEVNULL, startupinfo=si,
        )
    except Exception as exc:
        log.debug("Could not run ipconfig: %s", exc)
        return []

    if not out:
        log.debug("ipconfig returned no output")
        return []

    interfaces: list[tuple[str, str, str, bytes, bool]] = []
    # Split on adapter header lines (e.g. "Ethernet adapter Local Area Connection:")
    for block in re.split(r"\n(?=\S)", out):
        # Extract adapter name
        header = re.match(r"^(.+adapter|.+interface)\s+(.+):\s*$", block, re.IGNORECASE)
        iface = header.group(2).strip() if header else "?"

        ip_m = re.search(r"IPv4 Address[^:]*:\s*([\d.]+)", block)
        if not ip_m:
            continue
        ip = ip_m.group(1).rstrip("(Preferred)")
        if ip.startswith("127."):
            continue

        # Subnet mask (dotted decimal on Windows) → convert to hex
        mask_m = re.search(r"Subnet Mask[^:]*:\s*([\d.]+)", block)
        if mask_m:
            try:
                mask_int = int(ipaddress.IPv4Address(mask_m.group(1)))
                mask_hex = f"0x{mask_int:08x}"
            except ValueError:
                mask_hex = "0xffffff00"
        else:
            mask_hex = "0xffffff00"

        # MAC address (Windows format: XX-XX-XX-XX-XX-XX)
        mac_m = re.search(r"Physical Address[^:]*:\s*([0-9A-Fa-f]{2}(?:[-:][0-9A-Fa-f]{2}){5})", block)
        if mac_m:
            mac = bytes(int(x, 16) for x in re.split(r"[-:]", mac_m.group(1)))
        else:
            mac = b"\x00" * 6

        # Consider active if has a valid gateway or is media connected
        active = bool(re.search(r"Default Gateway[^:]*:\s*\d", block))
        interfaces.append((iface, ip, mask_hex, mac, active))
    return interfaces


def _get_network_info(target: str = "8.8.8.8") -> tuple[str, bytes, str]:
    """Return (ip_str, mac_bytes, broadcast_ip) for the best LAN interface.

    Pioneer DJ Link devices use link-local addresses (169.254.x.x) for direct
    (non-switch) connections.  We therefore prefer any *active* link-local
    interface over the kernel-routing choice, which usually points at Wi-Fi
    and is on a different subnet from the CDJs.
    """
    interfaces = _parse_ifconfig_interfaces()

    # ── 1. Prefer active link-local interfaces (Pioneer direct-connect subnet) ──
    for _, ip, mask, mac, active in interfaces:
        if active and ip.startswith("169.254."):
            broadcast = _subnet_broadcast(ip, mask)
            log.debug(
                "VirtualCDJ interface: link-local %s broadcast=%s (preferred over routed)", ip, broadcast
            )
            return ip, mac, broadcast

    # ── 2. Fall back to the interface chosen by kernel routing ─────────────────
    selected_ip: str | None = None
    selected_mac: bytes = b"\x00" * 6
    selected_mask = "0xffffff00"
    _routed_ip: str | None = None

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((target, 80))
        _routed_ip = s.getsockname()[0]
        s.close()
        for _, ip, mask, mac, _ in interfaces:
            if ip == _routed_ip:
                selected_ip, selected_mac, selected_mask = ip, mac, mask
                break
    except OSError:
        pass

    if selected_ip is None:
        for _, ip, mask, mac, active in interfaces:
            if active:
                selected_ip, selected_mac, selected_mask = ip, mac, mask
                break

    if selected_ip is None and interfaces:
        _, selected_ip, selected_mask, selected_mac, _ = interfaces[0]

    # ── 3. Platform-native fallback (Windows: ifconfig unavailable) ────────────
    # ifconfig does not exist on Windows, so _parse_ifconfig_interfaces() returns
    # an empty list and the routing-derived IP is never matched above.  Use it
    # directly here so the VirtualCDJ announces from the real LAN IP instead of
    # loopback — without this, CDJs on the network never see our keep-alives.
    if selected_ip is None and _routed_ip and not _routed_ip.startswith("127."):
        selected_ip = _routed_ip
        try:
            import uuid as _uuid
            mac_int = _uuid.getnode()
            selected_mac = mac_int.to_bytes(6, "big")
        except Exception:
            pass

    if selected_ip is None:
        selected_ip = "127.0.0.1"

    broadcast = _subnet_broadcast(selected_ip, selected_mask)
    return selected_ip, selected_mac, broadcast


def _subnet_broadcast(ip: str, netmask_hex: str) -> str:
    try:
        mask_int = int(netmask_hex, 16)
        netmask = str(ipaddress.IPv4Address(mask_int.to_bytes(4, "big")))
        return str(ipaddress.IPv4Network(f"{ip}/{netmask}", strict=False).broadcast_address)
    except ValueError:
        return "255.255.255.255"


# ── Packet builders (beat-link byte layout, CDJ-3000 compat) ─────────────────

def _name_bytes(name: str) -> bytes:
    return name.encode("ascii", errors="replace")[:_NAME_LEN].ljust(_NAME_LEN, b"\x00")


def _hdr(pkt: bytearray, ptype: int) -> None:
    pkt[0:10] = MAGIC
    pkt[10] = ptype
    pkt[11] = 0x00


def _build_hello(nb: bytes) -> bytes:
    """Type 0x0A — initial hello, 38 bytes (CDJ-3000 compat)."""
    pkt = bytearray(38)
    _hdr(pkt, 0x0a)
    pkt[_NAME_OFFSET: _NAME_OFFSET + _NAME_LEN] = nb
    pkt[32] = 0x01
    pkt[33] = 0x04                      # CDJ-3000 compat subtype
    struct.pack_into(">H", pkt, 34, 38)
    pkt[36] = 0x01
    pkt[37] = 0x40                      # CDJ-3000 extra byte
    return bytes(pkt)


def _build_stage1(nb: bytes, mac: bytes, counter: int) -> bytes:
    """Type 0x00 — stage-1 claim, 44 bytes."""
    pkt = bytearray(44)
    _hdr(pkt, 0x00)
    pkt[_NAME_OFFSET: _NAME_OFFSET + _NAME_LEN] = nb
    pkt[32] = 0x01
    pkt[33] = 0x03
    struct.pack_into(">H", pkt, 34, 44)
    pkt[_NUM_OFFSET] = counter          # packet counter (1-3) at 0x24
    pkt[37] = 0x01
    pkt[_MAC_OFFSET: _MAC_OFFSET + 6] = mac
    return bytes(pkt)


def _build_stage2(nb: bytes, ip_bytes: bytes, mac: bytes,
                  device_num: int, counter: int) -> bytes:
    """Type 0x02 — stage-2 claim, 50 bytes."""
    pkt = bytearray(50)
    _hdr(pkt, 0x02)
    pkt[_NAME_OFFSET: _NAME_OFFSET + _NAME_LEN] = nb
    pkt[32] = 0x01
    pkt[33] = 0x03
    struct.pack_into(">H", pkt, 34, 50)
    pkt[36:40] = ip_bytes               # IP at 0x24
    pkt[40:46] = mac                    # MAC at 0x28
    pkt[46] = device_num                # claimed slot at 0x2e
    pkt[47] = counter                   # packet counter at 0x2f
    pkt[48] = 0x01
    pkt[49] = 0x02                      # 0x02 = claiming specific number
    return bytes(pkt)


def _build_stage3(nb: bytes, device_num: int, counter: int) -> bytes:
    """Type 0x04 — final-stage claim, 38 bytes."""
    pkt = bytearray(38)
    _hdr(pkt, 0x04)
    pkt[_NAME_OFFSET: _NAME_OFFSET + _NAME_LEN] = nb
    pkt[32] = 0x01
    pkt[33] = 0x03
    struct.pack_into(">H", pkt, 34, 38)
    pkt[_NUM_OFFSET] = device_num       # device number at 0x24
    pkt[37] = counter                   # packet counter at 0x25
    return bytes(pkt)


def _build_keepalive(nb: bytes, ip_bytes: bytes, mac: bytes, device_num: int) -> bytes:
    """Type 0x06 — keepalive, 54 bytes (CDJ-3000 compat)."""
    pkt = bytearray(54)
    _hdr(pkt, 0x06)
    pkt[_NAME_OFFSET: _NAME_OFFSET + _NAME_LEN] = nb
    pkt[32] = 0x01
    pkt[33] = 0x02
    struct.pack_into(">H", pkt, 34, 54)
    pkt[_NUM_OFFSET] = device_num       # device number at 0x24
    pkt[37] = 0x01
    pkt[_MAC_OFFSET: _MAC_OFFSET + 6] = mac    # MAC at 0x26
    pkt[_IP_OFFSET: _IP_OFFSET + 4]   = ip_bytes  # IP at 0x2c
    pkt[48] = 0x02
    pkt[49] = 0x00
    pkt[50] = 0x00
    pkt[51] = 0x00
    pkt[52] = 0x01
    pkt[53] = 0x64  # CDJ-3000 compat — wrong value knocks player 5/6 offline!
    return bytes(pkt)


class VirtualCDJAnnouncer:
    """
    Async task that runs the full Pro DJ Link device-number claim handshake,
    then sends periodic keepalives so rekordbox emits status/beat packets.
    """

    def __init__(self, player_number: int = 5) -> None:
        self._player_number = player_number

    async def listen(self, stop_event: asyncio.Event) -> None:
        try:
            await self._listen_inner(stop_event)
        except Exception as exc:
            log.error("VirtualCDJ announcer crashed: %s", exc, exc_info=True)

    async def _listen_inner(self, stop_event: asyncio.Event) -> None:
        nb = _name_bytes(_DEVICE_NAME)
        num = self._player_number

        def make_socket(local_ip: str) -> socket.socket:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass
            try:
                sock.bind((local_ip, 0))
                log.debug("VirtualCDJ socket bound to %s", local_ip)
            except OSError as exc:
                log.warning(
                    "VirtualCDJ could not bind to %s: %s — broadcasts may fail",
                    local_ip,
                    exc,
                )
            sock.setblocking(False)
            return sock

        def should_recover(exc: OSError) -> bool:
            return exc.errno in {
                49,   # EADDRNOTAVAIL
                50,   # ENETDOWN
                51,   # ENETUNREACH
                64,   # EHOSTDOWN
                65,   # EHOSTUNREACH
            }

        sock: socket.socket | None = None
        try:
            while not stop_event.is_set():
                local_ip, local_mac, broadcast = await asyncio.get_running_loop().run_in_executor(
                    None, _get_network_info
                )

                if local_ip.startswith("127."):
                    log.warning(
                        "VirtualCDJ: loopback IP detected (%s) — announcements will not reach rekordbox on LAN",
                        local_ip,
                    )

                ip_bytes = ipaddress.IPv4Address(local_ip).packed
                log.info(
                    "VirtualCDJ: claiming player #%d  IP=%s  MAC=%s  broadcast=%s",
                    num,
                    local_ip,
                    ":".join(f"{b:02x}" for b in local_mac),
                    broadcast,
                )

                if sock is not None:
                    sock.close()
                sock = make_socket(local_ip)

                def send(pkt: bytes) -> bool:
                    try:
                        sock.sendto(pkt, (broadcast, PORT_ANNOUNCE))
                    except OSError as exc:
                        log.debug("VirtualCDJ broadcast send error: %s", exc)
                        return not should_recover(exc)
                    try:
                        sock.sendto(pkt, ("127.0.0.1", PORT_ANNOUNCE))
                    except OSError:
                        pass
                    return True

                # Phase 0: Hello (type 0x0A) × 3
                log.debug("VirtualCDJ phase 0: hello")
                restart = False
                for _ in range(3):
                    if stop_event.is_set():
                        return
                    if not send(_build_hello(nb)):
                        restart = True
                        break
                    await asyncio.sleep(_CLAIM_INTERVAL)
                if restart:
                    await asyncio.sleep(1.0)
                    continue

                # Phase 1: Stage-1 claim (type 0x00) × 3
                log.debug("VirtualCDJ phase 1: stage-1 claim")
                for i in (1, 2, 3):
                    if stop_event.is_set():
                        return
                    if not send(_build_stage1(nb, local_mac, i)):
                        restart = True
                        break
                    await asyncio.sleep(_CLAIM_INTERVAL)
                if restart:
                    await asyncio.sleep(1.0)
                    continue

                # Phase 2: Stage-2 claim (type 0x02) × 3
                log.debug("VirtualCDJ phase 2: stage-2 claim")
                for i in (1, 2, 3):
                    if stop_event.is_set():
                        return
                    if not send(_build_stage2(nb, ip_bytes, local_mac, num, i)):
                        restart = True
                        break
                    await asyncio.sleep(_CLAIM_INTERVAL)
                if restart:
                    await asyncio.sleep(1.0)
                    continue

                # Phase 3: Final-stage claim (type 0x04) × 3
                log.debug("VirtualCDJ phase 3: final-stage claim")
                for i in (1, 2, 3):
                    if stop_event.is_set():
                        return
                    if not send(_build_stage3(nb, num, i)):
                        restart = True
                        break
                    await asyncio.sleep(_CLAIM_INTERVAL)
                if restart:
                    await asyncio.sleep(1.0)
                    continue

                keepalive = _build_keepalive(nb, ip_bytes, local_mac, num)
                log.info(
                    "VirtualCDJ: player #%d claimed — keepalives every %.1fs",
                    num,
                    _KEEPALIVE_INTERVAL,
                )
                while not stop_event.is_set():
                    if not send(keepalive):
                        log.info("VirtualCDJ: network path changed, re-claiming player #%d", num)
                        break
                    await asyncio.sleep(_KEEPALIVE_INTERVAL)

                if stop_event.is_set():
                    return

                await asyncio.sleep(1.0)

        finally:
            if sock is not None:
                sock.close()
            log.info("VirtualCDJ announcer stopped")


# ── Legacy helper kept for constants.py / packet_parser.py compatibility ──────

def _broadcast_address(local_ip: str, netmask_hex: str = "0xffffff00") -> str:
    """Compute subnet broadcast from IP + ifconfig hex netmask."""
    return _subnet_broadcast(local_ip, netmask_hex)
