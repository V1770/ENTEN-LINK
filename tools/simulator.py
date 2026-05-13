#!/usr/bin/env python3
"""
Pioneer DJ Link — Hardware Simulator
=====================================
Sends realistic Pro DJ Link UDP packets to localhost so the application
can be developed and tested without physical Pioneer hardware.

Usage
-----
    python -m tools.simulator                   # 2 players, 128 BPM
    python -m tools.simulator --players 4       # 4 players
    python -m tools.simulator --players 1 --bpm 140.5
    python -m tools.simulator --bpm 124 --jitter 0
"""
from __future__ import annotations
import argparse
import asyncio
import random
import socket
import struct
import time

# ── Protocol constants (duplicated here so the simulator has no app deps) ──────
MAGIC         = b"Qspt1WmJOL"
PORT_ANNOUNCE = 50000
PORT_BEAT     = 50001
PORT_STATUS   = 50002
TARGET        = "127.0.0.1"

DEVICE_NAMES = [
    "CDJ-3000",
    "CDJ-2000NXS2",
    "CDJ-2000NXS",
    "XDJ-1000MK2",
]

TRACKS = [
    ("Obsidian Dreams",   "Solar Fields",  "Am",  7 * 60_000 + 23_000),
    ("Parallax",          "Maceo Plex",    "Dm",  8 * 60_000 + 10_000),
    ("Veil of Shadows",   "Robert Hood",   "Gm",  6 * 60_000 + 55_000),
    ("Nautilus",          "Bob James",     "Cm",  5 * 60_000 + 44_000),
]


# ── Packet builders ────────────────────────────────────────────────────────────

def _name_bytes(name: str) -> bytes:
    return name.encode("utf-8")[:20].ljust(20, b"\x00")


def build_announce(slot: int, name: str) -> bytes:
    """Device announce packet (type 0x06) — broadcast on port 50000."""
    pkt = bytearray(50)
    pkt[0:10] = MAGIC
    pkt[10]   = 0x06                      # type
    pkt[11:31]= _name_bytes(name)
    pkt[31]   = 0x01
    pkt[33]   = slot
    struct.pack_into(">H", pkt, 34, 50)   # packet length
    pkt[38:42]= b"\x7f\x00\x00\x01"      # IP = 127.0.0.1
    # MAC zeroed
    pkt[48]   = slot
    pkt[49]   = 0x01                      # device type CDJ
    return bytes(pkt)


def build_beat(slot: int, name: str, bpm: float, beat_in_bar: int) -> bytes:
    """Beat packet (type 0x28) — sent on port 50001 on every beat."""
    interval_ms = int(60_000 / bpm)
    pkt = bytearray(80)
    pkt[0:10]  = MAGIC
    pkt[10]    = 0x28
    pkt[11:31] = _name_bytes(name)
    pkt[31]    = 0x01
    pkt[33]    = slot
    struct.pack_into(">H", pkt, 34, 80)
    struct.pack_into(">I", pkt, 40, interval_ms)       # next beat
    struct.pack_into(">I", pkt, 44, interval_ms * 2)
    struct.pack_into(">I", pkt, 48, interval_ms * 4)
    struct.pack_into(">I", pkt, 52, interval_ms * 8)
    struct.pack_into(">I", pkt, 56, interval_ms * 16)
    struct.pack_into(">I", pkt, 60, interval_ms * 32)
    struct.pack_into(">I", pkt, 68, int(bpm * 100))    # BPM × 100
    pkt[75]    = beat_in_bar
    return bytes(pkt)


