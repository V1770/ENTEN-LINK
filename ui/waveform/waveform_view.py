"""
Scrolling waveform detail view — zoomed window centred on the current position.

Rendering
─────────
    Symmetric around the horizontal centre line (matches CDJ hardware display).
    Each source column is rendered directly as vertical bars for bass/mid/high,
    preserving the raw per-column deck data with no smoothing interpolation.

        Band / color mapping:
            bass  -> dark blue
            mid   -> dark cyan
            high  -> near-white (cool blue tint)
"""
from __future__ import annotations
import bisect
from typing import Optional

import numpy as np

from PyQt6.QtWidgets import QWidget, QSizePolicy
from PyQt6.QtGui import QPainter, QColor, QPen, QPixmap, QFont
from PyQt6.QtCore import Qt, QRect, QRectF

from ui.theme import C_BG, C_TEXT, C_TEXT_DIM, C_BORDER
from core.analysis.waveform_data import WaveformData

# ── Colours ───────────────────────────────────────────────────────────────────
_BG_COLOR       = QColor(C_BG)
_SILENCE_COLOR  = QColor(10, 10, 10)
_PLAYHEAD_COLOR = QColor(255, 255, 255, 220)
_CUE_COLOR      = QColor(255, 160, 0, 220)   # orange cue marker
_CENTER_LINE    = QColor(C_BORDER)
_NO_DATA_COLOR  = QColor(C_TEXT_DIM)
_GRID_Q_COLOR   = QColor(145, 145, 145, 210)
_GRID_BAR_COLOR = QColor(235, 64, 64, 235)
_GRID_TEXT      = QColor(230, 230, 230, 210)

# 3-band palette tuned to dark blue -> dark cyan -> cool near-white.
_BASS_RGB = (18, 58, 122)     # dark blue  — bass / low
_MID_RGB  = (22, 108, 126)    # dark cyan  — mid
_HIGH_RGB = (255, 255, 255)   # pure white — high / transients

# Match hardware visual headroom: keep peaks below full widget extent.
# Higher fill reduces unused vertical space around the waveform body.
_VERTICAL_SCALE = 0.98
_WAVEFORM_GAIN  = 2.0   # multiply raw levels before clamping to boost visual height
# Gaussian smoothing applied to the detail waveform height arrays on load.
# 0 = off; higher = smoother envelope (in waveform columns, ~6.7 ms each).
_DETAIL_SMOOTH_SIGMA = 0.0
_LEVEL_W_LOW = 0.58
_LEVEL_W_MID = 0.30
_LEVEL_W_HIGH = 0.12

# Supported detail zoom levels (total bars visible, centered on playhead).
_ZOOM_TOTAL_BARS = (2, 4, 8, 16, 32)
_DEFAULT_ZOOM_TOTAL_BARS = 4

_FALLBACK_COLS_PER_BAR = 300  # 150 col/s × 2 s/bar at 120 BPM
_PHASE_OFFSET_COLS = 0.0
_GRID_MIN_Q_SPACING_PX = 2

# Pre-rendered full-track pixmap: 1 px per waveform column.
# Tracks up to _MAX_CACHE_W cols get cached; longer tracks fall back to per-frame.
_CACHE_COL_PX = 1
_MAX_CACHE_W  = 12_000   # ~80 s at 150 col/s; avoids multi-second cache builds on long tracks


