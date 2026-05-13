"""
Minimal Pioneer rekordbox export.pdb parser.

Parses the page-based DeviceSQL database that rekordbox writes to USB/SD
media (file: /PIONEER/rekordbox/export.pdb).  Only the row types we need
to resolve a track ID into displayable metadata + analyze-file paths
are decoded:

    Tracks, Artists, Albums, Genres, Keys, Colors, Artwork.

Format reference: Deep-Symmetry/crate-digger Kaitai Struct definition
(rekordbox_pdb.ksy).  All multi-byte integers are little-endian.

Usage
─────
    db = parse_pdb(open("export.pdb", "rb").read())
    track = db.tracks.get(rekordbox_id)
    artist_name = db.artists.get(track.artist_id, "")
"""
from __future__ import annotations
import logging
import struct
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# Page types we care about.
PT_TRACKS  = 0
PT_GENRES  = 1
PT_ARTISTS = 2
PT_ALBUMS  = 3
PT_KEYS    = 5
PT_COLORS  = 6
PT_ARTWORK = 13

_INTERESTING = {PT_TRACKS, PT_ARTISTS, PT_ALBUMS, PT_GENRES,
                PT_KEYS, PT_COLORS, PT_ARTWORK}


@dataclass
class TrackRow:
    id: int = 0
    title: str = ""
    artist_id: int = 0
    album_id: int = 0
    genre_id: int = 0
    key_id: int = 0
    color_id: int = 0
    artwork_id: int = 0
    tempo: int = 0           # BPM × 100
    duration: int = 0        # seconds
    bitrate: int = 0
    sample_rate: int = 0
    sample_depth: int = 0
    rating: int = 0
    play_count: int = 0
    year: int = 0
    track_number: int = 0
    disc_number: int = 0
    file_size: int = 0
    comment: str = ""
    date_added: str = ""
    analyze_path: str = ""
    file_path: str = ""
    filename: str = ""


@dataclass
class PdbDatabase:
    tracks:  dict[int, TrackRow] = field(default_factory=dict)
    artists: dict[int, str] = field(default_factory=dict)
    albums:  dict[int, str] = field(default_factory=dict)
    genres:  dict[int, str] = field(default_factory=dict)
    keys:    dict[int, str] = field(default_factory=dict)
    colors:  dict[int, str] = field(default_factory=dict)
    artwork: dict[int, str] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────
# device_sql_string decoder
# ─────────────────────────────────────────────────────────────────────────
def _read_sql_string(buf: bytes, off: int) -> str:
    """Decode a device_sql_string at offset `off`.  Returns "" on error."""
    if off < 0 or off >= len(buf):
        return ""
    kind = buf[off]
    try:
        if kind == 0x40:  # long ASCII: kind(1) + length(2 LE) + pad(1) + text
            if off + 4 > len(buf):
                return ""
            length = struct.unpack_from("<H", buf, off + 1)[0]
            end = off + length
            if length < 4 or end > len(buf):
                return ""
            return buf[off + 4:end].decode("ascii", errors="replace").rstrip("\x00")
        if kind == 0x90:  # long UTF-16LE: same header, text is UTF-16LE
            if off + 4 > len(buf):
                return ""
            length = struct.unpack_from("<H", buf, off + 1)[0]
            end = off + length
            if length < 4 or end > len(buf):
                return ""
            text = buf[off + 4:end]
            # Spec says trailing 4 bytes (== two UTF-16 NULs) must be ignored.
            if len(text) >= 2:
                text = text[:-2]
            return text.decode("utf-16-le", errors="replace").rstrip("\x00")
        # short ASCII: kind byte encodes (length<<1) | 1; total entry = length bytes,
        # text size = length - 1 starting at offset 1.
        total = kind >> 1
        if total < 1 or off + total > len(buf):
            return ""
        return buf[off + 1:off + total].decode("ascii", errors="replace").rstrip("\x00")
    except Exception as exc:
        log.debug("device_sql_string decode failed at off=%d kind=0x%02X: %s",
                  off, kind, exc)
        return ""


