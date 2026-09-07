#!/usr/bin/env python3
"""Post a 'stage view' of the most interesting upcoming debate to Discord.

Most interesting = the upcoming match with the most cross-agent critique
traffic. Read-only on the ledger; posts via the same webhook as the nightly
summary (scripts/notify_discord.py). Fails soft when no webhook/contest.
"""
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from db import connect  # noqa: E402

MAX_ROWS = 6


def webhook_url():
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if url:
        return url
    env = Path.home() / ".hermes" / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("DISCORD_WEBHOOK_URL="):
                return line.split("=", 1)[1].strip()
    return None


def build_stage(conn):
    row = conn.execute(
        "SELECT d.match_id, COUNT(*) c FROM discussion d "
        "JOIN matches m ON m.match_id = d.match_id "
        "WHERE m.status = 'not_started' "
        "GROUP BY d.match_id ORDER BY c DESC, d.match_id LIMIT 1").fetchone()
    if not row:
        return None
    match = conn.execute(
        "SELECT team1, team2, begin_at, league, tournament FROM matches "
        "WHERE match_id = ?", (row["match_id"],)).fetchone()
    lines = [
        f"**{match['team1']} vs {match['team2']}** — "
        f"{(match['begin_at'] or '?')[:16].replace('T', ' ')} UTC "
        f"({match['league'] or '?'}/{match['tournament'] or '?'})",
        "",
    ]
    picks = conn.execute(
        "SELECT agent, selection, confidence FROM picks WHERE match_id = ? "
        "ORDER BY agent", (row["match_id"],)).fetchall()
    lines.append("__Round 1 — positions__")
    for p in picks:
        lines.append(f"• **{p['agent']}**: {p['selection']} @ {p['confidence']:.2f}")
    lines.append("")
    lines.append("__Round 2 — cross-examination__")
    stmts = conn.execute(
        "SELECT agent, statement FROM discussion WHERE match_id = ? AND round = 2 "
        "ORDER BY id", (row["match_id"],)).fetchall()
    if not stmts:
        lines.append("• (no objections filed — unanimous pick)")
    for s in stmts[:MAX_ROWS]:
        lines.append(f"• **{s['agent']}**: {s['statement']}")
    if len(stmts) > MAX_ROWS:
        lines.append(f"• …and {len(stmts) - MAX_ROWS} more in the ledger")
    return "\n".join(lines)


def main():
    conn = connect()
    content = build_stage(conn)
    conn.close()
    if not content:
        print("no debate content to post")
        return 0
    webhook = webhook_url()
    if not webhook:
        print("no DISCORD_WEBHOOK_URL — would post:\n" + content[:400])
        return 0
    data = __import__("json").dumps(
        {"content": f"**cvbets debate stage**\n{content}"[:1900]}).encode()
    req = urllib.request.Request(
        webhook, data=data, headers={"Content-Type": "application/json",
                                     "User-Agent": "cvbets-bot (github actions, 1.0)"})
    urllib.request.urlopen(req, timeout=20)
    print("debate stage posted to Discord")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())