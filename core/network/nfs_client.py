"""
Minimal NFSv2 / RPC client for reading files off Pioneer DJ devices.

Pioneer CDJ/XDJ units expose their USB drives as NFSv2 shares on the
link-local network (port 2049, no authentication required).  This module
implements just enough of the RPC / NFSv2 protocol to:

  1. Connect to the PORTMAP daemon (port 111) to find the MOUNT and NFS ports.
  2. Call the MOUNT protocol to obtain the root file handle for a given path.
  3. Walk a POSIX path via NFSPROC_LOOKUP to get any file's handle + size.
  4. Stream the file in 8 kB chunks using NFSPROC_READ.

All I/O is synchronous (blocking sockets) and intended to run inside a
QThread or asyncio.to_thread so the Qt event loop stays responsive.

Usage
-----
    with NfsClient(device_ip) as nfs:
        data = nfs.read_file("/PIONEER/rekordbox/export.pdb")

Download with progress callback
---------------------------------
    def on_progress(done, total):
        print(f"{done}/{total} bytes")

    with NfsClient(device_ip) as nfs:
        data = nfs.read_file("/Contents/DJ Set/track.aiff",
                              progress_cb=on_progress)
"""
from __future__ import annotations

import logging
import os
import random
import socket
import struct
from typing import Callable, Optional

log = logging.getLogger(__name__)

# ── RPC / XDR constants ───────────────────────────────────────────────────────
_CALL      = 0
_REPLY     = 1
_RPC_VERS  = 2
_AUTH_NONE = 0
_SUCCESS   = 0

# Well-known RPC program numbers
_PORTMAP_PROG = 100_000
_PORTMAP_VERS = 2
_PORTMAP_GETPORT = 3

_MOUNT_PROG = 100_005
_MOUNT_VERS = 1
_MOUNTPROC_MNT = 1

_NFS_PROG  = 100_003
_NFS_VERS  = 2
_NFSPROC_GETATTR = 1
_NFSPROC_LOOKUP  = 4
_NFSPROC_READ    = 6

_NFHSIZE   = 32      # NFSv2 fixed file-handle size
_READ_CHUNK = 2_048  # bytes per READ call (matches crate-digger DEFAULT_READ_SIZE;
                     # smaller chunks reduce IP fragmentation losses).

# NFS status codes
_NFS_OK      = 0
_NFSERR_PERM = 1
_NFSERR_NOENT = 2

# NFS file type
_NFREG = 1  # regular file
_NFDIR = 2  # directory

_CONNECT_TIMEOUT = 5.0
_READ_TIMEOUT    = 10.0
# Pioneer players reply within a few ms; aggressive retransmit recovers from UDP loss.
_RPC_RETRANSMIT  = 0.25
_DEFAULT_NFS_PORT   = 2049
_DEFAULT_MOUNT_PORT = 635


# ── XDR helpers ──────────────────────────────────────────────────────────────

def _pack_uint(n: int) -> bytes:
    return struct.pack(">I", n & 0xFFFFFFFF)


def _pack_opaque(data: bytes) -> bytes:
    n = len(data)
    pad = (4 - n % 4) % 4
    return struct.pack(">I", n) + data + b"\x00" * pad


def _pack_string(s: str) -> bytes:
    return _pack_opaque(s.encode("utf-8", errors="replace"))


def _pack_pioneer_string(s: str) -> bytes:
    """Pioneer NFS servers expect path strings encoded as UTF-16LE.

    See crate-digger FileFetcher.CHARSET = UTF_16LE.
    """
    return _pack_opaque(s.encode("utf-16-le", errors="replace"))


class _XdrReader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def uint(self) -> int:
        v, = struct.unpack_from(">I", self._data, self._pos)
        self._pos += 4
        return v

    def opaque_fixed(self, n: int) -> bytes:
        v = self._data[self._pos:self._pos + n]
        self._pos += (n + 3) & ~3
        return v

    def opaque(self) -> bytes:
        n = self.uint()
        v = self._data[self._pos:self._pos + n]
        pad = (4 - n % 4) % 4
        self._pos += n + pad
        return v

    def string(self) -> str:
        return self.opaque().decode("utf-8", errors="replace")

    def remaining(self) -> bytes:
        return self._data[self._pos:]


# ── RPC over UDP (Pioneer's NFSv2 servers are UDP-only) ─────────────────────

