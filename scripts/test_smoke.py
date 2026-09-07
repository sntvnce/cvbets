"""Smoke tests for Phase 1 on a THROWAWAY db (never data/ledger.db).

Covers: schema init, upsert behavior, chalkbot rate-based pick + default
branch + one-pick-per-match, grade result/brier, agent_stats, idempotency
(grade twice -> no change; chalkbot twice -> no new picks; append-only).
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import chalkbot
import db
import grade
import ingest

tmp = Path(tempfile.mkdtemp(prefix="cvbets_smoke_"))
db_path = tmp / "smoke.db"
conn = db.init_db(db_path)
fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}: {name}" + (f" ({detail})" if detail else ""))
    if not cond:
        fails.append(name)


print("[1] ingest.to_row: wrapper unwrap + winner/score extraction")
m = {
    "id": 9001, "status": "finished", "begin_at": "2026-09-01T12:00:00Z",
    "number_of_games": 3,
    "league": {"name": "L"}, "serie": {"name": "S"}, "tournament": {"name": "T"},
    "opponents": [
        {"type": "Team", "opponent": {"id": 1, "name": "Alpha"}},
        {"type": "Team", "opponent": {"id": 2, "name": "Beta"}},
    ],
    "winner": {"id": 2, "name": "Beta"},
    "results": [{"team_id": 1, "score": 1}, {"team_id": 2, "score": 2}],
}
row = ingest.to_row(m)
assert row is not None, "to_row returned None for a valid finished match"
check("wrapper opponents -> names/ids",
      row and row["team1"] == "Alpha" and row["team2"] == "Beta"
      and row["team1_id"] == 1 and row["team2_id"] == 2)
check("winner from winner.id", row["winner"] == "Beta")
check("score from results", row["score"] == "1-2")
check("format bo3", row["format"] == "bo3")
check("canceled skipped", ingest.to_row({**m, "status": "canceled"}) is None)
check("1 opponent skipped",
      ingest.to_row({**m, "status": "not_started", "opponents": m["opponents"][:1]}) is None)

print("[2] upsert: insert then re-upsert keeps winner, updates status")
n, u, s = ingest.upsert_rows(conn, [m])
check("first insert = 1 new", (n, u, s) == (1, 0, 0))
m2 = {**m, "status": "not_started", "winner": None, "results": []}  # stale refetch
n, u, s = ingest.upsert_rows(conn, [m2])
w = conn.execute("SELECT winner, score, status FROM matches WHERE match_id=9001").fetchone()
check("stale refetch does not clobber winner/score", w["winner"] == "Beta" and w["score"] == "1-2")
check("stale refetch does not downgrade finished status", w["status"] == "finished")

print("[3] history for win rates: Alpha 3-1 vs Delta last 30d, Beta 1-3")
hist = [
    {"id": 8001 + i, "status": "finished", "begin_at": "2026-08-2%dT12:00:00Z" % i,
     "opponents": [{"opponent": {"id": 1, "name": "Alpha"}},
                   {"opponent": {"id": 3, "name": "Delta"}}],
     "winner": {"id": 1 if i < 3 else 3, "name": "Alpha" if i < 3 else "Delta"},
     "results": []}
    for i in range(4)
]
hist += [
    {"id": 8010 + i, "status": "finished", "begin_at": "2026-08-2%dT13:00:00Z" % i,
     "opponents": [{"opponent": {"id": 2, "name": "Beta"}},
                   {"opponent": {"id": 3, "name": "Delta"}}],
     "winner": {"id": 3 if i < 3 else 2, "name": "Delta" if i < 3 else "Beta"},
     "results": []}
    for i in range(4)
]
ingest.upsert_rows(conn, hist)
rates = chalkbot.win_rates(conn)
# NB: match 9001 (Alpha vs Beta) from section [2] is also in the 30d window,
# so Alpha is 3-2 (0.6, n=5) and Beta 2-3 (0.4, n=5).
check("Alpha rate 0.60 (n=5)", rates.get("Alpha") == (0.6, 5), str(rates.get("Alpha")))
check("Beta rate 0.40 (n=5)", rates.get("Beta") == (0.4, 5), str(rates.get("Beta")))

print("[4] chalkbot: rate-based pick, default branch, one pick per match")
conn.execute("INSERT INTO matches (match_id, begin_at, team1, team2, team1_id, team2_id, "
             "format, status) VALUES (9100, '2026-09-10T12:00:00Z', 'Alpha', 'Beta', 1, 2, "
             "'bo3', 'not_started')")
conn.execute("INSERT INTO matches (match_id, begin_at, team1, team2, team1_id, team2_id, "
             "format, status) VALUES (9101, '2026-09-10T12:00:00Z', 'Rookie1', 'Rookie2', 7, 8, "
             "'bo3', 'not_started')")
conn.commit()
out = chalkbot.run(conn)
p = conn.execute("SELECT selection, confidence, reasoning FROM picks WHERE match_id=9100").fetchone()
check("picks higher win rate (Alpha)", p["selection"] == "Alpha", f"{p['selection']} @ {p['confidence']}")
check("confidence 0.5 + 0.5*(0.6-0.4) = 0.60", abs(p["confidence"] - 0.60) < 1e-9)
q = conn.execute("SELECT selection, confidence FROM picks WHERE match_id=9101").fetchone()
check("thin-sample default: team1 @ 0.50", q["selection"] == "Rookie1" and q["confidence"] == 0.50)
out2 = chalkbot.run(conn)
check("second run makes 0 new picks", out2["made"] == 0, str(out2))
check("exactly one chalkbot pick per match",
      conn.execute("SELECT COUNT(*) c FROM picks").fetchone()["c"] == 2)

print("[5] grade: result/brier, agent_stats, idempotent rerun")
conn.execute("UPDATE matches SET status='finished', winner='Beta', score='1-2' "
             "WHERE match_id=9100")
conn.commit()
g = grade.run(conn)
pick = conn.execute("SELECT result, brier FROM picks WHERE match_id=9100").fetchone()
check("Alpha lost -> result 0", pick["result"] == 0)
check("brier = (0.60-0)^2 = 0.36", abs(pick["brier"] - 0.36) < 1e-9)
st = conn.execute("SELECT n_picks, wins, brier_mean FROM agent_stats WHERE agent='chalkbot'").fetchone()
check("agent_stats n=1 wins=0 brier=0.36",
      st["n_picks"] == 1 and st["wins"] == 0 and abs(st["brier_mean"] - 0.36) < 1e-9)
before = conn.execute("SELECT result, brier, id FROM picks ORDER BY id").fetchall()
g2 = grade.run(conn)
after = conn.execute("SELECT result, brier, id FROM picks ORDER BY id").fetchall()
check("grade rerun: 0 graded, picks unchanged",
      g2["graded"] == 0 and [tuple(r) for r in before] == [tuple(r) for r in after])
g3 = grade.run(conn)
check("agent_stats rerun: no row churn", g3["stats"].get("chalkbot", {}).get("n_picks") == 1)

print("[6] append-only invariant: no code path mutates picks outside grade guard")
src = (Path(__file__).resolve().parent.parent / "chalkbot.py").read_text()
src += (Path(__file__).resolve().parent.parent / "ingest.py").read_text()
src += (Path(__file__).resolve().parent.parent / "daily.py").read_text()
import re
mutations = re.findall(r"(?:UPDATE|DELETE\s+FROM)\s+picks", src, re.I)
check("no UPDATE/DELETE on picks outside grade.py", not mutations, str(mutations))

conn.close()
shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
sys.exit(0 if not fails else 1)