"""Tests for the Phase 4 LLM expert 'sage' on a THROWAWAY db (never ledger.db).

Covers: prompt-builder includes form/30d/h2h/round-1/debate facts; defensive
JSON parse (clean / fenced / embedded / garbage / out-of-range / wrong team /
word-cap); fallback coin-flip on unparseable output; full run() through the
REAL gate subprocess with a stubbed LLM (accept + duplicate rejection +
one-pick-per-match + max_matches cap); unreachable-endpoint graceful skip;
append-only source scan.
No network: the LLM is always stubbed via injectable probe/ask.
"""
import io
import contextlib
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import chalkbot  # noqa: E402
import db  # noqa: E402
import expert_sage as sage  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="cvbets_sage_"))
db_path = tmp / "sage_test.db"
conn = db.init_db(db_path)
fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}: {name}" + (f" ({detail})" if detail else ""))
    if not cond:
        fails.append(name)


def iso(days_ago, hour=12):
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%dT") + f"{hour:02d}:00:00Z"


def fin(mid, days_ago, t1, t2, winner):
    conn.execute(
        "INSERT INTO matches (match_id, begin_at, team1, team2, team1_id, team2_id, "
        "format, status, winner) VALUES (?, ?, ?, ?, 1, 2, 'bo3', 'finished', ?)",
        (mid, iso(days_ago), t1, t2, winner),
    )


print("[0] fixture: history + upcoming matches + round-1 picks + debate")
# Alpha vs Beta H2H: Alpha 3-1 (also feeds form/30d)
for i, w in enumerate(["Alpha", "Alpha", "Beta", "Alpha"]):
    fin(8000 + i, 20 - i, "Alpha", "Beta", w)
# Alpha 4-0 vs Gamma; Beta 1-3 vs Delta (same window)
for i in range(4):
    fin(8100 + i, 16 - i, "Alpha", "Gamma", "Alpha")
    fin(8200 + i, 16 - i, "Beta", "Delta", "Beta" if i == 0 else "Delta")
# OLD match outside the 30-day window: feeds last-10 form, NOT 30d rates
fin(8300, 45, "Alpha", "Zeta", "Alpha")
conn.execute("INSERT INTO matches (match_id, begin_at, team1, team2, team1_id, "
             "team2_id, format, status) VALUES (9500, ?, 'Alpha', 'Beta', 1, 2, "
             "'bo3', 'not_started')", (iso(-2),))
conn.execute("INSERT INTO matches (match_id, begin_at, team1, team2, team1_id, "
             "team2_id, format, status) VALUES (9501, ?, 'Gamma', 'Delta', 3, 4, "
             "'bo3', 'not_started')", (iso(-2),))
conn.commit()
for agent, sel, conf, why in [
    ("chalkbot", "Alpha", 0.60, "chalkbot: 30d rates favor Alpha."),
    ("form", "Alpha", 0.55, "form: hotter last-5."),
    ("h2h", "Alpha", 0.65, "h2h: Alpha leads 3-1."),
]:
    assert db.insert_pick(conn, 9500, agent, sel, conf, why)
conn.execute("INSERT INTO discussion (match_id, agent, round, statement) VALUES "
             "(9500, 'form', 2, '@h2h history says Alpha, but momentum is real.')")
conn.execute("INSERT INTO discussion (match_id, agent, round, statement) VALUES "
             "(9500, 'h2h', 2, '@chalkbot backs Alpha, and the fixture agrees 3-1.')")
conn.commit()
RATES = chalkbot.win_rates(conn)

print("[1] prompt-builder includes form / 30d / h2h / round-1 / debate facts")
m = conn.execute("SELECT * FROM matches WHERE match_id=9500").fetchone()
msgs = sage.build_messages(conn, m, rates=RATES)
sysmsg, user = msgs[0]["content"], msgs[1]["content"]
check("two messages (system+user)", len(msgs) == 2 and sysmsg and user)
check("both team names quoted", '"Alpha"' in user and '"Beta"' in user)
check("last-10 form line with W-L", "last-10 form" in user and "8W-1L" in user
      and "2W-6L" in user, "Alpha 8W-1L, Beta 2W-6L expected")
check("30-day win rates exclude the 45d-old match",
      "30-day win rate" in user and "(7-1, n=8)" in user and "(2-6, n=8)" in user)
check("h2h record line", "Head-to-head" in user and "3-1" in user
      and "across 4 meeting(s)" in user)
