"""
Binary packet decoder for the Pro DJ Link protocol.
Stateless — call parse() once per incoming UDP datagram.
"""
from __future__ import annotations
import logging
import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

log = logging.getLogger(__name__)

from core.network.constants import (
    MAGIC,
    PKT_DEVICE_ANNOUNCE, PKT_PRECISE_POSITION, PKT_BEAT, PKT_CDJ_STATUS, PKT_MIXER_STATUS,
    CDJOffset, BeatOffset, PreciseOffset, AnnounceOffset,
    FLAG_ON_AIR, FLAG_SYNC, FLAG_MASTER, FLAG_PLAYING, PORT_METADATA,
    PLAY_STATE_LOOP, FLAG_MT_HINT, FLAG_BPM_SYNC,
)


class PacketType(IntEnum):
    DEVICE_ANNOUNCE = PKT_DEVICE_ANNOUNCE
    PRECISE_POSITION= PKT_PRECISE_POSITION
    BEAT            = PKT_BEAT
    CDJ_STATUS      = PKT_CDJ_STATUS
    MIXER_STATUS    = PKT_MIXER_STATUS
    UNKNOWN         = 0xFF

    @classmethod
    def _missing_(cls, value: object) -> "PacketType":
        return cls.UNKNOWN


@dataclass
class ParsedPacket:
    """
    Decoded Pro DJ Link packet.
    Fields irrelevant to the packet type remain at their zero/empty defaults.
    """
    type: PacketType
    device_name: str
    device_number: int

    # CDJ status / beat shared fields
    bpm: float = 0.0
    beat_in_bar: int = 0
    play_state_byte: int = 0
    flags_byte: int = 0
    pitch: float = 0.0              # normalised: -1.0 (slow) … 0.0 … +1.0 (fast)
    position_ms: int = 0
    beat_number: int = 0
    track_source_slot: int = 0      # 0=none 1=CD 2=SD 3=USB 4=collection
    track_source_player: int = 0    # player number of the track source
    track_type: int = 0             # 0=none 1=rekordbox 2=unanalyzed 5=CD audio
    track_rekordbox_id: int = 0     # rekordbox library ID (0 = not loaded)
    next_beat_ms: int = 0
    second_beat_ms: int = 0
    next_bar_ms: int = 0
    fourth_beat_ms: int = 0
    second_bar_ms: int = 0
    eighth_beat_ms: int = 0
    effective_bpm: float = 0.0
    track_length_ms: int = 0

    # Announce fields
    ip_address: str = ""

    # Derived convenience booleans (CDJ_STATUS only)
    is_playing: bool = False
    is_master: bool = False
    is_sync: bool = False
    is_on_air: bool = False
    loop_active: bool = False
    master_tempo: bool = False
    loop_start_ms: int = 0
    loop_end_ms: int = 0

    raw: bytes = field(default=b"", repr=False)


