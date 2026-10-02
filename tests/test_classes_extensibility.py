"""Tests for the class-based extensible architecture: Config, Probes, and Tools."""

import unittest
from config import StreamGraphConfig
from probes import BaseStreamProbe, ProbeRegistry
from tools_registry import BaseChatTool, ChatToolRegistry


class TestClassWiseArchitecture(unittest.TestCase):
    def test_config_loading(self):
        cfg = StreamGraphConfig.load()
        self.assertIsNotNone(cfg.neo4j.uri)
        self.assertGreater(cfg.probing.concurrency_limit, 0)
        self.assertGreater(cfg.adaptive.default_interval_s, 0)
        self.assertIsNotNone(cfg.ai.chatbot_model)
        self.assertEqual(cfg.langsmith.project, "pr-jaunty-duck-89")
        self.assertTrue(cfg.langsmith.enabled)

    def test_probe_registry_and_extensibility(self):
        protocols = ProbeRegistry.supported_protocols()
        self.assertIn("HLS", protocols)
        self.assertIn("HTTP", protocols)
        self.assertIn("ICMP", protocols)

        # Register a custom probe class
        @ProbeRegistry.register("CUSTOM_SRT")
        class CustomSrtProbe(BaseStreamProbe):
            protocol = "CUSTOM_SRT"
            async def check(self, url, **kwargs):
                return None

        self.assertIn("CUSTOM_SRT", ProbeRegistry.supported_protocols())
        probe = ProbeRegistry.get("CUSTOM_SRT")
        self.assertIsNotNone(probe)
        self.assertEqual(probe.protocol, "CUSTOM_SRT")

    def test_tool_registry_and_extensibility(self):
        tools = ChatToolRegistry.all_tools()
        self.assertIn("get_stream_freshness_summary", tools)

        # Register a custom diagnostic tool
        @ChatToolRegistry.register
        class PingCounterTool(BaseChatTool):
            name = "custom_ping_counter"
            description = "Count total server nodes in graph."
            category = "Testing"
            icon = "🔢"
            def execute(self, executor, args):
                yield {"type": "token", "text": "42"}

        self.assertIn("custom_ping_counter", ChatToolRegistry.all_tools())
        defs = ChatToolRegistry.definitions()
        names = [d["function"]["name"] for d in defs]
        self.assertIn("custom_ping_counter", names)

        meta = ChatToolRegistry.metadata_map()
        self.assertEqual(meta["custom_ping_counter"]["category"], "Testing")
        self.assertEqual(meta["custom_ping_counter"]["icon"], "🔢")


if __name__ == "__main__":
    unittest.main()
