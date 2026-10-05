"""Model Context Protocol (MCP) Server for Stream Graph.

Standard Open-Source MCP v1.0 specification implementation providing modular,
extensible tools for multi-channel notifications (WhatsApp, SMS, Email, Slack,
Telegram, Webhook, PagerDuty) and future system extensions.
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import time
import urllib.request
import urllib.error
from abc import ABC, abstractmethod
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Type

logger = logging.getLogger("mcp_server")


# --- Outbound URL safety (webhooks): only public http(s) addresses -------------------------------------------------
import ipaddress
import socket
from urllib.parse import urlparse


def unsafe_url(url: str) -> Optional[str]:
    """Why this URL must not be called, or None. Only http/https to a public address: no file://, and nothing that
    resolves to this machine, the Docker network or any private, link-local (cloud metadata) or reserved range."""
    try:
        parts = urlparse(url)
    except ValueError:
        return "not a valid URL"
    if parts.scheme not in ("http", "https"):
        return f"scheme '{parts.scheme or '?'}' not allowed (http/https only)"
    host = parts.hostname
    if not host:
        return "no host"
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return f"host '{host}' does not resolve"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
                or ip.is_unspecified):
            return f"'{host}' resolves to a private/internal address ({ip})"
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a public URL redirecting to an internal one must not be followed


_NO_REDIRECTS = urllib.request.build_opener(_NoRedirect)


# =============================================================================
# Base MCP Tool Definition
# =============================================================================

class BaseMCPTool(ABC):
    """Abstract Base Class conforming to Model Context Protocol (MCP) v1.0 standard."""

    name: str = ""
    title: str = ""
    description: str = ""
    category: str = "Notifications"
    icon: str = "🔔"
    provider: str = "Open-Source Driver"
    parameters: Dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    @abstractmethod
    def execute(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the tool logic and return result dictionary."""
        raise NotImplementedError

    def sample_args(self) -> Dict[str, Any]:
        """Returns sample input arguments for UI testing & documentation."""
        return {}

    def to_mcp_spec(self) -> Dict[str, Any]:
        """Returns Model Context Protocol (MCP) standard tool specification."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.parameters,
        }

    def metadata(self) -> Dict[str, Any]:
        """Returns full metadata for web UI inspection and testing."""
        return {
            "name": self.name,
            "title": self.title or self.name,
            "description": self.description,
            "category": self.category,
            "icon": self.icon,
            "provider": self.provider,
            "parameters": self.parameters,
            "sample_args": self.sample_args(),
        }


# =============================================================================
# Built-in MCP Notification Tools
# =============================================================================

class MCPWhatsAppTool(BaseMCPTool):
    """MCP Tool to send urgent WhatsApp alert notifications."""

    name = "mcp_send_whatsapp"
    title = "WhatsApp Alert Dispatcher"
    description = (
        "Dispatches an urgent stream incident or operational alert to a target WhatsApp "
        "phone number via Meta Cloud API or Twilio WhatsApp gateway."
    )
    category = "Notifications"
    icon = "💬"
    provider = "Meta Cloud API / Twilio WhatsApp"
    parameters = {
        "type": "object",
        "properties": {
            "to_phone": {
                "type": "string",
                "description": "Recipient phone number with country code (e.g. +919876543210)",
            },
            "message": {
                "type": "string",
                "description": "Alert message body with incident summary and action steps",
            },
            "channel_name": {
                "type": "string",
                "description": "Affected broadcast channel name (e.g. Rang Manch)",
            },
            "severity": {
                "type": "string",
                "enum": ["INFO", "WARNING", "CRITICAL"],
                "description": "Alert severity level",
            },
        },
        "required": ["to_phone", "message"],
    }

    def sample_args(self) -> Dict[str, Any]:
        return {
            "to_phone": "+919876543210",
            "message": "CRITICAL: Stream FinalLink for Rang Manch is DOWN. Consecutive packet loss detected.",
            "channel_name": "Rang Manch",
            "severity": "CRITICAL",
        }

    def execute(self, args: Dict[str, Any]) -> Dict[str, Any]:
        phone = args.get("to_phone", "").strip()
        msg = args.get("message", "").strip()
        sev = args.get("severity", "WARNING").upper()
        ch = args.get("channel_name", "")

        token = os.environ.get("WHATSAPP_API_TOKEN")
        phone_id = os.environ.get("WHATSAPP_PHONE_ID")

        if token and phone_id:
            try:
                url = f"https://graph.facebook.com/v18.0/{phone_id}/messages"
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                }
                body = {
                    "messaging_product": "whatsapp",
                    "to": phone.replace("+", ""),
                    "type": "text",
                    "text": {"body": f"[{sev}] {ch + ' · ' if ch else ''}{msg}"},
                }
                req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp_data = json.loads(resp.read().decode("utf-8"))
                    return {"status": "sent", "provider": "Meta Cloud API", "response": resp_data}
            except Exception as e:
                logger.error(f"WhatsApp dispatch failed: {e}")
                return {"status": "error", "error": str(e)}

        # Sandbox / Mock mode
        logger.info(f"[MCP-WhatsApp-Sandbox] Sent to {phone} [{sev}]: {msg}")
        return {
            "status": "not_sent", "sent": False, "reason": "no credentials configured (sandbox): nothing was delivered",
            "provider": "Mock / Sandbox Mode (Set WHATSAPP_API_TOKEN to go live)",
            "to": phone,
            "severity": sev,
            "message_length": len(msg),
            "timestamp": time.time(),
        }


class MCPSMSTool(BaseMCPTool):
    """MCP Tool to send urgent SMS notifications."""

    name = "mcp_send_sms"
    title = "SMS Urgent Dispatcher"
    description = (
        "Dispatches a short priority SMS text alert for P0 network or server outages "
        "when internet connectivity is compromised."
    )
    category = "Notifications"
    icon = "📱"
    provider = "Twilio / AWS SNS / MSG91"
    parameters = {
        "type": "object",
        "properties": {
            "to_phone": {
                "type": "string",
                "description": "Recipient phone number with country code (e.g. +919876543210)",
            },
            "message": {
                "type": "string",
                "description": "SMS text (concise, max 160 chars recommended)",
            },
            "priority": {
                "type": "string",
                "enum": ["normal", "urgent", "p0"],
                "description": "Priority tier of the SMS",
            },
        },
        "required": ["to_phone", "message"],
    }

    def sample_args(self) -> Dict[str, Any]:
        return {
            "to_phone": "+919876543210",
            "message": "NOC P0 ALERT: Node cdn.ottlive.co.in is UNREACHABLE. Check gateway router.",
            "priority": "urgent",
        }

    def execute(self, args: Dict[str, Any]) -> Dict[str, Any]:
        phone = args.get("to_phone", "").strip()
        msg = args.get("message", "").strip()
        priority = args.get("priority", "urgent")

        account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
        auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
        from_phone = os.environ.get("TWILIO_PHONE_NUMBER")

        if account_sid and auth_token and from_phone:
            try:
                import base64
                url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
                auth = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
                headers = {
                    "Authorization": f"Basic {auth}",
                    "Content-Type": "application/x-www-form-urlencoded",
                }
                data = urllib.parse.urlencode({"To": phone, "From": from_phone, "Body": msg}).encode()
                req = urllib.request.Request(url, data=data, headers=headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp_data = json.loads(resp.read().decode())
                    return {"status": "sent", "provider": "Twilio SMS", "sid": resp_data.get("sid")}
            except Exception as e:
                logger.error(f"SMS dispatch failed: {e}")
                return {"status": "error", "error": str(e)}

        logger.info(f"[MCP-SMS-Sandbox] Dispatched to {phone} (Priority: {priority}): {msg}")
        return {
            "status": "not_sent", "sent": False, "reason": "no credentials configured (sandbox): nothing was delivered",
            "provider": "Mock / Sandbox Mode (Set TWILIO_ACCOUNT_SID to go live)",
            "to": phone,
            "priority": priority,
            "chars": len(msg),
            "timestamp": time.time(),
        }


class MCPEmailTool(BaseMCPTool):
    """MCP Tool to send detailed HTML incident reports via Email."""

    name = "mcp_send_email"
    title = "Email Incident Report Dispatcher"
    description = (
        "Sends formatted HTML incident summaries, RCA diagnosis tables, and shift "
        "handover reports to engineering mailing lists."
    )
    category = "Notifications"
    icon = "✉️"
    provider = "SMTP / Resend / SendGrid"
    parameters = {
        "type": "object",
        "properties": {
            "to_email": {
                "type": "string",
                "description": "Recipient email address (e.g. noc@ottlive.co.in)",
            },
            "subject": {
                "type": "string",
                "description": "Email subject line",
            },
            "html_body": {
                "type": "string",
                "description": "HTML or rich text body containing the report/telemetry summary",
            },
            "severity": {
                "type": "string",
                "enum": ["INFO", "WARNING", "CRITICAL"],
                "description": "Incident severity header",
            },
        },
        "required": ["to_email", "subject", "html_body"],
    }

    def sample_args(self) -> Dict[str, Any]:
        return {
            "to_email": "ops-team@ottlive.co.in",
            "subject": "[CRITICAL] Outage Incident Report: Server 172.232.121.127",
            "html_body": "<h3>Stream Graph Incident</h3><p>Server experienced 5 consecutive failures. Traceroute reveals packet drop at hop 4.</p>",
            "severity": "CRITICAL",
        }

    def execute(self, args: Dict[str, Any]) -> Dict[str, Any]:
        to_email = args.get("to_email", "").strip()
        subject = args.get("subject", "").strip()
        html = args.get("html_body", "").strip()
        sev = args.get("severity", "WARNING").upper()

        smtp_host = os.environ.get("SMTP_HOST")
        smtp_port = int(os.environ.get("SMTP_PORT", 587))
        smtp_user = os.environ.get("SMTP_USER")
        smtp_pass = os.environ.get("SMTP_PASS")
        smtp_from = os.environ.get("SMTP_FROM", smtp_user or "alerts@streamgraph.local")

        if smtp_host and smtp_user and smtp_pass:
            try:
                msg = MIMEMultipart("alternative")
                msg["Subject"] = f"[{sev}] {subject}"
                msg["From"] = smtp_from
                msg["To"] = to_email
                msg.attach(MIMEText(html, "html"))

                with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
                    server.starttls()
                    server.login(smtp_user, smtp_pass)
                    server.sendmail(smtp_from, [to_email], msg.as_string())
                return {"status": "sent", "provider": "SMTP Server", "recipient": to_email}
            except Exception as e:
                logger.error(f"Email dispatch failed: {e}")
                return {"status": "error", "error": str(e)}

        logger.info(f"[MCP-Email-Sandbox] Dispatched to {to_email} | Subject: [{sev}] {subject}")
        return {
            "status": "not_sent", "sent": False, "reason": "no credentials configured (sandbox): nothing was delivered",
            "provider": "Mock / Sandbox Mode (Set SMTP_HOST & SMTP_USER to go live)",
            "to": to_email,
            "subject": subject,
            "severity": sev,
            "html_bytes": len(html),
            "timestamp": time.time(),
        }


class MCPSlackTool(BaseMCPTool):
    """MCP Tool to send BlockKit formatted incident cards to Slack."""

    name = "mcp_send_slack"
    title = "Slack Channel Webhook"
    description = (
        "Broadcasts rich incident alerts with telemetry metrics directly into a NOC Slack channel."
    )
    category = "ChatOps"
    icon = "💼"
    provider = "Slack Incoming Webhook"
    parameters = {
        "type": "object",
        "properties": {
            "channel": {
                "type": "string",
                "description": "Target channel name (e.g. #noc-alerts)",
            },
            "headline": {
                "type": "string",
                "description": "Brief headline for the alert",
            },
            "details": {
                "type": "string",
                "description": "Markdown body with telemetry metrics and RCA findings",
            },
            "status": {
                "type": "string",
                "enum": ["RESOLVED", "INVESTIGATING", "DOWN"],
                "description": "Current status tag",
            },
        },
        "required": ["headline", "details"],
    }

    def sample_args(self) -> Dict[str, Any]:
        return {
            "channel": "#noc-alerts",
            "headline": "🚨 Stream Degraded: Rang Manch FinalLink",
            "details": "*Server:* `cdn.ottlive.co.in`\n*Latency:* 414ms\n*Segment Age:* 5.4s\n*RCA:* Upstream origin latency spike",
            "status": "DOWN",
        }

    def execute(self, args: Dict[str, Any]) -> Dict[str, Any]:
        webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
        headline = args.get("headline", "")
        details = args.get("details", "")
        st = args.get("status", "INVESTIGATING")

        color = "#e53935" if st == "DOWN" else "#43a047" if st == "RESOLVED" else "#fb8c00"
        payload = {
            "attachments": [
                {
                    "color": color,
                    "title": headline,
                    "text": details,
                    "footer": "Stream Graph MCP Alert",
                    "ts": int(time.time()),
                }
            ]
        }

        if webhook_url:
            try:
                req = urllib.request.Request(
                    webhook_url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return {"status": "sent", "provider": "Slack Webhook", "code": resp.status}
            except Exception as e:
                logger.error(f"Slack webhook failed: {e}")
                return {"status": "error", "error": str(e)}

        logger.info(f"[MCP-Slack-Sandbox] Posted alert: {headline}")
        return {
            "status": "not_sent", "sent": False, "reason": "no credentials configured (sandbox): nothing was delivered",
            "provider": "Mock / Sandbox Mode (Set SLACK_WEBHOOK_URL to go live)",
            "headline": headline,
            "channel": args.get("channel", "#noc-alerts"),
            "status_tag": st,
            "timestamp": time.time(),
        }


class MCPTelegramTool(BaseMCPTool):
    """MCP Tool to send alerts to Telegram channels or groups."""

    name = "mcp_send_telegram"
    title = "Telegram Broadcast Bot"
    description = (
        "Sends real-time incident broadcasts to Telegram NOC engineering channels or direct chats."
    )
    category = "ChatOps"
    icon = "✈️"
    provider = "Telegram Bot API"
    parameters = {
        "type": "object",
        "properties": {
            "chat_id": {
                "type": "string",
                "description": "Telegram chat ID or @channelusername",
            },
            "text": {
                "type": "string",
                "description": "Markdown formatted message text",
            },
        },
        "required": ["text"],
    }

    def sample_args(self) -> Dict[str, Any]:
        return {
            "chat_id": "@noc_stream_alerts",
            "text": "🔴 *CRITICAL ALERT*\nChannel: *Rang Manch*\nServer: `172.232.121.127`\nStatus: *DOWN* (Ping fail 5x)",
        }

    def execute(self, args: Dict[str, Any]) -> Dict[str, Any]:
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = args.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID", "@noc_stream_alerts")
        text = args.get("text", "")

        if bot_token and chat_id:
            try:
                url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                payload = {
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                }
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp_data = json.loads(resp.read().decode())
                    return {"status": "sent", "provider": "Telegram Bot", "ok": resp_data.get("ok")}
            except Exception as e:
                logger.error(f"Telegram dispatch failed: {e}")
                return {"status": "error", "error": str(e)}

        logger.info(f"[MCP-Telegram-Sandbox] Sent to {chat_id}: {text}")
        return {
            "status": "not_sent", "sent": False, "reason": "no credentials configured (sandbox): nothing was delivered",
            "provider": "Mock / Sandbox Mode (Set TELEGRAM_BOT_TOKEN to go live)",
            "chat_id": chat_id,
            "text_preview": text[:120],
            "timestamp": time.time(),
        }


class MCPGenericWebhookTool(BaseMCPTool):
    """MCP Tool to fire custom JSON webhooks (n8n, Zapier, Make, custom NOC APIs)."""

    name = "mcp_send_webhook"
    title = "Generic HTTP Webhook Hook"
    description = (
        "Dispatches an arbitrary JSON telemetry payload to any external HTTP webhook "
        "(compatible with Zapier, n8n, Make, AWS Lambda, or custom NOC monitoring hubs)."
    )
    category = "Integrations"
    icon = "🔗"
    provider = "HTTP POST JSON"
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Destination webhook URL (e.g. https://hooks.zapier.com/...)",
            },
            "event_type": {
                "type": "string",
                "description": "Event classification (e.g. stream.outage, node.recovered)",
            },
            "payload": {
                "type": "object",
                "description": "Structured JSON payload to transmit",
            },
        },
        "required": ["url", "payload"],
    }

    def sample_args(self) -> Dict[str, Any]:
        return {
            "url": "https://api.noc-hub.example/v1/webhook",
            "event_type": "stream.outage",
            "payload": {
                "domain": "cdn.ottlive.co.in",
                "status": "DOWN",
                "latency_ms": 414,
                "freshness": "STALE",
            },
        }

    def execute(self, args: Dict[str, Any]) -> Dict[str, Any]:
        url = args.get("url", "").strip()
        problem = unsafe_url(url) if url else None
        if problem:
            return {"status": "error", "sent": False, "error": f"Refused webhook URL: {problem}"}
        event_type = args.get("event_type", "alert")
        body = args.get("payload", {})

        if not url:
            return {"status": "error", "error": "Missing webhook URL"}

        try:
            req_data = json.dumps({"event": event_type, "timestamp": time.time(), "data": body}).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=req_data,
                headers={"Content-Type": "application/json", "User-Agent": "StreamGraph-MCP/1.0"},
            )
            with _NO_REDIRECTS.open(req, timeout=10) as resp:  # a redirect could point inside: not followed
                return {"status": "sent", "sent": True, "http_code": resp.status, "url": url}
        except Exception as e:
            logger.error(f"Webhook to {url} failed: {e}")
            return {"status": "error", "sent": False, "error": f"{type(e).__name__}: {e}", "target_url": url}


class MCPPagerDutyTool(BaseMCPTool):
    """MCP Tool to trigger or resolve incidents via PagerDuty Events API v2."""

    name = "mcp_trigger_pagerduty"
    title = "PagerDuty Incident Orchestrator"
    description = (
        "Triggers, acknowledges, or resolves high-severity streaming incidents on "
        "PagerDuty for automated on-call rotation paging."
    )
    category = "Escalation"
    icon = "🚨"
    provider = "PagerDuty Events API v2"
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["trigger", "acknowledge", "resolve"],
                "description": "Event action",
            },
            "summary": {
                "type": "string",
                "description": "Short incident summary",
            },
            "severity": {
                "type": "string",
                "enum": ["critical", "error", "warning", "info"],
                "description": "PagerDuty incident severity",
            },
            "source": {
                "type": "string",
                "description": "Source identifier (e.g. streamgraph-spider)",
            },
        },
        "required": ["summary"],
    }

    def sample_args(self) -> Dict[str, Any]:
        return {
            "action": "trigger",
            "summary": "Rang Manch FinalLink Stream Outage - 5 Consecutive Health Check Failures",
            "severity": "critical",
            "source": "streamgraph-spider",
        }

    def execute(self, args: Dict[str, Any]) -> Dict[str, Any]:
        routing_key = os.environ.get("PAGERDUTY_ROUTING_KEY")
        action = args.get("action", "trigger")
        summary = args.get("summary", "")
        sev = args.get("severity", "critical")
        source = args.get("source", "streamgraph")

        if routing_key:
            try:
                url = "https://events.pagerduty.com/v2/enqueue"
                payload = {
                    "routing_key": routing_key,
                    "event_action": action,
                    "payload": {
                        "summary": summary,
                        "severity": sev,
                        "source": source,
                    },
                }
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp_data = json.loads(resp.read().decode())
                    return {"status": "sent", "provider": "PagerDuty", "dedup_key": resp_data.get("dedup_key")}
            except Exception as e:
                logger.error(f"PagerDuty dispatch failed: {e}")
                return {"status": "error", "error": str(e)}

        logger.info(f"[MCP-PagerDuty-Sandbox] Action: {action} | Summary: {summary}")
        return {
            "status": "not_sent", "sent": False, "reason": "no credentials configured (sandbox): nothing was delivered",
            "provider": "Mock / Sandbox Mode (Set PAGERDUTY_ROUTING_KEY to go live)",
            "action": action,
            "severity": sev,
            "summary": summary,
            "timestamp": time.time(),
        }


# =============================================================================
# Central MCP Server Registry
# =============================================================================

class MCPServer:
    """Model Context Protocol (MCP) Server hosting tools for LLM agent integration."""

    def __init__(self) -> None:
        self._tools: Dict[str, BaseMCPTool] = {}
        self._register_defaults()

    def register(self, tool: BaseMCPTool) -> None:
        """Register an MCP tool into the registry."""
        self._tools[tool.name] = tool
        logger.info(f"Registered MCP tool: {tool.name} [{tool.category}]")

    def get_tool(self, name: str) -> Optional[BaseMCPTool]:
        """Lookup an MCP tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> Dict[str, Any]:
        """Returns standard Model Context Protocol (MCP) tools payload."""
        return {
            "tools": [t.to_mcp_spec() for t in self._tools.values()]
        }

    def get_tools_metadata(self) -> List[Dict[str, Any]]:
        """Returns detailed metadata for web UI inspection and testing."""
        return [t.metadata() for t in self._tools.values()]

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Executes an MCP tool adhering to standard MCP response envelope."""
        tool = self.get_tool(name)
        if not tool:
            return {
                "content": [
                    {"type": "text", "text": f"Error: Tool '{name}' not found on MCP Server."}
                ],
                "isError": True,
            }

        try:
            result = tool.execute(arguments)
            return {
                "content": [
                    {"type": "text", "text": json.dumps(result, indent=2)}
                ],
                "result": result,
                "isError": result.get("status") == "error",
            }
        except Exception as e:
            logger.error(f"Error executing MCP tool {name}: {e}")
            return {
                "content": [
                    {"type": "text", "text": f"Execution error: {str(e)}"}
                ],
                "isError": True,
            }

    def _register_defaults(self) -> None:
        """Pre-populate the server with all built-in notification & integration tools."""
        self.register(MCPWhatsAppTool())
        self.register(MCPSMSTool())
        self.register(MCPEmailTool())
        self.register(MCPSlackTool())
        self.register(MCPTelegramTool())
        self.register(MCPGenericWebhookTool())
        self.register(MCPPagerDutyTool())


# Global singleton MCP Server instance
mcp_server = MCPServer()
