"""
QThread that hosts the asyncio event loop for all UDP network receivers
and the TCP metadata client.

Windows note
------------
Python's default asyncio event loop on Windows (ProactorEventLoop) does NOT
support UDP datagram protocols.  We force SelectorEventLoop on all platforms
so behaviour is identical on macOS and Windows 11.
"""
from __future__ import annotations
import asyncio
import logging
import sys

from PyQt6.QtCore import QThread

from core.network.discovery import DiscoveryReceiver
from core.network.status_receiver import StatusReceiver
from core.network.beat_receiver import BeatReceiver
from core.network.virtual_cdj import VirtualCDJAnnouncer

log = logging.getLogger(__name__)


class NetworkWorker(QThread):
    def __init__(self, event_bus, network_cfg, metadata_client=None) -> None:
        super().__init__()
        self._bus = event_bus
        self._cfg = network_cfg
        self._metadata_client = metadata_client
        self._virtual_cdj_number: int | None = getattr(network_cfg, "virtual_cdj_player", 5)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self.setObjectName("NetworkWorker")

    # ── QThread entry point ───────────────────────────────────────────
    def run(self) -> None:
        # Force SelectorEventLoop on Windows for UDP support
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception as exc:
            log.error("Network worker crashed: %s", exc, exc_info=True)
            self._bus.network_error.emit(str(exc))
        finally:
            self._loop.close()
            self._loop = None

    async def _main(self) -> None:
        self._stop_event = asyncio.Event()

        discovery = DiscoveryReceiver(self._bus, self._virtual_cdj_number)
        status    = StatusReceiver(self._bus)
        beat      = BeatReceiver(self._bus)

        tasks = [
            asyncio.create_task(discovery.listen(self._stop_event), name="discovery"),
            asyncio.create_task(status.listen(self._stop_event),    name="status"),
            asyncio.create_task(beat.listen(self._stop_event),      name="beat"),
        ]

        if self._virtual_cdj_number:
            announcer = VirtualCDJAnnouncer(self._virtual_cdj_number)
            tasks.append(asyncio.create_task(
                announcer.listen(self._stop_event), name="virtual-cdj"
            ))

        if self._metadata_client is not None:
            tasks.append(asyncio.create_task(
                self._metadata_client.run(self._stop_event), name="metadata"
            ))

        log.info("Network worker started — %d tasks active", len(tasks))
        await self._stop_event.wait()

        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("Network worker stopped cleanly")

    # ── Called from the Qt main thread ───────────────────────────────
    def stop(self) -> None:
        if self._loop is not None and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