def build_status(
    slot: int,
    name: str,
    bpm: float,
    position_ms: int,
    beat_number: int,
    beat_in_bar: int,
    is_playing: bool,
    is_master: bool,
    pitch: float = 0.0,
    track_source_slot: int = 3,   # 3=USB (most common real-world source)
    track_rekordbox_id: int = 0,
    loop_active: bool = False,
) -> bytes:
    """CDJ status packet (type 0x0a) — sent at ~8 Hz on port 50002."""
    LENGTH = 212
    pkt = bytearray(LENGTH)

    pkt[0:10]  = MAGIC
    pkt[10]    = 0x0A
    pkt[11:31] = _name_bytes(name)
    pkt[31]    = 0x01
    pkt[33]    = slot
    struct.pack_into(">H", pkt, 34, LENGTH)

    pkt[40]    = track_source_slot              # source: USB
    pkt[41]    = slot                           # source player = self
    struct.pack_into(">I", pkt, 44, track_rekordbox_id)   # rekordbox ID

    struct.pack_into(">I", pkt, 92, int(bpm * 100))       # BPM × 100

    flags = 0
    if is_playing: flags |= 0x40
    if is_master:  flags |= 0x20
    pkt[121]   = flags
    # P1 play state: 0x04 = playing a loop, 0x03 = normal play, 0x05 = paused
    if loop_active and is_playing:
        pkt[123] = 0x04
    else:
        pkt[123] = 0x03 if is_playing else 0x05

    struct.pack_into(">I", pkt, 132, beat_number)          # absolute beat
    pkt[136]   = beat_in_bar

    # Pitch: uint24 BE, centre 0x100000
    raw_pitch = int(0x100000 + pitch * 0x100000)
    raw_pitch  = max(0, min(0x1FFFFF, raw_pitch))
    pkt[140]   = (raw_pitch >> 16) & 0xFF
    pkt[141]   = (raw_pitch >> 8)  & 0xFF
    pkt[142]   =  raw_pitch        & 0xFF

    struct.pack_into(">I", pkt, 156, position_ms)       # position ms

    return bytes(pkt)


# ── TCP Metadata Server (port 12523) ──────────────────────────────────────────

PORT_METADATA = 12523
GREETING      = b"\x11\x00\x00\x00\x01"   # both client and server use this
ARG_NUMBER    = 0x0F
ARG_STRING    = 0x14
ARG_BLOB      = 0x03

# Request type codes (big-endian)
REQ_METADATA        = 0x00002002
REQ_WAVEFORM_PREVIEW = 0x00002602


def _num_arg(v: int) -> bytes:
    return struct.pack(">BBBBI", ARG_NUMBER, 0, 0, 0, v)


def _str_arg(s: str) -> bytes:
    d = s.encode("utf-16-be")
    return bytes([ARG_STRING]) + struct.pack(">I", len(d)) + d


def _blob_arg(b: bytes) -> bytes:
    return bytes([ARG_BLOB]) + struct.pack(">I", len(b)) + b


def _build_message(tx_id: int, msg_type: int, *fields: bytes) -> bytes:
    body = b"".join(fields)
    return struct.pack(">III", tx_id, msg_type, len(fields)) + body


def _generate_waveform(duration_ms: int) -> bytes:
    """Deterministic synthetic 400-byte waveform preview."""
    import math
    rng = random.Random(duration_ms)
    data = bytearray(400)
    for i in range(400):
        pos    = i / 400
        energy = 0.25 + 0.55 * abs(math.sin(pos * math.pi * 5 + 0.4))
        energy += rng.uniform(-0.08, 0.12)
        energy  = max(0.04, min(1.0, energy))
        height    = int(energy * 14)
        whiteness = int(energy * 12)
        data[i]   = (height << 4) | min(15, whiteness)
    return bytes(data)