check("round-1 picks with agents+confidence",
      "chalkbot: \"Alpha\" @ 0.60" in user and "h2h: \"Alpha\" @ 0.65" in user)
check("round-2 critique included", "momentum is real." in user)
check("system demands strict 3-field JSON",
      "STRICT JSON" in sysmsg and "selection" in sysmsg and "confidence" in sysmsg
      and "reasoning" in sysmsg and "100 words" in sysmsg)
m2 = conn.execute("SELECT * FROM matches WHERE match_id=9501").fetchone()
user2 = sage.build_messages(conn, m2, rates=RATES)[1]["content"]
check("no-data match: placeholders present",
      "(none recorded)" in user2 and "(no debate recorded)" in user2
      and "no prior meetings" in user2)

print("[2] parse_llm_pick: clean / fenced / embedded / garbage / contract edges")
good = '{"selection": "Alpha", "confidence": 0.62, "reasoning": "form edge."}'
check("clean JSON", sage.parse_llm_pick(good, "Alpha", "Beta") ==
      ("Alpha", 0.62, "form edge."))
check("fenced ```json", sage.parse_llm_pick(
    "```json\n" + good + "\n```", "Alpha", "Beta") == ("Alpha", 0.62, "form edge."))
check("fenced plain ```", sage.parse_llm_pick(
    "```\n" + good + "\n```", "Alpha", "Beta") == ("Alpha", 0.62, "form edge."))
check("prose + unbalanced decoy brace + JSON", sage.parse_llm_pick(
    'Hmm {oops not json. Answer: {"selection": "Beta", "confidence": 0.7, '
    '"reasoning": "value pick."} hope that helps', "Alpha", "Beta") ==
    ("Beta", 0.7, "value pick."))
check("garbage -> None", sage.parse_llm_pick("no json at all", "Alpha", "Beta") is None)
check("empty -> None", sage.parse_llm_pick("", "Alpha", "Beta") is None)
check("None -> None", sage.parse_llm_pick(None, "Alpha", "Beta") is None)
check("confidence 0.3 -> None", sage.parse_llm_pick(
    '{"selection": "Alpha", "confidence": 0.3, "reasoning": "x"}', "Alpha", "Beta") is None)
check("confidence 1.2 -> None", sage.parse_llm_pick(
    '{"selection": "Alpha", "confidence": 1.2, "reasoning": "x"}', "Alpha", "Beta") is None)
check("selection not team1/team2 -> None", sage.parse_llm_pick(
    '{"selection": "Gamma", "confidence": 0.7, "reasoning": "x"}', "Alpha", "Beta") is None)
check("string confidence -> None", sage.parse_llm_pick(
    '{"selection": "Alpha", "confidence": "0.7", "reasoning": "x"}', "Alpha", "Beta") is None)
check("whitespace-variant selection canonicalized",
      sage.parse_llm_pick('{"selection": " Alpha", "confidence": 0.7, "reasoning": "x"}',
                          "Alpha", "Beta")[0] == "Alpha")
check("exact stored name with leading space kept",
      sage._canonical_team(" Phantom Academy", " Phantom Academy", "Entropy")
      == " Phantom Academy")
long_why = " ".join(f"word{i}" for i in range(150))
parsed = sage.parse_llm_pick(
    '{"selection": "Alpha", "confidence": 0.9, "reasoning": "%s"}' % long_why,
    "Alpha", "Beta")
check("reasoning >100 words truncated to 100", parsed is not None
      and len(parsed[2].split()) == 100)
check("extra keys tolerated (we build the gate payload ourselves)",
      sage.parse_llm_pick(
          '{"selection": "Alpha", "confidence": 0.7, "reasoning": "x", "note": "hi"}',
          "Alpha", "Beta") == ("Alpha", 0.7, "x"))

print("[3] pick_sage: fallback on unparseable, prefix + cap on good output")
fb = sage.pick_sage(conn, m, rates=RATES, ask=lambda msgs: "complete garbage")
check("fallback selection team1 @ 0.50",
      fb["selection"] == "Alpha" and fb["confidence"] == 0.50 and fb["fallback"])
check("fallback reasoning exact", fb["reasoning"] == "sage: LLM output unparseable")
ok = sage.pick_sage(conn, m, rates=RATES, ask=lambda msgs: good)
check("good output: selection+confidence pass through",
      ok["selection"] == "Alpha" and ok["confidence"] == 0.62 and not ok["fallback"])
check("good output reasoning prefixed 'sage:'", ok["reasoning"] == "sage: form edge.")

