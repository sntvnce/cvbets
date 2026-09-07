"""End-to-end tests for scripts/submit_pick.py (throwaway db + live-DB probe)."""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from db import connect, init_db  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="cvbets_submit_"))
test_db = tmp / "test.db"
conn = init_db(test_db)
conn.execute("INSERT INTO matches (match_id, begin_at, team1, team2, team1_id, team2_id, "
             "format, status) VALUES (9200, '2026-09-10T12:00:00Z', 'Alpha', 'Beta', 1, 2, "
             "'bo3', 'not_started')")
conn.execute("INSERT INTO matches (match_id, begin_at, team1, team2, team1_id, team2_id, "
             "format, status) VALUES (9201, '2026-09-01T12:00:00Z', 'Alpha', 'Beta', 1, 2, "
             "'bo3', 'finished')")
conn.commit()
conn.close()

SCRIPT = ROOT / "scripts" / "submit_pick.py"
VALID = {"agent": "chief", "match_id": 9200, "selection": "Alpha",
         "confidence": 0.62, "reasoning": "Alpha has the better recent record."}
fails = []


def run_case(name, payload, expect_exit, expect_frag):
    if isinstance(payload, (dict, list)):
        import json
        safe = "".join(c if c.isalnum() else "_" for c in name)
        p = tmp / f"{safe}.json"
        p.write_text(json.dumps(payload))
        src = str(p)
    elif payload == "RAWBAD":
        r = subprocess.run([sys.executable, str(SCRIPT), "-", "--db", str(test_db)],
                           input="{not json", capture_output=True, text=True, cwd=str(ROOT))
        out = r.stdout + r.stderr
        ok = r.returncode == expect_exit and expect_frag in out
        print(f"  {'PASS' if ok else 'FAIL'}: {name} (exit {r.returncode})")
        if not ok:
            fails.append(name)
            print(f"    expected exit {expect_exit} containing {expect_frag!r}; got: {out[:300]}")
        return
    else:
        src = payload
    r = subprocess.run([sys.executable, str(SCRIPT), src, "--db", str(test_db)],
                       capture_output=True, text=True, cwd=str(ROOT))
    out = r.stdout + r.stderr
    ok = r.returncode == expect_exit and expect_frag in out
    print(f"  {'PASS' if ok else 'FAIL'}: {name} (exit {r.returncode})")
    if not ok:
        fails.append(name)
        print(f"    expected exit {expect_exit} containing {expect_frag!r}; got: {out[:300]}")


print("[submit_pick] acceptance + rejection paths")
run_case("valid pick accepted", VALID, 0, "ACCEPTED")
run_case("duplicate agent/match rejected", VALID, 1, "already has pick")
run_case("second agent same match ok",
         {**VALID, "agent": "expert2"}, 0, "ACCEPTED")
run_case("wrong selection", {**VALID, "agent": "exp3", "selection": "Delta"}, 1, "not one of the opponents")
run_case("confidence too low", {**VALID, "agent": "exp4", "confidence": 0.4}, 1, "outside [0.5, 1.0]")
run_case("confidence too high", {**VALID, "agent": "exp5", "confidence": 1.1}, 1, "outside [0.5, 1.0]")
run_case("reasoning over 100 words",
         {**VALID, "agent": "exp6", "reasoning": " ".join(["word"] * 101)}, 1, "101 words")
run_case("finished match rejected", {**VALID, "match_id": 9201, "agent": "exp7"}, 1, "not 'not_started'")
run_case("unknown match rejected", {**VALID, "match_id": 999999, "agent": "exp8"}, 1, "not found")
run_case("extra key rejected", {**VALID, "agent": "exp9", "odds": 1.5}, 1, "unknown key")
run_case("missing key rejected", {"agent": "exp10", "match_id": 9200}, 1, "missing key")
run_case("invalid json rejected", "RAWBAD", 1, "invalid JSON")
run_case("missing file rejected", str(tmp / "nope.json"), 1, "file not found")

print("[submit_pick] live-DB rejection probe (must not write)")
live = connect()
probe_match = live.execute("SELECT match_id, team1 FROM matches WHERE status='not_started' "
                           "ORDER BY match_id LIMIT 1").fetchone()
before = live.execute("SELECT COUNT(*) c FROM picks").fetchone()["c"]
live.close()
bad = tmp / "real_bad.json"
import json
bad.write_text(json.dumps({"agent": "probe", "match_id": probe_match["match_id"],
                           "selection": probe_match["team1"], "confidence": 0.4,
                           "reasoning": "live rejection probe"}))
r = subprocess.run([sys.executable, str(SCRIPT), str(bad)], capture_output=True,
                   text=True, cwd=str(ROOT))
live = connect()
after = live.execute("SELECT COUNT(*) c FROM picks").fetchone()["c"]
live.close()
ok = r.returncode == 1 and "outside [0.5, 1.0]" in r.stdout and before == after
print(f"  {'PASS' if ok else 'FAIL'}: live probe rejected, picks count unchanged "
      f"({before} -> {after})")
if not ok:
    fails.append("live probe")

print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
raise SystemExit(0 if not fails else 1)