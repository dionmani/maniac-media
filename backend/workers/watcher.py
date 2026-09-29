"""Event-driven download folder monitoring with watchdog.

Filesystem events are debounced (downloads produce bursts of writes) and translated into a
single ``download_folder_scan`` job plus an immediate ``download_monitor`` sync, so completed
downloads are picked up in seconds instead of waiting for the next poll. Polling remains as a
safety net for network shares where change notifications are unreliable.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from backend.core.settings import get_settings
from backend.workers.queue import job_queue

log = logging.getLogger("maniac.watcher")
DEBOUNCE_SECONDS = 10.0


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: "DownloadWatcher") -> None:
        self.watcher = watcher

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in ("created", "moved", "closed", "modified"):
            self.watcher.poke()


class DownloadWatcher:
    def __init__(self) -> None:
        self._observer: Observer | None = None
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self.path: str | None = None

    def start(self) -> None:
        settings = get_settings()
        if not settings.watch_download_folder:
            return
        path = Path(settings.download_folder)
        if not path.is_dir():
            log.warning("Download folder %s missing – watcher not started", path)
            return
        try:
            observer = Observer()
            observer.schedule(_Handler(self), str(path), recursive=True)
            observer.daemon = True
            observer.start()
            self._observer, self.path = observer, str(path)
            log.info("Watching download folder %s", path)
        except Exception as exc:  # e.g. inotify limits, unsupported network share
            log.warning("Filesystem watcher unavailable (%s); falling back to polling", exc)

    def stop(self) -> None:
        if self._timer:
            self._timer.cancel()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    def restart(self) -> None:
        self.stop()
        self.start()

    def poke(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(DEBOUNCE_SECONDS, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        try:
            job_queue.enqueue("download_monitor", trigger="event", dedupe_key="watcher")
            job_queue.enqueue("download_folder_scan", trigger="event", dedupe_key="watcher")
        except Exception as exc:
            log.warning("Watcher could not enqueue jobs: %s", exc)

    @property
    def active(self) -> bool:
        return self._observer is not None and self._observer.is_alive()


watcher = DownloadWatcher()
