#!/usr/bin/env python3
"""Generate the static cvbets dashboard -> site/index.html.

Pure read-only: SELECTs from data/ledger.db, writes one self-contained HTML
file (inline CSS, no JS, no external deps). Regenerate anytime with:
  .venv/bin/python scripts/generate_site.py
Deploy later by dropping the site/ folder into GitHub Pages.
"""
import html
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db import connect  # noqa: E402

OUT = ROOT / "site" / "index.html"


def esc(v):
    return html.escape(str(v)) if v is not None else "—"


def day(v):
    return html.escape((v or "?")[:16].replace("T", " ") + " UTC") if v else "—"


def bar(conf):
    pct = max(0.0, min(1.0, conf)) * 100
    return (f'<div class="bar"><div class="fill" style="width:{pct:.0f}%"></div></div>'
            f'<span class="pct">{conf:.2f}</span>')


def main():
    conn = connect()

    totals = conn.execute(
        "SELECT (SELECT COUNT(*) FROM matches) m, "
        "(SELECT COUNT(*) FROM picks) p, "
        "(SELECT COUNT(*) FROM picks WHERE result IS NOT NULL) g, "
        "(SELECT COUNT(*) FROM picks WHERE result IS NULL) pend").fetchone()

    standings = conn.execute(
        "SELECT agent, COUNT(*) n, SUM(result) w, SUM(1 - result) l, AVG(brier) bm "
        "FROM picks WHERE result IS NOT NULL GROUP BY agent ORDER BY n DESC").fetchall()

    upcoming = conn.execute(
        "SELECT p.agent, p.selection, p.confidence, m.begin_at, m.team1, m.team2, "
        "m.league, m.tournament, m.format "
        "FROM picks p JOIN matches m ON m.match_id = p.match_id "
        "WHERE p.result IS NULL AND m.status = 'not_started' "
        "ORDER BY m.begin_at LIMIT 150").fetchall()

    results = conn.execute(
        "SELECT p.agent, p.selection, p.confidence, p.result, p.brier, "
        "m.begin_at, m.team1, m.team2, m.score "
        "FROM picks p JOIN matches m ON m.match_id = p.match_id "
        "WHERE p.result IS NOT NULL ORDER BY p.id DESC LIMIT 100").fetchall()
    conn.close()

    gen_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    s_rows = ""
    if not standings:
        s_rows = '<tr><td colspan="6" class="empty">No graded picks yet.</td></tr>'
    for r in standings:
        n, w = r["n"], r["w"] or 0
        s_rows += (f"<tr><td>{html.escape(r['agent'])}</td><td>{n}</td>"
                   f"<td>{w}–{r['l'] or 0}</td><td>{w / n:.1%}</td>"
                   f"<td>{(r['bm'] or 0):.4f}</td><td>—</td></tr>")

    u_rows = ""
    if not upcoming:
        u_rows = '<tr><td colspan="6" class="empty">No pending picks.</td></tr>'
    for r in upcoming:
        u_rows += (f"<tr><td>{day(r['begin_at'])}</td>"
                   f"<td>{esc(r['league'])}<span class='sub'>{esc(r['tournament'])}</span></td>"
                   f"<td>{esc(r['team1'])} <b>vs</b> {esc(r['team2'])} "
                   f"<span class='sub'>{esc(r['format'])}</span></td>"
                   f"<td class='pick'>{html.escape(r['selection'])}</td>"
                   f"<td>{bar(r['confidence'])}</td>"
                   f"<td>{html.escape(r['agent'])}</td></tr>")

    r_rows = ""
    if not results:
        r_rows = '<tr><td colspan="7" class="empty">No graded picks yet.</td></tr>'
    for r in results:
        cls = "win" if r["result"] == 1 else "loss"
        tag = "WIN" if r["result"] == 1 else "LOSS"
        r_rows += (f"<tr><td>{day(r['begin_at'])}</td>"
                   f"<td>{esc(r['team1'])} <b>vs</b> {esc(r['team2'])}</td>"
                   f"<td class='pick'>{html.escape(r['selection'])}</td>"
                   f"<td>{r['confidence']:.2f}</td>"
                   f"<td>{esc(r['score'])}</td>"
                   f"<td class='{cls}'>{tag}</td>"
                   f"<td>{(r['brier'] or 0):.4f}</td></tr>")

    page = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cvbets — CS2 prediction tracker</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; padding:2rem; background:#0d1117; color:#c9d1d9;
        font:15px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif; }}
  h1 {{ margin:0 0 .2rem; color:#e6edf3; font-size:1.6rem; }}
  .tagline {{ color:#8b949e; margin:0 0 1.5rem; font-size:.9rem; }}
  .stats {{ display:flex; gap:1rem; flex-wrap:wrap; margin-bottom:2rem; }}
  .stat {{ background:#161b22; border:1px solid #30363d; border-radius:8px;
         padding:.8rem 1.2rem; min-width:9rem; }}
  .stat b {{ display:block; font-size:1.4rem; color:#e6edf3; }}
  h2 {{ color:#e6edf3; font-size:1.1rem; margin:2.2rem 0 .6rem;
      border-bottom:1px solid #21262d; padding-bottom:.4rem; }}
  table {{ border-collapse:collapse; width:100%; margin-bottom:1rem; }}
  th, td {{ text-align:left; padding:.45rem .7rem; border-bottom:1px solid #21262d; }}
  th {{ color:#8b949e; font-weight:600; font-size:.8rem; text-transform:uppercase; }}
  tr:hover td {{ background:#161b22; }}
  .sub {{ display:block; color:#8b949e; font-size:.78rem; }}
  .pick {{ color:#58a6ff; font-weight:600; }}
  .win {{ color:#3fb950; font-weight:700; }}
  .loss {{ color:#f85149; font-weight:700; }}
  .empty {{ color:#8b949e; font-style:italic; }}
  .bar {{ display:inline-block; width:90px; height:8px; background:#21262d;
        border-radius:4px; vertical-align:middle; margin-right:.4rem; }}
  .fill {{ height:8px; border-radius:4px; background:#58a6ff; }}
  .pct {{ color:#8b949e; font-size:.85rem; }}
  footer {{ margin-top:3rem; color:#8b949e; font-size:.8rem;
          border-top:1px solid #21262d; padding-top:1rem; }}
</style></head><body>
<h1>cvbets</h1>
<p class="tagline">Simulated CS2 prediction tracker — match-winner picks, scored by
Brier score. No real money. Generated {gen_ts}.</p>
<div class="stats">
  <div class="stat"><b>{totals['m']}</b>matches tracked</div>
  <div class="stat"><b>{totals['p']}</b>picks logged</div>
  <div class="stat"><b>{totals['g']}</b>graded</div>
  <div class="stat"><b>{totals['pend']}</b>pending</div>
</div>
<h2>Agent standings</h2>
<table><tr><th>Agent</th><th>Picks</th><th>W–L</th><th>Win %</th><th>Mean Brier</th><th>ROI</th></tr>
{s_rows}</table>
<h2>Upcoming picks</h2>
<table><tr><th>Kickoff</th><th>Event</th><th>Match</th><th>Pick</th><th>Confidence</th><th>Agent</th></tr>
{u_rows}</table>
<h2>Recent results</h2>
<table><tr><th>Date</th><th>Match</th><th>Pick</th><th>Conf</th><th>Score</th><th>Result</th><th>Brier</th></tr>
{r_rows}</table>
<footer>cvbets Phase 1 — deterministic data layer. Picks are append-only; only the
grader writes results. Bets/ROI activate once an odds source exists.</footer>
</body></html>"""

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(page)
    print(f"wrote {OUT} ({len(page):,} bytes) — {len(upcoming)} upcoming, "
          f"{len(results)} recent results shown")


if __name__ == "__main__":
    main()