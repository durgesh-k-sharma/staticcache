"""Answer, from the log alone: is caching worth it, and what will it be limited by?"""
from __future__ import annotations

from collections import Counter

from .logparse import Trace
from .simulate import infinite_cache


def _pct(a, b):
    return f"{100 * a / b:5.1f}%" if b else "  n/a"


def report(tr: Trace, top: int = 10) -> str:
    s = tr.stats
    out: list[str] = []
    w = out.append

    lines, n = s["lines"], len(tr)
    w("== Log overview ==")
    w(f"lines read            {lines:>12,}")
    w(f"malformed lines       {s['malformed']:>12,}  ({_pct(s['malformed'], lines).strip()})")
    ok = lines - s["malformed"]
    w("status codes       " + "  ".join(
        f"{c}:{_pct(v, ok).strip()}" for c, v in s["status_counts"].most_common(6)))
    w("methods            " + "  ".join(
        f"{m}:{_pct(v, ok).strip()}" for m, v in s["method_counts"].most_common(4)))

    total_bytes = sum(tr.sizes[i] for i in tr.ids)
    w("")
    w("== Static assets (cacheable candidates) ==")
    w(f"GET 200/304 requests  {s['static_requests'] + s['other_get_ok_requests']:>12,}")
    w(f"  of which static     {n:>12,}  ({_pct(n, s['static_requests'] + s['other_get_ok_requests']).strip()})")
    w(f"  dropped (size unknown, only ever seen as 304): {s['dropped_unknown_size']:,}")
    w(f"unique static objects {tr.unique_objects:>12,}")
    w(f"bytes requested       {total_bytes / 2**30:>12.2f} GiB")

    counts = Counter(tr.ids)
    once = sum(1 for c in counts.values() if c == 1)
    w("")
    w("== Popularity (what makes admission control worthwhile) ==")
    w(f"one-hit wonders       {once:>12,}  = {_pct(once, len(counts)).strip()} of objects, "
      f"{_pct(once, n).strip()} of requests")
    ranked = sorted(counts.values(), reverse=True)
    for frac in (0.01, 0.10):
        k = max(1, int(len(ranked) * frac))
        w(f"top {frac:>4.0%} of objects   -> {_pct(sum(ranked[:k]), n).strip()} of requests")
    ceil = infinite_cache(tr)
    w(f"hit-ratio ceiling     {ceil.hit_ratio:>11.1%}   (infinite cache; the rest are compulsory misses)")
    w(f"byte-hit ceiling      {ceil.byte_hit_ratio:>11.1%}")

    w("")
    w(f"== Top {top} static objects ==")
    for i, c in counts.most_common(top):
        w(f"{c:>9,}  {tr.sizes[i]:>10,} B  {tr.urls[i]}")

    w("")
    w("== Static requests by hour of day (log local time) ==")
    hc = s["hour_counts"]
    peak = max(hc.values(), default=1)
    for h in sorted(hc):
        w(f"{h}h {'#' * round(40 * hc[h] / peak):<40} {hc[h]:,}")
    return "\n".join(out)
