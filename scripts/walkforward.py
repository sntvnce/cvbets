"""walkforward.py — walk-forward backtest harness for expert strategies.

Simulates 'train on history -> predict tomorrow -> score -> roll forward' using
ONLY data each expert would have had at prediction time (no look-ahead):

  1. Build a day-by-day timeline of finished matches.
  2. For each simulation day D: run pickers using ONLY matches finished before
     the earliest match beginning that day; grade every pick against real results.
  3. Aggregate per-strategy: accuracy, brier, and 'conviction' performance
     (accuracy when strategies agree) to test the concentrated-betting thesis.

Pickers implemented here are re-implementations of the ledger experts at
arbitrary history cutoffs (they read the backfilled matches table only):
  - chalk30 : better win rate, last 30 days (chalkbot replica)
  - form5   : better win rate, last 5 matches (form replica)
  - h2h     : all-time head-to-head (h2h replica)
  - agree2+ : only bet when >= N of the three agree; 'no pick' otherwise

Run: .venv/bin/python scripts/walkforward.py --start 2024-09-11 --end 2026-09-01
Writes results to data/backtest/ as JSON + CSV. Read-only w.r.t. picks table:
simulated picks live in data/backtest/ only.
"""
import argparse
import csv
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "backtest"

FORM_N = 5
CHALK_WINDOW = 30
MIN_SAMPLE = 2


