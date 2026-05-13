"""
3-band waveform data matching the rekordbox colour scheme:
    • Blue   (#00A0F0)    — low frequency (bass)
    • Yellow (#FFC800)    — mid frequency
    • White  (#FFFFFF)    — high frequency (treble)

Data sources
────────────
  from_preview_bytes()        Monochrome preview from MSG_WAVEFORM_PREV.
                              900-byte blob: 400 columns × 2 bytes (height 0-31,
                              whiteness 0-7) + 100-byte tiny preview (ignored).
                              Legacy 400-byte nibble format also accepted.

  from_detail_bytes()         Monochrome detail from MSG_WAVEFORM_DET (0x2904).
                              1 byte/column: bits 7-5 = whiteness (0=blue, 7=white),
                              bits 4-0 = height (0-31).  Falls back if Nxs2
                              color is unavailable.

  from_nxs2_detail_bytes()    Nxs2 color detail from PWV5 tag in ANLZ0000.EXT.
                              2 bytes/column after a 34-byte tag header.
                              Bit layout: [15-13] red, [12-10] green, [9-7] blue,
                              [6-2] height.  Populates raw_colors for true-color
                              rendering and low_h/mid_h/high_h for 3-band fallback.

  synthetic()                 Deterministic test waveform with musical structure.
"""
from __future__ import annotations
import math
import random
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class WaveformData:
    """
    Per-column waveform data.

    3-band mode (raw_colors is None):
        low_h, mid_h, high_h hold 0.0–1.0 energy per column.
        Rendered as three overlapping coloured bars.

    True-color mode (raw_colors is not None):
        raw_colors holds (r, g, b) 0–255 per column.
        heights holds the overall column height 0.0–1.0.
        low_h/mid_h/high_h are also populated as a fallback.
        Rendered as a single bar with the actual hardware colour.
    """
    low_h:  list[float]   # bass   — rendered blue in 3-band mode
    mid_h:  list[float]   # mid    — rendered yellow in 3-band mode
    high_h: list[float]   # treble — rendered white in 3-band mode
    bpm: float = 0.0
    raw_colors: Optional[list[tuple[int, int, int]]] = None  # (r,g,b) per col
    heights:    Optional[list[float]] = None                  # 0.0–1.0 per col

    @property
    def column_count(self) -> int:
        return len(self.low_h)

    # ── CDJ preview blob (400 bytes, 1 band inferred) ─────────────────
    @classmethod
    def from_preview_bytes(cls, data: bytes) -> "WaveformData":
        """
        Parse the monochrome waveform preview from MSG_WAVEFORM_PREV.

        Modern format (900 bytes): 400 columns × 2 bytes:
            byte 0 = height  (0-31)
            byte 1 = whiteness (0-7, 0=blue/bass, 7=white/treble)
        plus a 100-byte tiny preview at the end (ignored).

        Legacy format (400 bytes): 1 byte / column, high nibble = height,
        low nibble = whiteness.  Accepted for backwards compatibility.
        """
        low_h:  list[float] = []
        mid_h:  list[float] = []
        high_h: list[float] = []

        if len(data) >= 800:
            # Modern 2-byte-per-column format
            for i in range(400):
                h = data[i * 2]     / 31.0   # height 0-31
                w = data[i * 2 + 1] / 7.0    # whiteness 0-7
                low_h.append(max(0.0, min(1.0, h * (1.0 - w))))
                mid_h.append(max(0.0, min(1.0, h * (1.0 - abs(2.0 * w - 1.0)) * 0.9)))
                high_h.append(max(0.0, min(1.0, h * w)))
        else:
            # Legacy 1-byte nibble format
            for byte in data[:400]:
                h = ((byte >> 4) & 0x0F) / 15.0   # overall column height
                w = (byte & 0x0F) / 15.0           # whiteness: 0=blue, 1=white
                low_h.append(max(0.0, min(1.0, h * (1.0 - w))))
                mid_h.append(max(0.0, min(1.0, h * (1.0 - abs(2.0 * w - 1.0)) * 0.9)))
                high_h.append(max(0.0, min(1.0, h * w)))

        return cls(low_h=low_h, mid_h=mid_h, high_h=high_h)

    # ── CDJ monochrome detail (0x2904, 1 byte/col) ────────────────────
    @classmethod
    def from_detail_bytes(cls, data: bytes) -> "WaveformData":
        """
        Parse monochrome waveform detail from MSG_WAVEFORM_DET (0x2904).

        Per byte (djl-analysis):
            bits 7–5: whiteness (0 = darkest blue, 7 = near-white)
            bits 4–0: height (0–31)
        """
        low_h:  list[float] = []
        mid_h:  list[float] = []
        high_h: list[float] = []
        for b in data:
            h = (b & 0x1F) / 31.0          # bits 4–0
            w = (b >> 5) / 7.0             # bits 7–5
            low_h.append(max(0.0, min(1.0, h * (1.0 - w))))
            mid_h.append(max(0.0, min(1.0, h * (1.0 - abs(2.0 * w - 1.0)) * 0.9)))
            high_h.append(max(0.0, min(1.0, h * w)))
        return cls(low_h=low_h, mid_h=mid_h, high_h=high_h)

    # ── Nxs2 color detail (PWV5 tag from ANLZ0000.EXT, 2 bytes/col) ───
    @classmethod
    def from_nxs2_detail_bytes(cls, data: bytes) -> "WaveformData":
        """
        Parse Nxs2 color waveform detail (PWV5 tag via MSG 0x2c04).
        The blob is the raw ANLZ tag; waveform segments begin at byte 34.

        Per 2-byte segment (djl-analysis):
            bits 15–13: red   (0–7)
            bits 12–10: green (0–7)
            bits  9– 7: blue  (0–7)
            bits  6– 2: height (0–31)
            bits  1– 0: padding
        """
        _HEADER = 34
        if len(data) < _HEADER + 2:
            return cls(low_h=[], mid_h=[], high_h=[])

        seg = data[_HEADER:]
        n = len(seg) // 2

        low_h:  list[float] = []
        mid_h:  list[float] = []
        high_h: list[float] = []
        raw_colors: list[tuple[int, int, int]] = []
        heights:    list[float] = []

        for i in range(n):
            val    = (seg[i * 2] << 8) | seg[i * 2 + 1]
            red    = (val >> 13) & 0x07
            green  = (val >> 10) & 0x07
            blue   = (val >>  7) & 0x07
            height = (val >>  2) & 0x1F

            h = height / 31.0
            # 3-band fallback: blue → bass, green → mid, red → treble
            low_h.append(max(0.0, min(1.0, (blue  / 7.0) * h)))
            mid_h.append(max(0.0, min(1.0, (green / 7.0) * h)))
            high_h.append(max(0.0, min(1.0, (red   / 7.0) * h)))
            heights.append(h)
            # Expand 3-bit channels to 8-bit (0–7 → 0–252)
            raw_colors.append((min(255, red * 36), min(255, green * 36), min(255, blue * 36)))

        return cls(low_h=low_h, mid_h=mid_h, high_h=high_h,
                   raw_colors=raw_colors, heights=heights)

    # ── Real audio file → 3-band waveform ────────────────────────────
    @classmethod
    def from_audio_file(cls, path, col_rate: float = 150.0) -> "WaveformData":
        """
        Compute a high-resolution 3-band waveform from a WAV or AIFF file.

        Uses an overlapping STFT (2048-point window, hop = 1/col_rate seconds)
        for sufficient frequency resolution in the bass band while keeping
        Pioneer's 150 col/s time resolution.

        Crossovers (visual, matching rekordbox colour bands):
            bass  < 300 Hz   — blue
            mid   300–4000 Hz — yellow
            high  > 4000 Hz  — white

        Each band is peak-normalised to the 99th percentile so that typical
        transients fill the full scale and a single outlier spike does not
        crush the whole track.

        Requires numpy (standard project dependency).
        """
        import wave
        import numpy as np
        from numpy.lib.stride_tricks import as_strided
        from pathlib import Path as _Path

        p = _Path(path)
        ext = p.suffix.lower()

        if ext == ".wav":
            with wave.open(str(p), "rb") as wf:
                n_channels = wf.getnchannels()
                sampwidth  = wf.getsampwidth()
                framerate  = wf.getframerate()
                n_frames   = wf.getnframes()
                raw_bytes  = wf.readframes(n_frames)
        elif ext in (".aif", ".aiff"):
            import aifc
            with aifc.open(str(p), "rb") as wf:
                n_channels = wf.getnchannels()
                sampwidth  = wf.getsampwidth()
                framerate  = wf.getframerate()
                n_frames   = wf.getnframes()
                raw_bytes  = wf.readframes(n_frames)
        else:
            raise ValueError(
                f"Unsupported format '{ext}'.  Use WAV or AIFF."
            )

        # ── Decode raw bytes to float32 mono ──────────────────────────
        if sampwidth == 1:
            samples = (
                np.frombuffer(raw_bytes, dtype=np.uint8).astype(np.float32)
                / 128.0 - 1.0
            )
        elif sampwidth == 2:
            samples = (
                np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32)
                / 32768.0
            )
        elif sampwidth == 3:
            arr = np.frombuffer(raw_bytes, dtype=np.uint8).reshape(-1, 3)
            s32 = (
                arr[:, 2].astype(np.int32) << 16
                | arr[:, 1].astype(np.int32) << 8
                | arr[:, 0].astype(np.int32)
            )
            s32 = np.where(s32 >= (1 << 23), s32 - (1 << 24), s32)
            samples = s32.astype(np.float32) / float(1 << 23)
        elif sampwidth == 4:
            samples = (
                np.frombuffer(raw_bytes, dtype=np.int32).astype(np.float32)
                / float(1 << 31)
            )
        else:
            raise ValueError(f"Unsupported sample width: {sampwidth} bytes.")

        if n_channels > 1:
            samples = samples.reshape(-1, n_channels).mean(axis=1)

        # ── Overlapping STFT ──────────────────────────────────────────
        # hop  = 1/col_rate seconds (e.g. ~294 samples @ 44100 Hz / 150 col/s)
        # fft_size = 2048: gives ~21.5 Hz/bin @ 44100 Hz — enough to isolate
        #   the bass kick fundamental (40–120 Hz) cleanly in its own band.
        # Overlap ≈ 86 %: each column still maps to exactly 1/col_rate seconds.
        hop      = max(1, int(framerate / col_rate))
        fft_size = 2048
        window   = np.hanning(fft_size).astype(np.float32)

        # Pad so the last hop aligns to a complete FFT frame
        pad     = np.zeros(fft_size, dtype=np.float32)
        padded  = np.concatenate([samples.astype(np.float32), pad])
        n_cols  = max(1, (len(samples) - 1) // hop + 1)

        # Zero-copy overlapping frame view then multiply by window (forces copy)
        byte_stride = padded.strides[0]
        frames = as_strided(
            padded,
            shape=(n_cols, fft_size),
            strides=(byte_stride * hop, byte_stride),
        ) * window   # (n_cols, fft_size) — writeable copy

        spec  = np.abs(np.fft.rfft(frames, axis=1))           # (n_cols, fft_size//2+1)
        freqs = np.fft.rfftfreq(fft_size, d=1.0 / framerate)

        def _estimate_bpm() -> float:
            """Estimate BPM from onset flux autocorrelation over rhythmic bands."""
            rhythm_mask = (freqs >= 30) & (freqs < 2_000)
            cols_r = spec[:, rhythm_mask]
            if cols_r.shape[1] == 0:
                return 120.0

            # Mean energy in rhythm band, then positive frame-to-frame changes.
            e = cols_r.mean(axis=1).astype(np.float64)
            flux = np.maximum(np.diff(e, prepend=e[0]), 0.0)
            if np.max(flux) <= 1e-12:
                return 120.0

            # Whiten and autocorrelate via FFT for O(n log n) complexity.
            x = flux - np.mean(flux)
            if np.max(np.abs(x)) <= 1e-12:
                return 120.0
            n = len(x)
            nfft = 1 << int(np.ceil(np.log2(max(8, 2 * n))))
            acf = np.fft.irfft(np.abs(np.fft.rfft(x, nfft)) ** 2, nfft)[:n]

            # Search only plausible BPM range.
            bpm_min, bpm_max = 70.0, 180.0
            lag_min = max(1, int(round(col_rate * 60.0 / bpm_max)))
            lag_max = min(n - 1, int(round(col_rate * 60.0 / bpm_min)))
            if lag_max <= lag_min:
                return 120.0

            lag = lag_min + int(np.argmax(acf[lag_min:lag_max + 1]))
            bpm = (col_rate * 60.0) / lag

            # Fold obvious octave errors into a DJ-typical window.
            while bpm < 85.0:
                bpm *= 2.0
            while bpm > 170.0:
                bpm /= 2.0
            return float(max(70.0, min(180.0, bpm)))

        est_bpm = _estimate_bpm()
        sixteenth_cols = (col_rate * 60.0) / (est_bpm * 4.0)

        # Crossover frequencies
        # Bass uses a narrow sub-bass window (30–200 Hz) so the kick drum
        # attack stands out clearly against any sustained pad that sits in
        # the 200–300 Hz region, preventing a high sustained floor.
        bass_mask = (freqs >= 30)  & (freqs <  200)
        mid_mask  = (freqs >= 200) & (freqs < 4_000)
        high_mask =  freqs >= 4_000

        def _band(mask: np.ndarray, tau: float, sustain_frac: float,
                  min_gap_cols: int, onset_quantile: float) -> np.ndarray:
            """
            Spectral-flux onset envelope.

            Instead of raw STFT energy (which stays high for sustained pads
            and produces a flat waveform), we use the *positive difference*
            between consecutive frames (spectral flux) as the onset signal,
            then apply an exponential-decay peak-hold.  Each transient attack
            (kick, hat, snare) produces a decaying leaf shape; regions with
            slowly-varying sustained content produce near-zero values.

            sustain_frac: fraction of raw energy added as a floor so that
            bands with legitimate sustained content (mid/orange) remain
            visible between beats.
            tau: decay time-constant in columns (1 col ≈ 6.7 ms at 150 col/s)
            min_gap_cols: minimum distance between accepted onsets.
            onset_quantile: only flux peaks above this percentile are accepted.
            """
            cols_b = spec[:, mask]
            if cols_b.shape[1] == 0:
                return np.zeros(n_cols, dtype=np.float32)

            energy = cols_b.max(axis=1).astype(np.float64)

            # Positive spectral flux: only rising energy counts as an onset
            flux = np.maximum(np.diff(energy, prepend=energy[0]), 0.0)

            # Gate onset density so micro-transients do not flood the detail view.
            # This approximates rekordbox's sparser transient spacing.
            thr = float(np.percentile(flux, onset_quantile))
            triggers = np.zeros_like(flux)
            i = 0
            gap = max(1, int(min_gap_cols))
            while i < n_cols:
                if flux[i] >= thr:
                    j_end = min(n_cols, i + gap)
                    j_peak = i + int(np.argmax(flux[i:j_end]))
                    triggers[j_peak] = flux[j_peak]
                    i = j_end
                else:
                    i += 1

            # Blend onset flux with a small sustain floor
            signal = triggers + energy * sustain_frac

            # Exponential peak-hold decay: each onset decays over tau columns
            alpha = float(np.exp(-1.0 / tau))
            env = np.empty(n_cols, dtype=np.float64)
            v = 0.0
            for i in range(n_cols):
                v = max(signal[i], v * alpha)
                env[i] = v

            emax = env.max()
            if emax > 1e-10:
                env /= emax
            # Hard gate: silence anything below 12% so breaks are visually empty
            env[env < 0.12] = 0.0
            return np.clip(env, 0.0, 1.0).astype(np.float32)

        return cls(
            # tau in columns; sustain_frac controls how much sustained energy bleeds in
            # min_gap_cols is BPM-adaptive (around 1/16-note spacing).
            low_h  = _band(bass_mask, tau=18.0, sustain_frac=0.00,
                           min_gap_cols=max(4, int(round(sixteenth_cols * 1.05))),
                           onset_quantile=88.0).tolist(),
            mid_h  = _band(mid_mask,  tau=12.0, sustain_frac=0.35,
                           min_gap_cols=max(4, int(round(sixteenth_cols * 0.85))),
                           onset_quantile=75.0).tolist(),
            high_h = _band(high_mask, tau= 5.0, sustain_frac=0.00,
                           min_gap_cols=max(3, int(round(sixteenth_cols * 0.65))),
                           onset_quantile=75.0).tolist(),
            bpm=est_bpm,
        )

    # ── Deterministic synthetic waveform for testing ──────────────────
    @classmethod
    def synthetic(cls, columns: int = 4_500, bpm: float = 120.0,
                  seed: int = 42) -> "WaveformData":
        """
        Generate a musically-structured 3-band waveform that visually matches
        the rekordbox color-detail style: sharp leaf/wing shapes per beat,
        clear valleys between beats, sustained orange mid body.

        Default 4 500 columns ≈ 30 s at 150 col/s (Pioneer standard).
        Sections: intro → build → drop → breakdown → second drop → outro.

        Rendering contract (matches WaveformView/OverviewStrip):
            low_h  → blue  (bass): sharp kick spike, near-zero between beats
            mid_h  → orange (mid): kick-accented but with sustained body
            high_h → white (high): 8th-note hi-hat spikes, near-zero otherwise
        """
        rng = random.Random(seed)
        cols_per_beat = (150.0 * 60.0) / bpm   # 75.0 at 120 BPM

        low_h:  list[float] = []
        mid_h:  list[float] = []
        high_h: list[float] = []

        for i in range(columns):
            pos = i / columns

            # ── Section peak levels ───────────────────────────────────
            # These are the MAXIMUM the envelope can reach; the floor is
            # determined by the exponential decay between beats.
            if pos < 0.10:                          # intro
                pl, pm, ph = 0.40, 0.50, 0.60
            elif pos < 0.25:                        # build-up
                t  = (pos - 0.10) / 0.15
                pl = 0.40 + 0.55 * t * t
                pm = 0.50 + 0.45 * t
                ph = 0.60 + 0.35 * t
            elif pos < 0.60:                        # drop
                pl, pm, ph = 0.95, 0.82, 0.88
            elif pos < 0.70:                        # breakdown
                pl, pm, ph = 0.18, 0.72, 0.30
            elif pos < 0.85:                        # second drop
                pl, pm, ph = 0.98, 0.86, 0.92
            else:                                   # outro (fade)
                t  = (pos - 0.85) / 0.15
                pl = 0.98 * (1.0 - t)
                pm = 0.86 * (1.0 - t)
                ph = 0.92 * (1.0 - t)

            # ── Beat-aligned envelopes ────────────────────────────────
            # beat_phase 0 = onset of beat, 1 = onset of next beat
            beat_phase = (i % cols_per_beat) / cols_per_beat
            # hat_phase  0 = onset of 8th note (every half beat)
            hat_phase  = (i % (cols_per_beat / 2.0)) / (cols_per_beat / 2.0)

            # Exponential decay from onset → near-zero by ~40 % of beat
            kick_env = math.exp(-beat_phase * 10.0)
            hat_env  = math.exp(-hat_phase  * 14.0)

            # ── Bass: almost entirely kick-driven (creates clear valleys) ──
            l = pl * (0.04 + 0.96 * kick_env) + rng.uniform(-0.015, 0.02)

            # ── Mid: kick accent + sustained body (orange is always visible) ─
            # Floor = 25 % of peak so orange body fills the beat width,
            # then spikes higher on the kick.
            m = pm * (0.25 + 0.75 * max(kick_env * 0.90, hat_env * 0.25)) \
                + rng.uniform(-0.02, 0.03)

            # ── High: hat pattern only — fine spikes, near-zero elsewhere ──
            h = ph * (0.02 + 0.98 * hat_env) + rng.uniform(-0.01, 0.015)

            low_h.append( max(0.0, min(1.0, l)))
            mid_h.append( max(0.0, min(1.0, m)))
            high_h.append(max(0.0, min(1.0, h)))

        return cls(low_h=low_h, mid_h=mid_h, high_h=high_h, bpm=bpm)