# ─────────────────────────────────────────────────────────────────────────
# Row decoders.  All offsets are relative to row_base (start of the row
# inside the page heap).  Field layouts come from the crate-digger ksy.
# ─────────────────────────────────────────────────────────────────────────
def _parse_track_row(page: bytes, row_base: int) -> Optional[TrackRow]:
    # Field offsets per crate-digger rekordbox_pdb.ksy / track_row:
    #   0   subtype u2          | 2   index_shift u2     | 4   bitmask u4
    #   8   sample_rate u4      | 12  composer_id u4     | 16  file_size u4
    #   20  unknown u4          | 24  u2 (=19048)        | 26  u2 (=30967)
    #   28  artwork_id u4       | 32  key_id u4          | 36  original_artist_id u4
    #   40  label_id u4         | 44  remixer_id u4      | 48  bitrate u4
    #   52  track_number u4     | 56  tempo u4 (BPM×100) | 60  genre_id u4
    #   64  album_id u4         | 68  artist_id u4       | 72  id u4
    #   76  disc_number u2      | 78  play_count u2      | 80  year u2
    #   82  sample_depth u2     | 84  duration u2        | 86  u2 (=41)
    #   88  color_id u1         | 89  rating u1          | 90  u2  | 92  u2
    #   94  ofs_strings: u2 × 21    -- end at 94 + 42 = 136
    if row_base + 136 > len(page) or row_base < 0:
        return None
    try:
        sample_rate = struct.unpack_from("<I", page, row_base + 8)[0]
        file_size   = struct.unpack_from("<I", page, row_base + 16)[0]
        artwork_id  = struct.unpack_from("<I", page, row_base + 28)[0]
        key_id      = struct.unpack_from("<I", page, row_base + 32)[0]
        bitrate     = struct.unpack_from("<I", page, row_base + 48)[0]
        track_no    = struct.unpack_from("<I", page, row_base + 52)[0]
        tempo       = struct.unpack_from("<I", page, row_base + 56)[0]
        genre_id    = struct.unpack_from("<I", page, row_base + 60)[0]
        album_id    = struct.unpack_from("<I", page, row_base + 64)[0]
        artist_id   = struct.unpack_from("<I", page, row_base + 68)[0]
        track_id    = struct.unpack_from("<I", page, row_base + 72)[0]
        disc_no     = struct.unpack_from("<H", page, row_base + 76)[0]
        play_count  = struct.unpack_from("<H", page, row_base + 78)[0]
        year        = struct.unpack_from("<H", page, row_base + 80)[0]
        sample_dep  = struct.unpack_from("<H", page, row_base + 82)[0]
        duration    = struct.unpack_from("<H", page, row_base + 84)[0]
        color_id    = page[row_base + 88]
        rating      = page[row_base + 89]
        ofs = struct.unpack_from("<21H", page, row_base + 94)
    except struct.error:
        return None

    def s(idx: int) -> str:
        return _read_sql_string(page, row_base + ofs[idx])

    return TrackRow(
        id=track_id,
        artist_id=artist_id, album_id=album_id, genre_id=genre_id,
        key_id=key_id, color_id=color_id, artwork_id=artwork_id,
        tempo=tempo, duration=duration, bitrate=bitrate,
        sample_rate=sample_rate, sample_depth=sample_dep, rating=rating,
        play_count=play_count, year=year,
        track_number=track_no, disc_number=disc_no, file_size=file_size,
        date_added=s(10), analyze_path=s(14), comment=s(16),
        title=s(17), filename=s(19), file_path=s(20),
    )


def _parse_artist_row(page: bytes, row_base: int) -> Optional[tuple[int, str]]:
    if row_base + 10 > len(page):
        return None
    try:
        subtype = struct.unpack_from("<H", page, row_base)[0]
        rid     = struct.unpack_from("<I", page, row_base + 4)[0]
        if subtype & 0x04:
            ofs = struct.unpack_from("<H", page, row_base + 0x0a)[0]
        else:
            ofs = page[row_base + 9]
    except struct.error:
        return None
    return rid, _read_sql_string(page, row_base + ofs)


def _parse_album_row(page: bytes, row_base: int) -> Optional[tuple[int, str]]:
    if row_base + 22 > len(page):
        return None
    try:
        subtype = struct.unpack_from("<H", page, row_base)[0]
        rid     = struct.unpack_from("<I", page, row_base + 12)[0]
        if subtype & 0x04:
            ofs = struct.unpack_from("<H", page, row_base + 0x16)[0]
        else:
            ofs = page[row_base + 21]
    except struct.error:
        return None
    return rid, _read_sql_string(page, row_base + ofs)


def _parse_key_row(page: bytes, row_base: int) -> Optional[tuple[int, str]]:
    if row_base + 8 > len(page):
        return None
    rid = struct.unpack_from("<I", page, row_base)[0]
    return rid, _read_sql_string(page, row_base + 8)


def _parse_color_row(page: bytes, row_base: int) -> Optional[tuple[int, str]]:
    if row_base + 8 > len(page):
        return None
    rid = struct.unpack_from("<H", page, row_base + 5)[0]
    return rid, _read_sql_string(page, row_base + 8)


def _parse_genre_row(page: bytes, row_base: int) -> Optional[tuple[int, str]]:
    if row_base + 4 > len(page):
        return None
    rid = struct.unpack_from("<I", page, row_base)[0]
    return rid, _read_sql_string(page, row_base + 4)


def _parse_artwork_row(page: bytes, row_base: int) -> Optional[tuple[int, str]]:
    if row_base + 4 > len(page):
        return None
    rid = struct.unpack_from("<I", page, row_base)[0]
    return rid, _read_sql_string(page, row_base + 4)


