"""Pluggable team notifications. Anything with `send(subject, text, key)` works.

- ResendNotifier: Resend's HTTP API, with an Idempotency-Key so a retry never
  sends the same email twice (Resend keeps keys for 24 hours).
- SMTPNotifier: any SMTP server (Gmail app password, Microsoft 365, Postmark...).
- CommandNotifier: hands the email to an existing mail script on the host,
  called as `<command> <to> <subject> <body_file>`.
- ConsoleNotifier: prints, for dry runs.
"""
from __future__ import annotations

import os
import smtplib
import subprocess
import tempfile
from email.mime.text import MIMEText
from typing import Callable, List, Optional, Protocol

from .http import request


class Notifier(Protocol):
    def send(self, subject: str, text: str, key: str) -> None: ...


class ConsoleNotifier:
    def __init__(self):
        self.sent: List[dict] = []

    def send(self, subject: str, text: str, key: str) -> None:
        self.sent.append({"subject": subject, "text": text, "key": key})
        print(f"--- email ({key}) ---\nSubject: {subject}\n\n{text}\n")


class ResendNotifier:
    def __init__(self, api_key: str, from_address: str, to: List[str], send: Optional[Callable] = None):
        self.api_key, self.from_address, self.to = api_key, from_address, to
        self._send = send or request

    def send(self, subject: str, text: str, key: str) -> None:
        self._send("Resend", "POST", "https://api.resend.com/emails",
                   {"Authorization": f"Bearer {self.api_key}", "Idempotency-Key": key},
                   json_body={"from": self.from_address, "to": self.to, "subject": subject, "text": text})


class SMTPNotifier:
    def __init__(self, host: str, port: int, username: str, password: str,
                 from_address: str, to: List[str], smtp_factory: Callable = smtplib.SMTP):
        self.host, self.port, self.username, self.password = host, port, username, password
        self.from_address, self.to, self._smtp = from_address, to, smtp_factory

    def send(self, subject: str, text: str, key: str) -> None:
        msg = MIMEText(text)
        msg["Subject"], msg["From"], msg["To"] = subject, self.from_address, ", ".join(self.to)
        msg["X-Order-Flow-Key"] = key
        with self._smtp(self.host, self.port, timeout=20) as s:
            s.starttls()
            if self.username:
                s.login(self.username, self.password)
            s.sendmail(self.from_address, self.to, msg.as_string())


class CommandNotifier:
    def __init__(self, command: List[str], to: str, run: Callable = subprocess.run):
        self.command, self.to, self._run = command, to, run

    def send(self, subject: str, text: str, key: str) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(text)
            path = f.name
        try:
            result = self._run(self.command + [self.to, subject, path], capture_output=True, text=True, timeout=60)
            if result.returncode != 0:
                raise RuntimeError(f"mail command failed: {result.stderr.strip()[:300]}")
        finally:
            os.unlink(path)


def from_env(env=os.environ) -> Notifier:
    kind = env.get("NOTIFIER", "console").lower()
    to = [a.strip() for a in env.get("NOTIFY_TO", "").split(",") if a.strip()]
    if kind == "resend":
        return ResendNotifier(env["RESEND_API_KEY"], env["NOTIFY_FROM"], to)
    if kind == "smtp":
        return SMTPNotifier(env["SMTP_HOST"], int(env.get("SMTP_PORT", "587")), env.get("SMTP_USERNAME", ""),
                            env.get("SMTP_PASSWORD", ""), env["NOTIFY_FROM"], to)
    if kind == "command":
        return CommandNotifier(env["NOTIFY_COMMAND"].split(), to[0])
    return ConsoleNotifier()