print("[4] run() end-to-end via REAL gate subprocess (stubbed LLM)")
calls = []


def fake_ask(messages):
    calls.append(messages)
    return ('```json\n{"selection": "Beta", "confidence": 0.66, '
            '"reasoning": "h2h underdog value."}\n```')


res = sage.run(conn=conn, db_path=str(db_path), probe=lambda: True,
               ask=fake_ask, max_matches=40)
check("2 matches picked (9500+9501)", res["made"] == 2 and res["eligible"] == 2, str(res))
sage_rows = conn.execute("SELECT match_id, selection, confidence, reasoning FROM picks "
                         "WHERE agent='sage' ORDER BY match_id").fetchall()
check("gate stored exact LLM JSON for 9500 (Beta @ 0.66)",
      sage_rows[0]["match_id"] == 9500 and sage_rows[0]["selection"] == "Beta"
      and abs(sage_rows[0]["confidence"] - 0.66) < 1e-9)
check("9501: stub's 'Beta' not a team there -> fallback Gamma @ 0.50",
      sage_rows[1]["match_id"] == 9501 and sage_rows[1]["selection"] == "Gamma"
      and sage_rows[1]["confidence"] == 0.50
      and sage_rows[1]["reasoning"] == "sage: LLM output unparseable")
check("reasoning prefixed sage:", all(r["reasoning"].startswith("sage:") for r in sage_rows))
check("one prompt per match", len(calls) == 2)
res2 = sage.run(conn=conn, db_path=str(db_path), probe=lambda: True,
                ask=fake_ask, max_matches=40)
check("rerun: 0 eligible, 0 made (one pick per match)",
      res2["made"] == 0 and res2["eligible"] == 0, str(res2))
check("still exactly 2 sage picks",
      conn.execute("SELECT COUNT(*) c FROM picks WHERE agent='sage'").fetchone()["c"] == 2)
dup_ok = sage.submit_via_gate("sage", 9500, "Alpha", 0.5, "sage: duplicate probe",
                              str(db_path))
check("gate rejects duplicate sage pick", dup_ok is False)
check("duplicate did not append",
      conn.execute("SELECT COUNT(*) c FROM picks WHERE agent='sage'").fetchone()["c"] == 2)

print("[5] unreachable endpoint: graceful skip, no picks")
conn.execute("INSERT INTO matches (match_id, begin_at, team1, team2, team1_id, "
             "team2_id, format, status) VALUES (9502, ?, 'Epsilon', 'Zeta', 5, 6, "
             "'bo3', 'not_started')", (iso(-1),))
conn.commit()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    res3 = sage.run(conn=conn, db_path=str(db_path), probe=lambda: False, ask=fake_ask)
check("returns skipped flag", res3.get("skipped") is True and res3["made"] == 0, str(res3))
check("prints exact skip message",
      "sage skipped: LLM endpoint unreachable" in buf.getvalue())
check("no sage pick written for 9502",
      conn.execute("SELECT COUNT(*) c FROM picks WHERE match_id=9502 AND "
                   "agent='sage'").fetchone()["c"] == 0)

print("[6] max_matches cap + rate-limit sleep honored")
for mid, t1, t2 in [(9503, "Eta", "Theta"), (9504, "Iota", "Kappa")]:
    conn.execute("INSERT INTO matches (match_id, begin_at, team1, team2, team1_id, "
                 "team2_id, format, status) VALUES (?, ?, ?, ?, 7, 8, 'bo3', "
                 "'not_started')", (mid, iso(-1), t1, t2))
conn.commit()
calls.clear()
res4 = sage.run(conn=conn, db_path=str(db_path), probe=lambda: True,
                ask=fake_ask, max_matches=2)
check("cap respected: only 2 of 3 eligible", res4["made"] == 2 and res4["eligible"] == 2,
      str(res4))
check("one prompt per capped match", len(calls) == 2)
sage_picks_now = conn.execute("SELECT COUNT(*) c FROM picks WHERE agent='sage'").fetchone()["c"]
check("total sage picks now 4", sage_picks_now == 4)

print("[7] append-only invariant: sage source has no UPDATE/DELETE on picks")
import re  # noqa: E402
src = (HERE / "expert_sage.py").read_text() + (ROOT / "daily.py").read_text()
mutations = re.findall(r"(?:UPDATE|DELETE\s+FROM)\s+picks", src, re.I)
check("no UPDATE/DELETE on picks in sage/daily", not mutations, str(mutations))

conn.close()
shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
sys.exit(0 if not fails else 1)