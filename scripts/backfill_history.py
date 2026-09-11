"""backfill_history.py — one-shot deep history backfill for walk-forward backtesting.

Fetches /csgo/matches/past page by page (newest-first, per_page=100) until matches
are older than --start (default: 2 years back), upserting into the SAME ledger.db
via ingest.to_row/upsert_rows (same skip rules, same upsert guards — winner/score
are never clobbered). Designed to run in GitHub Actions where PANDASCORE_TOKEN is
a repo secret. Handles 429s by honoring X-RateLimit-Reset.

Run: python scripts/backfill_history.py --start 2024-09-11 [--max-pages 400]
Progress prints every 10 pages; safe to re-run (idempotent upserts).
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # for db + ingest imports

import ingest  # noqa: E402
from db import init_db  # noqa: E402

BASE = "https://api.pandascore.co"
PER_PAGE = 100


def get_token():
    token = os.environ.get("PANDASCORE_TOKEN") or ingest.load_env().get("PANDASCORE_TOKEN")
    if not token:
        raise SystemExit("PANDASCORE_TOKEN missing (env or ./.env)")
    return token


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="oldest begin_at date, YYYY-MM-DD")
    ap.add_argument("--max-pages", type=int, default=1000)
    args = ap.parse_args()

    token = get_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    conn = init_db()
    fetched = new = updated = skipped = 0
    started = time.time()
    try:
        with httpx.Client(headers=headers, timeout=30.0) as client:
            for page in range(1, args.max_pages + 1):
                for attempt in range(5):
                    resp = client.get(f"{BASE}/csgo/matches/past",
                                      params={"per_page": PER_PAGE, "page": page})
                    if resp.status_code == 429:
                        reset = resp.headers.get("x-ratelimit-reset")
                        wait = max(5, int(reset or "60") - int(time.time())) if reset else 65
                        print(f"[page {page}] 429 rate-limited; sleeping {wait}s", flush=True)
                        time.sleep(wait)
                        continue
                    resp.raise_for_status()
                    break
                else:
                    raise SystemExit(f"rate-limit retries exhausted on page {page}")

                rows = resp.json()
                if not rows:
                    print(f"page {page}: empty — history exhausted", flush=True)
                    break
                fetched += len(rows)
                n, u, s = ingest.upsert_rows(conn, rows)
                new, updated, skipped = new + n, updated + u, skipped + s

                dates = [r.get("begin_at", "")[:10] for r in rows if r.get("begin_at")]
                oldest = min(dates) if dates else None
                if page % 10 == 0 or page == 1:
                    rate = fetched / max(1e-9, time.time() - started)
                    print(f"page {page}: {fetched} fetched ({rate:.0f}/s), oldest={oldest}, "
                          f"+{new} new", flush=True)
                if len(rows) < PER_PAGE or (oldest and oldest < args.start):
                    print(f"page {page}: reached {oldest} (< {args.start}) — done", flush=True)
                    break
                time.sleep(0.35)  # polite pacing; 429 handler is the real guard
    finally:
        conn.close()

    summary = {"fetched": fetched, "new": new, "updated": updated,
               "skipped": skipped, "started": args.start,
               "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    print("SUMMARY " + json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()