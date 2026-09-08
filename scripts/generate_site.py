#!/usr/bin/env python3
"""Generate the cvbets dashboard -> site/index.html (single-file, interactive).

Pure read-only, stdlib only: SELECTs from data/ledger.db and writes ONE
self-contained dark-theme HTML file (inline CSS + vanilla JS, no CDN, no
external deps). Everything is server-rendered first, so every core table stays
readable with JavaScript disabled; the embedded `const DATA` payload is then
used by the JS to add sorting/filtering and interactive calibration/debate
views.

Sections: stats, agent standings, upcoming picks, pending results (awaiting
grade, LIVE badge for running matches), recent results, team profiles
(last-10 form / 30-day rate / all-time record / sparkline), top-5 debates,
per-agent calibration by confidence bucket.

Regenerate:  .venv/bin/python scripts/generate_site.py
Deploy: workflow mirrors site/index.html to docs/ for GitHub Pages.
"""
import html
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db import connect  # noqa: E402

OUT = ROOT / "site" / "index.html"

AGENT_COLORS = {"chalkbot": "#58a6ff", "form": "#3fb950", "h2h": "#d29922"}
FALLBACK_COLORS = ["#bc8cff", "#f778ba", "#79c0ff", "#ffa657", "#7ee787"]
BUCKETS = ((0.5, 0.6, "0.50–0.60"), (0.6, 0.7, "0.60–0.70"),
           (0.7, 0.8, "0.70–0.80"), (0.8, 1.0001, "0.80–1.00"))
MIN_BUCKET_N = 5          # buckets with fewer graded picks -> "insufficient data"
DEBATE_TOP = 5            # most-discussed upcoming matches to show
UPCOMING_LIMIT = 400      # per-pick rows (raised: the old 150 cap silently truncated)
RESULTS_LIMIT = 120
SPARK_LEN = 12            # matches in the team sparkline / recent list


def esc(v):
    return html.escape(str(v), quote=True) if v is not None else "—"


def disp(v):
    """Display value: trimmed text or em-dash."""
    s = (str(v).strip() if v is not None else "")
    return html.escape(s, quote=True) if s else "—"


def day(v):
    return html.escape((v or "?")[:16].replace("T", " ") + " UTC") if v else "—"


def now_utc():
    return datetime.now(timezone.utc)


def iso_ago(iso_ts, ref):
    """Days (float) between an ISO/SQL timestamp and ref, or None."""
    if not iso_ts:
        return None
    t = iso_ts.strip().replace(" ", "T")
    if t.endswith("Z"):
        t = t[:-1]
    try:
        dt = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0.0, (ref - dt).total_seconds() / 86400.0)


def agent_color(agent):
    if agent in AGENT_COLORS:
        return AGENT_COLORS[agent]
    h = sum(agent.encode("utf-8"))
    return FALLBACK_COLORS[h % len(FALLBACK_COLORS)]


def bar(conf):
    pct = max(0.0, min(1.0, conf)) * 100
    return (f'<div class="bar"><div class="fill" style="width:{pct:.0f}%"></div></div>'
            f'<span class="pct">{conf:.2f}</span>')


def spark(outcomes):
    """Tiny inline SVG sparkline for a W/L sequence (works without JS)."""
    if not outcomes:
        return '<svg class="spark" width="96" height="24" aria-hidden="true"></svg>'
    n = len(outcomes)
    pad, w = 4.0, 96.0
    step = (w - 2 * pad) / (n - 1) if n > 1 else 0.0
    pts, dots = [], []
    for i, o in enumerate(outcomes):
        x = pad + i * step
        y = 5.0 if o == "W" else 19.0
        pts.append(f"{x:.1f},{y:.1f}")
        dots.append(f'<circle class="d{o}" cx="{x:.1f}" cy="{y:.1f}" r="2.2"/>')
    if n == 1:
        pts = pts * 2  # zero-length polyline is invisible; keep the dot
    return (f'<svg class="spark" width="96" height="24" viewBox="0 0 96 24" '
            f'aria-hidden="true"><polyline points="{" ".join(pts)}"/>'
            f'{"".join(dots)}</svg>')


# ---------------------------------------------------------------- data ----

