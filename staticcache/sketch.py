"""Count-Min sketch: approximate per-key frequency in fixed memory.

It only ever *over*-estimates (hash collisions add counts, nothing subtracts).
We use two tricks from the TinyLFU paper:

  * small saturating counters (cap=15 -> 4 bits each in a real implementation)
  * periodic halving, so old popularity decays ("aging")
"""
from __future__ import annotations

from .bloom import hash_pair


class CountMinSketch:
    def __init__(self, width: int, depth: int = 4, cap: int = 15):
        if width <= 0 or depth <= 0:
            raise ValueError("width and depth must be positive")
        self.width, self.depth, self.cap = width, depth, cap
        self.rows = [[0] * width for _ in range(depth)]

    def _cols(self, item):
        h1, h2 = hash_pair(item)
        w = self.width
        return [(h1 + r * h2) % w for r in range(self.depth)]

    def add(self, item) -> None:
        cols = self._cols(item)
        cur = min(self.rows[r][c] for r, c in enumerate(cols))
        if cur >= self.cap:
            return
        # Conservative update: only bump the counters that equal the minimum.
        # Tighter estimates than incrementing all of them.
        for r, c in enumerate(cols):
            if self.rows[r][c] == cur:
                self.rows[r][c] = cur + 1

    def estimate(self, item) -> int:
        return min(self.rows[r][c] for r, c in enumerate(self._cols(item)))

    def halve(self) -> None:
        self.rows = [[v >> 1 for v in row] for row in self.rows]

    @property
    def logical_bytes(self) -> int:
        """Size if counters were packed at 4 bits (cap=15), as in production."""
        bits = max(1, self.cap.bit_length())
        return (self.width * self.depth * bits + 7) // 8