class MetadataServer:
    """
    Async TCP server on port 12523.
    Responds to TRACK_METADATA and WAVEFORM_PREVIEW requests with synthetic data
    derived from the simulated players' track info.
    """

    def __init__(self, players: list) -> None:
        # {rekordbox_id: SimPlayer}
        self._by_id = {p.track_rekordbox_id: p for p in players}

    async def run(self) -> None:
        try:
            server = await asyncio.start_server(
                self._handle_client, "127.0.0.1", PORT_METADATA
            )
        except OSError as exc:
            print(f"  [META] Could not start TCP server on :{PORT_METADATA} — {exc}")
            return
        print(f"  Metadata server listening on TCP :{PORT_METADATA}")
        async with server:
            await server.serve_forever()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            # Greeting exchange
            await reader.readexactly(5)
            writer.write(GREETING)
            await writer.drain()

            while True:
                # Read 12-byte message header
                try:
                    header = await asyncio.wait_for(reader.readexactly(12), timeout=10.0)
                except asyncio.TimeoutError:
                    break
                tx_id, req_type, arg_count = struct.unpack_from(">III", header)

                # Read numeric arguments
                args: list[int] = []
                for _ in range(arg_count):
                    tag = (await reader.readexactly(1))[0]
                    if tag == ARG_NUMBER:
                        rest = await reader.readexactly(7)  # 3 padding + 4 value
                        args.append(struct.unpack_from(">I", rest, 3)[0])
                    elif tag in (ARG_STRING, ARG_BLOB):
                        length = struct.unpack_from(">I", await reader.readexactly(4))[0]
                        await reader.readexactly(length)   # discard
                    else:
                        break   # unknown tag — bail

                response = self._respond(tx_id, req_type, args)
                if response:
                    writer.write(response)
                    await writer.drain()

        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()

    def _respond(self, tx_id: int, req_type: int, args: list[int]) -> bytes:
        track_id = args[3] if len(args) >= 4 else 0
        player   = self._by_id.get(track_id)
        if player is None and self._by_id:
            player = next(iter(self._by_id.values()))

        if req_type == REQ_METADATA and player:
            title, artist, key, dur_ms = player.track
            return _build_message(
                tx_id, REQ_METADATA,
                _num_arg(10),               # item count placeholder
                _str_arg(title),
                _str_arg(artist),
                _str_arg(""),               # album
                _str_arg(""),               # genre
                _str_arg(""),               # comment
                _str_arg(key),
                _num_arg(0),               # rating
                _num_arg(dur_ms // 1000),  # duration in seconds
                _num_arg(0),               # artwork ID
            )

        if req_type == REQ_WAVEFORM_PREVIEW and player:
            _, _, _, dur_ms = player.track
            wf = _generate_waveform(dur_ms)
            return _build_message(
                tx_id, REQ_WAVEFORM_PREVIEW,
                _num_arg(0),
                _blob_arg(wf),
            )

        return b""


# ── Simulated player state ─────────────────────────────────────────────────────

class SimPlayer:
    def __init__(
        self,
        slot: int,
        bpm: float,
        jitter: float,
        is_master: bool,
    ) -> None:
        self.slot             = slot
        self.bpm              = bpm + random.uniform(-jitter, jitter)
        self.name             = DEVICE_NAMES[(slot - 1) % len(DEVICE_NAMES)]
        self.track            = TRACKS[(slot - 1) % len(TRACKS)]
        self.pitch            = random.uniform(-0.03, 0.03)
        self.is_master        = is_master
        self.is_playing       = True
        # Player 1 starts in loop immediately; others start normal
        self.loop_active      = (slot == 1)
        # Unique rekordbox ID per player (non-zero triggers metadata fetch)
        self.track_rekordbox_id = slot * 1000 + 1

        self._beat_count  = 0
        self._beat_in_bar = 1
        self._position_ms = 0

    @property
    def effective_bpm(self) -> float:
        return self.bpm * (1.0 + self.pitch)

    @property
    def beat_interval_s(self) -> float:
        return 60.0 / self.effective_bpm

    def tick_beat(self) -> None:
        """Advance internal state by one beat."""
        self._beat_count  += 1
        self._beat_in_bar  = ((self._beat_count - 1) % 4) + 1
        self._position_ms  = int(self._beat_count * self.beat_interval_s * 1000)
        self._position_ms  = min(self._position_ms, self.track[3])
        # Player 1: loop for 8 beats, then off for 8 beats (32-beat cycle)
        # Player 2: opposite phase so you always have one in loop
        if self.slot == 1:
            phase = self._beat_count % 16
            self.loop_active = phase < 8
        elif self.slot == 2:
            phase = self._beat_count % 16
            self.loop_active = phase >= 8

    @property
    def beat_number(self) -> int:  return self._beat_count
    @property
    def beat_in_bar(self) -> int:  return self._beat_in_bar
    @property
    def position_ms(self) -> int:  return self._position_ms


# ── Simulator tasks ────────────────────────────────────────────────────────────

class Simulator:
    def __init__(self, player_count: int, base_bpm: float, jitter: float) -> None:
        self._players = [
            SimPlayer(
                slot=i + 1,
                bpm=base_bpm,
                jitter=jitter,
                is_master=(i == 0),
            )
            for i in range(player_count)
        ]
        self._sock_ann = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock_beat = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock_stat = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def _print_header(self) -> None:
        print(f"\n  Pioneer DJ Link — Simulator  ({len(self._players)} player(s))\n")
        for p in self._players:
            print(
                f"  Slot {p.slot}  {p.name:<16}  "
                f"BPM={p.effective_bpm:.2f}  "
                f"Track: '{p.track[0]}' — {p.track[1]}"
                + ("  [MASTER]" if p.is_master else "")
            )
        print(f"\n  Sending to {TARGET}  ·  Ctrl-C to stop\n")

    async def run(self) -> None:
        self._print_header()
        meta_server = MetadataServer(self._players)
        tasks = [
            asyncio.create_task(meta_server.run(), name="tcp-metadata"),
        ]
        for p in self._players:
            tasks += [
                asyncio.create_task(self._announce_task(p)),
                asyncio.create_task(self._status_task(p)),
                asyncio.create_task(self._beat_task(p)),
            ]
        await asyncio.gather(*tasks)

    async def _announce_task(self, p: SimPlayer) -> None:
        while True:
            self._sock_ann.sendto(build_announce(p.slot, p.name), (TARGET, PORT_ANNOUNCE))
            await asyncio.sleep(1.0)

    async def _status_task(self, p: SimPlayer) -> None:
        while True:
            pkt = build_status(
                slot=p.slot, name=p.name, bpm=p.effective_bpm,
                position_ms=p.position_ms, beat_number=p.beat_number,
                beat_in_bar=p.beat_in_bar, is_playing=p.is_playing,
                is_master=p.is_master, pitch=p.pitch,
                track_source_slot=3,              # USB
                track_rekordbox_id=p.track_rekordbox_id,
                loop_active=p.loop_active,
            )
            self._sock_stat.sendto(pkt, (TARGET, PORT_STATUS))
            await asyncio.sleep(1 / 8)   # 8 Hz

    async def _beat_task(self, p: SimPlayer) -> None:
        """Fire one beat packet per beat, timed to the player's BPM."""
        while True:
            await asyncio.sleep(p.beat_interval_s)
            if p.is_playing:
                p.tick_beat()
                pkt = build_beat(p.slot, p.name, p.effective_bpm, p.beat_in_bar)
                self._sock_beat.sendto(pkt, (TARGET, PORT_BEAT))
                bar_marker = "◆" if p.beat_in_bar == 1 else "·"
                print(
                    f"  [{p.slot}] {bar_marker} beat {p.beat_in_bar}/4"
                    f"  bpm={p.effective_bpm:.2f}"
                    f"  pos={p.position_ms // 1000:>4}s"
                )


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pioneer DJ Link hardware simulator — sends fake UDP packets to localhost"
    )
    parser.add_argument(
        "--players", type=int, default=2, choices=[1, 2, 3, 4],
        help="Number of simulated CDJ players (default: 2)",
    )
    parser.add_argument(
        "--bpm", type=float, default=128.0,
        help="Base BPM for all players (default: 128.0)",
    )
    parser.add_argument(
        "--jitter", type=float, default=2.0,
        help="Random BPM variation per player in ± BPM (default: 2.0)",
    )
    args = parser.parse_args()

    sim = Simulator(player_count=args.players, base_bpm=args.bpm, jitter=args.jitter)
    try:
        asyncio.run(sim.run())
    except KeyboardInterrupt:
        print("\n  Simulator stopped.")


if __name__ == "__main__":
    main()