def _make_call(prog: int, vers: int, proc: int, body: bytes) -> tuple[int, bytes]:
    xid = random.randint(1, 0xFFFFFFFF)
    header = struct.pack(">IIIIII", xid, _CALL, _RPC_VERS, prog, vers, proc)
    cred = struct.pack(">II", _AUTH_NONE, 0)
    verf = struct.pack(">II", _AUTH_NONE, 0)
    return xid, header + cred + verf + body


def _udp_call(ip: str, port: int, prog: int, vers: int, proc: int,
              body: bytes, *, retries: int = 8,
              timeout: float = _RPC_RETRANSMIT) -> bytes:
    """Issue one RPC call over UDP, retry on packet loss, return reply body."""
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        xid, msg = _make_call(prog, vers, proc, body)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            sock.sendto(msg, (ip, port))
            # Discard reply datagrams whose XID doesn't match (loss / reorder).
            for _ in range(8):
                data, _src = sock.recvfrom(65535)
                if len(data) < 24:
                    continue
                rxid, mtype = struct.unpack_from(">II", data, 0)
                if rxid != xid:
                    continue
                if mtype != _REPLY:
                    raise IOError(f"Expected RPC REPLY, got {mtype}")
                r = _XdrReader(data[8:])
                reply_stat = r.uint()
                if reply_stat != 0:
                    raise IOError(f"RPC MSG_DENIED: {reply_stat}")
                _vf = r.uint()
                _vb = r.opaque()
                accept_stat = r.uint()
                if accept_stat != _SUCCESS:
                    raise IOError(f"RPC accept_stat={accept_stat}")
                return r.remaining()
            raise TimeoutError("No matching XID in reply burst")
        except (TimeoutError, socket.timeout) as exc:
            last_exc = exc
            log.debug("UDP RPC %s:%d prog=%d proc=%d attempt %d/%d timed out",
                      ip, port, prog, proc, attempt + 1, retries)
            continue
        finally:
            sock.close()
    raise TimeoutError(f"UDP RPC timed out after {retries} attempts: {last_exc}")


# ── PORTMAP ──────────────────────────────────────────────────────────────────

def _portmap_getport(ip: str, prog: int, vers: int) -> int:
    """Query PORTMAP on port 111 (UDP) for the port of (prog, vers)."""
    try:
        body = struct.pack(">IIII", prog, vers, 17, 0)  # proto=17=UDP
        reply = _udp_call(ip, 111, _PORTMAP_PROG, _PORTMAP_VERS,
                          _PORTMAP_GETPORT, body, retries=4, timeout=0.5)
        port, = struct.unpack_from(">I", reply)
        return int(port) if port > 0 else 0
    except Exception as exc:
        log.debug("PORTMAP query failed for prog=%d vers=%d on %s: %s",
                  prog, vers, ip, exc)
        return 0


# ── MOUNT ────────────────────────────────────────────────────────────────────

def _mount_mnt(ip: str, port: int, export_path: str) -> bytes:
    """Call MOUNTPROC_MNT (UDP) and return the 32-byte root file handle."""
    body = _pack_pioneer_string(export_path)
    reply = _udp_call(ip, port, _MOUNT_PROG, _MOUNT_VERS, _MOUNTPROC_MNT, body)
    r = _XdrReader(reply)
    status = r.uint()
    if status != 0:
        raise IOError(f"MOUNT '{export_path}' failed: status={status}")
    return r.opaque_fixed(_NFHSIZE)


# ── NFS v2 ───────────────────────────────────────────────────────────────────

def _nfs_lookup(ip: str, port: int, dir_fh: bytes,
                name: str) -> tuple[bytes, int]:
    """LOOKUP name inside dir_fh → (file_handle, file_size).  size=0 for dirs."""
    body = dir_fh + _pack_pioneer_string(name)
    reply = _udp_call(ip, port, _NFS_PROG, _NFS_VERS, _NFSPROC_LOOKUP, body)
    r = _XdrReader(reply)
    status = r.uint()
    if status == _NFSERR_NOENT:
        raise FileNotFoundError(f"NFS: '{name}' not found")
    if status != _NFS_OK:
        raise IOError(f"NFS LOOKUP error: {status}")
    fh = r.opaque_fixed(_NFHSIZE)
    ftype = r.uint()
    _mode = r.uint()
    _nlink = r.uint()
    _uid = r.uint()
    _gid = r.uint()
    size = r.uint()
    return fh, (size if ftype == _NFREG else 0)


