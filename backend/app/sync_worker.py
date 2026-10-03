import logging
import os
import signal
import threading
import uuid

from .config import get_settings
from .literature import LiteratureSync
from .worker import retire_worker, touch_worker

log = logging.getLogger(__name__)


def main():
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    sync = LiteratureSync() if settings.literature_site_url else None
    worker_id = f'sync-{os.getpid()}-{uuid.uuid4().hex[:12]}'
    shutdown = threading.Event()
    heartbeat_stop = threading.Event()

    def request_shutdown(signum, frame):
        log.info('Shutdown requested; finishing the current literature sync')
        shutdown.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    def heartbeat():
        while not heartbeat_stop.is_set():
            try:
                touch_worker(worker_id, component='sync')
            except Exception:
                log.exception('Sync heartbeat failed')
            heartbeat_stop.wait(min(30, max(1, settings.job_heartbeat_seconds)))

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        while not shutdown.is_set():
            if sync is not None:
                try:
                    log.info('Literature sync: %s', sync.run_once())
                except Exception:
                    log.exception('Literature sync failed; current knowledge remains available')
            shutdown.wait(max(60, settings.literature_sync_interval_seconds))
    finally:
        heartbeat_stop.set()
        thread.join(timeout=2)
        retire_worker(worker_id, 'sync')


if __name__ == '__main__':
    main()
