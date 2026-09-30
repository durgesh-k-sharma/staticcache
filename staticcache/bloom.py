"""A from-scratch Bloom filter.

A Bloom filter answers "have I (probably) seen this before?" using a fixed
bit array. It can return false positives but never false negatives, and it
cannot delete. Both properties shape how we use it in the cache:

  * A false positive on the doorkeeper just admits one extra one-hit wonder.
  * "No deletion" means we periodically clear the whole filter (a window).
"""
from __future__ import annotations

import hashlib
import math


def optimal_params(n: int, p: float) -> tuple[int, int]:
    """Bits `m` and hash count `k` for `n` items at target false-positive rate `p`.

    m = -n ln(p) / (ln 2)^2        k = (m / n) ln 2
    """
    if n <= 0 or not (0 < p < 1):
        raise ValueError("need n > 0 and 0 < p < 1")
    m = math.ceil(-n * math.log(p) / (math.log(2) ** 2))
    k = max(1, round(m / n * math.log(2)))
    return m, k


def hash_pair(item: str | bytes) -> tuple[int, int]:
    """Two independent 64-bit hashes from a single BLAKE2b call."""
    data = item.encode("utf-8", "surrogateescape") if isinstance(item, str) else item
    d = hashlib.blake2b(data, digest_size=16).digest()
    # Force h2 odd so it is never 0 and cycles well even for power-of-two sizes.
    return int.from_bytes(d[:8], "little"), int.from_bytes(d[8:], "little") | 1


class BloomFilter:
    def __init__(self, m: int, k: int):
        if m <= 0 or k <= 0:
            raise ValueError("m and k must be positive")
        self.m = m
        self.k = k
        self.bits = bytearray((m + 7) // 8)
        self.count = 0  # adds that changed at least one bit ~= distinct items

    @classmethod
    def for_capacity(cls, n: int, p: float = 0.01) -> "BloomFilter":
        return cls(*optimal_params(n, p))

    # Kirsch-Mitzenmacher: g_i(x) = h1(x) + i*h2(x) gives k hashes from two
    # with no measurable loss in false-positive rate.
    def _indexes(self, item):
        h1, h2 = hash_pair(item)
        m = self.m
        return [(h1 + i * h2) % m for i in range(self.k)]

    def add(self, item) -> bool:
        """Insert `item`. Returns True if it was *probably already present*."""
        bits = self.bits
        already = True
        for idx in self._indexes(item):
            byte, mask = idx >> 3, 1 << (idx & 7)
            if not bits[byte] & mask:
                already = False
                bits[byte] |= mask
        if not already:
            self.count += 1
        return already

    def __contains__(self, item) -> bool:
        bits = self.bits
        return all(bits[i >> 3] & (1 << (i & 7)) for i in self._indexes(item))

    def clear(self) -> None:
        self.bits = bytearray(len(self.bits))
        self.count = 0

    def union(self, other: "BloomFilter") -> "BloomFilter":
        if (self.m, self.k) != (other.m, other.k):
            raise ValueError("filters must have identical m and k")
        out = BloomFilter(self.m, self.k)
        out.bits = bytearray(a | b for a, b in zip(self.bits, other.bits))
        out.count = self.count + other.count  # upper bound
        return out

    # ---- introspection -------------------------------------------------
    @property
    def size_bytes(self) -> int:
        return len(self.bits)

    def fill_ratio(self) -> float:
        ones = int.from_bytes(self.bits, "little").bit_count()
        return ones / self.m

    def estimated_fpr(self) -> float:
        """Current false-positive probability, from how full the array is."""
        return self.fill_ratio() ** self.k

    def estimated_cardinality(self) -> float:
        """Swamidass-Baldi estimate of distinct items inserted: -(m/k) ln(1 - X/m)."""
        fill = self.fill_ratio()
        return math.inf if fill >= 1 else -(self.m / self.k) * math.log(1 - fill)
