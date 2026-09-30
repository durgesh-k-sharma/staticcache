import os
import tempfile
import unittest

from staticcache import (BloomDoorkeeper, BloomFilter, Cache, CachingFetcher,
                         CountMinSketch, ExactDoorkeeper, TinyLFU)
from staticcache.bloom import optimal_params
from staticcache.logparse import is_static, load_trace, parse_line
from staticcache import simulate as sim, synth


class BloomTests(unittest.TestCase):
    def test_no_false_negatives(self):
        bf = BloomFilter.for_capacity(5000, 0.01)
        for i in range(5000):
            bf.add(f"k{i}")
        self.assertTrue(all(f"k{i}" in bf for i in range(5000)))

    def test_false_positive_rate_near_target(self):
        bf = BloomFilter.for_capacity(10_000, 0.01)
        for i in range(10_000):
            bf.add(f"in-{i}")
        fp = sum(f"out-{i}" in bf for i in range(50_000)) / 50_000
        self.assertLess(fp, 0.02)          # target 1%; allow 2x slack
        self.assertLess(abs(bf.estimated_fpr() - fp), 0.01)

    def test_add_reports_prior_presence(self):
        bf = BloomFilter.for_capacity(100)
        self.assertFalse(bf.add("a"))
        self.assertTrue(bf.add("a"))

    def test_cardinality_estimate(self):
        bf = BloomFilter.for_capacity(10_000, 0.01)
        for i in range(8000):
            bf.add(i.to_bytes(4, "little"))
        self.assertAlmostEqual(bf.estimated_cardinality(), 8000, delta=300)

    def test_union_and_clear(self):
        a, b = BloomFilter(1024, 3), BloomFilter(1024, 3)
        a.add("x"); b.add("y")
        u = a.union(b)
        self.assertIn("x", u); self.assertIn("y", u)
        u.clear()
        self.assertNotIn("x", u)
        with self.assertRaises(ValueError):
            a.union(BloomFilter(2048, 3))

    def test_sizing_formula(self):
        m, k = optimal_params(1000, 0.01)
        self.assertAlmostEqual(m / 1000, 9.59, delta=0.05)  # ~9.6 bits/item at 1%
        self.assertEqual(k, 7)


class SketchTests(unittest.TestCase):
    def test_never_underestimates_below_cap(self):
        cms = CountMinSketch(256, 4, cap=15)
        truth = {f"k{i}": (i % 12) + 1 for i in range(200)}
        for k, c in truth.items():
            for _ in range(c):
                cms.add(k)
        self.assertTrue(all(cms.estimate(k) >= c for k, c in truth.items()))

    def test_saturates_and_halves(self):
        cms = CountMinSketch(64, 4, cap=15)
        for _ in range(100):
            cms.add("hot")
        self.assertEqual(cms.estimate("hot"), 15)
        cms.halve()
        self.assertEqual(cms.estimate("hot"), 7)


class CacheTests(unittest.TestCase):
    def test_lru_eviction_by_bytes(self):
        c = Cache(100)
        for k, s in (("a", 40), ("b", 40)):
            c.lookup(k); c.insert(k, s)
        c.lookup("a")                      # a becomes most recent
        c.lookup("c"); c.insert("c", 40)   # must evict b
        self.assertTrue(c.lookup("a")[0])
        self.assertFalse(c.lookup("b")[0])
        self.assertLessEqual(c.used, 100)

    def test_oversize_object_not_cached(self):
        c = Cache(10)
        c.lookup("big")
        self.assertFalse(c.insert("big", 11))
        self.assertEqual(c.oversize, 1)

    def test_doorkeeper_admits_on_second_miss_only(self):
        for policy in (BloomDoorkeeper(window=1000), ExactDoorkeeper()):
            c = Cache(1000, policy)
            c.lookup("x"); self.assertFalse(c.insert("x", 10))   # first sighting
            c.lookup("x"); self.assertTrue(c.insert("x", 10))    # second
            self.assertTrue(c.lookup("x")[0])

    def test_tinylfu_protects_frequent_entry(self):
        pol = TinyLFU(expected_items=1000)
        c = Cache(100, pol)
        for _ in range(6):                  # make "hot" clearly popular
            c.lookup("hot")
            if "hot" not in c.items:
                c.insert("hot", 100)
        c.lookup("cold"); self.assertFalse(c.insert("cold", 100))
        self.assertIn("hot", c.items)

    def test_caching_fetcher_avoids_origin_calls(self):
        calls = []
        f = CachingFetcher(lambda u: calls.append(u) or b"x" * 10, 1000)
        for _ in range(5):
            self.assertEqual(f.get("/a.gif"), b"x" * 10)
        self.assertEqual(f.origin_calls, 1)
        self.assertEqual(calls, ["/a.gif"])


class ParseTests(unittest.TestCase):
    def test_parse_normal_and_edge_lines(self):
        r = parse_line('199.72.81.55 - - [01/Jul/1995:00:00:01 -0400] "GET /a/b.gif HTTP/1.0" 200 6245')
        self.assertEqual((r.method, r.url, r.status, r.nbytes), ("GET", "/a/b.gif", 200, 6245))
        r = parse_line('h - - [01/Jul/1995:00:00:01 -0400] "GET /x.gif" 304 -')   # no protocol, "-" bytes
        self.assertEqual((r.url, r.status, r.nbytes), ("/x.gif", 304, 0))
        self.assertIsNone(parse_line("total garbage"))
        self.assertIsNone(parse_line('h - - [t] "" 200 5'))

    def test_is_static(self):
        self.assertTrue(is_static("/images/NASA-logosmall.GIF"))
        self.assertFalse(is_static("/cgi-bin/imagemap/countdown?99,176"))
        self.assertFalse(is_static("/images/a.gif?v=2"))
        self.assertFalse(is_static("/history/apollo/"))
        self.assertFalse(is_static("/shuttle/x.html"))
        self.assertTrue(is_static("/shuttle/x.html", include_html=True))


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.log = os.path.join(self.dir, "log")
        synth.generate(self.log, 30_000, seed=7)

    def test_pipeline_and_policy_ordering(self):
        tr = load_trace(self.log)
        self.assertGreater(len(tr), 10_000)
        self.assertGreater(tr.stats["malformed"], 0)
        cap = 2 * 2**20
        lru = sim.simulate(tr, cap, sim.Admission())
        bloom = sim.simulate(tr, cap, BloomDoorkeeper(window=tr.unique_objects))
        ceiling = sim.infinite_cache(tr)
        self.assertGreater(lru.hits, 0)
        self.assertLessEqual(lru.hits, ceiling.hits)
        self.assertLessEqual(bloom.hits, ceiling.hits)
        self.assertEqual(lru.requests, bloom.requests)
        self.assertLess(bloom.admitted, lru.admitted)   # doorkeeper admits fewer objects

    def test_deterministic(self):
        again = os.path.join(self.dir, "log2")
        synth.generate(again, 30_000, seed=7)
        with open(self.log) as a, open(again) as b:
            self.assertEqual(a.read(), b.read())


if __name__ == "__main__":
    unittest.main()
