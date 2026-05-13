"""
Full-track overview strip — the entire waveform compressed into a thin bar.

Performance strategy
────────────────────
The waveform (possibly 36 000+ columns for a 4-min track) is pre-rendered into
a QPixmap once on data-change and on resize.  paintEvent() just blits the
cached pixmap then draws the playhead — a O(1) GPU blit regardless of column
count.  set_position() requests a repaint of only the two thin vertical strips
that changed (old and new playhead position) rather than the full widget.

This keeps 6 simultaneous deck overviews at 8 Hz well within budget.
"""
from __future__ import annotations
from typing import Optional

import math
import numpy as np

from PyQt6.QtWidgets import QWidget
from PyQt6.QtGui import QPainter, QColor, QPixmap
from PyQt6.QtCore import Qt, QRect

from ui.theme import C_BG_WIDGET, C_BORDER
from core.analysis.waveform_data import WaveformData

_PLAYHEAD_COLOR = QColor(255, 255, 255, 220)
_CUE_COLOR      = QColor(255, 160, 0, 220)   # orange cue marker
_BG_COLOR       = QColor(C_BG_WIDGET)
_BORDER_COLOR   = QColor(C_BORDER)

# XDJ-like overview palette: mostly darker blue body with cyan breaks.
_BODY_BLUE  = (20, 60, 148)
_BREAK_CYAN = (28, 218, 255)
# Contrast multiplier applied to the color interpolation (>1 = more contrast).
_COLOR_CONTRAST = 3.0

# Overview draw height ratio for one-sided (half) waveform rendering.
_MAX_HALF_HEIGHT_RATIO = 0.90
# Log compression: ln(1 + k*x) / ln(1 + k).
# Higher k = more acoustic-style squashing; drops fill ceiling, breaks stay tall.
_LOG_K    = 150.0
# Gaussian smoothing applied before normalization (in columns).
# Higher = smoother envelope, fewer sharp spikes.
_SMOOTH_SIGMA = 1.0




