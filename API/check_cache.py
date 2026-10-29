"""Offline cache checks for you to run: python -m API.check_cache (no API calls)."""

from pathlib import Path
import tempfile
import unittest

from .cache import ResponseCache


class CacheChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = [1000.0]
        self.cache = ResponseCache(Path(self.directory.name), clock=lambda: self.now[0])
        self.key = self.cache.key("test-token", "https://example.invalid", "GET",
                                  "/ohlc/", {"instruments": "CL", "count": 3}, None)

    def test_shared_across_instances_and_returns_independent_data(self):
        self.cache.put(self.key, {"data": [{"price": -0.13}]}, 3600)
        another = ResponseCache(self.directory.name, clock=lambda: self.now[0])
        result = another.get(self.key, 3600)
        self.assertEqual(result["payload"]["data"][0]["price"], -0.13)
        result["payload"]["data"][0]["price"] = 99
        self.assertEqual(self.cache.get(self.key, 3600)["payload"]["data"][0]["price"], -0.13)

    def test_reads_do_not_extend_expiry_and_expired_rows_are_deleted(self):
        self.cache.put(self.key, [], 3600)
        self.now[0] = 4599
        self.assertIsNotNone(self.cache.get(self.key, 3600))
        self.now[0] = 4600
        self.assertIsNone(self.cache.get(self.key, 3600))
        connection = self.cache._connect()
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM responses").fetchone()[0], 0)
        finally:
            connection.close()

    def test_shorter_ttl_and_explicit_bypass(self):
        self.cache.put(self.key, None, 3600)
        self.assertIsNotNone(self.cache.get(self.key, 3600))
        self.assertIsNone(self.cache.get(self.key, 0))
        self.now[0] += 61
        self.assertIsNone(self.cache.get(self.key, 60))
        self.assertIsNotNone(self.cache.get(self.key, 3600))

    def test_keys_canonicalize_dictionary_order_and_isolate_tokens_and_bodies(self):
        make_key = self.cache.key
        args = ("token", "https://example.invalid", "POST", "/tas/")
        self.assertEqual(make_key(*args, {"a": 1, "b": 2}, {"x": 3, "y": 4}),
                         make_key(*args, {"b": 2, "a": 1}, {"y": 4, "x": 3}))
        self.assertNotEqual(make_key(*args, None, {"products": ["CL"]}),
                            make_key(*args, None, {"products": ["CO"]}))
        self.assertNotEqual(make_key(*args, None, None),
                            make_key("different-token", *args[1:], None, None))


if __name__ == "__main__":
    unittest.main()
