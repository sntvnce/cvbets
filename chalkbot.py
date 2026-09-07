"""chalkbot — deterministic, zero-LLM baseline picker.

For every not_started match with no existing 'chalkbot' pick:
  - if both teams have >= 2 finished matches in the last 30 days in our own
    matches table and their win rates differ, pick the higher win rate with
    confidence = 0.5 + 0.5 * (rate_pick - rate_other), clamped to [0.5, 1.0];
  - otherwise (either team < 2 recorded matches, or an exact rate tie)
    deterministically pick team1 with confidence 0.50.

Picks are APPEND-ONLY: never more than one chalkbot pick per match, and
existing picks are never modified.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from db import init_db, insert_pick, log

AGENT = "chalkbot"
WINDOW_DAYS = 30
MIN_SAMPLE = 2


def win_rates(conn):
    """{team_name: (win_rate, matches_played)} over finished matches in the
    last WINDOW_DAYS days, computed from our own matches table only."""
    since = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT team1, team2, winner FROM matches "
        "WHERE status = 'finished' AND begin_at IS NOT NULL AND substr(begin_at, 1, 10) >= ?",
        (since,),
    ).fetchall()
    played = defaultdict(int)
    wins = defaultdict(int)
    for r in rows:
        for team in (r["team1"], r["team2"]):
            if team:
                played[team] += 1
        if r["team1"] and r["winner"] == r["team1"]:
            wins[r["team1"]] += 1
        elif r["team2"] and r["winner"] == r["team2"]:
            wins[r["team2"]] += 1
    return {team: (wins[team] / played[team], played[team]) for team in played}


def run(conn=None):
    own = conn or init_db()
    rates = win_rates(own)
    log(f"chalkbot: {len(rates)} teams with matches in last {WINDOW_DAYS}d")
    rows = own.execute(
        "SELECT match_id, team1, team2 FROM matches "
        "WHERE status = 'not_started' AND team1 IS NOT NULL AND team2 IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM picks WHERE picks.match_id = matches.match_id "
        "AND agent = ?)",
        (AGENT,),
    ).fetchall()
    made = 0
    for r in rows:
        rate1, n1 = rates.get(r["team1"], (0.0, 0))
        rate2, n2 = rates.get(r["team2"], (0.0, 0))
        if n1 >= MIN_SAMPLE and n2 >= MIN_SAMPLE and rate1 != rate2:
            if rate1 > rate2:
                sel, conf = r["team1"], min(1.0, 0.5 + 0.5 * (rate1 - rate2))
                hi, lo, nhi, nlo = rate1, rate2, n1, n2
            else:
                sel, conf = r["team2"], min(1.0, 0.5 + 0.5 * (rate2 - rate1))
                hi, lo, nhi, nlo = rate2, rate1, n2, n1
            reasoning = (f"chalkbot: L{WINDOW_DAYS}d win rate {hi:.2f} (n={nhi}) "
                         f"vs {lo:.2f} (n={nlo}); higher rate picked.")
        else:
            sel, conf = r["team1"], 0.50
            reasoning = (f"chalkbot: thin/equal sample (team1 n={n1}, team2 n={n2}); "
                         f"default team1 at 0.50.")
        if insert_pick(own, r["match_id"], AGENT, sel, round(conf, 4), reasoning):
            made += 1
            log(f"chalkbot: pick match {r['match_id']} -> {sel} @ {conf:.2f}")
    log(f"chalkbot: {made} new picks ({len(rows)} eligible matches)")
    if conn is None:
        own.close()
    return {"eligible": len(rows), "made": made}


if __name__ == "__main__":
    run()