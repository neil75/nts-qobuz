"""Notification helpers for service mode.

Supports four optional transports, all configured via the `notifications`
block of the service config. Any combination may be enabled — failures
sending one notification do not prevent others from being sent.

  notifications.ntfy_topic       - ntfy.sh topic URL (e.g. https://ntfy.sh/my-topic)
  notifications.discord_webhook  - Discord webhook URL
  notifications.telegram         - {"bot_token": "...", "chat_id": "..."}
  notifications.email            - {"smtp_host": ..., "smtp_port": 587,
                                    "smtp_user": ..., "smtp_password": ...,
                                    "from": ..., "to": ..., "use_starttls": true}

Filtering knobs (all default to true except notify_on_no_changes):
  notifications.notify_on_success     - send when tracks were added
  notifications.notify_on_error       - send on auth failures, exceptions
  notifications.notify_on_no_changes  - send even when nothing was added
"""

import smtplib
import ssl
from email.message import EmailMessage
from typing import Optional

import requests
from rich.console import Console

console = Console()

DEFAULT_TIMEOUT = 15


class Notifier:
    def __init__(self, config: Optional[dict]):
        self.config = config or {}

    def _channel_enabled(self, key: str) -> bool:
        v = self.config.get(key)
        if not v:
            return False
        if key == "telegram":
            return bool(v.get("bot_token") and v.get("chat_id"))
        if key == "email":
            return bool(v.get("smtp_host") and v.get("from") and v.get("to"))
        return bool(v)  # string URLs

    @property
    def enabled(self) -> bool:
        return any(
            self._channel_enabled(k)
            for k in ("ntfy_topic", "discord_webhook", "telegram", "email")
        )

    def should_send(self, level: str, has_changes: bool) -> bool:
        if not self.enabled:
            return False
        if level == "error":
            return bool(self.config.get("notify_on_error", True))
        if has_changes:
            return bool(self.config.get("notify_on_success", True))
        return bool(self.config.get("notify_on_no_changes", False))

    def notify(self, title: str, body: str, level: str = "info") -> None:
        """Fire-and-forget: send `body` via every configured transport."""
        if not self.enabled:
            return

        for fn, key in (
            (self._send_ntfy, "ntfy_topic"),
            (self._send_discord, "discord_webhook"),
            (self._send_telegram, "telegram"),
            (self._send_email, "email"),
        ):
            if not self._channel_enabled(key):
                continue
            try:
                fn(title, body, level)
            except Exception as e:
                console.print(f"[yellow]Notification via {key} failed: {e}[/yellow]")

    # ------------------------------------------------------------------
    # Transports
    # ------------------------------------------------------------------

    def _send_ntfy(self, title: str, body: str, level: str) -> None:
        url = self.config["ntfy_topic"]
        priority = {"info": "default", "warning": "high", "error": "urgent"}.get(
            level, "default"
        )
        tags = {
            "info": "white_check_mark",
            "warning": "warning",
            "error": "rotating_light",
        }.get(level, "")
        headers = {
            "Title": title.encode("utf-8"),
            "Priority": priority,
        }
        if tags:
            headers["Tags"] = tags
        resp = requests.post(
            url,
            data=body.encode("utf-8"),
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()

    def _send_discord(self, title: str, body: str, level: str) -> None:
        url = self.config["discord_webhook"]
        colour = {"info": 0x2ECC71, "warning": 0xF1C40F, "error": 0xE74C3C}.get(
            level, 0x95A5A6
        )
        payload = {
            "embeds": [
                {"title": title, "description": body[:4000], "color": colour}
            ]
        }
        resp = requests.post(url, json=payload, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()

    def _send_telegram(self, title: str, body: str, level: str) -> None:
        cfg = self.config["telegram"]
        bot_token = cfg["bot_token"]
        chat_id = cfg["chat_id"]
        text = f"*{title}*\n\n{body}"
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        resp = requests.post(
            url,
            data={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()

    def _send_email(self, title: str, body: str, level: str) -> None:
        cfg = self.config["email"]
        msg = EmailMessage()
        msg["Subject"] = f"[nts-qobuz] {title}"
        msg["From"] = cfg["from"]
        msg["To"] = cfg["to"]
        msg.set_content(body)

        host = cfg["smtp_host"]
        port = int(cfg.get("smtp_port", 587))
        user = cfg.get("smtp_user")
        password = cfg.get("smtp_password")
        use_starttls = cfg.get("use_starttls", True)

        if port == 465:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=DEFAULT_TIMEOUT) as s:
                if user and password:
                    s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=DEFAULT_TIMEOUT) as s:
                s.ehlo()
                if use_starttls:
                    s.starttls(context=ssl.create_default_context())
                    s.ehlo()
                if user and password:
                    s.login(user, password)
                s.send_message(msg)
