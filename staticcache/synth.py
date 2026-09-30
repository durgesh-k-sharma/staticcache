"""Generate a synthetic access log in the same format as NASA-HTTP.

For development/CI when the real traces aren't at hand. It reproduces the
properties that matter to a cache -- NOT the real NASA numbers:

  * Zipf-like popularity over a fixed catalogue of assets
  * a long tail of URLs requested once (one-hit wonders)
  * heavy-tailed (log-normal) object sizes
  * 304 Not Modified replies with 0 bytes, 404s, dynamic /cgi-bin/ hits
  * occasional truncated transfers and unparseable lines
"""
from __future__ import annotations

import bisect
import itertools
import math
import random
from datetime import datetime, timedelta

_EXTS = ["gif"] * 8 + ["jpg"] * 3 + ["xbm", "mpg", "png", "wav"]


def generate(path: str, n_requests: int = 300_000, seed: int = 1995,
             n_assets: int = 4_000, zipf_s: float = 0.85,
             one_hit_fraction: float = 0.22, static_fraction: float = 0.65) -> None:
    rng = random.Random(seed)

    # Static catalogue: id -> (url, size)
    catalogue = []
    for i in range(n_assets):
        ext = rng.choice(_EXTS)
        size = int(min(6_000_000, max(120, rng.lognormvariate(math.log(9_000), 1.6))))
        catalogue.append((f"/images/asset-{i:05d}.{ext}", size))
    cum = list(itertools.accumulate((r + 1) ** -zipf_s for r in range(n_assets)))

    pages = [f"/shuttle/missions/sts-{rng.randint(1, 80)}/page-{j}.html" for j in range(600)]
    page_cum = list(itertools.accumulate((r + 1) ** -1.0 for r in range(len(pages))))
    hosts = [f"host{h}.example.net" if h % 3 else f"199.{h % 250}.{(h * 7) % 250}.{h % 200 + 1}"
             for h in range(20_000)]

    t = datetime(1995, 7, 1, 0, 0, 0)
    served_once: set[str] = set()
    unique_counter = 0

    with open(path, "w", encoding="latin-1") as out:
        for _ in range(n_requests):
            t += timedelta(seconds=rng.expovariate(1 / 1.4))
            host = rng.choice(hosts)
            ts = t.strftime("%d/%b/%Y:%H:%M:%S -0400")
            x = rng.random()

            if x < 0.0002:                                   # garbage line
                out.write(f'{host} - - [{ts}] "\\x16\\x03" 400 -\n')
                continue

            if x < static_fraction:
                if rng.random() < one_hit_fraction:          # one-hit wonder
                    unique_counter += 1
                    ext = rng.choice(_EXTS)
                    url = f"/images/once-{unique_counter:07d}.{ext}"
                    size = int(min(6_000_000, max(120, rng.lognormvariate(math.log(9_000), 1.6))))
                else:                                        # popular asset
                    j = bisect.bisect_left(cum, rng.random() * cum[-1])
                    url, size = catalogue[j]
                first = url not in served_once
                served_once.add(url)
                r = rng.random()
                if not first and r < 0.12:
                    status, nbytes = 304, 0                  # client revalidated
                elif r > 0.985:
                    status, nbytes = 404, 0
                else:
                    status = 200
                    nbytes = size if rng.random() > 0.03 else int(size * rng.random())  # aborted
            elif x < static_fraction + 0.03:
                status, nbytes = 200, rng.randint(400, 3000)
                url = f"/cgi-bin/imagemap/countdown?{rng.randint(100, 500)},{rng.randint(100, 400)}"
            else:
                j = bisect.bisect_left(page_cum, rng.random() * page_cum[-1])
                url = pages[j]
                status, nbytes = (200, rng.randint(1_000, 40_000)) if rng.random() > 0.02 else (404, 0)

            out.write(f'{host} - - [{ts}] "GET {url} HTTP/1.0" {status} {nbytes if nbytes else ("-" if status == 404 else 0)}\n')
