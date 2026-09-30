# staticcache

A byte-bounded cache for static assets, evaluated by replaying the
[NASA-HTTP traces](https://ita.ee.lbl.gov/html/contrib/NASA-HTTP.html)
(~3.46M requests, July + August 1995). Pure Python 3.10+, no dependencies.

The interesting question isn't "should I use an LRU?" but **"which objects
deserve a slot?"** Most URLs in a web log are requested once. A plain LRU
caches all of them and pushes out objects that would have been reused. Here,
probabilistic data structures act as cheap gatekeepers.

## Quick start

```bash
# 1. Get the real data (gzip is read directly, no need to unpack)
#    ftp://ita.ee.lbl.gov/traces/NASA_access_log_Jul95.gz
#    ftp://ita.ee.lbl.gov/traces/NASA_access_log_Aug95.gz

python -m staticcache analyze  NASA_access_log_Jul95.gz
python -m staticcache simulate NASA_access_log_Jul95.gz --cache-mb 1 4 16 64

# No data handy? Generate a NASA-shaped synthetic log
python -m staticcache generate /tmp/synth.log --requests 400000
python -m staticcache bloom-demo          # theory vs measured false-positive rate
python -m unittest discover -s tests
```

## Layout

| File | What it does |
|---|---|
| `bloom.py` | Bloom filter: sizing math, double hashing, check-and-add, fill/cardinality estimates, union |
| `sketch.py` | Count-Min sketch with conservative update, saturating counters, halving |
| `cache.py` | Byte-bounded LRU, admission policies, `CachingFetcher` (fetch-through wrapper) |
| `logparse.py` | Common Log Format parser, static-asset classifier, compact trace loader |
| `analysis.py` | Log statistics: one-hit wonders, popularity skew, hit-ratio ceiling |
| `simulate.py` | Trace replay + metrics (hit ratio, byte-hit ratio, origin fetches/bytes) |
| `synth.py` | Synthetic NASA-style log generator |

## Policies compared

1. **LRU**: admit everything.
2. **LRU + exact-set doorkeeper**: admit an object only on its 2nd request. Accurate, but memory grows with every distinct URL seen.
3. **LRU + Bloom doorkeeper**: same rule using a fixed-size Bloom filter. A false positive just admits one extra one-hit wonder. Since Bloom filters can't delete, it's cleared every `window` distinct keys.
4. **TinyLFU**: a Bloom doorkeeper (first sighting) in front of a Count-Min sketch (repeat counts, halved periodically). On a full cache, a newcomer must be *more frequent* than everything it would evict.

## Using it as a library

```python
from staticcache import CachingFetcher, BloomDoorkeeper

fetcher = CachingFetcher(origin_fetch=my_http_get,        # url -> bytes
                         capacity_bytes=64 * 2**20,
                         admission=BloomDoorkeeper(window=200_000))
body = fetcher.get("/images/NASA-logosmall.gif")           # origin called only if needed
```

`Cache.lookup(key)` must be called on every request (it feeds the admission
policy); call `Cache.insert(key, size)` only after a miss.

## Modelling decisions worth knowing

- **Static = by extension** (images, css/js, media, pdf...); URLs with `?` or `/cgi-bin/` are excluded. `--include-html` adds `.html/.htm/.txt`. The NASA site is mostly HTML, so this choice changes results substantially. Try both.
- **Only GET with status 200/304** count. 304 means a client revalidated an object, so it is still a request the cache could serve.
- **Object size = the largest byte count ever logged for that URL**, because aborted transfers log fewer bytes. Objects only ever seen as 304 have no known size and are dropped (reported by `analyze`).
- **Objects never change** in the simulation (no TTL/invalidation). Real assets need `Cache-Control`/ETag handling; the logs can't tell you about that.
- **Metrics**: *hit ratio* counts requests avoided; *byte-hit ratio* counts bytes not pulled from origin. They can disagree, since caching many small hot objects wins the first and big ones the second.

## Things to try next

- Sweep `--filter-items` far below the unique-object count and watch Bloom false positives erode the doorkeeper's advantage (`bf.estimated_fpr()` tells you when).
- Add a size-aware rule (e.g. don't admit objects > 1% of the cache).
- Cooperative caching: N edge caches each publish a Bloom "cache digest" of their contents so a miss can be routed to a peer (the Summary Cache / Squid cache-digest idea).
- Replace the blake2b double-hash with a faster non-crypto hash (e.g. xxhash) if simulating full months matters.
- Split the trace by day: train nothing, but check whether policies rank the same in July as in August.
