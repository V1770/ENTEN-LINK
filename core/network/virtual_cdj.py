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

# ── Module-level cache of known 169.254.x.x peer IPs ─────────────────────────
# Populated by discovery when Pioneer devices are found on the network.
# Used by _get_network_info() to do a routing trick to a KNOWN neighbor
# (more reliable than 169.254.0.1 which has no ARP entry on this machine).
_seen_link_local_peers: set[str] = set()


def add_link_local_peer(ip: str) -> None:
    """Register a peer 169.254.x.x IP seen on the network (called by discovery)."""
    if ip.startswith("169.254."):
        _seen_link_local_peers.add(ip)


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


def _win_startupinfo():
    """Return a STARTUPINFO that hides the console without breaking stdout."""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return si


def _parse_ipconfig_interfaces() -> list[tuple[str, str, str, bytes, bool]]:
    """Windows equivalent of _parse_ifconfig_interfaces using 'ipconfig /all'."""
    try:
        out = subprocess.check_output(
            ["ipconfig", "/all"],
            # Use errors="replace" to survive any code-page quirks in ipconfig output.
            text=True, encoding="utf-8", errors="replace",
            timeout=4, stderr=subprocess.DEVNULL, startupinfo=_win_startupinfo(),
        )
    except Exception as exc:
        log.warning("Could not run 'ipconfig /all': %s", exc)
        return []

    if not out or not out.strip():
        log.warning("'ipconfig /all' returned no output")
        return []

    interfaces: list[tuple[str, str, str, bytes, bool]] = []
    # Split on adapter header lines.  ipconfig blocks start at column 0;
    # property lines are indented with spaces.  Strip \r so CRLF is not an issue.
    out = out.replace("\r", "")
    for block in re.split(r"\n(?=\S)", out):
        header = re.match(r"^(.+adapter|.+interface)\s+(.+):\s*$", block, re.IGNORECASE)
        iface = header.group(2).strip() if header else "?"

        # Match English "IPv4 Address" AND German "IPv4-Adresse" / "Autoconfiguration-IPv4-Adresse"
        ip_m = re.search(
            r"(?:Autoconfiguration[- ])?IPv4[- ]Addr(?:ess|esse)[^:]*:\s*([\d.]+)",
            block, re.IGNORECASE,
        )
        if not ip_m:
            continue
        # Strip "(Preferred)" (EN) or "(Bevorzugt)" (DE) suffix
        ip = ip_m.group(1)
        for _suf in ("(Preferred)", "(Bevorzugt)"):
            ip = ip.removesuffix(_suf)
        ip = ip.strip()
        if ip.startswith("127."):
            continue

        # "Subnet Mask" (EN) / "Subnetzmaske" (DE)
        mask_m = re.search(r"(?:Subnet Mask|Subnetzmaske)[^:]*:\s*([\d.]+)", block, re.IGNORECASE)
        if mask_m:
            try:
                mask_int = int(ipaddress.IPv4Address(mask_m.group(1)))
                mask_hex = f"0x{mask_int:08x}"
            except ValueError:
                mask_hex = "0xffffff00"
        else:
            mask_hex = "0xffff0000" if ip.startswith("169.254.") else "0xffffff00"

        # "Physical Address" (EN) / "Physische Adresse" (DE)
        mac_m = re.search(
            r"(?:Physical Address|Physische Adresse)[^:]*:\s*([0-9A-Fa-f]{2}(?:[-:][0-9A-Fa-f]{2}){5})",
            block, re.IGNORECASE,
        )
        if mac_m:
            mac = bytes(int(x, 16) for x in re.split(r"[-:]", mac_m.group(1)))
        else:
            mac = b"\x00" * 6

        # "Default Gateway" (EN) / "Standardgateway" (DE)
        active = bool(re.search(r"(?:Default Gateway|Standardgateway)[^:]*:\s*\d", block, re.IGNORECASE))
        interfaces.append((iface, ip, mask_hex, mac, active))
    return interfaces


