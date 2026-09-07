"""Phase 2 expert agents — deterministic strategies, submitted via the gate.

Two experts (no LLM yet; LLM experts will use the same door later):
  'form' — better win rate over each team's LAST 5 finished matches (recency
           window by count, not days); ties/thin data -> team1 @ 0.50.
  'h2h'  — all-time head-to-head record between the two teams in our ledger;
           no meetings or tied record -> team1 @ 0.50.

Every pick goes through scripts/submit_pick.py (the only sanctioned write
path): JSON payload on disk -> gate validates -> db.insert_pick appends.
Picks remain APPEND-ONLY; the gate refuses duplicates.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from db import init_db, log  # noqa: E402

LAST_N = 5
GATE = ROOT / "submit_pick.py"


def last_n_form(conn, team, n=LAST_N):
    """Win rate over the team's most recent n finished matches (any opponent)."""
    rows = conn.execute(
        "SELECT team1, team2, winner FROM matches "
        "WHERE status = 'finished' AND (team1 = ? OR team2 = ?) "
        "ORDER BY begin_at DESC LIMIT ?",
        (team, team, n),
    ).fetchall()
    if not rows:
        return None, 0
    wins = sum(1 for r in rows if r["winner"] == team)
    return wins / len(rows), len(rows)


def h2h_record(conn, team_a, team_b):
    """(wins_a, wins_b, meetings) across all finished matches between the two."""
    rows = conn.execute(
        "SELECT winner FROM matches WHERE status = 'finished' "
        "AND ((team1 = ? AND team2 = ?) OR (team1 = ? AND team2 = ?))",
        (team_a, team_b, team_b, team_a),
    ).fetchall()
    wins_a = sum(1 for r in rows if r["winner"] == team_a)
    wins_b = sum(1 for r in rows if r["winner"] == team_b)
    return wins_a, wins_b, len(rows)


def pick_form(conn, match):
    """form expert: last-5-games form comparison."""
    t1, t2 = match["team1"], match["team2"]
    r1, n1 = last_n_form(conn, t1)
    r2, n2 = last_n_form(conn, t2)
    if n1 == 0 or n2 == 0 or r1 is None or r2 is None or r1 == r2:
        return t1, 0.50, f"form: no differential in last-{LAST_N} form " \
                         f"(t1 {r1 if r1 is not None else '-'}/n{n1}, " \
                         f"t2 {r2 if r2 is not None else '-'}/n{n2}); default team1."
    if r1 > r2:
        sel, hi, lo, nhi, nlo = t1, r1, r2, n1, n2
    else:
        sel, hi, lo, nhi, nlo = t2, r2, r1, n2, n1
    conf = min(1.0, 0.5 + 0.5 * (hi - lo))
    return sel, round(conf, 4), f"form: last-{LAST_N} win rate {hi:.2f} (n={nhi}) " \
                                f"vs {lo:.2f} (n={nlo}); hotter team picked."


def pick_h2h(conn, match):
    """h2h expert: all-time head-to-head in the ledger."""
    t1, t2 = match["team1"], match["team2"]
    w1, w2, n = h2h_record(conn, t1, t2)
    if n == 0 or w1 == w2:
        return t1, 0.50, f"h2h: {'no prior meetings' if n == 0 else f'tied {w1}-{w2} in {n}'}; " \
                         f"default team1."
    if w1 > w2:
        sel, sw, sl = t1, w1, w2
    else:
        sel, sw, sl = t2, w2, w1
    conf = min(1.0, 0.5 + 0.05 * (sw - sl))
    return sel, round(conf, 4), f"h2h: {sw}-{sl} across {n} meetings; " \
                                f"historical dominator picked."


EXPERTS = {"form": pick_form, "h2h": pick_h2h}


def submit_via_gate(agent, match_id, selection, confidence, reasoning, db_path=None):
    """Write the contract JSON and hand it to the gate as a real client would."""
    payload = {"agent": agent, "match_id": match_id, "selection": selection,
               "confidence": confidence, "reasoning": reasoning}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(payload, fh)
        path = fh.name
    cmd = [sys.executable, str(GATE), path]
    if db_path:
        cmd += ["--db", str(db_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
    Path(path).unlink(missing_ok=True)
    ok = proc.returncode == 0 and "ACCEPTED" in proc.stdout
    if not ok:
        log(f"expert {agent}: gate rejected match {match_id}: "
            f"{(proc.stdout + proc.stderr).strip()[:200]}")
    return ok


def run(agent=None, conn=None, db_path=None):
    """Run one expert (or all) over eligible not_started matches via the gate."""
    own = conn or init_db(db_path)
    names = [agent] if agent else list(EXPERTS)
    totals = {}
    for name in names:
        if name not in EXPERTS:
            log(f"experts: unknown expert '{name}' — skipping")
            continue
        rows = own.execute(
            "SELECT match_id, team1, team2 FROM matches "
            "WHERE status = 'not_started' AND team1 IS NOT NULL AND team2 IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM picks p WHERE p.match_id = matches.match_id "
            "AND p.agent = ?)",
            (name,),
        ).fetchall()
        made = 0
        for r in rows:
            sel, conf, why = EXPERTS[name](own, r)
            if submit_via_gate(name, r["match_id"], sel, conf, why, db_path):
                made += 1
        totals[name] = made
        log(f"expert {name}: {made} picks submitted via gate "
            f"({len(rows)} eligible)")
    if conn is None:
        own.close()
    return totals


if __name__ == "__main__":
    run(agent=sys.argv[1] if len(sys.argv) > 1 else None)