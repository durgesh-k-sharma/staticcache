"""Replay a trace through a cache and report what it saved."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .cache import (Admission, BloomDoorkeeper, Cache, ExactDoorkeeper, TinyLFU)
from .logparse import Trace


@dataclass
class Result:
    name: str
    requests: int
    hits: int
    bytes_total: int
    bytes_hit: int
    admitted: int = 0
    rejected: int = 0
    evictions: int = 0
    filter_bytes: int = 0

    @property
    def hit_ratio(self) -> float:
        return self.hits / self.requests if self.requests else 0.0

    @property
    def byte_hit_ratio(self) -> float:
        return self.bytes_hit / self.bytes_total if self.bytes_total else 0.0

    @property
    def origin_fetches(self) -> int:
        return self.requests - self.hits

    @property
    def origin_bytes(self) -> int:
        return self.bytes_total - self.bytes_hit


def simulate(trace: Trace, capacity_bytes: int, admission: Admission) -> Result:
    cache = Cache(capacity_bytes, admission)
    urls, sizes = trace.urls, trace.sizes
    hits = bytes_hit = bytes_total = 0
    for i in trace.ids:
        key, size = urls[i], sizes[i]
        bytes_total += size
        hit, _ = cache.lookup(key)
        if hit:
            hits += 1
            bytes_hit += size
        else:
            cache.insert(key, size)
    return Result(admission.name, len(trace.ids), hits, bytes_total, bytes_hit,
                  cache.admitted, cache.rejected, cache.evictions,
                  admission.memory_bytes())


def no_cache(trace: Trace) -> Result:
    total = sum(trace.sizes[i] for i in trace.ids)
    return Result("No cache", len(trace.ids), 0, total, 0)


def infinite_cache(trace: Trace) -> Result:
    """Upper bound: only compulsory (first-ever) misses remain."""
    seen: set[int] = set()
    hits = bytes_hit = bytes_total = 0
    for i in trace.ids:
        s = trace.sizes[i]
        bytes_total += s
        if i in seen:
            hits += 1
            bytes_hit += s
        else:
            seen.add(i)
    return Result("Ceiling (infinite cache)", len(trace.ids), hits, bytes_total, bytes_hit)


PolicyFactory = Callable[[], Admission]


def default_policies(filter_items: int) -> list[PolicyFactory]:
    return [
        lambda: Admission(),
        lambda: ExactDoorkeeper(),
        lambda: BloomDoorkeeper(window=filter_items),
        lambda: TinyLFU(expected_items=filter_items),
    ]
