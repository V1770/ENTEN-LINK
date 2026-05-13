"""
Pro DJ Link TCP Metadata Protocol.

Connection sequence
───────────────────
1. Connect to device IP, port 12523.
2. Send DB_SERVER_QUERY (21 bytes); receive 2-byte big-endian port number
   (always 1051 in practice, but we query to be safe).
3. Connect to device IP, discovered port.
4. Send GREETING (5 bytes); receive the same 5 bytes back.
5. Send make_setup_msg(D); receive RESP_SUCCESS.
6. Send requests / render-menu cycles.

Message wire format
───────────────────
Every field is prefixed by a one-byte type tag:
    0x0f  — 1-byte  big-endian integer
    0x10  — 2-byte  big-endian integer
    0x11  — 4-byte  big-endian integer
    0x14  — variable-length blob   (4-byte BE length + data)
    0x26  — UTF-16BE string        (4-byte BE length + data)

A full message is:
    [magic field]  [TxID field]  [type field]  [n_args field]
    [12-byte arg-type-tags blob]  [arg fields …]

where magic = 0x872349ae (encoded as a 4-byte number field).

Reference: https://djl-analysis.deepsymmetry.org/djl-analysis/track_metadata.html
"""
from __future__ import annotations

import struct
from typing import Union

# ── DB Server port discovery ──────────────────────────────────────────────────
DB_SERVER_QUERY = (
    b"\x00\x00\x00\x0f"    # header
    b"RemoteDBServ"          # 12 ASCII bytes
    b"\x00\x10"             # separator
    b"er\x00"               # tail
)
DB_SERVER_DEFAULT_PORT = 1051

# ── Protocol constants ────────────────────────────────────────────────────────
MSG_MAGIC   = 0x872349ae
SETUP_TX_ID = 0xfffffffe   # used for context-setup and disconnect

# One-byte TLV field type tags
_F_NUM1 = 0x0f
_F_NUM2 = 0x10
_F_NUM4 = 0x11
_F_BLOB = 0x14
_F_STR  = 0x26

# Argument type tags (used inside the 12-byte tags blob)
ARG_NUMBER = 0x06   # → 4-byte integer field
ARG_STRING = 0x02   # → UTF-16BE string field
ARG_BLOB   = 0x03   # → blob field (may be absent when preceding size arg = 0)

# ── Message type codes ────────────────────────────────────────────────────────
MSG_SETUP          = 0x0000
MSG_TRACK_METADATA = 0x2002
MSG_ALBUM_ART_REQ  = 0x2003
MSG_RENDER_MENU    = 0x3000
MSG_BEAT_GRID      = 0x2204
MSG_WAVEFORM_PREV  = 0x2004   # monochrome 400-column preview (900 bytes total)
MSG_WAVEFORM_DET   = 0x2904   # monochrome detail (150 col/s, 1 byte/col)
MSG_WAVEFORM_NXS2  = 0x2c04   # Nxs2 analysis-tag request (color waveform, song structure, etc.)

# Nxs2 analysis tag identifiers (ASCII reversed into big-endian 4-byte integers)
# PWV5 = color waveform detail from ANLZ0000.EXT
_TAG_PWV5  = 0x35565750   # 'P','W','V','5' → reversed
_EXT_FILE  = 0x00545845   # 'E','X','T',\0  → reversed + null-padded

# Response type codes
RESP_SUCCESS   = 0x4000
RESP_ALBUM_ART = 0x4002
RESP_MENU_HDR  = 0x4001
RESP_MENU_ITEM = 0x4101
RESP_MENU_FTR  = 0x4201

# ── Greeting ──────────────────────────────────────────────────────────────────
# Sent to (and echoed by) the DB server right after connecting.
GREETING = b"\x11\x00\x00\x00\x01"   # 4-byte number field, value 1


# ── Low-level field encoders ──────────────────────────────────────────────────

def _f4(value: int) -> bytes:
    return struct.pack(">BI", _F_NUM4, value)   # 5 bytes

def _f2(value: int) -> bytes:
    return struct.pack(">BH", _F_NUM2, value)   # 3 bytes

def _f1(value: int) -> bytes:
    return struct.pack(">BB", _F_NUM1, value)   # 2 bytes

def _fblob(data: bytes) -> bytes:
    return struct.pack(">BI", _F_BLOB, len(data)) + data

def _tags_field(arg_types: list[int]) -> bytes:
    """Encode the 12-byte argument-type-tag blob field."""
    padded = (list(arg_types) + [0] * 12)[:12]
    return _fblob(bytes(padded))


