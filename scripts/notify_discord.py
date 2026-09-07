#!/usr/bin/env python3
"""Post the latest cvbets nightly summary to Discord via webhook.

Reads the most recent "--- run ... ---- daily summary ----" block from
data/daily.log and posts it to DISCORD_WEBHOOK_URL (env or ~/.hermes/.env
fallback for local runs). With an argument, posts that text instead.
Exit 0 silently skips when no webhook is configured (e.g. local runs).
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DAILY_LOG = ROOT / "data" / "daily.log"


def load_webhook():
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if url:
        return url
    env = Path.home() / ".hermes" / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("DISCORD_WEBHOOK_URL="):
                return line.split("=", 1)[1].strip()
    return None


def latest_summary():
    text = DAILY_LOG.read_text() if DAILY_LOG.exists() else ""
    blocks = text.split("--- run ")
    if len(blocks) < 2:
        return None
    return blocks[-1].strip()


def send(webhook, content):
    data = json.dumps({"content": content[:1900]}).encode()
    req = urllib.request.Request(
        webhook, data=data, headers={"Content-Type": "application/json",
                                     "User-Agent": "cvbets-bot (github actions, 1.0)"})
    urllib.request.urlopen(req, timeout=20)


def main():
    webhook = load_webhook()
    if not webhook:
        print("no DISCORD_WEBHOOK_URL configured — skipping Discord delivery")
        return 0
    content = " ".join(sys.argv[1:]).strip() if len(sys.argv) > 1 else latest_summary()
    if not content:
        print("no summary content — nothing to send")
        return 0
    send(webhook, f"**cvbets nightly (GitHub Actions)**\n{content}")
    print("delivered to Discord webhook")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())