class OverviewStrip(QWidget):
    """Fixed-height waveform overview with QPixmap cache."""

    HEIGHT = 34

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(self.HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self._data: Optional[WaveformData] = None
        self._position_frac: float = 0.0
        self._pixmap: Optional[QPixmap] = None
        self._pixmap_dirty: bool = False
        self._bass_alpha = 165
        self._mid_alpha = 165
        self._high_alpha = 145
        self._sausage_alpha = int((self._bass_alpha + self._mid_alpha + self._high_alpha) / 3)
        self._cue_frac: float | None = None

    def set_waveform(self, data: WaveformData) -> None:
        self._data = data
        self._pixmap_dirty = True
        self.update()

    def clear(self) -> None:
        self._data = None
        self._position_frac = 0.0
        self._pixmap = None
        self._pixmap_dirty = False
        self._cue_frac = None
        self.update()

    def set_cue_marker(self, frac: float | None) -> None:
        """Set the cue-point marker position as a fraction of the track duration."""
        new_frac = max(0.0, min(1.0, float(frac))) if frac is not None else None
        if new_frac == self._cue_frac:
            return
        self._cue_frac = new_frac
        self.update()

    def set_position(self, frac: float) -> None:
        new_frac = max(0.0, min(1.0, frac))
        if new_frac == self._position_frac:
            return
        w = self.width()
        old_px = int(self._position_frac * w)
        new_px = int(new_frac * w)
        self._position_frac = new_frac
        # Only repaint the thin strips around old and new playhead positions
        margin = 2
        self.update(QRect(old_px - margin, 0, margin * 2 + 2, self.height()))
        self.update(QRect(new_px - margin, 0, margin * 2 + 2, self.height()))

    def set_band_opacity(self, bass_alpha: int, mid_alpha: int, high_alpha: int) -> None:
        bass_alpha = max(0, min(255, int(bass_alpha)))
        mid_alpha = max(0, min(255, int(mid_alpha)))
        high_alpha = max(0, min(255, int(high_alpha)))
        if (bass_alpha == self._bass_alpha
                and mid_alpha == self._mid_alpha
                and high_alpha == self._high_alpha):
            return
        self._bass_alpha = bass_alpha
        self._mid_alpha = mid_alpha
        self._high_alpha = high_alpha
        self._sausage_alpha = int((self._bass_alpha + self._mid_alpha + self._high_alpha) / 3)
        self._pixmap_dirty = True
        self.update()

    # ── Resize ────────────────────────────────────────────────────────────────
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._pixmap_dirty = True

    # ── Paint ─────────────────────────────────────────────────────────
    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        w = self.width()
        h = self.height()

        # Rebuild pixmap if data or size changed
        if self._pixmap_dirty or self._pixmap is None:
            self._rebuild_pixmap(w, h)

        # Blit cached waveform
        if self._pixmap is not None:
            painter.drawPixmap(0, 0, self._pixmap)
        else:
            painter.fillRect(0, 0, w, h, _BG_COLOR)

        # Cue marker (orange) — drawn before playhead so playhead is on top
        if self._cue_frac is not None:
            cx = int(self._cue_frac * w)
            painter.setPen(_CUE_COLOR)
            painter.drawLine(cx, 0, cx, h)

        # Playhead (drawn fresh every frame — not baked into pixmap)
        px = int(self._position_frac * w)
        painter.setPen(_PLAYHEAD_COLOR)
        painter.drawLine(px, 0, px, h)

        painter.end()

    # ── Pixmap builder ────────────────────────────────────────────────────────
    def _rebuild_pixmap(self, w: int, h: int) -> None:
        """Pre-render the waveform into a QPixmap.  Called once per data/resize."""
        self._pixmap_dirty = False
        if w <= 0 or h <= 0:
            return

        pm = QPixmap(w, h)
        pm.fill(_BG_COLOR)
        p  = QPainter(pm)
        if self._data is not None:
            n = self._data.column_count
            if n > 0:
                levels, color_levels = self._build_levels()

                if n <= w:
                    # Fewer source columns than pixels — stretch each column.
                    col_w_f = w / n
                    for i in range(n):
                        x = int(i * col_w_f)
                        cw = max(1, int((i + 1) * col_w_f) - x)
                        self._draw_sausage_column(p, x, cw, h, float(levels[i]), float(color_levels[i]))
                else:
                    # One sampled source column per pixel.
                    idx = np.linspace(0, n - 1, w, dtype=np.int32)
                    pool = levels[idx]
                    cpool = color_levels[idx]
                    for px in range(w):
                        self._draw_sausage_column(p, px, 1, h, float(pool[px]), float(cpool[px]))

        # Border
        p.setPen(_BORDER_COLOR)
        p.drawRect(0, 0, w - 1, h - 1)
        p.end()
        self._pixmap = pm

    def _build_levels(self) -> tuple[np.ndarray, np.ndarray]:
        """Return (height_levels, color_levels): smoothed heights and raw colors, both 0..1."""
        assert self._data is not None

        if self._data.heights is not None:
            src = np.asarray(self._data.heights, dtype=np.float32)
        else:
            low = np.asarray(self._data.low_h, dtype=np.float32)
            mid = np.asarray(self._data.mid_h, dtype=np.float32)
            high = np.asarray(self._data.high_h, dtype=np.float32)
            # Bias toward bass for the hardware-like dark-blue body.
            src = np.clip(low * 0.72 + mid * 0.20 + high * 0.08, 0.0, 1.0)

        if src.size == 0:
            return src, src

        log_denom = math.log1p(_LOG_K)

        def _compress(arr: np.ndarray) -> np.ndarray:
            peak = float(arr.max())
            if peak > 0.0:
                arr = arr / peak
            return np.clip(np.log1p(arr * _LOG_K) / log_denom, 0.0, 1.0)

        # Color levels: compress raw values (no smoothing) for sharp color edges.
        color_levels = _compress(src.copy())

        # Height levels: Gaussian smooth first, then compress.
        if _SMOOTH_SIGMA > 0 and src.size > 1:
            radius = max(1, int(3 * _SMOOTH_SIGMA))
            x = np.arange(-radius, radius + 1, dtype=np.float32)
            kernel = np.exp(-0.5 * (x / _SMOOTH_SIGMA) ** 2)
            kernel /= kernel.sum()
            src = np.convolve(src, kernel, mode="same")
        height_levels = _compress(src)

        return height_levels, color_levels

    def _draw_sausage_column(self, p: QPainter, x: int, cw: int, full_h: int, level: float, color_level: float) -> None:
        """Draw one overview column as a one-sided (half) waveform."""
        baseline = full_h - 2
        max_half = max(2, int(full_h * _MAX_HALF_HEIGHT_RATIO))
        level = max(0.0, min(1.0, level))
        bar_h = max(1, int(level * max_half))

        # Apply contrast stretch: push t toward 0 or 1 before interpolating.
        t = max(0.0, min(1.0, 0.5 + (color_level - 0.5) * _COLOR_CONTRAST))
        r = int(_BREAK_CYAN[0] * (1.0 - t) + _BODY_BLUE[0] * t)
        g = int(_BREAK_CYAN[1] * (1.0 - t) + _BODY_BLUE[1] * t)
        b = int(_BREAK_CYAN[2] * (1.0 - t) + _BODY_BLUE[2] * t)

        top = max(1, baseline - bar_h)
        p.fillRect(x, top, cw, baseline - top + 1, QColor(r, g, b, self._sausage_alpha))
