"""daily.py — nightly loop: ingest -> chalkbot -> experts -> grade -> summary.

Run: python daily.py   (reads PANDASCORE_TOKEN from env or ./.env)
A timestamped copy of each run summary is appended to data/daily.log.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

import chalkbot
import debate
import experts
import grade
import ingest
import expert_sage as sage  # noqa: E402
from db import init_db, log

ROOT = Path(__file__).resolve().parent
DAILY_LOG = ROOT / "data" / "daily.log"


def main():
    log("=== daily run: start ===")
    conn = init_db()
    try:
        ing = ingest.run(conn)
        cb = chalkbot.run(conn)
        ex = experts.run(conn=conn)
        db_ = debate.run(conn)
        sg = sage.run(conn=conn)
        gr = grade.run(conn)

        upcoming = conn.execute(
            "SELECT COUNT(*) AS c FROM matches WHERE status = 'not_started'"
        ).fetchone()["c"]
        with_picks = conn.execute(
            "SELECT COUNT(*) AS c FROM matches m WHERE m.status = 'not_started' "
            "AND EXISTS (SELECT 1 FROM picks p WHERE p.match_id = m.match_id)"
        ).fetchone()["c"]
        total_picks = conn.execute(
            "SELECT COUNT(*) AS c FROM picks").fetchone()["c"]

        skipped = ing["upcoming"]["skipped"] + ing["past"]["skipped"]
        lines = [
            "---- daily summary ----",
            f"matches ingested: upcoming {ing['upcoming']['new']} new / "
            f"{ing['upcoming']['updated']} updated; past {ing['past']['pages']} page(s) "
            f"{ing['past']['new']} new / {ing['past']['updated']} updated; {skipped} skipped",
            f"upcoming matches: {upcoming} not_started, {with_picks} with picks "
            f"(chalkbot +{cb['made']}, form +{ex.get('form', 0)}, h2h +{ex.get('h2h', 0)}, "
            f"sage +{sg.get('made', 0)}" + (", LLM unreachable" if sg.get("skipped") else "") + ")",
            f"picks graded: {gr['graded']} (pending now: {gr['pending']}; total picks: {total_picks})",
            f"debate: {db_['critiques']} new critiques posted",
            "agent_stats:",
        ]
        if gr["stats"]:
            lines.append(f"  {'agent':<12}{'n_picks':>9}{'wins':>7}{'brier':>9}{'roi':>9}")
            for agent, s in sorted(gr["stats"].items()):
                brier_s = f"{s['brier_mean']:.4f}" if s["brier_mean"] is not None else "-"
                roi_s = f"{s['roi']:+.1%}" if s["roi"] is not None else "-"
                lines.append(f"  {agent:<12}{s['n_picks']:>9}{s['wins']:>7}{brier_s:>9}{roi_s:>9}")
        else:
            lines.append("  (no graded picks yet)")
        lines.append(f"db: {ROOT / 'data' / 'ledger.db'}")

        print()
        for line in lines:
            print(line)
        log("=== daily run: done ===")

        DAILY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DAILY_LOG.open("a") as fh:
            fh.write(f"--- run {datetime.now(timezone.utc).isoformat(timespec='seconds')} ---\n")
            fh.write("\n".join(lines) + "\n")
    finally:
        conn.close()


if __name__ == "__main__":
    main()