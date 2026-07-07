"""A tiny thread-safe TTL + LRU cache.

Used only for the passthrough session-token store (see docs/design-spec.md §4). Ephemeral and
in-memory by design — it holds no persistent state and is emptied on process restart.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from threading import Lock


class TTLCache:
    """Maps a key to a value with per-entry TTL and a bounded, LRU-evicted size."""

    def __init__(self, *, max_entries: int, ttl_seconds: float) -> None:
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._entries: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._lock = Lock()

    def get(self, key: str) -> str | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at < time.monotonic():
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return value

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self._entries[key] = (time.monotonic() + self._ttl_seconds, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def delete(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)
