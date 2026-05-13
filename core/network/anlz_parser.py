"""
Minimal Pioneer rekordbox ANLZ track-analysis file parser.

ANLZ files (ANLZ0000.DAT and ANLZ0000.EXT) live alongside the audio in
the rekordbox export and contain waveforms, beat grids, cue points, etc.
We parse only the tags needed to display waveforms and beat grids:

    PWAV  monochrome 400-byte preview   (.DAT)
    PWV3  monochrome detail / scroll    (.EXT)
    PWV5  Nxs2 colour detail / scroll   (.EXT)
    PQTZ  beat grid                     (.DAT)

All tag headers are big-endian.  See crate-digger rekordbox_anlz.ksy.
"""
from __future__ import annotations
import logging
import struct
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class AnlzAssets:
    preview: Optional[bytes] = None              # raw bytes for from_preview_bytes
    detail:  Optional[bytes] = None              # raw 1-byte/col mono detail
    color_detail: Optional[bytes] = None         # raw 2-byte/col Nxs2 detail
    beat_grid: Optional[list[tuple[int, int, int]]] = None  # (beat_no, tempo×100, time_ms)


def parse_anlz(data: bytes) -> AnlzAssets:
    """Walk the type-tagged sections of an ANLZ file and pull out what we need."""
    out = AnlzAssets()
    if len(data) < 16 or data[:4] != b"PMAI":
        log.debug("ANLZ: bad magic / too short (%d bytes)", len(data))
        return out
    try:
        len_header = struct.unpack_from(">I", data, 4)[0]
        len_file   = struct.unpack_from(">I", data, 8)[0]
    except struct.error:
        return out
    end = min(len_file, len(data))
    pos = max(len_header, 16)
    while pos + 12 <= end:
        try:
            fourcc  = data[pos:pos + 4]
            len_tag = struct.unpack_from(">I", data, pos + 8)[0]
        except struct.error:
            break
        if len_tag < 12 or pos + len_tag > end:
            log.debug("ANLZ: bad tag len at pos=%d fourcc=%r len_tag=%d", pos, fourcc, len_tag)
            break
        body = data[pos + 12:pos + len_tag]
        try:
            _decode_tag(fourcc, body, out)
        except Exception as exc:
            log.debug("ANLZ tag decode failed (%r): %s", fourcc, exc)
        pos += len_tag
    return out


def _decode_tag(fourcc: bytes, body: bytes, out: AnlzAssets) -> None:
    if fourcc == b"PWAV":
        # len_data u4 BE, u4, then `len_data` bytes of preview.
        if len(body) < 8:
            return
        ldata = struct.unpack_from(">I", body, 0)[0]
        out.preview = bytes(body[8:8 + ldata])
        return

    if fourcc == b"PWV3":
        # len_entry_bytes u4 BE (=1), len_entries u4 BE, u4, then bytes.
        if len(body) < 12:
            return
        le = struct.unpack_from(">I", body, 0)[0]
        n  = struct.unpack_from(">I", body, 4)[0]
        out.detail = bytes(body[12:12 + le * n])
        return

    if fourcc == b"PWV5":
        # len_entry_bytes u4 BE (=2), len_entries u4 BE, u4, then bytes.
        if len(body) < 12:
            return
        le = struct.unpack_from(">I", body, 0)[0]
        n  = struct.unpack_from(">I", body, 4)[0]
        out.color_detail = bytes(body[12:12 + le * n])
        return

    if fourcc == b"PQTZ":
        # u4, u4, num_beats u4 BE, then num_beats × (beat_no u2, tempo u2, time u4) BE.
        if len(body) < 12:
            return
        num_beats = struct.unpack_from(">I", body, 8)[0]
        grid: list[tuple[int, int, int]] = []
        base = 12
        for _ in range(num_beats):
            if base + 8 > len(body):
                break
            bn = struct.unpack_from(">H", body, base)[0]
            tp = struct.unpack_from(">H", body, base + 2)[0]
            ts = struct.unpack_from(">I", body, base + 4)[0]
            grid.append((bn, tp, ts))
            base += 8
        out.beat_grid = grid
