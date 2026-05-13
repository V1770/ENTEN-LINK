"""
Pro DJ Link binary protocol constants.

Reference: https://djl-analysis.deepsymmetry.org/djl-analysis/
           https://github.com/Deep-Symmetry/beat-link (authoritative Java impl)

All byte offsets are relative to the start of the UDP datagram payload.
"""

# ── Magic header ───────────────────────────────────────────────────────────────
# Every Pro DJ Link packet begins with these 10 bytes.
MAGIC = b"Qspt1WmJOL"
MAGIC_LEN = 10

# ── Packet type identifiers (byte 10 in every packet) ─────────────────────────
PKT_DEVICE_ANNOUNCE = 0x06   # Device broadcast / keepalive    UDP 50000
PKT_PRECISE_POSITION= 0x0B   # Precise position (CDJ-3000+)    UDP 50001
PKT_BEAT            = 0x28   # Beat packet                     UDP 50001
PKT_CDJ_STATUS      = 0x0A   # CDJ player full status          UDP 50002
PKT_MIXER_STATUS    = 0x29   # DJM mixer status                UDP 50002

# ── Network ports ──────────────────────────────────────────────────────────────
PORT_ANNOUNCE = 50000
PORT_BEAT     = 50001
PORT_STATUS   = 50002

# ── Device type codes (announce packet, byte 49) ──────────────────────────────
DEVICE_CDJ = 0x01
DEVICE_DJM = 0x03
DEVICE_XDJ = 0x04


# ── Track source slot identifiers ─────────────────────────────────────────────
class TrackSlot:
    NONE       = 0x00   # no track loaded
    CD         = 0x01   # physical CD  (CDJ-2000NXS/NXS2)
                        # NOTE: CDJ-3000 has no CD drive — it reuses slot=0x01 for SD card
    SD_CARD    = 0x02   # SD card  (CDJ-2000NXS/NXS2); CDJ-3000 uses 0x01 for SD
    USB        = 0x03   # USB drive
    COLLECTION = 0x04   # rekordbox collection


# ── TCP metadata server ────────────────────────────────────────────────────────
PORT_METADATA = 12523


# ── CDJ Status packet field offsets (type 0x0A, UDP 50002) ────────────────────
class CDJOffset:
    """Byte offsets within a CDJ status packet payload."""
    DEVICE_NUMBER        = 33    # uint8   — player slot 1–4
    TRACK_SOURCE_PLAYER  = 40    # uint8   — player number of track source (D_r)
    TRACK_SOURCE_SLOT    = 41    # uint8   — 0=none 1=CD 2=SD 3=USB 4=collection (S_r)
    TRACK_TYPE           = 42    # uint8   — 0=none 1=rekordbox 2=unanalyzed 5=CD audio
    TRACK_REKORDBOX_ID   = 44    # uint32 BE — rekordbox library track ID
    BPM                  = 146   # uint16 BE — track BPM × 100 (bytes 0x92-0x93)
    FLAGS                = 137   # uint8   — status flag byte F (0x89 in beat-link docs)
    FLAGS_LEGACY         = 121   # legacy/simulator fallback
    PLAY_STATE           = 123   # uint8   — P1 detailed play mode enum
    BEAT_NUMBER          = 160   # uint32 BE — absolute beat within track (0xA0)
    BEAT_IN_BAR          = 166   # uint8   — 1–4 (0xA6)
    PITCH                = 141   # uint24 BE — Pitch1 payload bytes 0x8d-0x8f, 0x100000 = 0 %
    MASTER_TEMPO         = 344   # uint8   — 0x01 when Key Lock/MT on (CDJ-3000+, 0x158)
    LOOP_START_TICKS     = 438   # uint32 BE — loop start in 1/65536 s units (0x1b6)
    LOOP_END_TICKS       = 446   # uint32 BE — loop end in 1/65536 s units (0x1be)
    LOOP_ACTIVE_BEATS    = 456   # uint16 BE — active loop size in beats (0x1c8)
    MIN_LENGTH           = 167   # minimum to read fields through beat counters (0xA6)
                                 # (XDJ-700 sends 208 bytes, CDJ-2000NXS sends 212)
    MIN_LENGTH_3000      = 512   # CDJ-3000 extended packet length
    MIN_LENGTH_LOOP_INFO = 458   # minimum to read loop start/end/beat count


# Play-state P1 byte values (byte 123) used for mode detection
PLAY_STATE_LOOP = 0x04   # actively playing a loop (djl-analysis P1=04)


# Bit masks for CDJOffset.FLAGS
FLAG_ON_AIR  = 0x08   # channel is live (fader up)
FLAG_BPM_SYNC = 0x02  # degraded BPM-sync state (used as MT fallback on some models)
FLAG_SYNC    = 0x10   # sync mode active
FLAG_MASTER  = 0x20   # this player is the tempo master
FLAG_PLAYING = 0x40   # actively playing (most reliable play indicator)
FLAG_MT_HINT = 0x01   # model-dependent fallback hint for Master Tempo/Key Lock


# ── Beat packet field offsets (type 0x28, UDP 50001) ──────────────────────────
class BeatOffset:
    """Byte offsets within a beat packet payload."""
    DEVICE_NUMBER = 33
    NEXT_BEAT_MS  = 40    # uint32 BE — ms until next beat
    SECOND_BEAT_MS = 44   # uint32 BE — ms until second upcoming beat
    NEXT_BAR_MS    = 48   # uint32 BE — ms until next bar
    FOURTH_BEAT_MS = 52   # uint32 BE — ms until fourth upcoming beat
    SECOND_BAR_MS  = 56   # uint32 BE — ms until second upcoming bar
    EIGHTH_BEAT_MS = 60   # uint32 BE — ms until eighth upcoming beat
    PITCH         = 84    # uint32 BE — bytes 0x54-0x57, neutral 0x00100000
    BPM           = 90    # uint16 BE — bytes 0x5a-0x5b, track BPM × 100
    BEAT_IN_BAR   = 92    # uint8  — 1–4 (byte 0x5c)
    MIN_LENGTH    = 96


# ── Precise position packet field offsets (type 0x0B, UDP 50001) ────────────
class PreciseOffset:
    """Byte offsets within a precise position packet payload."""
    # Device number is not at the usual offset for this packet family.
    DEVICE_NUMBER     = 0x21
    TRACK_LENGTH_S    = 0x24   # uint32 BE — track length in seconds
    PLAYBACK_POS_MS   = 0x28   # uint32 BE — absolute playback position in ms
    RAW_PITCH_PERCENT = 0x2C   # int32 BE — effective pitch percentage × 100
    BPM_X10           = 0x38   # uint32 BE — effective BPM × 10
    MIN_LENGTH        = 0x3C


# ── Device announce field offsets (type 0x06, UDP 50000) ──────────────────────
class AnnounceOffset:
    """Byte offsets within a device announce packet payload."""
    DEVICE_NUMBER = 36    # 0x24 — beat-link DEVICE_NUMBER_OFFSET
    MAC_ADDRESS   = 38    # 0x26 — beat-link MAC_ADDRESS_OFFSET (MAC before IP)
    IP_ADDRESS    = 44    # 0x2c — beat-link IP address offset
    DEVICE_TYPE   = 37    # uint8 — 0x01=CDJ, 0x02=mixer
    MIN_LENGTH    = 50
