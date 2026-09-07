"""grade.py — settle finished matches against pending picks (idempotent).

For every pick whose match is finished and has a winner:
  result = 1 if matches.winner == picks.selection else 0
  brier  = (confidence - result)^2

This UPDATE (guarded by result IS NULL) is the ONLY mutation ever allowed on
the append-only picks table, and it fires at most once per pick. Then
agent_stats is upserted from graded picks; re-running when nothing changed
writes nothing (updated_at untouched), so double runs are true no-ops.
"""
from db import init_db, log


def _same_or_none(a, b):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return round(a, 6) == round(b, 6)


def grade_pending(conn):
    rows = conn.execute(
        "SELECT p.id, p.selection, p.confidence, m.winner "
        "FROM picks p JOIN matches m ON m.match_id = p.match_id "
        "WHERE p.result IS NULL AND m.status = 'finished' AND m.winner IS NOT NULL"
    ).fetchall()
    graded = 0
    for r in rows:
        result = 1 if r["winner"] == r["selection"] else 0
        brier = (r["confidence"] - result) ** 2
        cur = conn.execute(
            "UPDATE picks SET result = ?, brier = ? WHERE id = ? AND result IS NULL",
            (result, brier, r["id"]),
        )
        if cur.rowcount:
            graded += 1
            log(f"grade: pick {r['id']} ({r['selection']}) -> "
                f"{'WIN' if result else 'LOSS'} brier={brier:.4f}")
    conn.commit()
    return graded


def update_agent_stats(conn):
    """Recompute agent_stats from graded picks + settled bets.

    Returns (stats_dict, rows_changed). Rows are only written when the
    computed values differ from what is stored — keeps reruns idempotent.
    """
    rows = conn.execute(
        "SELECT agent, COUNT(*) AS n, SUM(result) AS w, AVG(brier) AS bm "
        "FROM picks WHERE result IS NOT NULL GROUP BY agent"
    ).fetchall()
    stats = {}
    changed = 0
    for r in rows:
        n, wins, brier_mean = r["n"], r["w"] or 0, r["bm"]
        bet = conn.execute(
            "SELECT SUM(pnl) AS pnl, SUM(stake) AS stake FROM bets "
            "WHERE agent = ? AND result IS NOT NULL",
            (r["agent"],),
        ).fetchone()
        roi = None
        if bet and bet["stake"]:
            roi = bet["pnl"] / bet["stake"]
        existing = conn.execute(
            "SELECT n_picks, wins, brier_mean, roi FROM agent_stats WHERE agent = ?",
            (r["agent"],),
        ).fetchone()
        stats[r["agent"]] = {"n_picks": n, "wins": wins,
                             "brier_mean": brier_mean, "roi": roi}
        same = (
            existing is not None
            and existing["n_picks"] == n
            and existing["wins"] == wins
            and _same_or_none(existing["brier_mean"], brier_mean)
            and _same_or_none(existing["roi"], roi)
        )
        if same:
            continue
        conn.execute(
            "INSERT INTO agent_stats (agent, n_picks, wins, brier_mean, roi, updated_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(agent) DO UPDATE SET "
            "n_picks = excluded.n_picks, wins = excluded.wins, "
            "brier_mean = excluded.brier_mean, roi = excluded.roi, "
            "updated_at = datetime('now')",
            (r["agent"], n, wins, brier_mean, roi),
        )
        changed += 1
    conn.commit()
    return stats, changed


def run(conn=None):
    own = conn or init_db()
    graded = grade_pending(own)
    stats, changed = update_agent_stats(own)
    pending = own.execute(
        "SELECT COUNT(*) AS c FROM picks WHERE result IS NULL").fetchone()["c"]
    log(f"grade: {graded} picks graded, {pending} still pending, "
        f"agent_stats rows changed: {changed}")
    if conn is None:
        own.close()
    return {"graded": graded, "pending": pending, "stats": stats}


if __name__ == "__main__":
    run()