"""PandaScore ingest for CS2 (Phase 1 data layer).

NOTE: PandaScore's CS2 endpoints still use the legacy /csgo/ prefix.
  - GET /csgo/matches/upcoming (per_page=100)
  - GET /csgo/matches/past     (per_page=100, paginated back 60 days)

Upserts into the matches table keyed by match_id. Canceled/postponed
matches and matches without exactly two named opponents are skipped.
Winner name and score are extracted from the match's winner/results fields.
Token comes from PANDASCORE_TOKEN env var or ./.env (simple key=value).
"""
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from db import init_db, log

ROOT = Path(__file__).resolve().parent
BASE = "https://api.pandascore.co"
PER_PAGE = 100
PAST_DAYS = 60
MAX_PAGES = 60
SKIP_STATUS = {"canceled", "postponed"}

UPSERT_MATCH = """
INSERT INTO matches (match_id, begin_at, league, serie, tournament,
                     team1, team2, team1_id, team2_id, format, status, winner, score)
VALUES (:match_id, :begin_at, :league, :serie, :tournament,
        :team1, :team2, :team1_id, :team2_id, :format, :status, :winner, :score)
ON CONFLICT(match_id) DO UPDATE SET
  begin_at    = excluded.begin_at,
  league      = excluded.league,
  serie       = excluded.serie,
  tournament  = excluded.tournament,
  team1       = COALESCE(excluded.team1, team1),
  team2       = COALESCE(excluded.team2, team2),
  team1_id    = COALESCE(excluded.team1_id, team1_id),
  team2_id    = COALESCE(excluded.team2_id, team2_id),
  format      = COALESCE(excluded.format, format),
  status      = CASE WHEN status = 'finished' AND excluded.status = 'not_started'
                     THEN status ELSE excluded.status END,
  winner      = COALESCE(excluded.winner, winner),
  score       = COALESCE(excluded.score, score),
  ingested_at = datetime('now')
"""


def load_env(path=None):
    """Tiny key=value parser for ./.env (no shell sourcing, no quotes magic)."""
    path = Path(path) if path else ROOT / ".env"
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def get_token():
    token = os.environ.get("PANDASCORE_TOKEN") or load_env().get("PANDASCORE_TOKEN")
    if not token:
        log("PANDASCORE_TOKEN not found (env or ./.env) — stopping.")
        raise SystemExit(1)
    return token


def fetch(client, path, params):
    resp = client.get(BASE + path, params=params)
    resp.raise_for_status()
    return resp.json()


def _team_of(o):
    """PandaScore wraps opponents as {'type': 'Team', 'opponent': {...}};
    some endpoints return the bare team dict instead. Normalize to the team
    dict (or None when the slot is empty/TBD)."""
    if not isinstance(o, dict):
        return None
    inner = o.get("opponent")
    if not isinstance(inner, dict):
        inner = o
    return inner if inner.get("name") else None


def two_opponents(m):
    """Return (opp1, opp2) when the match has exactly 2 named opponents."""
    opps = [t for t in (_team_of(o) for o in (m.get("opponents") or [])) if t]
    if len(opps) == 2:
        return opps[0], opps[1]
    o1, o2 = _team_of(m.get("opponent1")), _team_of(m.get("opponent2"))
    if o1 and o2:
        return o1, o2
    return None


def winner_and_score(m, opp1, opp2):
    """Extract winner team name and 'x-y' score from winner/results fields."""
    winner = None
    w = m.get("winner")
    if isinstance(w, dict):
        winner = w.get("name")
        if not winner and w.get("id") is not None:
            if w.get("id") == opp1.get("id"):
                winner = opp1["name"]
            elif w.get("id") == opp2.get("id"):
                winner = opp2["name"]
    elif isinstance(w, int):
        if w == opp1.get("id"):
            winner = opp1["name"]
        elif w == opp2.get("id"):
            winner = opp2["name"]

    score = None
    if opp1.get("id") is not None and opp2.get("id") is not None:
        by_id = {}
        for r in m.get("results") or []:
            if isinstance(r, dict) and r.get("team_id") is not None:
                by_id[r["team_id"]] = r.get("score")
        s1, s2 = by_id.get(opp1.get("id")), by_id.get(opp2.get("id"))
        if s1 is not None and s2 is not None:
            score = f"{s1}-{s2}"
            if winner is None:  # infer from scores when winner field is empty
                if s1 > s2:
                    winner = opp1["name"]
                elif s2 > s1:
                    winner = opp2["name"]
    return winner, score