class PacketParser:
    """Parse a raw UDP datagram into a ParsedPacket, or return None on failure."""

    def __init__(self) -> None:
        self._last_raw_diag: dict[int, tuple[int, int, int, int]] = {}

    def parse(self, data: bytes) -> Optional[ParsedPacket]:
        if len(data) < 36 or data[:MAGIC.__len__()] != MAGIC:
            return None

        ptype = PacketType(data[10])          # _missing_ handles unknown types

        # Pro DJ Link has packet-family-specific field positions.
        # Keepalive/announce packets (0x06) use beat-link offsets:
        #   name@0x0c (12), device#@0x24 (36)
        # CDJ status / beat packets still use device#@33.
        if ptype == PacketType.DEVICE_ANNOUNCE:
            device_name = data[12:32].rstrip(b"\x00").decode("utf-8", errors="replace")
            device_number = data[AnnounceOffset.DEVICE_NUMBER]
        else:
            device_name = data[11:31].rstrip(b"\x00").decode("utf-8", errors="replace")
            device_number = data[33]

        pkt = ParsedPacket(
            type=ptype,
            device_name=device_name,
            device_number=device_number,
            raw=data,
        )

        if ptype == PacketType.CDJ_STATUS:
            self._parse_cdj_status(data, pkt)
        elif ptype == PacketType.PRECISE_POSITION:
            self._parse_precise_position(data, pkt)
        elif ptype == PacketType.BEAT:
            self._parse_beat(data, pkt)
        elif ptype == PacketType.DEVICE_ANNOUNCE:
            self._parse_announce(data, pkt)

        return pkt

    # ──────────────────────────────────────────────────────────────────
    def _parse_cdj_status(self, data: bytes, p: ParsedPacket) -> None:
        if len(data) < CDJOffset.MIN_LENGTH:
            return

        p.track_source_player = data[CDJOffset.TRACK_SOURCE_PLAYER]
        p.track_source_slot   = data[CDJOffset.TRACK_SOURCE_SLOT]
        p.track_type          = data[CDJOffset.TRACK_TYPE]
        p.track_rekordbox_id  = struct.unpack_from(">I", data, CDJOffset.TRACK_REKORDBOX_ID)[0]
        
        # ── Diagnostic: dump raw bytes to verify offsets ──────────────────
        # Show bytes 38-52 which includes source_player(40), source_slot(41),
        # and track_id(44-47) to verify the parsing is reading from correct offsets
        if p.track_rekordbox_id > 0 or p.track_source_player > 0:
            diag_start = 38
            diag_end = min(52, len(data))
            raw_hex = data[diag_start:diag_end].hex()
            diag_tuple = (
                p.track_source_player,
                p.track_source_slot,
                p.track_type,
                p.track_rekordbox_id,
            )
            if self._last_raw_diag.get(p.device_number) != diag_tuple:
                self._last_raw_diag[p.device_number] = diag_tuple
                log.debug(
                    "RAW_DIAGNOSTIC: player=%d bytes[38:52]=%s | "
                    "parsed: source_player=%d(byte40) source_slot=%d(byte41) "
                    "track_type=%d(byte42) track_id=0x%08X(bytes44-47)",
                    p.device_number,
                    raw_hex,
                    p.track_source_player,
                    p.track_source_slot,
                    p.track_type,
                    p.track_rekordbox_id,
                )
        raw_bpm = struct.unpack_from(">H", data, CDJOffset.BPM)[0]
        p.bpm = 0.0 if raw_bpm == 0xFFFF else raw_bpm / 100.0
        primary_flags = data[CDJOffset.FLAGS]
        legacy_flags = data[CDJOffset.FLAGS_LEGACY] if len(data) > CDJOffset.FLAGS_LEGACY else 0
        p.flags_byte = primary_flags if primary_flags != 0 else legacy_flags
        p.play_state_byte  = data[CDJOffset.PLAY_STATE]
        raw_beat_number = struct.unpack_from(">I", data, CDJOffset.BEAT_NUMBER)[0]
        p.beat_number = 0 if raw_beat_number == 0xFFFFFFFF else int(raw_beat_number)
        p.beat_in_bar      = data[CDJOffset.BEAT_IN_BAR]

        # Pitch1: unsigned 24-bit payload bytes, centre value 0x100000 = 0 %.
        raw_pitch = (
            (data[CDJOffset.PITCH] << 16)
            | (data[CDJOffset.PITCH + 1] << 8)
            | data[CDJOffset.PITCH + 2]
        )
        p.pitch = (raw_pitch - 0x100000) / 0x100000
        # Pre-CDJ-3000 players do not report absolute playback position in the
        # regular status packet; that must be inferred from beat/grid data.
        p.position_ms = 0

        # Derive booleans. Some players do not reliably set FLAG_PLAYING,
        # so fall back to the detailed play-state byte when needed.
        p.is_playing = bool(p.flags_byte & FLAG_PLAYING) or p.play_state_byte in {
            0x03,  # normal playback
            0x04,  # loop playback
            0x07,  # cue play while held
            0x08,  # reverse / scratch playback
        }
        p.is_master  = bool(p.flags_byte & FLAG_MASTER)
        p.is_sync    = bool(p.flags_byte & FLAG_SYNC)
        p.is_on_air  = bool(p.flags_byte & FLAG_ON_AIR)

        # Loop active: P1 byte value 0x04 = playing a loop (djl-analysis)
        p.loop_active = (p.play_state_byte == PLAY_STATE_LOOP)

        if len(data) >= CDJOffset.MIN_LENGTH_LOOP_INFO:
            loop_beats = struct.unpack_from(">H", data, CDJOffset.LOOP_ACTIVE_BEATS)[0]
            start_ticks = struct.unpack_from(">I", data, CDJOffset.LOOP_START_TICKS)[0]
            end_ticks = struct.unpack_from(">I", data, CDJOffset.LOOP_END_TICKS)[0]
            # Loop ticks are in 1/65536 second units.
            p.loop_start_ms = int((start_ticks * 1000) / 65536)
            p.loop_end_ms = int((end_ticks * 1000) / 65536)
            if loop_beats > 0 and p.loop_end_ms > p.loop_start_ms:
                p.loop_active = True

        # Master Tempo (Key Lock): prefer explicit extended byte, with a
        # fallback hint bit seen on some models/firmwares.
        explicit_mt = (len(data) >= CDJOffset.MASTER_TEMPO + 1) and (data[CDJOffset.MASTER_TEMPO] == 0x01)
        p.master_tempo = explicit_mt or bool(p.flags_byte & (FLAG_MT_HINT | FLAG_BPM_SYNC))

    def _parse_beat(self, data: bytes, p: ParsedPacket) -> None:
        if len(data) < BeatOffset.MIN_LENGTH:
            return
        p.next_beat_ms = struct.unpack_from(">I", data, BeatOffset.NEXT_BEAT_MS)[0]
        p.second_beat_ms = struct.unpack_from(">I", data, BeatOffset.SECOND_BEAT_MS)[0]
        p.next_bar_ms = struct.unpack_from(">I", data, BeatOffset.NEXT_BAR_MS)[0]
        p.fourth_beat_ms = struct.unpack_from(">I", data, BeatOffset.FOURTH_BEAT_MS)[0]
        p.second_bar_ms = struct.unpack_from(">I", data, BeatOffset.SECOND_BAR_MS)[0]
        p.eighth_beat_ms = struct.unpack_from(">I", data, BeatOffset.EIGHTH_BEAT_MS)[0]
        raw_bpm = struct.unpack_from(">H", data, BeatOffset.BPM)[0]
        p.bpm = 0.0 if raw_bpm == 0xFFFF else raw_bpm / 100.0
        p.beat_in_bar = data[BeatOffset.BEAT_IN_BAR]
        raw_pitch = struct.unpack_from(">I", data, BeatOffset.PITCH)[0]
        p.pitch = (raw_pitch - 0x00100000) / 0x00100000
        p.effective_bpm = p.bpm * (1.0 + p.pitch) if p.bpm > 0.0 else 0.0

    def _parse_precise_position(self, data: bytes, p: ParsedPacket) -> None:
        """
        Parse precise position packet (0x0B on UDP :50001).
        Primarily emitted by CDJ-3000 class hardware; reports absolute playback
        position at high frequency.
        """
        if len(data) < PreciseOffset.MIN_LENGTH:
            return

        p.device_number = data[PreciseOffset.DEVICE_NUMBER]
        track_len_s = struct.unpack_from(">I", data, PreciseOffset.TRACK_LENGTH_S)[0]
        p.track_length_ms = int(track_len_s * 1000)
        p.position_ms = struct.unpack_from(">I", data, PreciseOffset.PLAYBACK_POS_MS)[0]

        # Raw signed pitch percentage x100 (e.g. +3.26% => 326)
        raw_pct_x100 = struct.unpack_from(">i", data, PreciseOffset.RAW_PITCH_PERCENT)[0]
        p.pitch = float(raw_pct_x100) / 10_000.0

        # Effective BPM x10; 0xFFFFFFFF means unknown.
        effective_bpm_x10 = struct.unpack_from(">I", data, PreciseOffset.BPM_X10)[0]
        if effective_bpm_x10 == 0xFFFFFFFF:
            p.bpm = 0.0
            p.effective_bpm = 0.0
        else:
            effective_bpm = float(effective_bpm_x10) / 10.0
            mult = max(0.001, 1.0 + p.pitch)
            p.bpm = effective_bpm / mult
            p.effective_bpm = effective_bpm

    def _parse_announce(self, data: bytes, p: ParsedPacket) -> None:
        if len(data) < AnnounceOffset.MIN_LENGTH:
            return
        ip = data[AnnounceOffset.IP_ADDRESS: AnnounceOffset.IP_ADDRESS + 4]
        p.ip_address = ".".join(str(b) for b in ip)
