import asyncio
import logging
from threading import Event

from vip.groups import VIPAccess


async def run_group_worker(access: VIPAccess, stop: Event, wake: asyncio.Event):
    """Periodic wake-up only; all work, retries and concurrency ownership live in the DB."""
    while not stop.is_set():
        try:
            await asyncio.to_thread(access.sync_once, stop)
        except Exception:
            # Never log an update, invite URL, SQL parameters or Telegram method representation.
            logging.getLogger(__name__).warning("VIP synchronization unavailable; will retry")
        if stop.is_set():
            break
        try:
            await asyncio.wait_for(wake.wait(), timeout=access.settings.vip_sync_interval_seconds)
            wake.clear()
        except TimeoutError:
            pass
