"""Application configuration — typed dataclasses with lightweight JSON persistence."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path

_log = logging.getLogger(__name__)


@dataclass
class NetworkConfig:
    announce_port: int = 50000
    beat_port: int = 50001
    status_port: int = 50002
    listen_address: str = "0.0.0.0"
    device_timeout_seconds: float = 5.0
    # Runtime value used by NetworkWorker. 0 = disabled/passive.
    virtual_cdj_player: int = 0
    # Default player number used when the app is launched with -t and --vp is omitted.
    # Use slot 6 to avoid colliding with real CDJ decks (slots 1-4) or a 5th CDJ.
    default_virtual_cdj_player: int = 6


@dataclass
class UIConfig:
    # Slots 1-4 = physical CDJs, slots 5-6 = rekordbox / software players
    max_player_slots: int = 6
    hidden_slots: list[int] = field(default_factory=list)
    # 0 = auto-fit columns based on window width, otherwise fixed column count.
    grid_columns: int = 0
    # When enabled, offline decks are not placed in the grid to save screen space.
    hide_offline_players: bool = True
    update_fps: int = 30
    dark_mode: bool = True
    beat_flash_ms: int = 80
    # Waveform band opacity (0-255): bass below, mid middle, high on top.
    waveform_bass_alpha: int = 165
    waveform_mid_alpha: int = 165
    waveform_high_alpha: int = 145
    # Detail waveform zoom window (total bars visible, centered on playhead).
    # Supported UI values: 2, 4, 8, 16, 32.
    waveform_detail_total_bars: int = 4
    # Show Artist-Track line and album in metadata panel.
    show_track_text: bool = False


@dataclass
class AppConfig:
    network: NetworkConfig = field(default_factory=NetworkConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    app_name: str = "Pioneer DJ Link"
    version: str = "0.3.0-phase3"
    # Author: Vittorio Becker


config = AppConfig()


def _config_path() -> Path:
    import sys, os
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "Pioneer DJ Link" / "settings.json"


def _apply_dataclass_updates(target, values: dict) -> None:
    valid_fields = {f.name for f in fields(target)}
    for key, value in values.items():
        if key not in valid_fields:
            continue
        current = getattr(target, key)
        if is_dataclass(current) and isinstance(value, dict):
            _apply_dataclass_updates(current, value)
        else:
            setattr(target, key, value)


def load_config() -> AppConfig:
    path = _config_path()
    _log.info("Config path: %s  (exists=%s)", path, path.exists())
    if not path.exists():
        _log.info("No saved settings — using defaults (virtual_cdj_player default=%s)",
                  config.network.default_virtual_cdj_player)
        return config

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("Could not read config file: %s", exc)
        return config

    if isinstance(data, dict):
        _apply_dataclass_updates(config, data)
    _log.info("Loaded settings: virtual_cdj_player=%s  default_virtual_cdj_player=%s",
              config.network.virtual_cdj_player,
              config.network.default_virtual_cdj_player)
    return config


def save_config() -> Path:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), indent=2, sort_keys=True), encoding="utf-8")
    _log.info("Saved settings to %s (virtual_cdj_player=%s  default_virtual_cdj_player=%s)",
              path, config.network.virtual_cdj_player, config.network.default_virtual_cdj_player)
    return path