# ─────────────────────────────────────────────────────────────────────────
# Page walker
# ─────────────────────────────────────────────────────────────────────────
def _walk_rows(page: bytes, len_page: int, num_row_offsets: int,
               page_type: int, db: PdbDatabase) -> None:
    """Iterate the row index at the end of the page and decode each row."""
    heap_pos = 40  # page header is exactly 40 bytes
    if num_row_offsets <= 0:
        return
    num_groups = (num_row_offsets - 1) // 16 + 1
    for group_index in range(num_groups):
        base = len_page - (group_index * 0x24)
        if base - 4 < 0 or base > len_page:
            break
        try:
            present_flags = struct.unpack_from("<H", page, base - 4)[0]
        except struct.error:
            break
        rows_in_group = min(16, num_row_offsets - group_index * 16)
        for row_index in range(rows_in_group):
            if not (present_flags >> row_index) & 1:
                continue
            ofs_pos = base - (6 + 2 * row_index)
            if ofs_pos < 0 or ofs_pos + 2 > len_page:
                continue
            ofs_row = struct.unpack_from("<H", page, ofs_pos)[0]
            row_base = heap_pos + ofs_row
            try:
                _decode_and_store(page, row_base, page_type, db)
            except Exception as exc:
                log.debug("row decode failed type=%d row_base=%d: %s",
                          page_type, row_base, exc)
                continue


def _decode_and_store(page: bytes, row_base: int, page_type: int,
                      db: PdbDatabase) -> None:
    if page_type == PT_TRACKS:
        tr = _parse_track_row(page, row_base)
        if tr is not None and tr.id:
            db.tracks[tr.id] = tr
        return
    if page_type == PT_ARTISTS:
        r = _parse_artist_row(page, row_base)
        if r and r[0]:
            db.artists[r[0]] = r[1]
        return
    if page_type == PT_ALBUMS:
        r = _parse_album_row(page, row_base)
        if r and r[0]:
            db.albums[r[0]] = r[1]
        return
    if page_type == PT_KEYS:
        r = _parse_key_row(page, row_base)
        if r and r[0]:
            db.keys[r[0]] = r[1]
        return
    if page_type == PT_COLORS:
        r = _parse_color_row(page, row_base)
        if r and r[0]:
            db.colors[r[0]] = r[1]
        return
    if page_type == PT_GENRES:
        r = _parse_genre_row(page, row_base)
        if r and r[0]:
            db.genres[r[0]] = r[1]
        return
    if page_type == PT_ARTWORK:
        r = _parse_artwork_row(page, row_base)
        if r and r[0]:
            db.artwork[r[0]] = r[1]


# ─────────────────────────────────────────────────────────────────────────
# Top-level: parse the database header → walk every interesting table.
# ─────────────────────────────────────────────────────────────────────────
def parse_pdb(data: bytes) -> PdbDatabase:
    """Parse an export.pdb byte blob and return a populated PdbDatabase."""
    if len(data) < 32:
        raise ValueError("PDB too small")
    # Header: u4 (zero), len_page u4, num_tables u4, next_unused u4, u4, sequence u4,
    # then 4-byte gap, then num_tables × table entries (16 bytes each).
    _, len_page, num_tables, _next, _, _seq = struct.unpack_from("<6I", data, 0)
    if len_page < 256 or len_page > 1 << 20:
        raise ValueError(f"PDB bogus page size {len_page}")
    table_off = 28
    db = PdbDatabase()

    # Each table entry: type u4, empty_candidate u4, first_page u4, last_page u4.
    chains: dict[int, tuple[int, int]] = {}
    for i in range(num_tables):
        base = table_off + i * 16
        if base + 16 > len(data):
            break
        ttype      = struct.unpack_from("<I", data, base)[0]
        first_page = struct.unpack_from("<I", data, base + 8)[0]
        last_page  = struct.unpack_from("<I", data, base + 12)[0]
        chains[ttype] = (first_page, last_page)

    for ttype, (first, last) in chains.items():
        if ttype not in _INTERESTING:
            continue
        page_idx = first
        seen: set[int] = set()
        while page_idx and page_idx not in seen:
            seen.add(page_idx)
            page_off = page_idx * len_page
            if page_off + len_page > len(data):
                log.debug("PDB page %d out of range for table type %d", page_idx, ttype)
                break
            page = data[page_off:page_off + len_page]
            # Page header (40 bytes): gap u4, page_index u4, type u4, next_page u4,
            # sequence u4, unknown u4, then 3 bytes of {num_row_offsets b13, num_rows b11}
            # (LE bit order), page_flags u1, free_size u2, used_size u2, etc.
            try:
                page_type     = struct.unpack_from("<I", page, 8)[0]
                next_page_idx = struct.unpack_from("<I", page, 12)[0]
            except struct.error:
                break
            three = page[24] | (page[25] << 8) | (page[26] << 16)
            num_row_offsets = three & 0x1FFF
            page_flags = page[27]
            is_data_page = (page_flags & 0x40) == 0

            if is_data_page and page_type == ttype:
                _walk_rows(page, len_page, num_row_offsets, page_type, db)

            if page_idx == last:
                break
            page_idx = next_page_idx

    log.info("PDB parsed: %d tracks, %d artists, %d albums, %d keys, "
             "%d genres, %d colors, %d artwork",
             len(db.tracks), len(db.artists), len(db.albums), len(db.keys),
             len(db.genres), len(db.colors), len(db.artwork))
    return db
