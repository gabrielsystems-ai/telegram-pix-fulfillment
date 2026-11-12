import asyncio
import logging


async def run_recovery_worker(recovery, stop, wake):
    while not stop.is_set():
        try:
            await asyncio.to_thread(recovery.sync_once, stop)
        except Exception:
            logging.getLogger(__name__).warning("Recovery processing unavailable; will retry")
        if stop.is_set():
            break
        try:
            await asyncio.wait_for(wake.wait(), timeout=60)
            wake.clear()
        except TimeoutError:
            pass
