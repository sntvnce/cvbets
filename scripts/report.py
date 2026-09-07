#!/usr/bin/env python3
"""Read-only W/L report from data/ledger.db.

Usage:
  python scripts/report.py                    # standings + last 20 graded picks
  python scripts/report.py --pending          # show pending picks instead
  python scripts/report.py --all              # every graded pick (no limit)
  python scripts/report.py --limit 50         # show last 50 graded
  python scripts/report.py --agent chalkbot   # filter to one agent

This script only SELECTs — the ledger is never written here.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db import connect  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="cvbets W/L report (read-only)")
    ap.add_argument("--agent", help="filter by agent name")
    ap.add_argument("--pending", action="store_true", help="show pending picks instead of graded")
    ap.add_argument("--all", action="store_true", help="show all graded picks (default: last 20)")
    ap.add_argument("--limit", type=int, default=20, help="how many graded picks to show (default 20)")
    args = ap.parse_args()

    conn = connect()
    where, params = "", []
    if args.agent:
        where = " AND p.agent = ?"
        params = [args.agent]

    print("== agent standings (graded picks) ==")
    rows = conn.execute(
        "SELECT agent, COUNT(*) n, SUM(result) w, SUM(1 - result) l, AVG(brier) bm "
        "FROM picks WHERE result IS NOT NULL" + where + " GROUP BY agent ORDER BY n DESC",
        params,
    ).fetchall()
    if not rows:
        print("  (no graded picks yet — run daily.py until matches finish)")
    for r in rows:
        n, w, l = r["n"], r["w"] or 0, r["l"] or 0
        print(f"  {r['agent']:<12} {n:>4} picks   {w}-{l}   win% {w / n:>5.1%}   "
              f"brier {(r['bm'] or 0):.4f}")

    bets = conn.execute(
        "SELECT COUNT(*) c, SUM(CASE WHEN result IS NOT NULL THEN 1 ELSE 0 END) s, SUM(pnl) p "
        "FROM bets").fetchone()
    print("== bets ==")
    if bets["c"] == 0:
        print("  (bets table empty — activates once an odds source exists; "
              "W/L above is tracked at the pick level)")
    else:
        pnl = f"{bets['p']:+.2f}" if bets["p"] is not None else "-"
        print(f"  rows: {bets['c']}   settled: {bets['s'] or 0}   total pnl: {pnl} units")

    if args.pending:
        print("== pending picks (oldest kickoff first) ==")
        rows = conn.execute(
            "SELECT p.agent, p.selection, p.confidence, m.begin_at, m.team1, m.team2 "
            "FROM picks p JOIN matches m ON m.match_id = p.match_id "
            "WHERE p.result IS NULL" + where + " ORDER BY m.begin_at",
            params,
        ).fetchall()
        print(f"  {len(rows)} pending")
        for r in rows[:30]:
            day = (r["begin_at"] or "?")[:10]
            print(f"  {day}  {r['team1']} vs {r['team2']}  ->  {r['selection']} "
                  f"@ {r['confidence']:.2f}  [{r['agent']}]")
        if len(rows) > 30:
            print(f"  ... and {len(rows) - 30} more (re-run with --agent to narrow)")
    else:
        label = "all graded picks" if args.all else f"last {args.limit} graded picks"
        print(f"== {label} (newest first) ==")
        rows = conn.execute(
            "SELECT p.id, p.agent, p.selection, p.confidence, p.result, p.brier, "
            "m.begin_at, m.team1, m.team2, m.winner, m.score "
            "FROM picks p JOIN matches m ON m.match_id = p.match_id "
            "WHERE p.result IS NOT NULL" + where + " ORDER BY p.id DESC" +
            ("" if args.all else f" LIMIT {int(args.limit)}"),
            params,
        ).fetchall()
        if not rows:
            print("  (none graded yet)")
        for r in rows:
            day = (r["begin_at"] or "?")[:10]
            outcome = "WIN " if r["result"] == 1 else "LOSS"
            print(f"  {day}  {r['team1']} vs {r['team2']}  ->  {r['selection']} "
                  f"@ {r['confidence']:.2f}  {outcome}  score {r['score'] or '-'}  "
                  f"brier {r['brier']:.4f}  [{r['agent']}]")
    conn.close()


if __name__ == "__main__":
    main()