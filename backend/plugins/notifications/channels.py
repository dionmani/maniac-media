"""Built-in notification channels: Email, Discord, Telegram, generic Webhook."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import smtplib
import ssl
from email.message import EmailMessage

from backend.core import http
from backend.domain.types import NotificationMessage
from backend.plugins.base import FieldSpec, NotificationPlugin, PluginError
from backend.plugins.registry import register

SEVERITY_COLOURS = {"info": 0x06B6D4, "success": 0x10B981, "warning": 0xF59E0B, "error": 0xEF4444}


@register
class EmailNotifier(NotificationPlugin):
    key = "email"
    name = "Email (SMTP)"
    description = "Send notifications through any SMTP server."
    fields = [
        FieldSpec("host", "SMTP host", required=True),
        FieldSpec("port", "Port", "number", default=587),
        FieldSpec("security", "Security", "select", default="starttls", options=["starttls", "ssl", "none"]),
        FieldSpec("username", "Username"),
        FieldSpec("password", "Password", "password", secret=True),
        FieldSpec("from_address", "From", required=True),
        FieldSpec("to_addresses", "To (comma separated)", required=True),
    ]

    def _send_sync(self, message: NotificationMessage) -> None:
        msg = EmailMessage()
        msg["Subject"] = f"[Maniac Media] {message.title}"
        msg["From"] = self.setting("from_address")
        msg["To"] = self.setting("to_addresses")
        lines = [message.body, ""] + [f"{k}: {v}" for k, v in message.fields.items()]
        msg.set_content("\n".join(lines))
        host, port, security = self.setting("host"), int(self.setting("port", 587)), self.setting("security", "starttls")
        context = ssl.create_default_context()
        server = smtplib.SMTP_SSL(host, port, context=context, timeout=20) if security == "ssl" else smtplib.SMTP(host, port, timeout=20)
        try:
            if security == "starttls":
                server.starttls(context=context)
            if self.setting("username"):
                server.login(self.setting("username"), self.setting("password", ""))
            server.send_message(msg)
        finally:
            server.quit()

    async def send(self, message: NotificationMessage) -> None:
        await asyncio.to_thread(self._send_sync, message)


@register
class DiscordNotifier(NotificationPlugin):
    key = "discord"
    name = "Discord"
    description = "Post rich embeds to a Discord channel webhook."
    fields = [
        FieldSpec("webhook_url", "Webhook URL", "password", required=True, secret=True),
        FieldSpec("username", "Bot name", default="Maniac Media"),
    ]

    async def send(self, message: NotificationMessage) -> None:
        embed = {
            "title": message.title,
            "description": message.body,
            "color": SEVERITY_COLOURS.get(message.severity, SEVERITY_COLOURS["info"]),
            "fields": [{"name": k, "value": str(v)[:1000], "inline": True} for k, v in message.fields.items()],
        }
        if message.image_url and message.image_url.startswith("http"):
            embed["thumbnail"] = {"url": message.image_url}
        async with http.client(timeout=15) as c:
            resp = await http.request(c, "POST", self.setting("webhook_url"), json={"username": self.setting("username"), "embeds": [embed]}, raise_for_status=False)
            if resp.status_code >= 300:
                raise PluginError(f"Discord returned HTTP {resp.status_code}")


@register
class TelegramNotifier(NotificationPlugin):
    key = "telegram"
    name = "Telegram"
    description = "Message a Telegram chat through a bot."
    fields = [
        FieldSpec("bot_token", "Bot token", "password", required=True, secret=True),
        FieldSpec("chat_id", "Chat id", required=True),
        FieldSpec("silent", "Silent", "bool", default=False),
    ]

    async def send(self, message: NotificationMessage) -> None:
        def esc(text: str) -> str:
            return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        text = f"<b>{esc(message.title)}</b>\n{esc(message.body)}"
        if message.fields:
            text += "\n" + "\n".join(f"<i>{esc(k)}</i>: {esc(str(v))}" for k, v in message.fields.items())
        url = f"https://api.telegram.org/bot{self.setting('bot_token')}/sendMessage"
        async with http.client(timeout=15) as c:
            resp = await http.request(
                c, "POST", url,
                json={"chat_id": self.setting("chat_id"), "text": text, "parse_mode": "HTML", "disable_notification": bool(self.setting("silent"))},
                raise_for_status=False,
            )
            if resp.status_code >= 300:
                raise PluginError(f"Telegram returned HTTP {resp.status_code}: {resp.json().get('description', '')}")


@register
class WebhookNotifier(NotificationPlugin):
    key = "webhook"
    name = "Webhook"
    description = "POST a JSON payload to any URL, optionally signed with HMAC-SHA256."
    fields = [
        FieldSpec("url", "URL", required=True),
        FieldSpec("method", "Method", "select", default="POST", options=["POST", "PUT"]),
        FieldSpec("secret", "Signing secret", "password", secret=True, help="Adds X-Maniac-Signature: sha256=<hmac>"),
    ]

    async def send(self, message: NotificationMessage) -> None:
        body = json.dumps(
            {"event": message.event, "title": message.title, "body": message.body, "fields": message.fields, "severity": message.severity}
        ).encode()
        headers = {"Content-Type": "application/json"}
        if self.setting("secret"):
            sig = hmac.new(self.setting("secret").encode(), body, hashlib.sha256).hexdigest()
            headers["X-Maniac-Signature"] = f"sha256={sig}"
        async with http.client(timeout=15) as c:
            resp = await http.request(c, self.setting("method", "POST"), self.setting("url"), content=body, headers=headers, raise_for_status=False)
            if resp.status_code >= 300:
                raise PluginError(f"Webhook returned HTTP {resp.status_code}")