def _powershell_link_local_ip() -> str | None:
    """Ask PowerShell for the machine's 169.254.x.x IP in Preferred state.

    Filters out Tentative/Duplicate addresses that WinSock refuses to bind.
    Returns a bare IP string or None.
    """
    try:
        # Only return addresses in 'Preferred' state — Tentative/Duplicate
        # cause WinSock to reject bind() with WSAEADDRNOTAVAIL (10049).
        cmd = (
            "$a = Get-NetIPAddress -AddressFamily IPv4 "
            "| Where-Object { $_.IPAddress -like '169.254.*' }; "
            "$a | ForEach-Object { Write-Host (\"STATE:\"+$_.IPAddress+\":\"+$_.AddressState) }; "
            "$a | Where-Object { $_.AddressState -eq 'Preferred' } "
            "| Select-Object -First 1 -ExpandProperty IPAddress"
        )
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            text=True, timeout=6, stderr=subprocess.DEVNULL,
            startupinfo=_win_startupinfo(),
        )
        for line in out.splitlines():
            if line.startswith("STATE:"):
                parts = line.split(":")
                if len(parts) >= 3:
                    log.info("VirtualCDJ: address %s state=%s", parts[1], parts[2])
        # Last non-STATE line is the Preferred IP (if any)
        ip_lines = [l.strip() for l in out.splitlines()
                    if l.strip() and not l.startswith("STATE:")]
        if ip_lines:
            ip = ip_lines[-1]
            if ip.startswith("169.254."):
                return ip
    except Exception as exc:
        log.warning("PowerShell Get-NetIPAddress failed: %s", exc)
    return None


def _powershell_link_local_if_index(ip: str) -> int:
    """Return the Windows interface index for the given IP address, or 0 on failure.

    Used to set IP_UNICAST_IF so outgoing broadcast packets are forced through
    the correct NIC even when bound to INADDR_ANY.
    """
    try:
        cmd = (
            f"Get-NetIPAddress -AddressFamily IPv4 -IPAddress '{ip}' "
            "| Select-Object -First 1 -ExpandProperty InterfaceIndex"
        )
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            text=True, timeout=6, stderr=subprocess.DEVNULL,
            startupinfo=_win_startupinfo(),
        )
        return int(out.strip())
    except Exception as exc:
        log.debug("PowerShell InterfaceIndex lookup failed for %s: %s", ip, exc)
        return 0


