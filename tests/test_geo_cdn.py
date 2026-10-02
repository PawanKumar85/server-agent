"""Unit tests for Geolocation, Server Distance, and CDN Placement Tool."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import unittest
from geo_cdn_tool import (
    haversine_distance_km,
    resolve_server_geolocation,
    compute_network_stretch,
    analyze_best_cdn_locations,
    RecommendCdnPlacementTool,
    ServerGeoDistanceMatrixTool,
    CANDIDATE_CDN_POPS
)
from tools_registry import ChatToolRegistry


class TestGeoCDNTool(unittest.TestCase):

    def test_haversine_mumbai_to_delhi(self):
        # Mumbai ~ 19.0760, 72.8777; Delhi/Noida ~ 28.5355, 77.3910
        dist = haversine_distance_km(19.0760, 72.8777, 28.5355, 77.3910)
        # Expected distance ~ 1,150 km to 1,200 km
        self.assertGreaterEqual(dist, 1100.0)
        self.assertLessEqual(dist, 1250.0)

    def test_resolve_server_geolocation(self):
        geo_mumbai = resolve_server_geolocation("cdn.ottlive.co.in")
        self.assertEqual(geo_mumbai["city"], "Mumbai")
        self.assertEqual(geo_mumbai["country"], "India")

        geo_noida = resolve_server_geolocation("gtc.ottlive.co.in")
        self.assertEqual(geo_noida["city"], "Noida")

    def test_speed_of_light_network_stretch(self):
        # 1,150 km distance with 65 ms observed RTT
        stretch = compute_network_stretch(1150.0, 65.0)
        self.assertEqual(stretch["distance_km"], 1150.0)
        # Fiber RTT limit ~ 11.5 ms
        self.assertGreaterEqual(stretch["theoretical_fiber_rtt_ms"], 10.0)
        # Stretch factor ~ 65 / 11.5 ~ 5.6
        self.assertGreater(stretch["stretch_factor"], 4.0)
        self.assertIn("TROMBONE_INEFFICIENT", stretch["efficiency"])

    def test_cdn_placement_analysis(self):
        nodes = [
            {"id": "origin.mumbai", "lat": 19.0760, "lon": 72.8777, "latency": 25.0, "weight": 2},
            {"id": "gtc.ottlive.co.in/gtcpunjabi", "lat": 28.5355, "lon": 77.3910, "latency": 85.0, "weight": 8, "channel": "gtcpunjabi"},
            {"id": "punjabshort", "lat": 28.5355, "lon": 77.3910, "latency": 92.0, "weight": 9, "channel": "punjabshort"}
        ]
        res = analyze_best_cdn_locations(nodes)
        self.assertIsNotNone(res["top_recommendation"])
        # With high latency/failures in North India, Delhi NCR must be the top recommended PoP
        top_pop = res["top_recommendation"]
        self.assertEqual(top_pop["city"], "Noida")
        self.assertIn("gtcpunjabi", top_pop["covered_channels"])
        self.assertGreater(top_pop["expected_latency_savings_ms"], 30.0)

    def test_tools_registered_in_registry(self):
        t1 = ChatToolRegistry.get("recommend_cdn_placement")
        t2 = ChatToolRegistry.get("get_server_geo_matrix")
        self.assertIsNotNone(t1)
        self.assertIsNotNone(t2)
        self.assertEqual(t1.name, "recommend_cdn_placement")
        self.assertEqual(t2.name, "get_server_geo_matrix")


if __name__ == "__main__":
    unittest.main()
