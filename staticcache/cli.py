from __future__ import annotations

import argparse
import math
import time

from . import analysis, simulate, synth
from .bloom import BloomFilter
from .logparse import load_trace


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024


def cmd_generate(a):
    synth.generate(a.out, a.requests, seed=a.seed)
    print(f"wrote {a.requests:,} synthetic requests to {a.out}")


def cmd_analyze(a):
    tr = load_trace(a.log, include_html=a.include_html, limit=a.limit)
    print(analysis.report(tr))


def cmd_simulate(a):
    t0 = time.time()
    tr = load_trace(a.log, include_html=a.include_html, limit=a.limit)
    print(f"loaded {len(tr):,} static requests / {tr.unique_objects:,} objects "
          f"in {time.time() - t0:.1f}s")
    items = a.filter_items or tr.unique_objects
    policies = simulate.default_policies(items)
    print(f"admission filters sized for {items:,} items\n")

    for mb in a.cache_mb:
        cap = int(mb * 2**20)
        rows = [simulate.no_cache(tr)]
        for make in policies:
            rows.append(simulate.simulate(tr, cap, make()))
        rows.append(simulate.infinite_cache(tr))
        print(f"--- cache size {mb:g} MB ---")
        print(f"{'policy':<30}{'hit%':>7}{'byte-hit%':>11}{'origin fetches':>16}"
              f"{'origin bytes':>14}{'rejected':>10}{'filter mem':>12}")
        for r in rows:
            print(f"{r.name:<30}{r.hit_ratio:>7.1%}{r.byte_hit_ratio:>11.1%}"
                  f"{r.origin_fetches:>16,}{_fmt_bytes(r.origin_bytes):>14}"
                  f"{r.rejected:>10,}{_fmt_bytes(r.filter_bytes) if r.filter_bytes else '-':>12}")
        print()


def cmd_bloom_demo(a):
    n = a.items
    print(f"{n:,} members, {a.probes:,} non-member probes\n")
    print(f"{'bits/item':>9}{'k':>4}{'size':>11}{'theory FPR':>12}{'measured FPR':>14}")
    for bpi in (4, 6, 8, 10, 12, 16):
        m = n * bpi
        k = max(1, round(bpi * math.log(2)))
        bf = BloomFilter(m, k)
        for i in range(n):
            bf.add(f"member-{i}")
        assert all(f"member-{i}" in bf for i in range(0, n, 97)), "false negative!"
        fp = sum(f"other-{i}" in bf for i in range(a.probes))
        theory = (1 - math.exp(-k * n / m)) ** k
        print(f"{bpi:>9}{k:>4}{_fmt_bytes(bf.size_bytes):>11}{theory:>12.4%}{fp / a.probes:>14.4%}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="staticcache", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="write a synthetic NASA-style log")
    g.add_argument("out")
    g.add_argument("--requests", type=int, default=300_000)
    g.add_argument("--seed", type=int, default=1995)
    g.set_defaults(fn=cmd_generate)

    for name, fn, helptext in (("analyze", cmd_analyze, "log statistics"),
                               ("simulate", cmd_simulate, "compare cache policies")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("log", help="access log (.gz ok)")
        s.add_argument("--include-html", action="store_true",
                       help="also treat .html/.htm/.txt as cacheable")
        s.add_argument("--limit", type=int, help="only read the first N lines")
        s.set_defaults(fn=fn)
        if name == "simulate":
            s.add_argument("--cache-mb", type=float, nargs="+", default=[1, 4, 16, 64])
            s.add_argument("--filter-items", type=int,
                           help="Bloom/CMS sizing (default: number of unique objects)")

    b = sub.add_parser("bloom-demo", help="theoretical vs measured false-positive rate")
    b.add_argument("--items", type=int, default=50_000)
    b.add_argument("--probes", type=int, default=200_000)
    b.set_defaults(fn=cmd_bloom_demo)

    a = p.parse_args(argv)
    a.fn(a)
