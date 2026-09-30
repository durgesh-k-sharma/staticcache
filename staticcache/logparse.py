"""Parse Common Log Format (the NASA-HTTP traces) into a compact cache trace.

Example line:
    199.72.81.55 - - [01/Jul/1995:00:00:01 -0400] "GET /history/apollo/ HTTP/1.0" 200 6245

Real-world quirks handled here:
  * bytes may be "-" (and 304 responses carry 0 bytes)
  * some request fields lack the protocol, or are garbage
  * a few lines are simply malformed -> counted and skipped
  * byte counts on 200s reflect what was actually *sent*, so aborted
    transfers appear smaller. We use the MAX size seen per URL as its size.
"""
from __future__ import annotations

import gzip
import re
from array import array
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Iterator, NamedTuple, Optional

LINE_RE = re.compile(
    r'^(?P<host>\S+) \S+ \S+ \[(?P<ts>[^\]]+)\] "(?P<req>.*)" (?P<status>\d{3}) (?P<bytes>\d+|-)\s*$'
)

STATIC_EXTS = frozenset({
    # images
    "gif", "jpg", "jpeg", "png", "xbm", "bmp", "ico", "tif", "tiff", "svg", "webp",
    # web assets
    "css", "js", "woff", "woff2", "ttf",
    # audio / video / documents / archives (large, immutable-ish)
    "wav", "au", "mp3", "mid", "mpg", "mpeg", "mov", "avi", "qt",
    "pdf", "ps", "eps", "zip", "gz", "tar", "z",
})
HTML_EXTS = frozenset({"html", "htm", "txt"})


class Request(NamedTuple):
    host: str
    ts: str          # raw, e.g. "01/Jul/1995:00:00:01 -0400" (parsing datetimes is slow)
    method: str
    url: str
    status: int
    nbytes: int      # 0 when "-"


def parse_line(line: str) -> Optional[Request]:
    m = LINE_RE.match(line)
    if not m:
        return None
    parts = m["req"].split()
    if len(parts) < 2:
        return None
    b = m["bytes"]
    return Request(m["host"], m["ts"], parts[0].upper(), parts[1],
                   int(m["status"]), 0 if b == "-" else int(b))


def open_log(path: str):
    # latin-1 never fails to decode; the 1995 logs contain stray high bytes.
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="latin-1", newline="")
    return open(path, "rt", encoding="latin-1", newline="")


def iter_requests(lines: Iterable[str]) -> Iterator[Optional[Request]]:
    """Yields a Request per line, or None for malformed lines."""
    for line in lines:
        yield parse_line(line)


def is_static(url: str, include_html: bool = False) -> bool:
    if "?" in url or "/cgi-bin/" in url:
        return False
    last = url.rsplit("/", 1)[-1]
    if "." not in last:
        return False
    ext = last.rsplit(".", 1)[-1].lower()
    return ext in STATIC_EXTS or (include_html and ext in HTML_EXTS)


# ------------------------------------------------------------------ traces
@dataclass
class Trace:
    """Static-asset requests as integer ids into `urls` / `sizes`, in log order."""
    urls: list[str] = field(default_factory=list)
    sizes: list[int] = field(default_factory=list)
    ids: array = field(default_factory=lambda: array("I"))
    stats: dict = field(default_factory=dict)

    def __len__(self):
        return len(self.ids)

    @property
    def unique_objects(self) -> int:
        return len(set(self.ids))


def load_trace(path: str, include_html: bool = False,
               limit: Optional[int] = None) -> Trace:
    """Stream the log once, keeping only what the simulator needs."""
    index: dict[str, int] = {}
    tr = Trace()
    status_counts: Counter = Counter()
    method_counts: Counter = Counter()
    hour_counts: Counter = Counter()
    total = malformed = 0
    dynamic_or_other = 0

    with open_log(path) as f:
        for line in f:
            if limit is not None and total >= limit:
                break
            total += 1
            r = parse_line(line)
            if r is None:
                malformed += 1
                continue
            status_counts[r.status] += 1
            method_counts[r.method] += 1
            if r.method != "GET" or r.status not in (200, 304):
                continue
            if not is_static(r.url, include_html):
                dynamic_or_other += 1
                continue
            i = index.get(r.url)
            if i is None:
                i = index[r.url] = len(tr.urls)
                tr.urls.append(r.url)
                tr.sizes.append(0)
            if r.status == 200 and r.nbytes > tr.sizes[i]:
                tr.sizes[i] = r.nbytes
            tr.ids.append(i)
            hour_counts[r.ts[12:14]] += 1  # "HH" from "DD/Mon/YYYY:HH:..."

    # Objects only ever seen as 304 have no known size -> can't simulate them.
    before = len(tr.ids)
    tr.ids = array("I", (i for i in tr.ids if tr.sizes[i] > 0))
    tr.stats = {
        "lines": total, "malformed": malformed,
        "status_counts": status_counts, "method_counts": method_counts,
        "hour_counts": hour_counts,
        "static_requests_raw": before, "static_requests": len(tr.ids),
        "dropped_unknown_size": before - len(tr.ids),
        "other_get_ok_requests": dynamic_or_other,
    }
    return tr