def _get_network_info(target: str = "8.8.8.8") -> tuple[str, bytes, str]:
    """Return (ip_str, mac_bytes, broadcast_ip) for the best LAN interface.

    Pioneer DJ Link devices use link-local addresses (169.254.x.x) for direct
    (non-switch) connections.  We prefer link-local regardless of whether the
    adapter has a default gateway (it never does on Windows for link-local),
    then fall back to a non-VPN LAN address, then anything non-loopback.
    """
    def _is_vpn(ip: str) -> bool:
        # Tailscale uses 100.64.0.0/10 CGNAT range; also skip loopback/APIPA
        # that somehow has a gateway.
        return ip.startswith("100.") or ip.startswith("127.")

    interfaces = _parse_ifconfig_interfaces()
    log.info(
        "VirtualCDJ: ipconfig parsed %d interface(s): %s",
        len(interfaces),
        [ip for _, ip, _, _, _ in interfaces],
    )

    def _mac_for_ip(look_ip: str) -> tuple[bytes, str, str]:
        """Return (mac, mask_hex, broadcast) for a known IP, or uuid fallback."""
        for _, iip, imask, imac, _ in interfaces:
            if iip == look_ip:
                return imac, imask, _subnet_broadcast(look_ip, imask)
        import uuid as _uuid
        fallback_mac = _uuid.getnode().to_bytes(6, "big")
        return fallback_mac, "0xffff0000", "169.254.255.255"

    # ── 0. Routing-trick to KNOWN link-local peers (most reliable on Windows) ──
    # Ask the OS which local IP it would use to reach each known CDJ IP.
    # Windows has ARP entries for hosts it has already communicated with,
    # so connect() to a known peer returns the correct source interface IP
    # even when there is no 169.254.0.0/16 on-link route yet.
    for _peer in list(_seen_link_local_peers):
        try:
            _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            _s.connect((_peer, 50000))
            _ll_ip = _s.getsockname()[0]
            _s.close()
            if _ll_ip.startswith("169.254."):
                _mac, _mask, _bc = _mac_for_ip(_ll_ip)
                log.info("VirtualCDJ: routing trick to peer %s → %s broadcast=%s", _peer, _ll_ip, _bc)
                return _ll_ip, _mac, _bc
        except OSError as _e:
            log.debug("VirtualCDJ: routing trick to peer %s failed: %s", _peer, _e)

    # ── 0.1 Routing-trick to generic link-local space ─────────────────────────
    try:
        _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _s.connect(("169.254.0.1", 50000))
        _ll_ip = _s.getsockname()[0]
        _s.close()
        if _ll_ip.startswith("169.254."):
            _mac, _mask, _bc = _mac_for_ip(_ll_ip)
            log.info("VirtualCDJ: link-local routing trick → %s broadcast=%s", _ll_ip, _bc)
            return _ll_ip, _mac, _bc
        log.warning("VirtualCDJ: routing trick to 169.254.0.1 returned %s (not link-local)", _ll_ip)
    except OSError as exc:
        log.warning("VirtualCDJ: routing trick to 169.254.0.1 failed: %s", exc)

    # ── 0.5 PowerShell Get-NetIPAddress — immune to ipconfig encoding issues ──
    import sys as _sys
    if _sys.platform == "win32":
        _ps_ip = _powershell_link_local_ip()
        if _ps_ip:
            _mac, _mask, _bc = _mac_for_ip(_ps_ip)
            log.info("VirtualCDJ: PowerShell found link-local %s broadcast=%s", _ps_ip, _bc)
            return _ps_ip, _mac, _bc

        # ── 0.6 PowerShell fallback — accept Tentative/Duplicate addresses ──────
        # bind() will fail for a Tentative address, but INADDR_ANY + IP_UNICAST_IF
        # is immune to DAD state and routes packets through the correct NIC.
        # The keepalive loop's rebind_check will upgrade to a direct bind once the
        # address transitions to Preferred (usually within a few seconds).
        try:
            _cmd_any = (
                "Get-NetIPAddress -AddressFamily IPv4 "
                "| Where-Object { $_.IPAddress -like '169.254.*' } "
                "| Select-Object -First 1 -ExpandProperty IPAddress"
            )
            _out_any = subprocess.check_output(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", _cmd_any],
                text=True, timeout=6, stderr=subprocess.DEVNULL,
                startupinfo=_win_startupinfo(),
            )
            _any_ip = _out_any.strip()
            if _any_ip.startswith("169.254."):
                _mac, _mask, _bc = _mac_for_ip(_any_ip)
                log.info(
                    "VirtualCDJ: PowerShell found 169.254.x.x %s (Tentative) — "
                    "will use INADDR_ANY + IP_UNICAST_IF  broadcast=%s",
                    _any_ip, _bc,
                )
                return _any_ip, _mac, _bc
        except Exception as _exc:
            log.debug("PowerShell any-state link-local lookup failed: %s", _exc)

    # ── 1. Prefer link-local (169.254.x.x) — Pioneer direct-connect subnet ──
    # On Windows, link-local adapters have no default gateway so active=False.
    # We want them regardless.
    for _, ip, mask, mac, _ in interfaces:
        if ip.startswith("169.254."):
            broadcast = _subnet_broadcast(ip, mask)
            log.debug(
                "VirtualCDJ interface: link-local %s broadcast=%s", ip, broadcast
            )
            return ip, mac, broadcast

    # ── 2. Routing-trick: follow kernel's choice, skip VPN/Tailscale ranges ──
    selected_ip: str | None = None
    selected_mac: bytes = b"\x00" * 6
    selected_mask = "0xffffff00"
    _routed_ip: str | None = None

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((target, 80))
        _routed_ip = s.getsockname()[0]
        s.close()
        if _routed_ip and not _is_vpn(_routed_ip):
            for _, ip, mask, mac, _ in interfaces:
                if ip == _routed_ip:
                    selected_ip, selected_mac, selected_mask = ip, mac, mask
                    break
    except OSError:
        pass

    # ── 3. Any active non-VPN interface ──────────────────────────────────────
    if selected_ip is None:
        for _, ip, mask, mac, active in interfaces:
            if active and not _is_vpn(ip):
                selected_ip, selected_mac, selected_mask = ip, mac, mask
                break

    # ── 4. Any non-VPN interface ──────────────────────────────────────────────
    if selected_ip is None:
        for _, ip, mask, mac, _ in interfaces:
            if not _is_vpn(ip):
                selected_ip, selected_mac, selected_mask = ip, mac, mask
                break

    # ── 5. Windows fallback: ipconfig gave us nothing useful, use routing IP ──
    # Only use _routed_ip if it's not a VPN address (avoids Tailscale fallback).
    if selected_ip is None and _routed_ip and not _is_vpn(_routed_ip):
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

        # IP_UNICAST_IF (Windows Vista+): forces outgoing packets through a
        # specific NIC even when the socket is bound to INADDR_ANY.  This is
        # critical when Windows routing would otherwise pick the Wi-Fi adapter
        # (192.168.x.x) instead of the Ethernet NIC (169.254.x.x) connected
        # to the CDJs.  Value = 31 (not in Python's socket module constants).
        _IP_UNICAST_IF = 31

        def make_socket(local_ip: str, if_index: int = 0) -> socket.socket:
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
                # WinError 10049 (WSAEADDRNOTAVAIL): Windows won't let us bind
                # to the link-local IP (DAD still running, or NIC in unusual
                # state).  Fall back to INADDR_ANY and use IP_UNICAST_IF to
                # force outgoing broadcasts through the correct NIC so the
                # source IP in the packet matches what we claim to be.
                log.warning(
                    "VirtualCDJ could not bind to %s: %s — falling back to INADDR_ANY",
                    local_ip, exc,
                )
                try:
                    sock.bind(("", 0))
                    if local_ip.startswith("169.254.") and if_index:
                        import sys as _sys
                        if _sys.platform == "win32":
                            try:
                                sock.setsockopt(
                                    socket.IPPROTO_IP, _IP_UNICAST_IF,
                                    struct.pack("=I", if_index),
                                )
                                log.info(
                                    "VirtualCDJ: IP_UNICAST_IF set to if_index=%d → "
                                    "outgoing broadcasts will use NIC %s",
                                    if_index, local_ip,
                                )
                            except OSError as exc3:
                                log.warning("VirtualCDJ: IP_UNICAST_IF failed: %s", exc3)
                except OSError as exc2:
                    log.warning("VirtualCDJ INADDR_ANY bind also failed: %s", exc2)
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

                # If the chosen IP is link-local (169.254.x.x), pre-fetch the
                # Windows interface index (needed for IP_UNICAST_IF fallback)
                # and retry the bind probe for up to ~60 s.  Windows APIPA NIC
                # sometimes takes 30-60 s to add its interface route even after
                # DAD completes and PowerShell can see the address.
                _ll_if_index = 0
                _bind_ok = False
                if not local_ip.startswith("169.254."):
                    # Non-link-local: we got a bad IP (e.g. Wi-Fi or loopback).
                    # Retry _get_network_info() at 1-second intervals for up to 15 s.
                    # On macOS the APIPA address appears within ~3 s; on Windows
                    # step 0.6 should have returned a Tentative IP already so this
                    # loop is only a safety net.
                    for _attempt in range(15):
                        await asyncio.sleep(1.0)
                        _fresh_ip, _fresh_mac, _fresh_bc = await asyncio.get_running_loop().run_in_executor(
                            None, _get_network_info
                        )
                        if _fresh_ip.startswith("169.254."):
                            local_ip, local_mac, broadcast = _fresh_ip, _fresh_mac, _fresh_bc
                            log.info(
                                "VirtualCDJ: link-local IP resolved on attempt %d/15: %s broadcast=%s",
                                _attempt + 1, local_ip, broadcast,
                            )
                            _bind_ok = True
                            break
                        log.debug(
                            "VirtualCDJ: still no link-local IP (attempt %d/15, got %s)",
                            _attempt + 1, _fresh_ip,
                        )
                    else:
                        log.warning(
                            "VirtualCDJ: no link-local IP after 15s — proceeding with %s; "
                            "CDJs may not respond with CDJ_STATUS", local_ip,
                        )
                else:
                    import sys as _sys
                    if _sys.platform == "win32":
                        _ll_if_index = await asyncio.get_running_loop().run_in_executor(
                            None, _powershell_link_local_if_index, local_ip
                        )
                        log.info(
                            "VirtualCDJ: interface index for %s = %d",
                            local_ip, _ll_if_index,
                        )
                    # Probe bind() to confirm address is in Preferred state.
                    # If it fails the address is Tentative/Duplicate; re-call
                    # _get_network_info() which will return Preferred IP once ready.
                    _probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    try:
                        _probe.bind((local_ip, 0))
                        _probe.close()
                        _bind_ok = True
                        log.info("VirtualCDJ: %s is bindable (Preferred state)", local_ip)
                    except OSError:
                        _probe.close()
                        log.info(
                            "VirtualCDJ: %s not yet bindable (Tentative?) — "
                            "proceeding immediately with INADDR_ANY + IP_UNICAST_IF; "
                            "will upgrade to direct bind once address is Preferred",
                            local_ip,
                        )
                        # _bind_ok stays False → make_socket() uses INADDR_ANY + IP_UNICAST_IF.
                        # The rebind_check in the keepalive loop probes every ~6 s and
                        # restarts the claim with a correctly-bound socket once Preferred.

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
                sock = make_socket(local_ip, if_index=_ll_if_index)

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
                _rebind_check_counter = 0
                while not stop_event.is_set():
                    if not send(keepalive):
                        log.info("VirtualCDJ: network path changed, re-claiming player #%d", num)
                        break
                    await asyncio.sleep(_KEEPALIVE_INTERVAL)
                    _rebind_check_counter += 1

                    # ── Case A: on INADDR_ANY for a link-local IP (Windows Tentative)
                    # Probe every ~6 s; once bindable, restart the claim with the
                    # correctly-bound socket so CDJs see the right source IP.
                    if not _bind_ok and local_ip.startswith("169.254."):
                        if _rebind_check_counter % 4 == 0:  # every ~6 s
                            _rp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                            try:
                                _rp.bind((local_ip, 0))
                                _rp.close()
                                log.info(
                                    "VirtualCDJ: %s is now bindable — restarting claim with correct socket",
                                    local_ip,
                                )
                                _bind_ok = True
                                break  # restart outer loop → re-claim via correct interface
                            except OSError:
                                _rp.close()

                    # ── Case B: stuck on a non-link-local IP (Wi-Fi fallback)
                    # This happens on Mac/Windows when the retry loop timed out
                    # before APIPA was assigned (e.g. Ethernet just plugged in).
                    # Check every ~12 s whether a 169.254.x.x address has appeared.
                    # If so, break out and re-claim on the correct interface.
                    elif not local_ip.startswith("169.254."):
                        if _rebind_check_counter % 8 == 0:  # every ~12 s
                            _chk_ip, _chk_mac, _chk_bc = await asyncio.get_running_loop().run_in_executor(
                                None, _get_network_info
                            )
                            if _chk_ip.startswith("169.254."):
                                log.info(
                                    "VirtualCDJ: link-local address %s appeared — "
                                    "restarting claim on correct interface",
                                    _chk_ip,
                                )
                                break  # restart outer loop

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