def format_of(m):
    n = m.get("number_of_games")
    if n in (1, 2, 3, 5):
        return f"bo{n}"
    return None


def to_row(m):
    """Build an upsert dict for a match, or None when it must be skipped."""
    status = m.get("status")
    if not status or status in SKIP_STATUS:
        return None
    opps = two_opponents(m)
    if not opps:
        return None
    opp1, opp2 = opps
    winner, score = winner_and_score(m, opp1, opp2)
    return {
        "match_id": m["id"],
        "begin_at": m.get("begin_at"),
        "league": (m.get("league") or {}).get("name"),
        "serie": (m.get("serie") or {}).get("name"),
        "tournament": (m.get("tournament") or {}).get("name"),
        "team1": opp1["name"],
        "team2": opp2["name"],
        "team1_id": opp1.get("id"),
        "team2_id": opp2.get("id"),
        "format": format_of(m),
        "status": status,
        "winner": winner,
        "score": score,
    }


def upsert_rows(conn, matches):
    new = updated = skipped = 0
    for m in matches:
        row = to_row(m)
        if row is None:
            skipped += 1
            continue
        exists = conn.execute(
            "SELECT 1 FROM matches WHERE match_id = ?", (row["match_id"],)
        ).fetchone()
        conn.execute(UPSERT_MATCH, row)
        if exists:
            updated += 1
        else:
            new += 1
    conn.commit()
    return new, updated, skipped


def ingest_upcoming(client, conn):
    rows = fetch(client, "/csgo/matches/upcoming",
                 {"per_page": PER_PAGE, "page": 1, "sort": "begin_at"})
    new, updated, skipped = upsert_rows(conn, rows)
    log(f"upcoming: {len(rows)} fetched -> {new} new, {updated} updated, {skipped} skipped")
    return {"fetched": len(rows), "new": new, "updated": updated, "skipped": skipped}


def ingest_past(client, conn):
    # NOTE: sort=-begin_at puts NULL-begin_at canceled bracket slots first, so
    # we use the endpoint's default order (newest concluded matches first).
    cutoff = (datetime.now(timezone.utc) - timedelta(days=PAST_DAYS)).strftime("%Y-%m-%d")
    fetched = new = updated = skipped = 0
    pages_used = 0
    for page in range(1, MAX_PAGES + 1):
        rows = fetch(client, "/csgo/matches/past", {"per_page": PER_PAGE, "page": page})
        pages_used += 1
        if not rows:
            break
        fetched += len(rows)
        n, u, s = upsert_rows(conn, rows)
        new, updated, skipped = new + n, updated + u, skipped + s
        # Stop once a page reaches matches older than the 60-day window.
        # NULL begin_at rows (canceled slots) are ignored for this check.
        real_dates = [r["begin_at"][:10] for r in rows if r.get("begin_at")]
        oldest = min(real_dates) if real_dates else None
        if len(rows) < PER_PAGE or (oldest is not None and oldest < cutoff):
            break
        time.sleep(0.4)
    log(f"past {PAST_DAYS}d: {fetched} fetched across {pages_used} page(s) -> "
        f"{new} new, {updated} updated, {skipped} skipped")
    return {"fetched": fetched, "new": new, "updated": updated,
            "skipped": skipped, "pages": pages_used}


def run(conn=None):
    own = conn or init_db()
    token = get_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    summary = {}
    try:
        with httpx.Client(headers=headers, timeout=30.0) as client:
            summary["upcoming"] = ingest_upcoming(client, own)
            summary["past"] = ingest_past(client, own)
    finally:
        if conn is None:
            own.close()
    return summary


if __name__ == "__main__":
    run()