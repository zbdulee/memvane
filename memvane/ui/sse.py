"""Server-sent events of the dashboard: a bounded hub of streams and a stat-based file watcher.

The hub holds at most ``MAX_SSE_CLIENTS`` streams; one more is refused (the
server answers ``429`` and keeps the open streams). ``activeClients`` and
``lastSseClosedAt`` drive the idle shutdown and the session-start auto-open.
A stream whose queue overflows is dropped rather than slowing the others.
The watcher compares ``(st_ino, st_size, st_mtime_ns)`` so a rotation, a
rename or a branch switch shows as a change.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import queue
import threading
import time

from memvane.ui.types import MAX_SSE_CLIENTS, SSE_EVENTS

QUEUE_LIMIT = 256


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def frame(event, data):
    """One SSE frame (bytes) for a known event name and a JSON value."""
    if event not in SSE_EVENTS:
        raise ValueError('ui_sse_event_unknown')
    body = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return f'event: {event}\ndata: {body}\n\n'.encode('utf-8')


class Client:
    """One open stream: frames queue here until the handler writes them."""

    def __init__(self):
        self.frames = queue.Queue(maxsize=QUEUE_LIMIT)
        self.closed = threading.Event()

    def push(self, data):
        try:
            self.frames.put_nowait(data)
            return True
        except queue.Full:
            self.closed.set()
            return False

    def next(self, timeout):
        """The next frame, or None after ``timeout`` seconds or once closed."""
        if self.closed.is_set() and self.frames.empty():
            return None
        try:
            return self.frames.get(timeout=timeout)
        except queue.Empty:
            return None


class SseHub:
    def __init__(self, limit=MAX_SSE_CLIENTS, clock=time.monotonic, wall=time.time):
        self._limit = limit
        self._clock = clock
        self._wall = wall
        self._clients = []
        self._lock = threading.Lock()
        self.last_closed_at = None       # wall seconds of the last stream close
        self.last_closed_mono = None     # monotonic seconds of the same close
        self.draining = None

    def register(self):
        """A new client, or None when the hub is full or draining."""
        with self._lock:
            if self.draining is not None or len(self._clients) >= self._limit:
                return None
            client = Client()
            self._clients.append(client)
            return client

    def unregister(self, client):
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)
                self.last_closed_at = self._wall()
                self.last_closed_mono = self._clock()
        client.closed.set()

    @property
    def active_clients(self):
        with self._lock:
            return len(self._clients)

    def broadcast(self, event, data):
        """Queue one frame on every stream; streams that overflow are dropped. Returns the reached count."""
        payload = frame(event, data)
        with self._lock:
            clients = list(self._clients)
        reached = 0
        for client in clients:
            if client.push(payload):
                reached += 1
            else:
                self.unregister(client)
        return reached

    def drain(self, reason):
        """Tell every stream the server is going away and refuse new ones."""
        with self._lock:
            self.draining = reason
            clients = list(self._clients)
        payload = frame('server_draining', {'reason': reason})
        for client in clients:
            client.push(payload)
            client.closed.set()

    def grace_active(self, grace_seconds):
        """A stream closed less than ``grace_seconds`` ago (a reload or a tab hand-over)."""
        last = self.last_closed_mono
        return last is not None and self._clock() - last < grace_seconds


def stat_key(path):
    """``(st_ino, st_size, st_mtime_ns)`` of ``path`` (no symlink followed), or None."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return (info.st_ino, info.st_size, info.st_mtime_ns)


class FileWatcher:
    """Reports the labels whose files changed since the previous poll."""

    def __init__(self):
        self._seen = {}

    def poll(self, watched):
        """``watched`` maps a file path to a label; returns the sorted labels of changed files."""
        changed = set()
        current = {}
        for path, label in watched.items():
            key = stat_key(path)
            current[path] = key
            if path in self._seen and self._seen[path] != key:
                changed.add(label)
        self._seen = current
        return sorted(changed)
