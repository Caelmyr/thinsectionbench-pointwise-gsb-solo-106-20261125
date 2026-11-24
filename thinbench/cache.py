"""A small byte-accounted LRU for immutable source files and generated reports."""
from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass

from petro.raster import normalized_array


@dataclass(frozen=True)
class Entry:
    item: object
    size: int


class MemoryCache:
    def __init__(self, byte_budget, max_items):
        self.byte_budget = byte_budget
        self.max_items = max_items
        self.entries = OrderedDict()
        self.bytes = 0
        self.lock = threading.RLock()

    def _remove(self, key):
        previous = self.entries.pop(key, None)
        if previous:
            self.bytes -= previous.size
        return previous

    def get(self, key):
        with self.lock:
            entry = self.entries.get(key)
            if entry is None:
                return None
            self.entries.move_to_end(key)
            return entry.item

    def put(self, key, value, size):
        if size > self.byte_budget:
            return value
        with self.lock:
            already = self.entries.get(key)
            if already is not None:
                self.entries.move_to_end(key)
                return already.item
            self.entries[key] = Entry(value, size)
            self.bytes += size
            while self.bytes > self.byte_budget or len(self.entries) > self.max_items:
                oldest = next(iter(self.entries))
                self._remove(oldest)
            return value

    def remove_prefix(self, prefix):
        with self.lock:
            for key in tuple(self.entries):
                if key[:len(prefix)] == prefix:
                    self._remove(key)

    def statistics(self):
        with self.lock:
            return {
                "items": len(self.entries),
                "bytes": self.bytes,
                "budget": self.byte_budget,
            }


class SourceCache(MemoryCache):
    def __init__(self):
        super().__init__(120 * 1024 * 1024, 14)

    def array(self, study_id, item, path):
        key = ("source", study_id, item["id"], item["sha256"])
        found = self.get(key)
        if found is not None:
            return found
        image = normalized_array(path)
        image.setflags(write=False)
        return self.put(key, image, image.nbytes)


class RenderCache(MemoryCache):
    def __init__(self):
        super().__init__(32 * 1024 * 1024, 40)

    def rendered(self, study_id, revision, operation, arguments, producer):
        key = ("render", study_id, revision, operation, arguments)
        saved = self.get(key)
        if saved is not None:
            return saved
        payload = producer()
        if not isinstance(payload, bytes):
            raise TypeError("Rendered result must be bytes")
        return self.put(key, payload, len(payload))
