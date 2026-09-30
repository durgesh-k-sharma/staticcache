"""Byte-bounded LRU cache + admission policies.

Why admission matters: on a web workload a big share of URLs are requested
exactly once ("one-hit wonders"). A plain LRU caches every one of them,
evicting objects that *would* have been reused. An admission policy decides,
on a miss, whether the newly fetched object deserves a slot at all.

Protocol per request:
    hit, value = cache.lookup(key)      # always call; records the access
    if not hit: cache.insert(key, size) # after fetching from origin
"""
from __future__ import annotations

import sys
from collections import OrderedDict
from typing import Callable, Optional

from .bloom import BloomFilter
from .sketch import CountMinSketch


# ---------------------------------------------------------------- admission
class Admission:
    """Admit-everything baseline (plain LRU)."""
    name = "LRU"

    def record(self, key: str) -> None:
        """Called on every request, hit or miss."""

    def admit(self, key: str, size: int, victims: list[tuple[str, int]]) -> bool:
        """Called on a miss. `victims` are the entries that would be evicted."""
        return True

    def memory_bytes(self) -> int:
        return 0


class ExactDoorkeeper(Admission):
    """Admit on the 2nd sighting, tracked with an exact set. Accurate, but
    memory grows with every distinct URL ever seen. Reference for the Bloom version."""
    name = "LRU + exact-set doorkeeper"

    def __init__(self):
        self.seen: set[str] = set()
        self._seen_before = False

    def record(self, key):
        self._seen_before = key in self.seen
        self.seen.add(key)

    def admit(self, key, size, victims):
        return self._seen_before

    def memory_bytes(self):
        return sys.getsizeof(self.seen) + sum(sys.getsizeof(k) for k in self.seen)


class BloomDoorkeeper(Admission):
    """Admit on the 2nd sighting, tracked with a Bloom filter.

    Fixed memory. A false positive admits a one-hit wonder (a small waste,
    never a correctness bug). Bloom filters can't delete, so the filter is
    cleared once `window` distinct keys have been added, which also bounds
    the false-positive rate.
    """
    name = "LRU + Bloom doorkeeper"

    def __init__(self, window: int = 100_000, fpr: float = 0.01):
        self.window = window
        self.bf = BloomFilter.for_capacity(window, fpr)
        self._seen_before = False

    def record(self, key):
        self._seen_before = self.bf.add(key)
        if self.bf.count >= self.window:
            self.bf.clear()

    def admit(self, key, size, victims):
        return self._seen_before

    def memory_bytes(self):
        return self.bf.size_bytes


class TinyLFU(Admission):
    """Doorkeeper Bloom filter + Count-Min sketch (Einziger et al., 2017).

    First sighting only touches the doorkeeper; repeats are counted in the
    sketch (so the sketch is not wasted on one-hit wonders). When the cache is
    full, a candidate is admitted only if it is more frequent than every entry
    it would evict. All counts decay by half every `sample_size` accesses.
    """
    name = "TinyLFU (Bloom + CMS)"

    def __init__(self, expected_items: int = 100_000, sample_factor: int = 10,
                 fpr: float = 0.01, depth: int = 4):
        self.expected = expected_items
        self.door = BloomFilter.for_capacity(expected_items, fpr)
        width = 1 << max(4, (expected_items - 1).bit_length())  # next power of two
        self.cms = CountMinSketch(width, depth)
        self.sample_size = expected_items * sample_factor
        self._accesses = 0

    def record(self, key):
        if self.door.add(key):
            self.cms.add(key)
        self._accesses += 1
        if self.door.count >= self.expected:
            self.door.clear()
        if self._accesses >= self.sample_size:  # aging
            self.cms.halve()
            self.door.clear()
            self._accesses = 0

    def frequency(self, key) -> int:
        return self.cms.estimate(key) + (1 if key in self.door else 0)

    def admit(self, key, size, victims):
        if not victims:  # free space: nobody gets hurt
            return True
        f = self.frequency(key)
        return all(f > self.frequency(v) for v, _ in victims)

    def memory_bytes(self):
        return self.door.size_bytes + self.cms.logical_bytes


# -------------------------------------------------------------------- cache
class Cache:
    def __init__(self, capacity_bytes: int, admission: Optional[Admission] = None):
        self.capacity = capacity_bytes
        self.admission = admission or Admission()
        self.used = 0
        self.items: OrderedDict[str, tuple[int, object]] = OrderedDict()  # LRU first
        self.admitted = self.rejected = self.evictions = self.oversize = 0

    def lookup(self, key: str):
        """Returns (hit, value). Records the access with the admission policy."""
        self.admission.record(key)
        entry = self.items.get(key)
        if entry is None:
            return False, None
        self.items.move_to_end(key)
        return True, entry[1]

    def insert(self, key: str, size: int, value=None) -> bool:
        """Offer a freshly fetched object. Returns True if it was cached."""
        if key in self.items:
            return True
        if size > self.capacity:
            self.oversize += 1
            return False
        need = self.used + size - self.capacity
        victims: list[tuple[str, int]] = []
        if need > 0:
            freed = 0
            for k, (s, _) in self.items.items():  # oldest first
                victims.append((k, s))
                freed += s
                if freed >= need:
                    break
        if not self.admission.admit(key, size, victims):
            self.rejected += 1
            return False
        for k, s in victims:
            del self.items[k]
            self.used -= s
            self.evictions += 1
        self.items[key] = (size, value)
        self.used += size
        self.admitted += 1
        return True


class CachingFetcher:
    """Drop-in fetch-through layer: `get(url)` hits origin only when needed."""

    def __init__(self, origin_fetch: Callable[[str], bytes], capacity_bytes: int,
                 admission: Optional[Admission] = None):
        self.origin_fetch = origin_fetch
        self.cache = Cache(capacity_bytes, admission)
        self.origin_calls = 0

    def get(self, url: str) -> bytes:
        hit, body = self.cache.lookup(url)
        if hit:
            return body
        self.origin_calls += 1
        body = self.origin_fetch(url)
        self.cache.insert(url, len(body), body)
        return body
