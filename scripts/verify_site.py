#!/usr/bin/env python3
"""Structural verification of the multi-page site (site/*.html) vs ledger.db.

Checks per page: well-formed structure, static row counts vs fresh DB queries,
embedded JSON payload integrity (on pages that embed it), team-profile math
spot-checks, cross-page nav links. Read-only.
"""
import html.parser
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from db import connect  # noqa: E402

SITE = ROOT / "site"
PAGES = ["index.html", "picks.html", "results.html", "teams.html",
         "debate.html", "calibration.html"]
errors, checks = [], []


def ok(name, cond, detail=""):
    checks.append((name, cond, detail))
    if not cond:
        errors.append(f"{name}: {detail}")


class TableParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.bad, self.tables, self.cur = [], [], [], None
        self.in_thead = False

    def handle_starttag(self, tag, attrs):
        if tag in ("table", "thead", "tbody", "tr", "details", "summary",
                   "div", "script", "style"):
            self.stack.append(tag)
        if tag == "table":
            d = dict(attrs)
            self.cur = {"id": d.get("id", ""), "rows": 0}
        if tag == "tr" and self.cur is not None and not self.in_thead:
            self.cur["rows"] += 1
        if tag == "thead":
            self.in_thead = True

    def handle_endtag(self, tag):
        if tag == "thead":
            self.in_thead = False
        if tag in ("table", "thead", "tbody", "tr", "details", "summary",
                   "div", "script", "style"):
            if not self.stack or self.stack[-1] != tag:
                self.bad.append(f"mismatched </{tag}>")
                if self.stack and self.stack[-1] == tag:
                    self.stack.pop()
            else:
                self.stack.pop()
        if tag == "table" and self.cur is not None:
            self.tables.append(self.cur)
            self.cur = None


pages = {}
for fname in PAGES:
    path = SITE / fname
    if not path.exists():
        ok(f"{fname} exists", False, "missing")
        continue
    pages[fname] = path.read_text(encoding="utf-8")
ok("all 6 pages exist", len(pages) == 6, f"found {sorted(pages)}")

# ---- per-page structure ----
parsers = {}
for fname, page in pages.items():
    tp = TableParser()
    tp.feed(page)
    parsers[fname] = tp
    ok(f"{fname} balanced tags", not tp.bad and not tp.stack,
       f"bad={tp.bad[:2]} unclosed={tp.stack[:4]}")
    ok(f"{fname} ends </html>", page.rstrip().endswith("</html>"))
    ok(f"{fname} no external src/href",
       not re.search(r'(?:src|href)="https?://', page))
    ok(f"{fname} nav present", 'class="tabs"' in page)
    ok(f"{fname} title", f"<title>" in page)

# nav links on every page point at the other pages, active tab marked
for fname, page in pages.items():
    active = re.search(r'class="tab active" href="([^"]+)"', page)
    ok(f"{fname} has exactly one active tab",
       active is not None and active.group(1) == fname,
       f"active={active.group(1) if active else None}")
    for other in PAGES:
        ok(f"{fname} links {other}", f'href="{other}"' in page)

# ---- DB ground truth ----
conn = connect()
ref = datetime.now(timezone.utc)


def scalar(q):
    return conn.execute(q).fetchone()[0]


db_standings = scalar(
    "SELECT COUNT(*) FROM (SELECT agent FROM picks WHERE result IS NOT NULL "
    "GROUP BY agent)")
db_upcoming = scalar(
    "SELECT COUNT(*) FROM picks p JOIN matches m ON m.match_id=p.match_id "
    "WHERE p.result IS NULL AND m.status='not_started'")
db_pending_m = scalar(
    "SELECT COUNT(DISTINCT p.match_id) FROM picks p "
    "JOIN matches m ON m.match_id=p.match_id WHERE p.result IS NULL")