def fetch_data(conn):
    """Run every query once; return (json_payload, render_context)."""
    ref = now_utc()
    cutoff30 = (ref - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ctx = {"ref": ref}  # type: ignore[assignment]

    totals = conn.execute(
        "SELECT (SELECT COUNT(*) FROM matches) m, "
        "(SELECT COUNT(*) FROM picks) p, "
        "(SELECT COUNT(*) FROM picks WHERE result IS NOT NULL) g, "
        "(SELECT COUNT(*) FROM picks WHERE result IS NULL) pend, "
        "(SELECT COUNT(DISTINCT match_id) FROM picks WHERE result IS NULL) await_m"
    ).fetchone()

    standings = conn.execute(
        "SELECT agent, COUNT(*) n, SUM(result) w, SUM(1 - result) l, AVG(brier) bm "
        "FROM picks WHERE result IS NOT NULL GROUP BY agent ORDER BY n DESC"
    ).fetchall()
    ctx["standings"] = standings

    upcoming = conn.execute(
        "SELECT p.match_id, p.agent, p.selection, p.confidence, "
        "m.begin_at, m.team1, m.team2, m.league, m.tournament, m.format "
        "FROM picks p JOIN matches m ON m.match_id = p.match_id "
        "WHERE p.result IS NULL AND m.status = 'not_started' "
        "ORDER BY m.begin_at LIMIT ?", (UPCOMING_LIMIT,)).fetchall()
    ctx["upcoming"] = upcoming

    results = conn.execute(
        "SELECT p.agent, p.selection, p.confidence, p.result, p.brier, "
        "m.begin_at, m.team1, m.team2, m.score "
        "FROM picks p JOIN matches m ON m.match_id = p.match_id "
        "WHERE p.result IS NOT NULL ORDER BY p.id DESC LIMIT ?",
        (RESULTS_LIMIT,)).fetchall()
    ctx["results"] = results

    # --- pending results: matches with ungraded picks, grouped per match ---
    pend_rows = conn.execute(
        "SELECT m.match_id, m.begin_at, m.status, m.team1, m.team2, "
        "m.league, m.tournament, m.format, MIN(p.locked_at) AS oldest, "
        "COUNT(*) AS n_picks "
        "FROM picks p JOIN matches m ON m.match_id = p.match_id "
        "WHERE p.result IS NULL "
        "GROUP BY m.match_id ORDER BY m.begin_at, m.match_id").fetchall()
    ctx["pend_rows"] = pend_rows
    pend_picks = {}
    for r in conn.execute(
            "SELECT match_id, agent, selection, confidence FROM picks "
            "WHERE result IS NULL ORDER BY match_id, agent"):
        pend_picks.setdefault(r["match_id"], []).append(dict(r))
    ctx["pend_picks"] = pend_picks
    oldest_days = None
    for r in pend_rows:
        d = iso_ago(r["oldest"], ref)
        if d is not None and (oldest_days is None or d > oldest_days):
            oldest_days = d

    # --- team profiles for every team appearing in upcoming picks ---------
    need = set()
    for r in upcoming:
        for t in (r["team1"], r["team2"]):
            if t and t.strip():
                need.add(t.strip())

    teams = {name: {"hist": [], "recent": []} for name in need}
    finished = conn.execute(
        "SELECT m.begin_at, m.team1, m.team2, m.winner, m.score FROM matches m "
        "WHERE m.status = 'finished' AND m.begin_at IS NOT NULL "
        "ORDER BY m.begin_at").fetchall()
    for r in finished:
        t1, t2 = r["team1"].strip(), r["team2"].strip()
        win = (r["winner"] or "").strip()
        for name, opp, won in ((t1, t2, win == t1), (t2, t1, win == t2)):
            rec = teams.get(name)
            if rec is None:
                continue  # team not in upcoming picks; skip
            rec["hist"].append((r["begin_at"], won))
            rec["recent"].append({"d": r["begin_at"][:10], "opp": opp,
                                  "res": "W" if won else "L", "score": r["score"]})

    teams_out = {}
    for name in sorted(need):
        rec = teams[name]
        hist, recent = rec["hist"], rec["recent"][-SPARK_LEN:]
        l10 = [w for _, w in hist[-10:]]
        d30 = [w for d, w in hist if d >= cutoff30]
        allp = [w for _, w in hist]
        teams_out[name] = {
            "last10": ["W" if w else "L" for w in l10],
            "last10_wins": sum(l10), "last10_n": len(l10),
            "d30_played": len(d30), "d30_wins": sum(d30),
            "all_played": len(allp), "all_wins": sum(allp),
            "recent": recent,
        }
    ctx["teams"] = teams_out

    # --- debate: top-5 most-discussed upcoming matches ---------------------
    top_ids = [r["match_id"] for r in conn.execute(
        "SELECT d.match_id, COUNT(*) c FROM discussion d "
        "JOIN matches m ON m.match_id = d.match_id "
        "WHERE m.status = 'not_started' AND EXISTS ("
        "  SELECT 1 FROM picks p WHERE p.match_id = d.match_id AND p.result IS NULL) "
        "GROUP BY d.match_id ORDER BY c DESC, MIN(m.begin_at), d.match_id "
        "LIMIT ?", (DEBATE_TOP,))]
    debate, r1_count = [], 0
    for mid in top_ids:
        m = conn.execute(
            "SELECT match_id, begin_at, team1, team2, league, tournament, format "
            "FROM matches WHERE match_id = ?", (mid,)).fetchone()
        stmts, r1 = [], 0
        for d in conn.execute(
                "SELECT agent, round, statement FROM discussion WHERE match_id = ? "
                "ORDER BY round, id", (mid,)):
            stmts.append({"agent": d["agent"], "round": d["round"],
                          "statement": d["statement"]})
            r1 += d["round"] == 1
        r1_count += r1
        picks = [{"agent": p["agent"], "selection": p["selection"],
                  "confidence": p["confidence"]} for p in pend_picks.get(mid, [])]
        debate.append({
            "match_id": mid, "team1": m["team1"].strip(), "team2": m["team2"].strip(),
            "begin_at": m["begin_at"], "league": m["league"],
            "tournament": m["tournament"], "format": m["format"],
            "n_statements": len(stmts), "n_round1": r1, "statements": stmts,
            "picks": picks,
        })
    ctx["debate"] = debate

    # --- calibration: graded picks bucketed by confidence ------------------
    agents = [r["agent"] for r in standings]
    by_agent = {a: [] for a in agents}
    for r in conn.execute(
            "SELECT agent, confidence, result FROM picks WHERE result IS NOT NULL"):
        for lo, hi, _label in BUCKETS:
            if lo <= r["confidence"] < hi:
                by_agent.setdefault(r["agent"], []).append(
                    (r["confidence"], r["result"]))
                break
    calibration = {}
    for a in agents:
        buckets = []
        for lo, hi, label in BUCKETS:
            sel = [(c, r) for c, r in by_agent.get(a, []) if lo <= c < hi]
            n = len(sel)
            buckets.append({
                "label": label, "n": n,
                "mean_conf": (sum(c for c, _ in sel) / n) if n else None,
                "wins": sum(r for _, r in sel),
                "actual": (sum(r for _, r in sel) / n) if n else None,
                "sufficient": n >= MIN_BUCKET_N,
            })
        calibration[a] = buckets
    ctx["calibration"] = calibration

    debate_agents = {s["agent"] for dd in debate for s in dd["statements"]}
    payload = {
        "generated": ref.strftime("%Y-%m-%d %H:%M UTC"),
        "totals": {"matches": totals["m"], "picks": totals["p"],
                   "graded": totals["g"], "pending_picks": totals["pend"],
                   "awaiting_results": totals["await_m"], "teams": len(need)},
        "agents": {a: agent_color(a) for a in sorted(set(agents) | debate_agents)},
        "standings": [{"agent": r["agent"], "n": r["n"], "wins": r["w"] or 0,
                       "losses": r["l"] or 0, "brier": r["bm"] or 0}
                      for r in standings],
        "matches": [{"match_id": r["match_id"], "begin_at": r["begin_at"],
                     "status": r["status"], "team1": r["team1"].strip(),
                     "team2": r["team2"].strip(), "league": r["league"],
                     "tournament": r["tournament"], "format": r["format"],
                     "oldest_locked": r["oldest"],
                     "age_days": (round(d, 2) if (d := iso_ago(r["oldest"], ref))
                                  is not None else None)}
                    for r in pend_rows],
        "picks": [{"match_id": mid, **p}
                  for mid, ps in pend_picks.items() for p in ps],
        "teams": teams_out,
        "discussion": debate,
        "calibration": calibration,
        "meta": {"upcoming_rows": len(upcoming), "results_rows": len(results),
                 "pending_matches": len(pend_rows),
                 "pending_picks": sum(len(v) for v in pend_picks.values()),
                 "teams": len(need), "debate_matches": len(debate),
                 "debate_statements": sum(d["n_statements"] for d in debate),
                 "round1_statements": r1_count,
                 "oldest_pending_days": (round(oldest_days, 2)
                                         if oldest_days is not None else None)},
    }
    return payload, ctx


# ------------------------------------------------------- static render ----

def standings_rows(standings):
    if not standings:
        return '<tr><td colspan="6" class="empty">No graded picks yet.</td></tr>'
    out = []
    for r in standings:
        n, w = r["n"], r["w"] or 0
        out.append(
            f'<tr><td><span class="adot" style="background:{agent_color(r["agent"])}"></span>'
            f'{esc(r["agent"])}</td><td data-v="{n}">{n}</td>'
            f'<td data-v="{w}">{w}–{r["l"] or 0}</td>'
            f'<td data-v="{w / n:.4f}">{w / n:.1%}</td>'
            f'<td data-v="{(r["bm"] or 0):.4f}">{(r["bm"] or 0):.4f}</td><td>—</td></tr>')
    return "".join(out)


def upcoming_rows(upcoming):
    if not upcoming:
        return '<tr><td colspan="6" class="empty">No pending picks.</td></tr>'
    out = []
    for r in upcoming:
        out.append(
            f'<tr><td data-v="{esc(r["begin_at"] or "")}">{day(r["begin_at"])}</td>'
            f'<td>{disp(r["league"])}<span class="sub">{disp(r["tournament"])}</span></td>'
            f'<td>{disp(r["team1"])} <b>vs</b> {disp(r["team2"])} '
            f'<span class="sub">{disp(r["format"])}</span></td>'
            f'<td class="pick">{esc(r["selection"])}</td>'
            f'<td data-v="{r["confidence"]:.2f}">{bar(r["confidence"])}</td>'
            f'<td><span class="adot" style="background:{agent_color(r["agent"])}"></span>'
            f'{esc(r["agent"])}</td></tr>')
    return "".join(out)


def pending_rows(pend_rows, pend_picks, ref):
    if not pend_rows:
        return '<tr><td colspan="6" class="empty">Nothing awaiting results.</td></tr>'
    out = []
    for r in pend_rows:
        live = ('<span class="badge live">LIVE</span>' if r["status"] == "running"
                else '<span class="badge">upcoming</span>' if r["status"] == "not_started"
                else '<span class="badge">needs grade</span>')
        age = iso_ago(r["oldest"], ref)
        picks = pend_picks.get(r["match_id"], [])
        pl = " · ".join(
            f'<span class="adot" style="background:{agent_color(p["agent"])}"></span>'
            f'{esc(p["agent"])}: {esc(p["selection"])} '
            f'<span class="pct">@ {p["confidence"]:.2f}</span>'
            for p in picks)
        out.append(
            f'<tr><td data-v="{esc(r["begin_at"] or "")}">{day(r["begin_at"])}</td>'
            f'<td>{live}</td>'
            f'<td>{disp(r["team1"])} <b>vs</b> {disp(r["team2"])} '
            f'<span class="sub">{disp(r["format"])} · {disp(r["league"])}</span></td>'
            f'<td class="pickpicks">{pl}</td>'
            f'<td data-v="{age if age is not None else 0}">'
            f'{f"{age:.1f} d" if age is not None else "—"}</td>'
            f'<td data-v="{len(picks)}">{len(picks)}</td></tr>')
    return "".join(out)


def results_rows(results):
    if not results:
        return '<tr><td colspan="8" class="empty">No graded picks yet.</td></tr>'
    out = []
    for r in results:
        cls, tag = ("win", "WIN") if r["result"] == 1 else ("loss", "LOSS")
        out.append(
            f'<tr><td data-v="{esc(r["begin_at"] or "")}">{day(r["begin_at"])}</td>'
            f'<td><span class="adot" style="background:{agent_color(r["agent"])}"></span>'
            f'{esc(r["agent"])}</td>'
            f'<td>{disp(r["team1"])} <b>vs</b> {disp(r["team2"])}</td>'
            f'<td class="pick">{esc(r["selection"])}</td>'
            f'<td data-v="{r["confidence"]:.2f}">{r["confidence"]:.2f}</td>'
            f'<td>{esc(r["score"])}</td>'
            f'<td class="{cls}" data-v="{r["result"]}">{tag}</td>'
            f'<td data-v="{(r["brier"] or 0):.4f}">{(r["brier"] or 0):.4f}</td></tr>')
    return "".join(out)


def team_cards(teams):
    if not teams:
        return '<p class="empty">No teams in upcoming picks yet.</p>'
    out = []
    for name, t in teams.items():
        l10n, l10w = t["last10_n"], t["last10_wins"]
        d30p, d30w = t["d30_played"], t["d30_wins"]
        ap, aw = t["all_played"], t["all_wins"]
        l10s = f"{l10w}–{l10n - l10w}" if l10n else "—"
        d30s = (f"{d30w / d30p:.0%} ({d30w}–{d30p - d30w})" if d30p else "no matches")
        alls = (f"{aw / ap:.0%} ({aw}–{ap - aw})" if ap else "no matches")
        seq = " ".join(f'<span class="o{"w" if o == "W" else "l"}">{o}</span>'
                       for o in t["last10"]) or "—"
        if t["recent"]:
            rows = "".join(
                f'<tr><td>{esc(m["d"])}</td><td>{esc(m["opp"])}</td>'
                f'<td class="{"win" if m["res"] == "W" else "loss"}">{m["res"]}</td>'
                f'<td>{esc(m["score"])}</td></tr>'
                for m in reversed(t["recent"]))
            recent = (f'<div class="tblock"><h4>Recent matches</h4>'
                      f'<table class="mini"><tr><th>Date</th><th>Opponent</th>'
                      f'<th>Res</th><th>Score</th></tr>{rows}</table></div>')
        else:
            recent = ('<div class="tblock"><h4>Recent matches</h4>'
                      '<p class="empty">No finished matches recorded.</p></div>')
        if not ap:
            hist = '<p class="empty">No finished matches recorded in the ledger.</p>'
        else:
            hist = (f'<div class="tblock"><h4>Form breakdown</h4>'
                    f'<p>Last 10: {seq} <span class="pct">({l10s})</span></p>'
                    f'<p>Last 30 days: {d30s}</p>'
                    f'<p>All time in DB: {alls}</p></div>')
        out.append(
            f'<details class="tcard" data-team="{esc(name.lower())}">'
            f'<summary>{spark(t["last10"][-SPARK_LEN:])}'
            f'<span class="tname">{esc(name)}</span>'
            f'<span class="tstat">L10 {l10s}</span>'
            f'<span class="tstat">30d {d30s}</span>'
            f'<span class="tstat">All {alls}</span></summary>'
            f'<div class="tbody">{hist}{recent}</div></details>')
    return "".join(out)


def debate_static(debate):
    if not debate:
        return '<p class="empty">No discussion recorded for upcoming matches.</p>'
    out = []
    for d in debate:
        by_agent = {}
        for s in d["statements"]:
            by_agent.setdefault(s["agent"], []).append(s)
        for p in d["picks"]:
            by_agent.setdefault(p["agent"], [])
        rows = []
        for agent in sorted(by_agent):
            color = agent_color(agent)
            pick = next((p for p in d["picks"] if p["agent"] == agent), None)
            pos = (f'{esc(pick["selection"])} <span class="pct">@ '
                   f'{pick["confidence"]:.2f}</span>' if pick else "—")
            r1 = [s for s in by_agent[agent] if s["round"] == 1]
            r2 = [s for s in by_agent[agent] if s["round"] == 2]
            r1_html = "".join(f'<p class="stmt r1">{esc(s["statement"])}</p>'
                              for s in r1) or \
                '<p class="stmt none">No round-1 statement recorded.</p>'
            r2_html = "".join(f'<p class="stmt r2">{esc(s["statement"])}</p>'
                              for s in r2) or \
                '<p class="stmt none">No round-2 critique recorded.</p>'
            rows.append(
                f'<div class="dbody" style="border-left:3px solid {color}">'
                f'<div class="ahead"><span class="adot" style="background:{color}"></span>'
                f'<b>{esc(agent)}</b><span class="pos">position: {pos}</span></div>'
                f'{r1_html}{r2_html}</div>')
        out.append(
            f'<div class="dcard"><div class="dhead"><b>{esc(d["team1"])} vs '
            f'{esc(d["team2"])}</b><span class="sub">{day(d["begin_at"])} · '
            f'{disp(d["format"])} · {disp(d["league"])}</span>'
            f'<span class="pill">{d["n_statements"]} statements</span></div>'
            f'<div class="dwrap">{"".join(rows)}</div></div>')
    return "".join(out)


def calibration_static(calibration):
    if not calibration:
        return '<p class="empty">No graded picks to calibrate.</p>'
    rows = []
    for agent, buckets in calibration.items():
        color = agent_color(agent)
        for b in buckets:
            if b["n"] == 0:
                status, act = "empty", "—"
            elif not b["sufficient"]:
                status, act = f"insufficient data (n={b['n']})", "—"
            else:
                status, act = "ok", f'{b["actual"]:.0%}'
            exp = f'{b["mean_conf"]:.0%}' if b["mean_conf"] is not None else "—"
            rows.append(
                f'<tr><td><span class="adot" style="background:{color}"></span>'
                f'{esc(agent)}</td><td>{esc(b["label"])}</td>'
                f'<td data-v="{b["n"]}">{b["n"]}</td><td>{exp}</td>'
                f'<td data-v="{b["actual"] if b["actual"] is not None else 0}">{act}</td>'
                f'<td class="{"empty" if status != "ok" else ""}">{status}</td></tr>')
    return ('<table id="t-calib" data-sortable><thead><tr><th>Agent</th>'
            '<th>Confidence bucket</th><th data-t="n">Graded n</th><th>Expected</th>'
            '<th data-t="n">Actual</th><th>Status</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


# ------------------------------------------------------------- assets ----

CSS = """
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; padding:2rem; background:#0d1117; color:#c9d1d9;
        font:15px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif; }
  h1 { margin:0 0 .2rem; color:#e6edf3; font-size:1.6rem; }
  h2 { color:#e6edf3; font-size:1.1rem; margin:2.2rem 0 .6rem;
      border-bottom:1px solid #21262d; padding-bottom:.4rem; }
  h4 { color:#8b949e; font-size:.78rem; text-transform:uppercase;
      margin:.2rem 0 .4rem; }
  .tagline { color:#8b949e; margin:0 0 1.5rem; font-size:.9rem; }
  /* tab navigation */
  nav.tabs { display:flex; gap:.3rem; flex-wrap:wrap; margin:0 0 1.6rem;
            border-bottom:1px solid #21262d; padding-bottom:.6rem; }
  nav.tabs a { background:#0d1117; color:#8b949e; border:1px solid #30363d;
               border-radius:6px; padding:.45rem .9rem; font-size:.88rem;
               text-decoration:none; display:inline-block; }
  nav.tabs a:hover { color:#e6edf3; background:#161b22; }
  nav.tabs a.active { color:#e6edf3; background:#161b22;
                      border-color:#58a6ff; font-weight:600; }
  section.page { display:none; }
  section.page.active { display:block; }
  .stats { display:flex; gap:1rem; flex-wrap:wrap; margin-bottom:2rem; }
  .stat { background:#161b22; border:1px solid #30363d; border-radius:8px;
         padding:.8rem 1.2rem; min-width:9rem; }
  .stat b { display:block; font-size:1.4rem; color:#e6edf3; }
  table { border-collapse:collapse; width:100%; margin-bottom:1rem; }
  th, td { text-align:left; padding:.45rem .7rem; border-bottom:1px solid #21262d; }
  th { color:#8b949e; font-weight:600; font-size:.8rem; text-transform:uppercase; }
  tr:hover td { background:#161b22; }
  .sub { display:block; color:#8b949e; font-size:.78rem; }
  .pick { color:#58a6ff; font-weight:600; }
  .win { color:#3fb950; font-weight:700; }
  .loss { color:#f85149; font-weight:700; }
  .empty { color:#8b949e; font-style:italic; }
  .bar { display:inline-block; width:90px; height:8px; background:#21262d;
        border-radius:4px; vertical-align:middle; margin-right:.4rem; }
  .fill { height:8px; border-radius:4px; background:#58a6ff; }
  .pct { color:#8b949e; font-size:.85rem; }
  .adot { display:inline-block; width:9px; height:9px; border-radius:50%;
         margin-right:.45rem; }
  .badge { display:inline-block; font-size:.68rem; letter-spacing:.06em;
          padding:.1rem .45rem; border:1px solid #30363d; border-radius:10px;
          color:#8b949e; text-transform:uppercase; }
  .badge.live { color:#f85149; border-color:#f85149; }
  .pickpicks { max-width:34rem; font-size:.86rem; line-height:1.7; }
  .summaryline { color:#8b949e; font-size:.86rem; margin:-.3rem 0 .8rem; }
  .js-only { display:none; }
  .js-on.js-only { display:inline-block; }
  .filter { background:#0d1117; color:#c9d1d9; border:1px solid #30363d;
           border-radius:6px; padding:.35rem .6rem; font-size:.85rem;
           margin:0 .5rem .6rem 0; width:14rem; }
  button.filter { width:auto; cursor:pointer; }
  .hint { color:#8b949e; font-size:.78rem; margin-right:.8rem; }
  th.sortable { cursor:pointer; user-select:none; }
  th.sortable:hover { color:#c9d1d9; }
  th.sortable::after { content:"\\2195"; opacity:.4; margin-left:.3rem; font-size:.75rem; }
  th.sorted-asc::after { content:"\\2191"; opacity:1; }
  th.sorted-desc::after { content:"\\2193"; opacity:1; }
  /* team cards */
  .tcard { border:1px solid #21262d; border-radius:8px; margin-bottom:.45rem;
          background:#0d1117; }
  .tcard summary { display:flex; align-items:center; gap:1rem; cursor:pointer;
                  padding:.5rem .8rem; list-style:none; flex-wrap:wrap; }
  .tcard summary::-webkit-details-marker { display:none; }
  .tcard summary:hover { background:#161b22; }
  .tname { color:#e6edf3; font-weight:600; min-width:14rem; }
  .tstat { color:#8b949e; font-size:.82rem; min-width:9rem; }
  .spark polyline { fill:none; stroke:#58a6ff; stroke-width:1.5; }
  .spark .dW { fill:#3fb950; } .spark .dL { fill:#f85149; }
  .o { display:inline-block; width:1.2rem; font-weight:700; text-align:center; }
  .ow { color:#3fb950; } .ol { color:#f85149; }
  .tbody { display:flex; gap:2rem; padding:.4rem .8rem .8rem; flex-wrap:wrap;
          border-top:1px solid #21262d; }
  .tblock { min-width:16rem; }
  table.mini { width:auto; font-size:.82rem; }
  table.mini th, table.mini td { padding:.25rem .6rem; }
  /* debate */
  .dcard { border:1px solid #21262d; border-radius:8px; margin-bottom:1rem;
          background:#0d1117; }
  .dhead { padding:.6rem .8rem; background:#161b22; display:flex; gap:.8rem;
          align-items:baseline; flex-wrap:wrap; }
  .dhead b { color:#e6edf3; }
  .pill { margin-left:auto; color:#8b949e; font-size:.75rem;
         border:1px solid #30363d; border-radius:10px; padding:.05rem .5rem; }
  .dwrap { padding:.5rem .8rem .8rem; }
  .dbody { margin:.6rem 0; padding:.35rem .7rem; background:#161b22;
          border-radius:6px; }
  .ahead { display:flex; gap:.6rem; align-items:baseline; }
  .pos { color:#8b949e; font-size:.82rem; }
  .stmt { margin:.35rem 0 0; font-size:.88rem; }
  .stmt.r1 { color:#e6edf3; }
  .stmt.r2 { color:#c9d1d9; }
  .stmt.none { color:#8b949e; font-style:italic; }
  /* calibration */
  .calgrid { display:flex; gap:1rem; flex-wrap:wrap; }
  .calcard { background:#161b22; border:1px solid #30363d; border-radius:8px;
            padding:.8rem 1rem; min-width:20rem; flex:1; }
  .calrow { margin:.5rem 0; font-size:.85rem; }
  .calrow .lbl { display:flex; justify-content:space-between;
                flex-wrap:wrap; gap:.3rem; color:#8b949e; }
  .cbar { height:8px; background:#21262d; border-radius:4px; margin-top:.2rem;
         position:relative; }
  .cbar > div { height:8px; border-radius:4px; position:absolute; top:0; }
  .sparse { color:#8b949e; font-style:italic; }
  footer { margin-top:3rem; color:#8b949e; font-size:.8rem;
          border-top:1px solid #21262d; padding-top:1rem; }
"""

JS = r"""
(function () {
  'use strict';

  function $(sel, root) { return Array.prototype.slice.call(
      (root || document).querySelectorAll(sel)); }

  // Lazy access to the payload <script> (declared as `const DATA` later in the
  // document — a lexical global, so it must be resolved lazily, not at parse).
  function payload() {
    try { return (typeof DATA === 'undefined') ? null : DATA; }
    catch (e) { return null; }
  }

  // Reveal JS-only controls (filters, sort hints).
  function enableControls() {
    $('.js-only').forEach(function (el) { el.classList.add('js-on'); });
  }

  // ---- sortable tables --------------------------------------------------
  function cellValue(td, kind) {
    var v = td.getAttribute('data-v');
    if (v === null || v === '') v = td.textContent.trim();
    if (kind === 'n') { var f = parseFloat(v); return isNaN(f) ? -Infinity : f; }
    return v.toLowerCase();
  }
  function makeSortable(table) {
    var ths = $('th', table.tHead || table);
    ths.forEach(function (th, i) {
      th.classList.add('sortable');
      var kind = th.getAttribute('data-t') === 'n' ? 'n' : 's';
      th.addEventListener('click', function () {
        var desc = th.classList.contains('sorted-asc');
        ths.forEach(function (o) { o.classList.remove('sorted-asc', 'sorted-desc'); });
        th.classList.add(desc ? 'sorted-desc' : 'sorted-asc');
        var tbody = table.tBodies[0];
        var keyed = $('tr', tbody).map(function (tr, idx) {
          var td = tr.children[i];
          return { tr: tr, idx: idx, v: td ? cellValue(td, kind) : '' };
        });
        keyed.sort(function (a, b) {
          var c;
          if (kind === 'n') c = a.v - b.v;
          else c = a.v < b.v ? -1 : (a.v > b.v ? 1 : 0);
          if (c === 0) c = a.idx - b.idx;
          return desc ? -c : c;
        });
        keyed.forEach(function (k) { tbody.appendChild(k.tr); });
      });
    });
  }

  // ---- row filters ------------------------------------------------------
  function wireFilters() {
    $('input[data-filter]').forEach(function (inp) {
      var table = document.getElementById(inp.getAttribute('data-filter'));
      if (!table) return;
      inp.addEventListener('input', function () {
        var q = inp.value.trim().toLowerCase();
        $('tr', table.tBodies[0]).forEach(function (tr) {
          tr.hidden = q !== '' &&
            tr.textContent.toLowerCase().indexOf(q) === -1;
        });
      });
    });
  }

  // ---- team profile enhancements ---------------------------------------
  function wireTeams() {
    var search = document.getElementById('teamsearch');
    var cards = $('.tcard');
    if (search) {
      search.addEventListener('input', function () {
        var q = search.value.trim().toLowerCase();
        cards.forEach(function (c) {
          c.hidden = q !== '' &&
            (c.getAttribute('data-team') || '').indexOf(q) === -1;
        });
      });
    }
    var ex = document.getElementById('texpand'),
        co = document.getElementById('tcollapse');
    if (ex) ex.addEventListener('click', function () {
      cards.forEach(function (c) { c.open = true; });
    });
    if (co) co.addEventListener('click', function () {
      cards.forEach(function (c) { c.open = false; });
    });
  }

  // ---- calibration view (rendered from DATA) ---------------------------
  function pctf(x) {
    return (x === null || x === undefined) ? '—' : Math.round(x * 100) + '%';
  }
  function renderCalibration() {
    var host = document.getElementById('calib');
    var fallback = document.getElementById('calib-static');
    var DATA = payload();
    if (!host || !DATA || !DATA.calibration) return;
    var html = '<div class="calgrid">';
    Object.keys(DATA.calibration).forEach(function (agent) {
      var color = (DATA.agents && DATA.agents[agent]) || '#8b949e';
      html += '<div class="calcard"><h4 style="color:' + color + '">' +
              esc(agent) + '</h4>';
      DATA.calibration[agent].forEach(function (b) {
        if (b.n === 0) {
          html += '<div class="calrow"><div class="lbl"><span>' + b.label +
                  '</span><span class="sparse">no graded picks</span></div></div>';
        } else if (!b.sufficient) {
          html += '<div class="calrow"><div class="lbl"><span>' + b.label +
                  '</span><span class="sparse">insufficient data (n=' + b.n +
                  ', need 5)</span></div>' +
                  '<div class="cbar"><div style="width:' +
                  Math.round((b.mean_conf || 0) * 100) +
                  '%;background:#30363d"></div></div></div>';
        } else {
          html += '<div class="calrow"><div class="lbl"><span>' + b.label +
                  '</span><span>actual ' + pctf(b.actual) + ' vs expected ' +
                  pctf(b.mean_conf) + ' · n=' + b.n + '</span></div>' +
                  '<div class="cbar"><div style="width:' +
                  Math.round((b.mean_conf || 0) * 100) +
                  '%;background:#30363d"></div>' +
                  '<div style="width:' + Math.round(b.actual * 100) +
                  '%;background:' + color +
                  ';opacity:.85;top:2px;height:4px"></div></div></div>';
        }
      });
      html += '</div>';
    });
    html += '</div>';
    host.innerHTML = html;
    host.hidden = false;
    if (fallback) fallback.hidden = true;
  }

  // ---- debate view (rendered from DATA) ---------------------------------
  function renderDebate() {
    var host = document.getElementById('debate');
    var fallback = document.getElementById('debate-static');
    var DATA = payload();
    if (!host || !DATA || !DATA.discussion) return;
    var html = '';
    DATA.discussion.forEach(function (m) {
      var byAgent = {};
      m.statements.forEach(function (s) {
        (byAgent[s.agent] = byAgent[s.agent] || []).push(s);
      });
      m.picks.forEach(function (p) {
        byAgent[p.agent] = byAgent[p.agent] || [];
      });
      var body = Object.keys(byAgent).sort().map(function (a) {
        var color = (DATA.agents && DATA.agents[a]) || '#8b949e';
        var pick = m.picks.filter(function (p) { return p.agent === a; })[0];
        var pos = pick ? esc(pick.selection) + ' @ ' +
                  pick.confidence.toFixed(2) : '—';
        var r1 = byAgent[a].filter(function (s) { return s.round === 1; });
        var r2 = byAgent[a].filter(function (s) { return s.round === 2; });
        var r1h = r1.length
          ? r1.map(function (s) {
              return '<p class="stmt r1">' + esc(s.statement) + '</p>';
            }).join('')
          : '<p class="stmt none">No round-1 statement recorded.</p>';
        var r2h = r2.length
          ? r2.map(function (s) {
              return '<p class="stmt r2">' + esc(s.statement) + '</p>';
            }).join('')
          : '<p class="stmt none">No round-2 critique recorded.</p>';
        return '<div class="dbody" style="border-left:3px solid ' + color + '">' +
               '<div class="ahead"><span class="adot" style="background:' + color +
               '"></span><b>' + esc(a) + '</b><span class="pos">position: ' + pos +
               '</span></div>' + r1h + r2h + '</div>';
      }).join('');
      html += '<div class="dcard"><div class="dhead"><b>' + esc(m.team1) + ' vs ' +
              esc(m.team2) + '</b><span class="sub">' + esc(fmtDay(m.begin_at)) +
              ' · ' + esc(m.format || '—') + ' · ' + esc(m.league || '—') +
              '</span><span class="pill">' + m.n_statements +
              ' statements</span></div>' +
              '<div class="dwrap">' + body + '</div></div>';
    });
    if (!html) html = '<p class="empty">No discussion recorded for upcoming matches.</p>';
    host.innerHTML = html;
    host.hidden = false;
    if (fallback) fallback.hidden = true;
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;',
               '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function fmtDay(v) {
    if (!v) return '—';
    return v.slice(0, 16).replace('T', ' ') + ' UTC';
  }

  function init() {
    enableControls();
    $('table[data-sortable]').forEach(makeSortable);
    wireFilters();
    wireTeams();
    renderCalibration();
    renderDebate();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
"""


def json_script(data):
    """Embed the payload per spec: `<script> const DATA = {...}; </script>`.

    JSON is escaped for safe inline embedding ('</' sequences), and emitted
    with ensure_ascii=True so the JS parser only ever sees ASCII source (the
    em-dashes etc. stay readable via the static HTML; the JSON strings still
    decode to the exact same text after \\uXXXX unescaping).
    """
    payload = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
    payload = payload.replace("</", "<\\/")
    return f"<script>const DATA = {payload};</script>"


def _page_shell(title, body, gen_ts, totals, summary, active, gen_ts_short, data):
    """One real HTML page of the multi-page site. Navigation is plain <a>
    links — works with JS disabled, real URLs on GitHub Pages."""
    tabs = [
        ("index.html", "Overview", "index"),
        ("picks.html", "Picks & Pending", "picks"),
        ("results.html", "Results", "results"),
        ("teams.html", "Teams", "teams"),
        ("debate.html", "Debate", "debate"),
        ("calibration.html", "Calibration", "calibration"),
    ]
    nav = "".join(
        f'<a class="tab{" active" if key == active else ""}" href="{href}">{label}</a>'
        for href, label, key in tabs
    )
    stats = f"""<div class="stats">
  <div class="stat"><b>{totals['matches']}</b>matches tracked</div>
  <div class="stat"><b>{totals['picks']}</b>picks logged</div>
  <div class="stat"><b>{totals['graded']}</b>graded</div>
  <div class="stat"><b>{totals['pending_picks']}</b>pending picks</div>
  <div class="stat"><b>{totals['awaiting_results']}</b>awaiting results</div>
  <div class="stat"><b>{totals['teams']}</b>teams in play</div>
</div>"""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — cvbets</title>
<style>{CSS}</style></head><body>
<h1>cvbets</h1>
<p class="tagline">Simulated CS2 prediction tracker — match-winner picks, scored by
Brier score. No real money. Generated {gen_ts}.</p>
{stats}
<nav class="tabs" aria-label="Dashboard sections">{nav}</nav>
{body}
<footer>cvbets — deterministic data layer + expert debate. Picks are append-only;
only the grader writes results. Bets/ROI activate once an odds source exists.
Data: {totals['matches']} matches, {totals['picks']} picks ({totals['graded']} graded,
{totals['pending_picks']} pending) · {summary} · generated {gen_ts_short}.</footer>
<script>{JS}</script>
{json_script(data) if ACTIVE_PAGE_EMBEDS_DATA.get(active, False) else ''}
</body></html>"""


# which pages embed the full JSON payload (only those using JS-enhanced views)
ACTIVE_PAGE_EMBEDS_DATA = {"index": True, "picks": False, "results": False,
                           "teams": True, "debate": True, "calibration": True}


def _section(html):
    return html

def main():
    conn = connect()
    data, ctx = fetch_data(conn)
    conn.close()

    t = data["totals"]
    m = data["meta"]
    gen_ts = data["generated"]
    gen_ts_short = gen_ts[:16]
    oldest = m["oldest_pending_days"]
    oldest_s = f"{oldest:.1f} days" if oldest is not None else "n/a"
    summary = (f'{t["awaiting_results"]} matches awaiting results, '
               f'{t["pending_picks"]} pending picks, oldest {oldest_s}')

    s_rows = standings_rows(ctx["standings"])
    u_rows = upcoming_rows(ctx["upcoming"])
    p_rows = pending_rows(ctx["pend_rows"], ctx["pend_picks"], ctx["ref"])
    r_rows = results_rows(ctx["results"])
    t_cards = team_cards(data["teams"])
    d_cards = debate_static(data["discussion"])
    c_static = calibration_static(data["calibration"])

    sort_hint = '<span class="hint js-only">click a column to sort</span>'

    pages = {}

    # ---- overview: standings + totals ----
    pages["index.html"] = _page_shell(
        "Overview",
        f"""<h2>Agent standings</h2>
{sort_hint}
<input class="filter js-only" type="search" placeholder="filter agents…"
       data-filter="t-standings" aria-label="Filter standings">
<table id="t-standings" data-sortable><thead><tr><th>Agent</th><th data-t="n">Picks</th><th data-t="n">W–L</th><th data-t="n">Win %</th><th data-t="n">Mean Brier</th><th>ROI</th></tr></thead>
<tbody>{s_rows}</tbody></table>
<h2>Calibration — graded picks by confidence bucket</h2>
<p class="hint">Actual win rate vs bucket mean confidence. Buckets with fewer
than {m.get('min_bucket_n', 5)} graded picks are marked insufficient data.</p>
<div id="calib" hidden></div>
<div id="calib-static">{c_static}</div>""",
        gen_ts, t, summary, "index", gen_ts_short, data)

    # ---- picks & pending ----
    pages["picks.html"] = _page_shell(
        "Picks & Pending",
        f"""<h2>Upcoming picks</h2>
{sort_hint}
<input class="filter js-only" type="search" placeholder="filter picks…"
       data-filter="t-upcoming" aria-label="Filter upcoming picks">
<table id="t-upcoming" data-sortable><thead><tr><th>Kickoff</th><th>Event</th><th>Match</th><th>Pick</th><th>Confidence</th><th>Agent</th></tr></thead>
<tbody>{u_rows}</tbody></table>
<h2>Pending results</h2>
<p class="summaryline">{summary}</p>
<p class="hint">Matches with picks awaiting a grade. LIVE = in progress now;
upcoming = not started yet.</p>
{sort_hint}
<input class="filter js-only" type="search" placeholder="filter matches…"
       data-filter="t-pending" aria-label="Filter pending results">
<table id="t-pending" data-sortable><thead><tr><th>Kickoff</th><th>Status</th><th>Match</th><th>Picks (agent: selection @ conf)</th><th data-t="n">Oldest pick</th><th data-t="n">Picks</th></tr></thead>
<tbody>{p_rows}</tbody></table>""",
        gen_ts, t, summary, "picks", gen_ts_short, data)

    # ---- results ----
    pages["results.html"] = _page_shell(
        "Results",
        f"""<h2>Recent results</h2>
{sort_hint}
<input class="filter js-only" type="search" placeholder="filter results…"
       data-filter="t-results" aria-label="Filter recent results">
<table id="t-results" data-sortable><thead><tr><th>Date</th><th>Agent</th><th>Match</th><th>Pick</th><th data-t="n">Conf</th><th>Score</th><th>Result</th><th data-t="n">Brier</th></tr></thead>
<tbody>{r_rows}</tbody></table>""",
        gen_ts, t, summary, "results", gen_ts_short, data)

    # ---- teams ----
    pages["teams.html"] = _page_shell(
        "Teams",
        f"""<h2>Team profiles</h2>
<p class="hint">Teams appearing in upcoming picks. Form from all finished matches
in the ledger (matches without a kickoff time are excluded from form windows).</p>
<span class="js-only"><input class="filter" type="search" id="teamsearch"
       placeholder="search teams…" aria-label="Search teams">
<button class="filter" id="texpand" type="button">Expand all</button>
<button class="filter" id="tcollapse" type="button">Collapse all</button></span>
<p class="summaryline">{len(data['teams'])} teams · click a card for match-by-match detail</p>
<div>{t_cards}</div>""",
        gen_ts, t, summary, "teams", gen_ts_short, data)

    # ---- debate ----
    pages["debate.html"] = _page_shell(
        "Debate",
        f"""<h2>Debates — top {len(data['discussion'])} most-discussed upcoming matches</h2>
<p class="hint">Round-1 positions and round-2 cross-examination from the
append-only discussion table. Where no round-1 statement was recorded, the
agent's locked pick is shown as its position.</p>
<div id="debate" hidden></div>
<div id="debate-static">{d_cards}</div>""",
        gen_ts, t, summary, "debate", gen_ts_short, data)

    # ---- calibration ----
    pages["calibration.html"] = _page_shell(
        "Calibration",
        f"""<h2>Calibration — graded picks by confidence bucket</h2>
<p class="hint">Actual win rate vs bucket mean confidence. Buckets with fewer
than {m.get('min_bucket_n', 5)} graded picks are marked insufficient data.</p>
<div id="calib" hidden></div>
<div id="calib-static">{c_static}</div>""",
        gen_ts, t, summary, "calibration", gen_ts_short, data)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    for fname, html in pages.items():
        (OUT.parent / fname).write_text(html)
        print(f"wrote {OUT.parent / fname} ({len(html):,} bytes)")
    print(f"totals: {m['upcoming_rows']} upcoming pick rows, "
          f"{m['pending_matches']} pending matches / {m['pending_picks']} pending picks, "
          f"{m['results_rows']} results, {m['teams']} team profiles, "
          f"{m['debate_matches']} debates ({m['debate_statements']} statements, "
          f"{m['round1_statements']} round-1), {len(data['calibration'])} calibration agents")


if __name__ == "__main__":
    main()