def _nfs_read(ip: str, port: int, fh: bytes, offset: int,
              count: int) -> bytes:
    """NFSPROC_READ — returns the data bytes read (UDP)."""
    body = fh + struct.pack(">III", offset, count, 0)
    reply = _udp_call(ip, port, _NFS_PROG, _NFS_VERS, _NFSPROC_READ, body)
    r = _XdrReader(reply)
    status = r.uint()
    if status != _NFS_OK:
        raise IOError(f"NFS READ error: status={status}")
    r.opaque_fixed(68)  # skip fattr
    return r.opaque()


# ── Public API ────────────────────────────────────────────────────────────────

class NfsClient:
    """
    Context-manager NFSv2 client for a single Pioneer device.

    Parameters
    ----------
    ip : str
        IP address of the Pioneer device.
    export : str
        NFS export path on the device.  Pioneer USB drives typically export
        '/B/' (slot B) or '/' depending on firmware.  Try '/' first.
    """

    def __init__(self, ip: str, export: Optional[str] = None) -> None:
        self._ip     = ip
        # When `export` is None we'll auto-probe Pioneer slot mounts.
        self._export = export
        self._mount_port = 0
        self._nfs_port   = 0
        self._root_fh: bytes | None = None

    # ── Context manager ───────────────────────────────────────────────
    def __enter__(self) -> "NfsClient":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ── Connection ────────────────────────────────────────────────────
    def connect(self) -> None:
        ip = self._ip

        # 1. Discover ports via PORTMAP (UDP); fall back to defaults.
        self._mount_port = _portmap_getport(ip, _MOUNT_PROG, 1) or _DEFAULT_MOUNT_PORT
        self._nfs_port   = _portmap_getport(ip, _NFS_PROG,   2) or _DEFAULT_NFS_PORT
        log.debug("NFS ports for %s: mount=%d nfs=%d", ip, self._mount_port, self._nfs_port)

        # 2. MOUNT — Pioneer exports per-slot roots: /B/=SD, /C/=USB, /A/=internal.
        candidates = ([self._export] if self._export
                      else ["/C/", "/B/", "/A/", "/"])
        last_exc: Optional[Exception] = None
        for export in candidates:
            try:
                fh = _mount_mnt(ip, self._mount_port, export)
                self._root_fh = fh
                self._export = export
                log.debug("Mounted %s:%s → fh=%s", ip, export, fh.hex())
                break
            except Exception as exc:
                last_exc = exc
                log.debug("MOUNT %s:%s failed: %s", ip, export, exc)
                continue
        if self._root_fh is None:
            raise IOError(f"All MOUNT attempts failed for {ip}: {last_exc}")

    def close(self) -> None:
        # UDP transport is connectionless; nothing to tear down.
        self._root_fh = None

    # ── File operations ───────────────────────────────────────────────
    def resolve_path(self, path: str) -> tuple[bytes, int]:
        """
        Walk `path` from the export root and return (file_handle, size_bytes).
        Raises FileNotFoundError if any component is missing.
        """
        if self._root_fh is None:
            raise RuntimeError("NfsClient not connected")
        fh = self._root_fh
        size = 0
        for part in [p for p in path.strip("/").split("/") if p]:
            fh, size = _nfs_lookup(self._ip, self._nfs_port, fh, part)
        return fh, size

    def read_file(
        self,
        path: str,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> bytes:
        """
        Download a file from the NFS export and return its bytes.

        Parameters
        ----------
        path : str
            Absolute path relative to the export root (e.g. '/Contents/track.mp3').
        progress_cb : callable, optional
            Called with (bytes_done, total_bytes) after each chunk.
        """
        fh, size = self.resolve_path(path)
        chunks: list[bytes] = []
        offset = 0
        while True:
            data = _nfs_read(self._ip, self._nfs_port, fh, offset, _READ_CHUNK)
            if not data:
                break
            chunks.append(data)
            offset += len(data)
            if progress_cb:
                progress_cb(offset, size or offset)
            if size and offset >= size:
                break
        return b"".join(chunks)

    def list_dir(self, path: str) -> list[str]:
        """List directory entries at `path` using NFSPROC_READDIR."""
        # Implementation stub — READDIR is proc 16 in NFSv2
        # Not required for file download; included for future use.
        raise NotImplementedError("READDIR not yet implemented")
