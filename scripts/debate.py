"""Debate stage — agents critique each other's pending picks (append-only).

Round 1 (the picks themselves) happened in chalkbot.py / experts.py.
This stage writes round 2: each agent posts short, templated critiques of
RIVAL agents' picks where it has real, computable disagreement — grounded
strictly in the ledger, never vibes. The discussion table is APPEND-ONLY:
statements are never edited or deleted (schema.sql).

Output discipline: each critique <= 240 chars. The future LLM expert reads
all of this before locking its own pick.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from db import init_db, log  # noqa: E402

MAX_STATEMENT = 240


def _insert(conn, match_id, agent, round_no, statement):
    statement = statement.strip()[:MAX_STATEMENT]
    cur = conn.execute(
        "INSERT INTO discussion (match_id, agent, round, statement) "
        "SELECT ?, ?, ?, ? WHERE NOT EXISTS ("
        "  SELECT 1 FROM discussion WHERE match_id = ? AND agent = ? AND round = ?)",
        (match_id, agent, round_no, statement, match_id, agent, round_no),
    )
    return cur.rowcount == 1


def _h2h_record(conn, team_a, team_b):
    rows = conn.execute(
        "SELECT winner FROM matches WHERE status = 'finished' "
        "AND ((team1 = ? AND team2 = ?) OR (team1 = ? AND team2 = ?))",
        (team_a, team_b, team_b, team_a),
    ).fetchall()
    return (sum(1 for r in rows if r["winner"] == team_a),
            sum(1 for r in rows if r["winner"] == team_b), len(rows))


def _last_n(conn, team, n=5):
    """(win_rate, games) over the team's last n finished matches; (None, 0) if none."""
    rows = conn.execute(
        "SELECT winner FROM matches WHERE status = 'finished' "
        "AND (team1 = ? OR team2 = ?) ORDER BY begin_at DESC LIMIT ?",
        (team, team, n),
    ).fetchall()
    if not rows:
        return None, 0
    return sum(1 for r in rows if r["winner"] == team) / len(rows), len(rows)


def critique_form(conn, match_id, team1, team2, picks_by_agent):
    """form's voice: challenge h2h when history contradicts recent form."""
    out = 0
    h2h_pick = picks_by_agent.get("h2h")
    if not h2h_pick:
        return 0
    sel = h2h_pick["selection"]
    other = team2 if sel == team1 else team1
    r_sel, n_sel = _last_n(conn, sel) or (None, 0)
    r_oth, n_oth = _last_n(conn, other) or (None, 0)
    if r_sel is not None and r_oth is not None and r_oth > r_sel:
        stmt = (f"@h2h history says {sel}, but recent form says otherwise: "
                f"{other} is {r_oth:.0%} last 5 vs {sel} at {r_sel:.0%}. "
                f"Momentum beats memory.")
        if _insert(conn, match_id, "form", 2, stmt):
            out += 1
    return out


def critique_h2h(conn, match_id, team1, team2, picks_by_agent):
    """h2h's voice: cite the actual head-to-head record against rivals."""
    out = 0
    w1, w2, n = _h2h_record(conn, team1, team2)
    for rival in ("chalkbot", "form"):
        pick = picks_by_agent.get(rival)
        if not pick or n == 0:
            continue
        sel = pick["selection"]
        sel_w = w1 if sel == team1 else w2
        opp = team2 if sel == team1 else team1
        opp_w = w2 if sel == team1 else w1
        if opp_w > sel_w:
            stmt = (f"@{rival} backs {sel}, but this exact fixture says otherwise: "
                    f"{opp} leads the H2H {opp_w}-{sel_w} over {n} meetings. "
                    f"Matchups are not streaks.")
            if _insert(conn, match_id, "h2h", 2, stmt):
                out += 1
    return out


def critique_chalkbot(conn, match_id, team1, team2, picks_by_agent):
    """chalkbot's voice: flag rivals whose pick contradicts 30-day rates."""
    out = 0
    rate1 = conn.execute(
        "SELECT COUNT(*) n, SUM(winner = team1) w FROM (SELECT team1, team2, winner "
        "FROM matches WHERE status='finished' AND (team1=? OR team2=?))",
        (team1, team1)).fetchone()
    # simpler: compute via python over recent rows
    rows = conn.execute(
        "SELECT team1, team2, winner FROM matches WHERE status = 'finished' "
        "AND (team1 = ? OR team2 = ? OR team1 = ? OR team2 = ?) "
        "AND substr(begin_at,1,10) >= date('now','-30 days')",
        (team1, team1, team2, team2)).fetchall()
    played = {team1: 0, team2: 0}
    wins = {team1: 0, team2: 0}
    for r in rows:
        for t in (r["team1"], r["team2"]):
            played[t] = played.get(t, 0) + 1
        if r["winner"] in wins:
            wins[r["winner"]] += 1
    for rival in ("form", "h2h"):
        pick = picks_by_agent.get(rival)
        if not pick:
            continue
        sel = pick["selection"]
        if sel not in played:
            continue
        other = team2 if sel == team1 else team1
        p_sel, w_sel = played.get(sel, 0), wins.get(sel, 0)
        p_oth, w_oth = played.get(other, 0), wins.get(other, 0)
        if p_sel and p_oth and (w_sel / p_sel) < (w_oth / p_oth):
            stmt = (f"@{rival} took {sel} but 30d rates favor {other}: "
                    f"{w_oth}/{p_oth} vs {w_sel}/{p_sel}. I side with the table.")
            if _insert(conn, match_id, "chalkbot", 2, stmt):
                out += 1
    return out


CRITICS = {"chalkbot": critique_chalkbot, "form": critique_form, "h2h": critique_h2h}


def run(conn=None):
    own = conn or init_db()
    rows = own.execute(
        "SELECT match_id, team1, team2 FROM matches WHERE status = 'not_started' "
        "AND team1 IS NOT NULL AND team2 IS NOT NULL ORDER BY begin_at").fetchall()
    made = 0
    for r in rows:
        picks = {p["agent"]: {"selection": p["selection"]} for p in own.execute(
            "SELECT agent, selection FROM picks WHERE match_id = ?", (r["match_id"],))}
        if "chalkbot" not in picks:
            continue  # everyone must have stated a round-1 position first
        for name, fn in CRITICS.items():
            made += fn(own, r["match_id"], r["team1"], r["team2"], picks)
    own.commit()
    log(f"debate: {made} new critiques (round 2) across {len(rows)} upcoming matches")
    if conn is None:
        own.close()
    return {"critiques": made, "matches": len(rows)}


if __name__ == "__main__":
    run()