db_pending_picks = scalar("SELECT COUNT(*) FROM picks WHERE result IS NULL")
db_results = scalar("SELECT COUNT(*) FROM picks WHERE result IS NOT NULL")
db_matches = scalar("SELECT COUNT(*) FROM matches")
db_live = scalar(
    "SELECT COUNT(DISTINCT p.match_id) FROM picks p "
    "JOIN matches m ON m.match_id=p.match_id "
    "WHERE p.result IS NULL AND m.status='running'")
db_teams = {r[0].strip() for r in conn.execute(
    "SELECT m.team1 FROM picks p JOIN matches m ON m.match_id=p.match_id "
    "WHERE p.result IS NULL AND m.status='not_started' "
    "UNION SELECT m.team2 FROM picks p JOIN matches m ON m.match_id=p.match_id "
    "WHERE p.result IS NULL AND m.status='not_started'") if r[0].strip()}
top5 = conn.execute(
    "SELECT d.match_id, COUNT(*) c FROM discussion d "
    "JOIN matches m ON m.match_id=d.match_id "
    "WHERE m.status='not_started' AND EXISTS (SELECT 1 FROM picks p "
    "  WHERE p.match_id=d.match_id AND p.result IS NULL) "
    "GROUP BY d.match_id ORDER BY c DESC, MIN(m.begin_at), d.match_id LIMIT 5"
).fetchall()
top5_ids = [r[0] for r in top5]
db_debate_stmts = 0
db_debate_r1 = 0
for mid in top5_ids:
    for r in conn.execute(
            "SELECT round, COUNT(*) n FROM discussion WHERE match_id=? "
            "GROUP BY round", (mid,)):
        db_debate_stmts += r[1]
        db_debate_r1 += r[0] == 1