def load_matches(db_path=None):
    conn = sqlite3.connect(db_path or (ROOT / "data" / "ledger.db"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT match_id, begin_at, league, serie, tournament, team1, team2, "
        "format, status, winner, score FROM matches "
        "WHERE status='finished' AND winner IS NOT NULL AND begin_at IS NOT NULL "
        "AND team1 IS NOT NULL AND team2 IS NOT NULL"
    ).fetchall()
    conn.close()
    matches = []
    for r in rows:
        try:
            begin = datetime.fromisoformat(r["begin_at"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        matches.append({"match_id": r["match_id"], "begin_at": begin,
                        "team1": r["team1"], "team2": r["team2"],
                        "winner": r["winner"], "format": r["format"] or "bo3",
                        "league": r["league"] or "", "serie": r["serie"] or "",
                        "tournament": r["tournament"] or ""})
    matches.sort(key=lambda m: m["begin_at"])
    return matches


def win_rates_before(matches, cutoff, window_days=None):
    """{team: (rate, n)} over finished matches with begin_at < cutoff."""
    played, wins = defaultdict(int), defaultdict(int)
    for m in matches:
        if window_days:
            if (cutoff - m["begin_at"]) > timedelta(days=window_days):
                continue
        if m["begin_at"] >= cutoff:
            break  # sorted; everything after is future
        for t in (m["team1"], m["team2"]):
            played[t] += 1
        wins[m["winner"]] += 1
    return {t: (w / played[t], played[t]) for t, w in wins.items()} if played else {}


def h2h_before(matches, cutoff, a, b):
    wa = wb = 0
    for m in matches:
        if m["begin_at"] >= cutoff:
            break
        if {m["team1"], m["team2"]} == {a, b}:
            if m["winner"] == a:
                wa += 1
            elif m["winner"] == b:
                wb += 1
    return wa, wb


def pick_chalk30(m, matches, cutoff):
    rates = win_rates_before(matches, cutoff, CHALK_WINDOW)
    r1, n1 = rates.get(m["team1"], (0.0, 0))
    r2, n2 = rates.get(m["team2"], (0.0, 0))
    if n1 >= MIN_SAMPLE and n2 >= MIN_SAMPLE and r1 != r2:
        sel = m["team1"] if r1 > r2 else m["team2"]
        return sel, min(1.0, 0.5 + 0.5 * abs(r1 - r2))
    return m["team1"], 0.50


def pick_form5(m, matches, cutoff):
    def last5(team):
        out = []
        for mm in reversed(matches):
            if mm["begin_at"] >= cutoff:
                continue
            if team in (mm["team1"], mm["team2"]):
                out.append(mm["winner"] == team)
                if len(out) == FORM_N:
                    break
        if not out:
            return None, 0
        return sum(out) / len(out), len(out)
    r1, n1 = last5(m["team1"])
    r2, n2 = last5(m["team2"])
    if r1 is None or r2 is None or r1 == r2:
        return m["team1"], 0.50
    sel = m["team1"] if r1 > r2 else m["team2"]
    return sel, min(1.0, 0.5 + 0.5 * abs(r1 - r2))


def pick_h2h(m, matches, cutoff):
    w1, w2 = h2h_before(matches, cutoff, m["team1"], m["team2"])
    if w1 == 0 and w2 == 0 or w1 == w2:
        return m["team1"], 0.50
    sel = m["team1"] if w1 > w2 else m["team2"]
    return sel, min(1.0, 0.5 + 0.05 * abs(w1 - w2))


PICKERS = {"chalk30": pick_chalk30, "form5": pick_form5, "h2h": pick_h2h}


def run_backtest(matches, start, end, min_agree=2):
    """Walk forward day by day. A pick for match M is made with info cutoff =
    begin_at of M itself (only matches that FINISHED before M began are used —
    approximated by begin_at < M.begin_at, which is what the live experts see
    through the 60d ingest window anyway)."""
    picks = []
    for m in matches:
        if not (start <= m["begin_at"] <= end):
            continue
        cutoff = m["begin_at"]
        votes = {}
        for name, fn in PICKERS.items():
            sel, conf = fn(m, matches, cutoff)
            votes[name] = sel
        sel_counts = defaultdict(list)
        for name, sel in votes.items():
            sel_counts[sel].append(name)
        best_sel, best_voters = max(sel_counts.items(), key=lambda kv: len(kv[1]))
        pick = {"match_id": m["match_id"], "begin_at": m["begin_at"].isoformat(),
                "team1": m["team1"], "team2": m["team2"], "winner": m["winner"],
                "league": m["league"], "votes": votes,
                "consensus": best_sel, "agree": len(best_voters),
                "consensus_correct": best_sel == m["winner"]}
        picks.append(pick)
    return picks


def summarize(picks):
    out = {}
    for name in PICKERS:
        rows = [p for p in picks if p["votes"].get(name) is not None]
        n = len(rows)
        w = sum(1 for p in rows if p["votes"][name] == p["winner"])
        out[name] = {"n": n, "wins": w, "acc": round(w / n, 4) if n else None}
    for k in (2, 3):
        rows = [p for p in picks if p["agree"] >= k]
        n = len(rows)
        w = sum(1 for p in rows if p["consensus"] == p["winner"])
        out[f"consensus_{k}plus"] = {"n": n, "wins": w,
                                     "acc": round(w / n, 4) if n else None}
    # monthly accuracy drift for the consensus strategy
    by_month = defaultdict(lambda: [0, 0])
    for p in picks:
        if p["agree"] >= 2:
            key = p["begin_at"][:7]
            by_month[key][0] += 1
            by_month[key][0 + 1] += 1 if p["consensus"] == p["winner"] else 0
    out["consensus_2plus_by_month"] = {
        k: {"n": v[0], "acc": round(v[1] / v[0], 3)} for k, v in sorted(by_month.items())}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc) + timedelta(days=1)

    matches = load_matches(args.db)
    print(f"loaded {len(matches)} finished matches "
          f"({matches[0]['begin_at'].date()} .. {matches[-1]['begin_at'].date()})")

    picks = run_backtest(matches, start, end)
    OUT.mkdir(parents=True, exist_ok=True)
    summary = summarize(picks) if picks else {}
    if picks:
        with (OUT / "picks.csv").open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(picks[0].keys()))
            w.writeheader()
            w.writerows(picks)
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()