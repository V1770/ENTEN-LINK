"""
Pioneer DJ Link — Entry Point
Phase 3: Network monitoring + sync drift analysis UI
"""
import sys
import logging
import argparse
from copy import deepcopy
from PyQt6.QtWidgets import QApplication

from config import config, load_config
from core.event_bus import EventBus
from core.devices.device_manager import DeviceManager
from core.analysis.sync_monitor import SyncMonitor
from core.db.local_db import LocalDB
from core.network.metadata_client import MetadataClient
from core.network.network_worker import NetworkWorker
from ui.main_window import MainWindow
from ui.theme import apply_dark_theme

import tempfile, pathlib

_log_path = pathlib.Path(tempfile.gettempdir()) / "djlink.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),                      # terminal
        logging.FileHandler(_log_path, mode="w"),     # always-flushed file
    ],
)

log = logging.getLogger("app")


def main() -> None:
    parser = argparse.ArgumentParser(description="Pioneer DJ Link monitor")
    parser.add_argument(
        "-t", "--test",
        action="store_true",
        help="(Deprecated — Virtual CDJ is now always on.)  Kept for compatibility.",
    )
    parser.add_argument(
        "--no-vcdj",
        action="store_true",
        help="Run in passive mode without broadcasting as a Virtual CDJ.",
    )
    parser.add_argument(
        "--vp",
        type=int,
        default=None,
        choices=range(1, 17),
        metavar="1-16",
        help="Override the saved Virtual CDJ player number for this session.",
    )
    parser.add_argument(
        "--debug-net",
        action="store_true",
        help="Enable verbose network logging (discovery/status/beat/metadata)",
    )
    args, qt_args = parser.parse_known_args()

    load_config()

    app = QApplication([sys.argv[0]] + qt_args)
    app.setApplicationName(config.app_name)
    app.setApplicationVersion(config.version)
    app.setOrganizationName("Pioneer DJ Link")
    apply_dark_theme(app)

    bus = EventBus.instance()
    device_manager = DeviceManager(bus, config.network)
    _sync_monitor = SyncMonitor(bus)

    # Suppress pyrekordbox warning noise before touching the DB.
    logging.getLogger("pyrekordbox").setLevel(logging.ERROR)
    logging.getLogger("pyrekordbox.db6.database").setLevel(logging.ERROR)

    _local_db = LocalDB(bus)
    _local_db.open()

    if args.debug_net:
        logging.getLogger("core.network.discovery").setLevel(logging.DEBUG)
        logging.getLogger("core.network.status_receiver").setLevel(logging.DEBUG)
        logging.getLogger("core.network.beat_receiver").setLevel(logging.DEBUG)
        logging.getLogger("core.network.metadata_client").setLevel(logging.DEBUG)
        logging.getLogger("core.analysis.playhead_tracker").setLevel(logging.DEBUG)

    # Virtual CDJ is on by default — Pioneer dbserver/NFS exchanges only work
    # when we are visible on the DJ Link network as a real player.  Use the
    # saved value unless overridden on the command line, and allow --no-vcdj
    # to drop back into passive listen mode.
    saved_vp = int(getattr(config.network, "default_virtual_cdj_player", 5)) or 5
    if args.no_vcdj:
        config.network.virtual_cdj_player = 0
    elif args.vp is not None:
        config.network.virtual_cdj_player = args.vp
        config.network.default_virtual_cdj_player = args.vp
    else:
        config.network.virtual_cdj_player = saved_vp

    if config.network.virtual_cdj_player:
        logging.getLogger("core.network.virtual_cdj").setLevel(
            logging.DEBUG if args.debug_net else logging.INFO
        )

    # Holder so the settings dialog can stop/start the worker on VP change.
    state: dict[str, object] = {"worker": None, "metadata": None}

    def build_worker() -> NetworkWorker:
        vp = int(config.network.virtual_cdj_player or 0)
        metadata = MetadataClient(bus, vp)
        worker = NetworkWorker(bus, deepcopy(config.network), metadata)
        state["metadata"] = metadata
        state["worker"] = worker
        worker.start()
        log.info("NetworkWorker started (Virtual CDJ player=%s)", vp or "off")
        return worker

    def restart_network() -> None:
        old = state.get("worker")
        if isinstance(old, NetworkWorker):
            log.info("Restarting NetworkWorker (VP changed → %s)",
                     config.network.virtual_cdj_player or "off")
            old.stop()
            old.wait(3000)
        build_worker()

    build_worker()

    window = MainWindow(
        bus, device_manager, config, _local_db,
        restart_network=restart_network,
    )
    window.show()

    exit_code = app.exec()

    final_worker = state.get("worker")
    if isinstance(final_worker, NetworkWorker):
        final_worker.stop()
        final_worker.wait(3000)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
# Author: Vittorio Becker
