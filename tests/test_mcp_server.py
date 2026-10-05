"""Unit tests for Model Context Protocol (MCP) Server and Tools."""

import unittest
from mcp_server import mcp_server, BaseMCPTool


class TestMCPServer(unittest.TestCase):
    def test_mcp_registry_defaults(self):
        tools = mcp_server.list_tools()
        self.assertIn("tools", tools)
        self.assertGreaterEqual(len(tools["tools"]), 7)

        names = [t["name"] for t in tools["tools"]]
        self.assertIn("mcp_send_whatsapp", names)
        self.assertIn("mcp_send_sms", names)
        self.assertIn("mcp_send_email", names)
        self.assertIn("mcp_send_slack", names)
        self.assertIn("mcp_send_telegram", names)
        self.assertIn("mcp_send_webhook", names)
        self.assertIn("mcp_trigger_pagerduty", names)

    def test_mcp_whatsapp_sandbox_execution(self):
        res = mcp_server.call_tool("mcp_send_whatsapp", {
            "to_phone": "+919876543210",
            "message": "Outage detected on server",
            "severity": "CRITICAL"
        })
        self.assertFalse(res.get("isError"))
        self.assertIn("content", res)
        self.assertEqual(res["result"]["status"], "not_sent")

    def test_mcp_email_sandbox_execution(self):
        res = mcp_server.call_tool("mcp_send_email", {
            "to_email": "noc@ottlive.co.in",
            "subject": "Stream Outage Alert",
            "html_body": "<h3>Server Down</h3>",
            "severity": "CRITICAL"
        })
        self.assertFalse(res.get("isError"))
        self.assertEqual(res["result"]["status"], "not_sent")

    def test_mcp_sms_sandbox_execution(self):
        res = mcp_server.call_tool("mcp_send_sms", {
            "to_phone": "+919876543210",
            "message": "P0 Alert: Gateway unreachable",
            "priority": "urgent"
        })
        self.assertFalse(res.get("isError"))
        self.assertEqual(res["result"]["status"], "not_sent")

    def test_mcp_unknown_tool(self):
        res = mcp_server.call_tool("mcp_unknown_tool", {})
        self.assertTrue(res.get("isError"))


if __name__ == "__main__":
    unittest.main()


def test_webhook_refuses_files_internal_and_private_addresses():
    from mcp_server import mcp_server, unsafe_url
    hook = mcp_server.get_tool("mcp_send_webhook")
    for url in ("file:///etc/hosts", "http://127.0.0.1:7474/", "http://localhost/x", "http://10.0.0.5/hook",
                "http://169.254.169.254/latest/meta-data/", "ftp://example.com/x", "gopher://x"):
        r = hook.execute({"url": url, "payload": {"a": 1}})
        assert r["status"] == "error" and r["sent"] is False and "Refused" in r["error"], (url, r)
    assert unsafe_url("https://8.8.8.8/hook") is None  # a public address is allowed


def test_nothing_is_reported_as_delivered_without_credentials(monkeypatch):
    from mcp_server import mcp_server
    for var in ("WHATSAPP_API_TOKEN", "TWILIO_ACCOUNT_SID", "SMTP_HOST", "SLACK_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN",
                "PAGERDUTY_ROUTING_KEY"):
        monkeypatch.delenv(var, raising=False)
    for tool in mcp_server._tools.values():
        if tool.name == "mcp_send_webhook":
            continue
        r = tool.execute(tool.sample_args())
        assert r["status"] == "not_sent" and r["sent"] is False and "nothing was delivered" in r["reason"], tool.name


def test_notification_tools_are_offered_only_when_asked_about():
    from tools import Tools
    names = lambda q: {d["function"]["name"] for d in Tools.definitions_for(q)}  # noqa: E731
    assert not any(n.startswith("mcp_") for n in names("is xcode4 up?"))
    assert "mcp_send_email" in names("send an email to the NOC team about tnpnews")
    assert "recommend_cdn_placement" in names("where should we put a new CDN?")