# ── Message encoder ───────────────────────────────────────────────────────────

def _encode_msg(tx_id: int, msg_type: int, arg_types: list[int], *arg_fields: bytes) -> bytes:
    """
    Assemble a complete DB-server message.

    arg_types  — determines the n_args value and the tags blob.
                 For waveform preview, declare 5 even though only 4 arg_fields
                 are passed (the trailing blob is intentionally absent when its
                 preceding size field is 0).
    arg_fields — already-encoded TLV field bytes for each present argument.
    """
    return (
        _f4(MSG_MAGIC)
        + _f4(tx_id)
        + _f2(msg_type)
        + _f1(len(arg_types))
        + _tags_field(arg_types)
        + b"".join(arg_fields)
    )


def _dmst(device: int, menu: int, slot: int, track_type: int = 1) -> bytes:
    """
    Encode the DMST combined first argument as a 4-byte number field.
    D = requesting device (1-4), M = menu location, S = slot, T = track type.
    """
    return _f4((device << 24) | (menu << 16) | (slot << 8) | track_type)


# ── Request builders ──────────────────────────────────────────────────────────

def make_setup_msg(player_num: int) -> bytes:
    """
    Context-setup message — sent once after the greeting exchange.
    player_num must be 1-4, present on the network, and != the target CDJ.
    """
    return _encode_msg(
        SETUP_TX_ID, MSG_SETUP,
        [ARG_NUMBER],
        _f4(player_num),
    )


def make_metadata_request(
    tx_id: int,
    device: int,
    slot: int,
    rekordbox_id: int,
    track_type: int = 1,
    menu: int = 1,
) -> bytes:
    """Request rekordbox track metadata (sets up the menu for rendering)."""
    return _encode_msg(
        tx_id, MSG_TRACK_METADATA,
        [ARG_NUMBER, ARG_NUMBER],
        _dmst(device, menu, slot, track_type),
        _f4(rekordbox_id),
    )


def make_render_menu(
    tx_id: int,
    device: int,
    slot: int,
    n_items: int,
    track_type: int = 1,
    menu: int = 1,
) -> bytes:
    """Render-menu — triggers the CDJ to stream all menu items."""
    return _encode_msg(
        tx_id, MSG_RENDER_MENU,
        [ARG_NUMBER, ARG_NUMBER, ARG_NUMBER, ARG_NUMBER, ARG_NUMBER, ARG_NUMBER],
        _dmst(device, menu, slot, track_type),
        _f4(0),         # offset
        _f4(n_items),   # limit
        _f4(0),         # unknown
        _f4(n_items),   # total (= limit works)
        _f4(0),         # unknown
    )


def make_waveform_preview_request(
    tx_id: int,
    device: int,
    slot: int,
    rekordbox_id: int,
    track_type: int = 1,
) -> bytes:
    """
    Request the monochrome waveform preview (900-byte blob).
    Declares 5 arg types but encodes only 4: the 5th (blob) is intentionally
    absent because its preceding size field is 0.
    """
    return _encode_msg(
        tx_id, MSG_WAVEFORM_PREV,
        [ARG_NUMBER, ARG_NUMBER, ARG_NUMBER, ARG_NUMBER, ARG_BLOB],  # 5 declared
        _dmst(device, 8, slot, track_type),   # M=8 for graphical data
        _f4(4),                   # unknown second arg (3 or 4 seen in the wild)
        _f4(rekordbox_id),
        _f4(0),                   # blob size = 0 → trailing blob absent from body
        # 5th argument (blob) intentionally omitted per protocol quirk
    )


def make_album_art_request(
    tx_id: int,
    device: int,
    slot: int,
    artwork_id: int,
    track_type: int = 1,
    high_res: bool = False,
) -> bytes:
    """Request track artwork image bytes for a known artwork ID."""
    arg_types = [ARG_NUMBER, ARG_NUMBER]
    arg_fields: list[bytes] = [
        _dmst(device, 8, slot, track_type),
        _f4(artwork_id),
    ]
    if high_res:
        arg_types.append(ARG_NUMBER)
        arg_fields.append(_f4(1))
    return _encode_msg(tx_id, MSG_ALBUM_ART_REQ, arg_types, *arg_fields)


