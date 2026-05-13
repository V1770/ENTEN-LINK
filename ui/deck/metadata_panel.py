"""Metadata panel with compact full-track details from dbserver responses."""
from __future__ import annotations
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel
from PyQt6.QtGui import QFont, QPixmap
from PyQt6.QtCore import Qt

from ui.theme import C_BG_WIDGET, C_BORDER, C_TEXT, C_TEXT_DIM, C_ACCENT
from core.devices.player_state import PlayerState


class MetadataPanel(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._meta = None
        self._last_state: PlayerState | None = None
        self._show_track_text = True
        self._show_artwork = True
        self._artwork_pixmap: QPixmap | None = None
        self._build()

    def _build(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(8)
        self.setMinimumHeight(72)

        self._artwork = QLabel("NO ART")
        self._artwork.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._artwork.setFixedSize(58, 58)
        self._artwork.setWordWrap(True)
        self._artwork.setStyleSheet(
            f"background: {C_BG_WIDGET}; border: 1px solid {C_BORDER}; "
            f"border-radius: 4px; color: {C_TEXT_DIM}; font-size: 10px; font-weight: bold;"
        )

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)

        line_font = QFont()
        line_font.setPointSize(13)
        line_font.setBold(True)

        detail_font = QFont()
        detail_font.setPointSize(11)

        self._main = QLabel("No Track")
        self._main.setFont(line_font)
        self._main.setStyleSheet(f"color: {C_TEXT}; letter-spacing: 0.2px;")
        self._main.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)

        self._detail_a = QLabel("")
        self._detail_a.setFont(detail_font)
        self._detail_a.setStyleSheet(
            f"color: {C_TEXT}; font-size: 11px; font-weight: bold;"
        )
        self._detail_a.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)

        self._detail_b = QLabel("")
        self._detail_b.setStyleSheet(f"color: #a8b7c2; font-size: 10px;")
        self._detail_b.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)

        text_col.addWidget(self._main)
        text_col.addWidget(self._detail_a)
        text_col.addWidget(self._detail_b)

        layout.addWidget(self._artwork)
        layout.addLayout(text_col, 1)

    def update_state(self, state: PlayerState) -> None:
        self._last_state = state
        title = state.track_title or (self._meta.title if self._meta else "")
        artist = state.track_artist or (self._meta.artist if self._meta else "")
        if not self._show_track_text:
            self._main.setText("")
        elif artist and title:
            self._main.setText(f"{artist}  -  {title}")
        elif artist:
            self._main.setText(artist)
        elif title:
            self._main.setText(title)
        else:
            self._main.setText("No Track")

        meta = self._meta
        parts_a: list[str] = []
        parts_b: list[str] = []

        album = meta.album if meta else ""
        if self._show_track_text and album:
            parts_a.append(album)

        key = state.track_key or (meta.key if meta else "")
        if key:
            parts_a.append(key)

        genre = meta.genre if meta else ""
        if genre:
            parts_a.append(genre)

        color = meta.color if meta else ""
        if color:
            parts_a.append(color)

        rating = int(meta.rating) if meta else 0
        if rating > 0:
            parts_a.append('*' * max(0, min(5, rating)))

        audio_format = (meta.audio_format if meta else "")
        if audio_format:
            parts_a.append(audio_format)

        if meta and meta.artwork_available:
            parts_a.append("ART")

        duration_ms = state.track_duration_ms or (meta.duration_ms if meta else 0)
        if duration_ms > 0:
            m, s = divmod(duration_ms // 1000, 60)
            parts_b.append(f"{m}:{s:02d}")

        bpm = float(meta.bpm) if meta and meta.bpm > 0 else 0.0
        if bpm > 0:
            parts_b.append(f"{bpm:.2f}")

        date_added = meta.date_added if meta else ""
        if date_added:
            parts_b.append(date_added)

        if state.track_source_slot == 4:
            parts_b.append("REKORDBOX")
        elif state.track_source_slot == 1:
            parts_b.append("CD")
        elif state.track_source_slot == 3:
            parts_b.append("USB")
        elif state.track_source_slot == 2:
            parts_b.append("SD")

        comment = meta.comment if meta else ""
        if comment:
            clipped = comment if len(comment) <= 80 else (comment[:77] + "...")
            parts_b.append(clipped)

        self._detail_a.setText("  ·  ".join(parts_a))
        self._detail_b.setText("  ·  ".join(parts_b))
        self._refresh_artwork_placeholder()

    def update_from_metadata(self, meta) -> None:
        """Apply Phase-2 TrackMetadata and retain it between status updates."""
        previous_artwork_id = int(getattr(self._meta, "artwork_id", 0) or 0) if self._meta is not None else 0
        self._meta = meta
        current_artwork_id = int(getattr(meta, "artwork_id", 0) or 0)
        if current_artwork_id != previous_artwork_id:
            self._artwork_pixmap = None
        if not bool(getattr(meta, "artwork_available", False)):
            self._artwork_pixmap = None
        self._refresh_artwork_placeholder()

    def set_artwork_bytes(self, image_bytes: bytes | None) -> None:
        if not image_bytes:
            self._artwork_pixmap = None
            self._refresh_artwork_placeholder()
            return
        pix = QPixmap()
        if not pix.loadFromData(image_bytes):
            self._artwork_pixmap = None
            self._refresh_artwork_placeholder()
            return
        self._artwork_pixmap = pix
        self._refresh_artwork_placeholder()

    def set_show_track_text(self, enabled: bool) -> None:
        self._show_track_text = bool(enabled)
        if self._last_state is not None:
            self.update_state(self._last_state)
        else:
            self._main.setText("No Track" if self._show_track_text else "")

    def set_show_artwork(self, enabled: bool) -> None:
        self._show_artwork = bool(enabled)
        self._artwork.setVisible(self._show_artwork)

    def _refresh_artwork_placeholder(self) -> None:
        if self._artwork_pixmap is not None and not self._artwork_pixmap.isNull():
            self._artwork.setText("")
            self._artwork.setPixmap(
                self._artwork_pixmap.scaled(
                    self._artwork.width(),
                    self._artwork.height(),
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            self._artwork.setStyleSheet(
                f"background: {C_BG_WIDGET}; border: 1px solid {C_ACCENT}; border-radius: 4px;"
            )
            return

        self._artwork.setPixmap(QPixmap())
        has_art = bool(self._meta and getattr(self._meta, "artwork_available", False))
        if has_art:
            self._artwork.setText("ART")
            self._artwork.setStyleSheet(
                f"background: {C_BG_WIDGET}; border: 1px solid {C_ACCENT}; "
                f"border-radius: 4px; color: {C_ACCENT}; font-size: 12px; font-weight: bold;"
            )
        else:
            self._artwork.setText("NO ART")
            self._artwork.setStyleSheet(
                f"background: {C_BG_WIDGET}; border: 1px solid {C_BORDER}; "
                f"border-radius: 4px; color: {C_TEXT_DIM}; font-size: 10px; font-weight: bold;"
            )