cutoff30 = (ref - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
spot = {}
for team in list(sorted(db_teams))[:3]:
    wins = []
    for r in conn.execute(
            "SELECT begin_at, team1, team2, winner FROM matches "
            "WHERE status='finished' AND begin_at IS NOT NULL "
            "AND (team1=? OR team2=?) ORDER BY begin_at", (team, team)):
        wins.append((r["begin_at"], (r["winner"] or "").strip() == team.strip()))
    l10 = [w for _, w in wins[-10:]]
    d30 = [w for d, w in wins if d >= cutoff30]
    spot[team] = {"l10": f"{sum(l10)}-{len(l10) - sum(l10)}",
                  "d30": (f"{sum(d30) / len(d30):.0%}" if d30 else "no matches"),
                  "all": f"{sum(w for _, w in wins)}-{len(wins) - sum(w for _, w in wins)}"}
conn.close()


def tbody_rows(fname, table_id):
    for t in parsers[fname].tables:
        if t["id"] == table_id:
            return t["rows"]
    return -1


# ---- per-page content checks ----
idx = pages["index.html"]
ok("index: standings rows == agents with graded picks",
   tbody_rows("index.html", "t-standings") == db_standings,
   f"html={tbody_rows('index.html', 't-standings')} db={db_standings}")
ok("index: calibration rows == agents x 4 buckets",
   tbody_rows("index.html", "t-calib") == db_standings * 4)

picks_pg = pages["picks.html"]
ok("picks: upcoming rows == DB", tbody_rows("picks.html", "t-upcoming") == db_upcoming,
   f"html={tbody_rows('picks.html', 't-upcoming')} db={db_upcoming}")
ok("picks: pending rows == distinct matches",
   tbody_rows("picks.html", "t-pending") == db_pending_m,
   f"html={tbody_rows('picks.html', 't-pending')} db={db_pending_m}")
ok("picks: awaiting-results summary line present",
   "matches awaiting results" in picks_pg)

res_pg = pages["results.html"]
ok("results: rows == graded (capped 120)",
   tbody_rows("results.html", "t-results") == min(db_results, 120),
   f"html={tbody_rows('results.html', 't-results')} db={db_results}")

teams_pg = pages["teams.html"]
n_team_cards = teams_pg.count('<details class="tcard"')
ok("teams: cards == distinct upcoming teams", n_team_cards == len(db_teams),
   f"html={n_team_cards} db={len(db_teams)}")
ok("teams: sparklines present", teams_pg.count("<svg") >= n_team_cards * 0.5)

deb_pg = pages["debate.html"]
_dstart = deb_pg.index('id="debate-static"')
_dend = deb_pg.index('<footer', _dstart)
n_debate_cards = deb_pg.count('<div class="dcard">', _dstart, _dend)
ok("debate: cards == top-5", n_debate_cards == min(len(top5_ids), 5),
   f"html={n_debate_cards} top5={len(top5_ids)}")

cal_pg = pages["calibration.html"]
ok("calibration: rows == agents x 4 buckets",
   tbody_rows("calibration.html", "t-calib") == db_standings * 4,
   f"html={tbody_rows('calibration.html', 't-calib')} db={db_standings * 4}")

n_live = picks_pg.count('<span class="badge live">LIVE</span>')
ok("picks: LIVE badges == live pending matches", n_live == db_live,
   f"html={n_live} db={db_live}")

# ---- embedded JSON payload on pages that embed it ----
for fname in ("index.html", "teams.html", "debate.html", "calibration.html"):
    page = pages[fname]
    m = re.search(r"<script>const DATA = (.*?);</script>", page, re.S)
    ok(f"{fname} DATA payload present", m is not None)
    if not m:
        continue
    try:
        data = json.loads(m.group(1))
        ok(f"{fname} payload valid JSON", True)
        if "teams" in data:
            ok(f"{fname} payload teams len", len(data["teams"]) == len(db_teams),
               f"{len(data['teams'])} vs {len(db_teams)}")
        if "discussion" in data:
            ok(f"{fname} payload debate stmts",
               sum(d["n_statements"] for d in data["discussion"]) == db_debate_stmts)
    except json.JSONDecodeError as e:
        ok(f"{fname} payload valid JSON", False, str(e))

# team math spot-check from the teams.html payload
m = re.search(r"<script>const DATA = (.*?);</script>", teams_pg, re.S)
if m:
    data = json.loads(m.group(1))
    for team, exp in spot.items():
        t = data["teams"].get(team)
        if t is None:
            ok(f"team {team!r} in payload", False, "missing")
            continue
        got_l10 = f"{t['last10_wins']}-{t['last10_n'] - t['last10_wins']}"
        got_all = f"{t['all_wins']}-{t['all_played'] - t['all_wins']}"
        got_d30 = (f"{t['d30_wins'] / t['d30_played']:.0%}"
                   if t["d30_played"] else "no matches")
        ok(f"team {team!r} last-10", got_l10 == exp["l10"], f"{got_l10} vs {exp['l10']}")
        ok(f"team {team!r} 30d", got_d30 == exp["d30"], f"{got_d30} vs {exp['d30']}")
        ok(f"team {team!r} all-time", got_all == exp["all"], f"{got_all} vs {exp['all']}")

# ---- no-JS degrade ----
ok("all core tables server-rendered",
   tbody_rows("index.html", "t-standings") > 0
   and tbody_rows("picks.html", "t-upcoming") > 0
   and tbody_rows("picks.html", "t-pending") > 0
   and tbody_rows("results.html", "t-results") > 0)
ok("team cards native <details>", n_team_cards > 0)
ok("debate static fallback present", n_debate_cards > 0)
ok("no external CDN/JS", all(
    not re.search(r'(?:src|href)="https?://', pg) for pg in pages.values()))

tot = sum(len(pg) for pg in pages.values())
print(f"multi-page site: {len(pages)} pages, {tot:,} bytes total")
print(f"DB: {db_matches} matches, {db_results} graded, {db_pending_picks} pending "
      f"on {db_pending_m} matches, {len(db_teams)} upcoming teams")
print()
w = max(len(n) for n, _, _ in checks)
npass = sum(1 for _, c, _ in checks if c)
for name, cond, detail in checks:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"  [{detail}]" if detail and not cond else ""))
print(f"\n{npass}/{len(checks)} checks passed")
sys.exit(0 if npass == len(checks) else 1)