class WaveformView(QWidget):
    """Scrolling, zoomed waveform centred on the current position."""

    MIN_HEIGHT = 46

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(self.MIN_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self._data: Optional[WaveformData] = None
        self._position_frac: float = 0.0
        self._bass_alpha = 165
        self._mid_alpha  = 165
        self._high_alpha = 255
        self._bass_color = QColor(*_BASS_RGB, self._bass_alpha)
        self._mid_color  = QColor(*_MID_RGB,  self._mid_alpha)
        self._high_color = QColor(*_HIGH_RGB, self._high_alpha)
        self._cache: Optional[QPixmap] = None   # pre-rendered full-track pixmap
        self._bpm: float = 0.0
        self._position_ms: int = 0
        self._beat_number: int = 0
        self._duration_ms: int = 0
        self._waveform_time_offset_ms: int = 0
        self._beat_times_ms: tuple[int, ...] = ()
        self._beat_within_bar: tuple[int, ...] = ()
        self._zoom_total_bars: int = _DEFAULT_ZOOM_TOTAL_BARS
        self._cue_frac: float | None = None

    # ── Public API ────────────────────────────────────────────────────

    def set_waveform(self, data: WaveformData) -> None:
        self._data = self._smooth_detail(data)
        self._rebuild_cache()
        self.update()

    @staticmethod
    def _smooth_detail(data: WaveformData) -> WaveformData:
        """Return a WaveformData with height arrays Gaussian-smoothed."""
        if _DETAIL_SMOOTH_SIGMA <= 0 or data.column_count < 2:
            return data
        radius = max(1, int(3 * _DETAIL_SMOOTH_SIGMA))
        x = np.arange(-radius, radius + 1, dtype=np.float32)
        kernel = np.exp(-0.5 * (x / _DETAIL_SMOOTH_SIGMA) ** 2)
        kernel /= kernel.sum()

        def _smooth(arr):
            if arr is None:
                return None
            a = np.convolve(np.asarray(arr, dtype=np.float32), kernel, mode="same")
            return type(arr)(a.tolist()) if not isinstance(arr, np.ndarray) else a

        return WaveformData(
            low_h=_smooth(data.low_h),
            mid_h=_smooth(data.mid_h),
            high_h=_smooth(data.high_h),
            bpm=data.bpm,
            raw_colors=data.raw_colors,  # color per-column bytes — leave unsmoothed
            heights=_smooth(data.heights),
        )

    def clear(self) -> None:
        self._data = None
        self._cache = None
        self._position_frac = 0.0
        self._cue_frac = None
        self.update()

    def set_position(self, frac: float) -> None:
        new_frac = max(0.0, min(1.0, frac))
        if abs(new_frac - self._position_frac) < 1e-6:
            return
        self._position_frac = new_frac
        self.update()

    def set_transport(self, bpm: float, position_ms: int, beat_number: int, duration_ms: int = 0) -> None:
        """Provide transport timing so the quarter-note grid can be drawn."""
        self._bpm = max(0.0, float(bpm))
        self._position_ms = max(0, int(position_ms))
        self._beat_number = max(0, int(beat_number))
        self._duration_ms = max(0, int(duration_ms))

    def set_waveform_time_offset_ms(self, offset_ms: int) -> None:
        """Set a visual time offset for trimmed leading waveform columns."""
        self._waveform_time_offset_ms = max(0, int(offset_ms))

    def set_band_opacity(self, bass_alpha: int, mid_alpha: int, high_alpha: int) -> None:
        bass_alpha = max(0, min(255, int(bass_alpha)))
        mid_alpha  = max(0, min(255, int(mid_alpha)))
        high_alpha = max(0, min(255, int(high_alpha)))
        if (bass_alpha == self._bass_alpha
                and mid_alpha == self._mid_alpha
                and high_alpha == self._high_alpha):
            return
        self._bass_alpha = bass_alpha
        self._mid_alpha  = mid_alpha
        self._high_alpha = high_alpha
        self._bass_color = QColor(*_BASS_RGB, self._bass_alpha)
        self._mid_color  = QColor(*_MID_RGB,  self._mid_alpha)
        self._high_color = QColor(*_HIGH_RGB, self._high_alpha)
        self._rebuild_cache()
        self.update()

    def set_zoom_total_bars(self, total_bars: int) -> None:
        """Set detail window width in musical bars (2, 4, 8, 16, 32)."""
        if total_bars not in _ZOOM_TOTAL_BARS:
            # Snap invalid values to nearest supported level.
            total_bars = min(_ZOOM_TOTAL_BARS, key=lambda v: abs(v - int(total_bars)))
        if total_bars == self._zoom_total_bars:
            return
        self._zoom_total_bars = int(total_bars)
        self.update()

    def set_beat_grid(self, beat_times_ms: tuple[int, ...], beat_within_bar: tuple[int, ...] = ()) -> None:
        """Provide absolute beat start times (ms) and optional within-bar markers."""
        # Preserve negative values (beats before track start); clamping to 0 causes
        # multiple pre-track beats to pile up at 00:00 and draws a phantom bar marker.
        self._beat_times_ms = tuple(int(t) for t in beat_times_ms)
        self._beat_within_bar = tuple(int(v) for v in beat_within_bar)
        self.update()

    def clear_beat_grid(self) -> None:
        self._beat_times_ms = ()
        self._beat_within_bar = ()
        self.update()

    def set_cue_marker(self, frac: float | None) -> None:
        """Set the cue-point marker position as a fraction of the waveform duration."""
        new_frac = max(0.0, min(1.0, float(frac))) if frac is not None else None
        if new_frac == self._cue_frac:
            return
        self._cue_frac = new_frac
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._rebuild_cache()

    # ── Cache ─────────────────────────────────────────────────────────

    def _rebuild_cache(self) -> None:
        """Pre-render the full waveform to an offscreen QPixmap.

        One column of audio data → _CACHE_COL_PX pixels wide.  The pixmap
        height matches the current widget height so paintEvent can blit
        without any scaling overhead.  If the track is too long to fit
        within _MAX_CACHE_W pixels the cache is left as None and paintEvent
        falls back to per-frame path rendering.
        """
        if self._data is None:
            self._cache = None
            return

        n = self._data.column_count
        total_w = n * _CACHE_COL_PX
        if total_w > _MAX_CACHE_W:
            self._cache = None
            return

        h      = max(self.height(), self.MIN_HEIGHT)
        half_h = h // 2
        scaled_half_h = max(1, int(half_h * _VERTICAL_SCALE))
        cw     = float(_CACHE_COL_PX)

        pix = QPixmap(total_w, h)
        pix.fill(_BG_COLOR)

        p = QPainter(pix)

        # Draw raw source columns directly (no interpolation between columns).
        if self._data.raw_colors is not None:
            # True-color mode: single centered bar per column using hardware RGB.
            for ci in range(n):
                x = int(ci * cw)
                w_col = max(1, int((ci + 1) * cw) - x)
                h_level = self._compress_visual_level(self._data.heights[ci])
                bar_h = min(scaled_half_h, int(h_level * scaled_half_h))
                if bar_h > 0:
                    r, g, b = self._data.raw_colors[ci]
                    p.fillRect(x, half_h - bar_h, w_col, bar_h * 2, QColor(r, g, b))
        else:
            for ci in range(n):
                x = int(ci * cw)
                w_col = max(1, int((ci + 1) * cw) - x)
                low = self._data.low_h[ci]
                mid = self._data.mid_h[ci]
                high = self._data.high_h[ci]
                bar_h = min(
                    scaled_half_h,
                    int(self._compress_visual_level(self._column_level(low, mid, high)) * scaled_half_h),
                )
                if bar_h > 0:
                    p.fillRect(x, half_h - bar_h, w_col, bar_h * 2, self._mix_band_color(low, mid, high))

        # Bake centre line into the cache (avoids an extra draw call per frame)
        p.setPen(QPen(_CENTER_LINE, 1))
        p.drawLine(0, half_h, total_w, half_h)

        p.end()
        self._cache = pix

    # ── Paint ─────────────────────────────────────────────────────────

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        # Keep overlay strokes pixel-stable frame-to-frame.
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, False)
        w = self.width()
        h = self.height()

        painter.fillRect(0, 0, w, h, _BG_COLOR)

        if self._data is None:
            painter.setPen(_NO_DATA_COLOR)
            painter.drawText(0, 0, w, h, Qt.AlignmentFlag.AlignCenter,
                             "Awaiting waveform data…")
            painter.end()
            return

        n      = self._data.column_count
        center = self._position_frac * n
        visible_cols = self._visible_cols(n)
        half   = visible_cols / 2.0

        if self._cache is not None and self._cache.height() == h:
            self._blit_cache(painter, w, h, n, center, half, visible_cols)
        else:
            self._paint_direct(painter, w, h, n, center, half, visible_cols)

        self._draw_grid(painter, w, h, n, center, half, visible_cols)
        self._draw_time_labels(painter, w, h)

        # Playhead drawn on top in both code paths
        playhead_offset_px = (_PHASE_OFFSET_COLS / max(1, visible_cols)) * w
        playhead_x = int(round((w / 2.0) + playhead_offset_px))
        playhead_x = max(0, min(w - 1, playhead_x))
        painter.setPen(QPen(_PLAYHEAD_COLOR, 2))
        painter.drawLine(playhead_x, self._top_label_height(), playhead_x, h)

        # Cue marker — orange vertical line offset from playhead by cue delta
        if self._cue_frac is not None and n > 0:
            cue_col = self._cue_frac * n
            cue_delta_cols = cue_col - center
            cue_px = int(round(w / 2.0 + (cue_delta_cols / max(1, visible_cols)) * w))
            if 0 <= cue_px < w:
                painter.setPen(QPen(_CUE_COLOR, 1))
                painter.drawLine(cue_px, self._top_label_height(), cue_px, h)

        painter.end()

    def _blit_cache(self, painter: QPainter, w: int, h: int,
                    n: int, center: float, half: float, visible_cols: int) -> None:
        """Fast path: one drawPixmap sub-rect blit, no path work per frame."""
        cache = self._cache
        src_x = (center - half) * _CACHE_COL_PX   # may be negative
        src_w = float(visible_cols * _CACHE_COL_PX)

        clamp_l = max(src_x, 0.0)
        clamp_r = min(src_x + src_w, float(cache.width()))

        if clamp_l >= clamp_r:
            painter.fillRect(0, 0, w, h, _SILENCE_COLOR)
            return

        # Left silence (before track start)
        left_sil = max(0, -src_x)
        dst_sil_l = round(left_sil / src_w * w)
        if dst_sil_l > 0:
            painter.fillRect(0, 0, dst_sil_l, h, _SILENCE_COLOR)

        # Right silence (after track end)
        right_sil = max(0, (src_x + src_w) - cache.width())
        dst_sil_r = round(right_sil / src_w * w)
        if dst_sil_r > 0:
            painter.fillRect(w - dst_sil_r, 0, dst_sil_r, h, _SILENCE_COLOR)

        # Track waveform
        dst_tw = w - dst_sil_l - dst_sil_r
        if dst_tw > 0:
            src_data_w = clamp_r - clamp_l
            # Disable filtering to avoid temporal shimmer while scrolling.
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            painter.drawPixmap(
                QRectF(float(dst_sil_l), 0.0, float(dst_tw), float(h)),
                cache,
                QRectF(clamp_l, 0.0, src_data_w, float(h)),
            )

    def _paint_direct(self, painter: QPainter, w: int, h: int,
                      n: int, center: float, half: float, visible_cols: int) -> None:
        """Slow path for tracks too long to cache: per-frame raw bar rendering."""
        half_h  = h // 2
        scaled_half_h = max(1, int(half_h * _VERTICAL_SCALE))
        start_src = center - half
        use_color = self._data.raw_colors is not None

        for x in range(w):
            src = int(start_src + (((x + 0.5) / max(1, w)) * visible_cols))
            if not (0 <= src < n):
                painter.fillRect(x, 0, 1, h, _SILENCE_COLOR)
                continue

            if use_color:
                h_level = self._compress_visual_level(self._data.heights[src])
                bar_h = min(scaled_half_h, int(h_level * scaled_half_h))
                if bar_h > 0:
                    r, g, b = self._data.raw_colors[src]
                    painter.fillRect(x, half_h - bar_h, 1, bar_h * 2, QColor(r, g, b))
            else:
                low = self._data.low_h[src]
                mid = self._data.mid_h[src]
                high = self._data.high_h[src]
                bar_h = min(
                    scaled_half_h,
                    int(self._compress_visual_level(self._column_level(low, mid, high)) * scaled_half_h),
                )
                if bar_h > 0:
                    painter.fillRect(x, half_h - bar_h, 1, bar_h * 2, self._mix_band_color(low, mid, high))

        painter.setPen(QPen(_CENTER_LINE, 1))
        painter.drawLine(0, half_h, w, half_h)

    def _draw_grid(self, painter: QPainter, w: int, h: int,
                   n: int, center: float, half: float, visible_cols: int) -> None:
        """Draw quarter-note grid with highlighted bar starts and bar numbers."""
        if self._data is None or n <= 0 or self._duration_ms <= 0:
            return

        # Preferred path: draw from real beat-grid timestamps to align with deck.
        if self._beat_times_ms:
            cols_per_ms = n / float(self._duration_ms)
            start_src = center - half
            max_src = start_src + visible_cols
            grid_offset_px = (_PHASE_OFFSET_COLS / max(1, visible_cols)) * w
            waveform_offset_ms = float(self._waveform_time_offset_ms)
            min_ms = (start_src / cols_per_ms) + waveform_offset_ms
            max_ms = (max_src / cols_per_ms) + waveform_offset_ms

            i0 = max(0, bisect.bisect_left(self._beat_times_ms, int(min_ms)) - 1)
            i1 = min(len(self._beat_times_ms), bisect.bisect_right(self._beat_times_ms, int(max_ms)) + 1)

            # At wide windows (e.g. 32 bars), beat lines can become denser than
            # a few pixels and shimmer during scrolling. Thin quarter-beat lines
            # adaptively while always keeping bar markers.
            beats_visible = max(1, i1 - i0)
            beat_px_est = w / float(beats_visible)
            if beat_px_est < 1.8:
                quarter_step = 4
            elif beat_px_est < 3.0:
                quarter_step = 2
            else:
                quarter_step = 1

            top_q_h = max(5, min(9, h // 10))
            top_bar_h = max(8, min(14, h // 7))
            painter.setPen(QPen(_GRID_Q_COLOR, 1))
            last_q_x = -10_000
            last_bar_x = -10_000

            for i in range(i0, i1):
                beat_ms = self._beat_times_ms[i]
                # Do not draw grid markers before track start.
                if beat_ms < 0:
                    continue
                # Do NOT clamp to 0 — let negative columns be filtered by x < -1 below.
                beat_col = (beat_ms - waveform_offset_ms) * cols_per_ms
                x = ((beat_col - start_src) / max(1, visible_cols)) * w + grid_offset_px
                if x < -1 or x > (w + 1):
                    continue

                # Prefer rekordbox beat-within-bar markers when available.
                if i < len(self._beat_within_bar) and 1 <= self._beat_within_bar[i] <= 4:
                    is_bar = (self._beat_within_bar[i] == 1)
                else:
                    # Fallback: every 4th beat from the first grid entry.
                    is_bar = (i % 4) == 0

                # Anchor thinning to absolute beat index, not i0-relative index.
                # Using (i - i0) causes visible phase-jumps when i0 shifts by 1
                # while scrolling, which looks like flicker.
                if (not is_bar) and quarter_step > 1 and (i % quarter_step != 0):
                    continue

                xi = int(round(x))
                if is_bar:
                    if xi - last_bar_x < 1:
                        continue
                    painter.setPen(QPen(_GRID_BAR_COLOR, 2.0))
                    painter.drawLine(xi, 0, xi, top_bar_h)
                    painter.setPen(QPen(_GRID_Q_COLOR, 1))
                    last_bar_x = xi
                else:
                    if xi - last_q_x < _GRID_MIN_Q_SPACING_PX:
                        continue
                    painter.drawLine(xi, 0, xi, top_q_h)
                    last_q_x = xi
            return

        # Fallback path when beat grid is unavailable.
        if self._bpm <= 0.0:
            return

        beat_ms = 60_000.0 / self._bpm
        cols_per_ms = n / float(self._duration_ms)
        beat_cols = beat_ms * cols_per_ms
        beat_px = beat_cols * (w / max(1, visible_cols))
        grid_offset_px = (_PHASE_OFFSET_COLS / max(1, visible_cols)) * w
        if beat_px < 9.0:
            return

        if beat_px < 1.8:
            quarter_step = 4
        elif beat_px < 3.0:
            quarter_step = 2
        else:
            quarter_step = 1

        visual_pos_ms = max(0, self._position_ms - self._waveform_time_offset_ms)
        beat_idx_at_or_before = int(visual_pos_ms // beat_ms) + 1
        phase_ms = visual_pos_ms - int((beat_idx_at_or_before - 1) * beat_ms)
        x0 = (w / 2.0) - ((phase_ms / beat_ms) * beat_px) + grid_offset_px

        start_k = int((-x0) // beat_px) - 1
        end_k = int((w - x0) // beat_px) + 1
        top_q_h = max(5, min(9, h // 10))
        top_bar_h = max(8, min(14, h // 7))
        painter.setPen(QPen(_GRID_Q_COLOR, 1))
        last_q_x = -10_000
        last_bar_x = -10_000
        for k in range(start_k, end_k + 1):
            x = x0 + k * beat_px
            if x < -1 or x > (w + 1):
                continue
            beat_idx = beat_idx_at_or_before + k
            if beat_idx <= 0:
                continue

            is_bar = ((beat_idx - 1) % 4) == 0
            if (not is_bar) and quarter_step > 1 and ((beat_idx - 1) % quarter_step != 0):
                continue
            xi = int(round(x))
            if is_bar:
                if xi - last_bar_x < 1:
                    continue
                painter.setPen(QPen(_GRID_BAR_COLOR, 2.0))
                painter.drawLine(xi, 0, xi, top_bar_h)
                painter.setPen(QPen(_GRID_Q_COLOR, 1))
                last_bar_x = xi
            else:
                if xi - last_q_x < _GRID_MIN_Q_SPACING_PX:
                    continue
                painter.drawLine(xi, 0, xi, top_q_h)
                last_q_x = xi

    def _visible_cols(self, n_cols: int) -> int:
        """Compute visible source columns from zoom bars and current transport data."""
        if n_cols <= 0:
            return _DEFAULT_ZOOM_TOTAL_BARS * _FALLBACK_COLS_PER_BAR

        # Prefer local beat-grid spacing for bar-accurate zoom at tempo changes.
        if self._beat_times_ms and self._duration_ms > 0:
            beat_idx = bisect.bisect_right(self._beat_times_ms, self._position_ms)
            if 1 <= beat_idx < len(self._beat_times_ms):
                local_beat_ms = max(1, self._beat_times_ms[beat_idx] - self._beat_times_ms[beat_idx - 1])
                cols_per_ms = n_cols / float(self._duration_ms)
                cols_per_bar = 4.0 * local_beat_ms * cols_per_ms
                visible = int(round(self._zoom_total_bars * cols_per_bar))
                return max(60, visible)

        # Preferred path: derive beat spacing from current BPM and known duration.
        if self._bpm > 0.0 and self._duration_ms > 0:
            beat_ms = 60_000.0 / self._bpm
            cols_per_ms = n_cols / float(self._duration_ms)
            cols_per_bar = 4.0 * beat_ms * cols_per_ms
            visible = int(round(self._zoom_total_bars * cols_per_bar))
        else:
            # Fallback when transport is not yet established.
            visible = self._zoom_total_bars * _FALLBACK_COLS_PER_BAR

        return max(60, visible)

    def _compress_visual_level(self, level: float) -> float:
        # Keep waveform shape unaltered: no noise gate or dynamic compression.
        return max(0.0, min(1.0, float(level) * _WAVEFORM_GAIN))

    def _column_level(self, low: float, mid: float, high: float) -> float:
        """Return a tighter single-height envelope from weighted band energy."""
        l = max(0.0, float(low))
        m = max(0.0, float(mid))
        h = max(0.0, float(high))
        return max(0.0, min(1.0, (l * _LEVEL_W_LOW) + (m * _LEVEL_W_MID) + (h * _LEVEL_W_HIGH)))

    def _draw_time_labels(self, painter: QPainter, w: int, h: int) -> None:
        if self._duration_ms <= 0:
            return
        font = QFont()
        font.setPointSize(10)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(C_TEXT))

        current = self._format_time_ms(self._position_ms)
        remaining = f"-{self._format_time_ms(max(0, self._duration_ms - self._position_ms))}"

        playhead_x = int(round((w / 2.0) + ((_PHASE_OFFSET_COLS / max(1, self._visible_cols(max(1, self._data.column_count if self._data else 1)))) * w)))
        current_rect = QRect(max(0, playhead_x - 54), 0, 108, 14)
        painter.drawText(current_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, current)

        right_rect = QRect(max(0, w - 112), 0, 108, 14)
        painter.drawText(right_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop, remaining)

    def _top_label_height(self) -> int:
        return 16 if self._duration_ms > 0 else 0

    @staticmethod
    def _format_time_ms(total_ms: int) -> str:
        ms = max(0, int(total_ms))
        total_s, milli = divmod(ms, 1000)
        minutes, seconds = divmod(total_s, 60)
        return f"{minutes:02d}:{seconds:02d}.{milli:03d}"

    def _mix_band_color(self, low: float, mid: float, high: float) -> QColor:
        """Return a single color blended from low/mid/high energy proportions."""
        l = max(0.0, float(low))
        m = max(0.0, float(mid))
        h = max(0.0, float(high))
        total = l + m + h
        if total <= 1e-9:
            return self._bass_color

        r = int(round((_BASS_RGB[0] * l + _MID_RGB[0] * m + _HIGH_RGB[0] * h) / total))
        g = int(round((_BASS_RGB[1] * l + _MID_RGB[1] * m + _HIGH_RGB[1] * h) / total))
        b = int(round((_BASS_RGB[2] * l + _MID_RGB[2] * m + _HIGH_RGB[2] * h) / total))
        a = int(round((self._bass_alpha * l + self._mid_alpha * m + self._high_alpha * h) / total))
        return QColor(r, g, b, max(0, min(255, a)))
