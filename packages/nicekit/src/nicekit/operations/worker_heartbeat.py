"""Non-blocking Celery worker heartbeat writer started from worker heart signals."""

from __future__ import annotations

import asyncio
import logging
import threading

from nicekit.core.config import get_settings
from nicekit.core.db import get_session_factory
from nicekit.core.event_loop import use_selector_event_loop_on_windows
from nicekit.operations.runtime import record_service_heartbeat

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_stop_event = threading.Event()
_thread: threading.Thread | None = None

use_selector_event_loop_on_windows()


def _run() -> None:
    interval = get_settings().operations_heartbeat_interval_seconds
    while not _stop_event.is_set():
        try:
            asyncio.run(record_service_heartbeat(get_session_factory(), role="worker"))
        except Exception:
            logger.error(
                "Celery worker operational heartbeat write failed",
                extra={"failure_code": "worker_heartbeat_write_failed"},
            )
        _stop_event.wait(interval)


def ensure_worker_heartbeat_writer() -> None:
    """Start one daemon writer in the worker main process."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop_event.clear()
        _thread = threading.Thread(
            target=_run,
            name="nicekit-worker-heartbeat",
            daemon=True,
        )
        _thread.start()


def stop_worker_heartbeat_writer() -> None:
    _stop_event.set()


__all__ = ["ensure_worker_heartbeat_writer", "stop_worker_heartbeat_writer"]
