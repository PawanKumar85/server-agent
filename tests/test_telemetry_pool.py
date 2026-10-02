"""Tests for the thread-safe In-Memory Telemetry Data Pool."""

import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

from telemetry_pool import TelemetryDataPool


class TestTelemetryPool(unittest.TestCase):
    def test_pool_initial_state(self):
        pool = TelemetryDataPool(ttl_seconds=2.0)
        self.assertFalse(pool.is_fresh())
        stats = pool.stats()
        self.assertEqual(stats["hits"], 0)
        self.assertEqual(stats["misses"], 0)
        self.assertEqual(stats["node_count"], 0)
        self.assertFalse(stats["is_fresh"])

    def test_pool_refresh_and_caching(self):
        pool = TelemetryDataPool(ttl_seconds=1.5)

        mock_driver = MagicMock()
        mock_records = [
            {
                "p": {
                    "domain": "live1.internal",
                    "status": "UP",
                    "lastLatencyMs": 1.2,
                    "server_ip": "10.0.0.1",
                    "links": '[{"url": "http://live1.internal/hls/sports1.m3u8", "channel": "sports1", "role": "FinalLink"}]',
                    "urlHealth": '{"http://live1.internal/hls/sports1.m3u8": {"up": true, "segment_age_s": 2.5, "latency_ms": 1.1}}',
                },
                "labels": ["FinalLink"],
            },
            {
                "p": {
                    "domain": "xcode2.internal",
                    "status": "UP",
                    "lastLatencyMs": 0.8,
                    "server_ip": "10.0.0.2",
                    "links": '[{"url": "http://xcode2.internal/hls/sports1.m3u8", "channel": "sports1", "role": "Transcoding"}]',
                    "urlHealth": "{}",
                },
                "labels": ["Transcoding"],
            },
        ]

        mock_driver.execute_query.return_value = MagicMock(records=mock_records)

        # First call -> Cache Miss -> query Neo4j driver
        nodes = pool.get_nodes(mock_driver)
        self.assertEqual(len(nodes), 2)
        self.assertIn("live1.internal", nodes)
        self.assertTrue(pool.is_fresh())
        self.assertEqual(mock_driver.execute_query.call_count, 1)

        # Second call within TTL -> Cache Hit -> zero Neo4j queries
        nodes2 = pool.get_nodes(mock_driver)
        self.assertEqual(len(nodes2), 2)
        self.assertEqual(mock_driver.execute_query.call_count, 1)  # Still 1!
        stats = pool.stats()
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["misses"], 1)
        self.assertEqual(stats["hit_ratio_pct"], 50.0)

        # Inverted channel index test
        channels = pool.get_channels(mock_driver)
        self.assertIn("sports1", channels)
        self.assertIn("FinalLink", channels["sports1"])
        self.assertIn("Transcoding", channels["sports1"])
        self.assertEqual(channels["sports1"]["FinalLink"][0]["segment_age_s"], 2.5)

    def test_pool_invalidation(self):
        pool = TelemetryDataPool(ttl_seconds=5.0)
        mock_driver = MagicMock()
        mock_driver.execute_query.return_value = MagicMock(records=[
            {"p": {"domain": "srv1", "links": "[]"}, "labels": []}
        ])

        pool.get_nodes(mock_driver)
        self.assertTrue(pool.is_fresh())
        self.assertEqual(mock_driver.execute_query.call_count, 1)

        # Invalidate forces refresh on next call
        pool.invalidate()
        self.assertFalse(pool.is_fresh())

        pool.get_nodes(mock_driver)
        self.assertEqual(mock_driver.execute_query.call_count, 2)

    def test_pool_direct_metric_update(self):
        pool = TelemetryDataPool(ttl_seconds=10.0)
        mock_driver = MagicMock()
        mock_driver.execute_query.return_value = MagicMock(records=[
            {"p": {"domain": "gtc.internal", "status": "UP", "lastLatencyMs": 5.0, "links": "[]"}, "labels": []}
        ])

        pool.get_nodes(mock_driver)
        node = pool.get_node("gtc.internal")
        self.assertEqual(node["lastLatencyMs"], 5.0)

        # Spider directly pushes new metrics into pool
        pool.update_node_metric("gtc.internal", status="DEGRADED", lastLatencyMs=45.2)

        updated = pool.get_node("gtc.internal")
        self.assertEqual(updated["status"], "DEGRADED")
        self.assertEqual(updated["lastLatencyMs"], 45.2)

    def test_pool_thread_safety(self):
        pool = TelemetryDataPool(ttl_seconds=10.0)
        mock_driver = MagicMock()
        mock_driver.execute_query.return_value = MagicMock(records=[
            {"p": {"domain": f"node-{i}", "links": "[]"}, "labels": []} for i in range(10)
        ])

        # Prepopulate
        pool.get_nodes(mock_driver)

        def worker(worker_id):
            for _ in range(50):
                pool.get_nodes(mock_driver)
                pool.update_node_metric(f"node-{worker_id % 10}", pingCount=worker_id)
                pool.get_channel("test-channel", mock_driver)

        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = [ex.submit(worker, i) for i in range(16)]
            for f in futures:
                f.result()

        st = pool.stats()
        self.assertGreater(st["hits"], 0)
        self.assertGreater(st["writes"], 0)


if __name__ == "__main__":
    unittest.main()