def make_beat_grid_request(
    tx_id: int,
    device: int,
    slot: int,
    rekordbox_id: int,
    track_type: int = 1,
) -> bytes:
    """Request the beat-grid blob for a track."""
    return _encode_msg(
        tx_id, MSG_BEAT_GRID,
        [ARG_NUMBER, ARG_NUMBER],
        _dmst(device, 8, slot, track_type),
        _f4(rekordbox_id),
    )


def make_waveform_detail_request(
    tx_id: int,
    device: int,
    slot: int,
    rekordbox_id: int,
    track_type: int = 1,
) -> bytes:
    """Request the monochrome waveform detail (150 segments/second, 1 byte/col)."""
    return _encode_msg(
        tx_id, MSG_WAVEFORM_DET,
        [ARG_NUMBER, ARG_NUMBER, ARG_NUMBER],
        _dmst(device, 1, slot, track_type),   # M=1 per spec (unusual for graphical)
        _f4(rekordbox_id),
        _f4(0),
    )


def make_nxs2_waveform_detail_request(
    tx_id: int,
    device: int,
    slot: int,
    rekordbox_id: int,
    track_type: int = 1,
) -> bytes:
    """
    Request the Nxs2 color waveform detail (PWV5 tag from ANLZ0000.EXT).
    Uses general analysis-tag request (0x2c04); response type is 0x4f02.
    Blob begins at byte 34 of the tag data; 2 bytes per segment.
    """
    return _encode_msg(
        tx_id, MSG_WAVEFORM_NXS2,
        [ARG_NUMBER, ARG_NUMBER, ARG_NUMBER, ARG_NUMBER],
        _dmst(device, 1, slot, track_type),   # M=1 per djl-analysis spec
        _f4(rekordbox_id),
        _f4(_TAG_PWV5),
        _f4(_EXT_FILE),
    )


# ── Message reader ────────────────────────────────────────────────────────────

async def _read_field(reader) -> tuple[int, Union[int, str, bytes]]:
    """Read one TLV field from the stream; return (type_tag, value)."""
    tag = (await reader.readexactly(1))[0]
    if tag == _F_NUM4:
        return tag, struct.unpack_from(">I", await reader.readexactly(4))[0]
    elif tag == _F_NUM2:
        return tag, struct.unpack_from(">H", await reader.readexactly(2))[0]
    elif tag == _F_NUM1:
        return tag, (await reader.readexactly(1))[0]
    elif tag in (_F_BLOB, _F_STR):
        length = struct.unpack_from(">I", await reader.readexactly(4))[0]
        payload_len = length * 2 if tag == _F_STR else length
        raw = await reader.readexactly(payload_len) if payload_len > 0 else b""
        if tag == _F_STR:
            # String-field lengths are counted in UTF-16 code units, not bytes.
            return tag, raw.decode("utf-16-be", errors="replace").rstrip("\x00")
        return tag, raw
    else:
        raise ValueError(f"Unknown TLV field tag 0x{tag:02X}")


async def read_message(reader) -> tuple[int, int, list]:
    """
    Read one complete DB-server message from the stream.

    Returns (tx_id, msg_type, args) where args is a list of decoded values:
    int for numbers, str for strings, bytes for blobs.

    Handles the protocol quirk where a blob argument is absent from the stream
    when the preceding numeric argument (its declared size) is 0.
    """
    # Magic
    _, magic = await _read_field(reader)
    if magic != MSG_MAGIC:
        raise ValueError(f"Bad magic: 0x{magic:08X} (expected 0x{MSG_MAGIC:08X})")

    # TxID
    _, tx_id = await _read_field(reader)

    # Message type (must be a 2-byte field)
    tag, msg_type = await _read_field(reader)
    if tag != _F_NUM2:
        raise ValueError(f"Expected 2-byte type field, got tag 0x{tag:02X}")

    # Arg count (1-byte field)
    _, n_args = await _read_field(reader)

    # Tags blob (always 12 bytes)
    _, tags_raw = await _read_field(reader)
    tags = list(tags_raw) if isinstance(tags_raw, (bytes, bytearray)) else []

    args: list = []
    for i in range(n_args):
        tag_type = tags[i] if i < len(tags) else ARG_NUMBER
        if tag_type == ARG_BLOB:
            # The preceding numeric arg holds the declared blob size.
            # If 0, the blob field is absent from the stream.
            prev = args[-1] if args else 0
            if isinstance(prev, int) and prev == 0:
                args.append(b"")
                break   # remaining args also absent per protocol quirk
            _, blob_val = await _read_field(reader)
            args.append(blob_val)
        else:
            _, value = await _read_field(reader)
            args.append(value)

    return tx_id, msg_type, args
