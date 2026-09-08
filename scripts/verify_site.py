#!/usr/bin/env python3
"""Structural verification of site/index.html against data/ledger.db.

Checks: well-formed structure (balanced tables, no truncation), static row
counts per section vs fresh DB queries, embedded JSON payload integrity, and
a spot-check of team-profile math. Read-only.
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

SITE = ROOT / "site" / "index.html"
page = SITE.read_text(encoding="utf-8")
errors, checks = [], []


def ok(name, cond, detail=""):
    checks.append((name, cond, detail))
    if not cond:
        errors.append(f"{name}: {detail}")


# ---- 1. well-formed / not truncated --------------------------------------
ok("ends with </html>", page.rstrip().endswith("</html>"), page[-60:])
ok("single <html>", page.count("<html") == 1)
ok("single <body>", page.count("<body") == 1)
ok("no external URLs in src/href",
   not re.search(r'(?:src|href)="https?://', page), "external dependency found")


class TableParser(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.bad = []
        self.tables = []          # list of (table_id, n_body_rows, n_cells_first_row)
        self.cur = None
        self.in_thead = False

    def handle_starttag(self, tag, attrs):
        if tag in ("table", "thead", "tbody", "tr", "td", "th", "details",
                   "summary", "div", "script", "style"):
            if tag not in ("td", "th", "tr") or True:
                pass
        if tag in ("table", "thead", "tbody", "tr", "details", "summary",
                   "div", "script", "style"):
            self.stack.append(tag)
        if tag == "table":
            d = dict(attrs)
            self.cur = {"id": d.get("id", ""), "rows": 0, "cols": None}
        if tag == "tr" and self.cur is not None and not self.in_thead:
            self.cur["rows"] += 1
            if self.cur["cols"] is None:
                self.cur["cols"] = 0
                self._counting = True
        if tag in ("td", "th") and self.cur is not None and self.cur["rows"] == 1 \
                and not self.in_thead:
            self.cur["cols"] += 1
        if tag == "thead":
            self.in_thead = True

    def handle_endtag(self, tag):
        if tag == "thead":
            self.in_thead = False
        if tag in ("table", "thead", "tbody", "tr", "details", "summary",
                   "div", "script", "style"):
            if not self.stack or self.stack[-1] != tag:
                self.bad.append(f"mismatched </{tag}> (stack top: "
                                f"{self.stack[-1] if self.stack else None})")
                if self.stack and self.stack[-1] == tag:
                    self.stack.pop()
            else:
                self.stack.pop()
        if tag == "table" and self.cur is not None:
            self.tables.append(self.cur)
            self.cur = None


tp = TableParser()
tp.feed(page)
ok("balanced tags (table/thead/tbody/tr/details/summary/div/script/style)",
   not tp.bad and not tp.stack, f"bad={tp.bad[:3]} unclosed={tp.stack[:5]}")

# ---- 2. row counts vs DB --------------------------------------------------
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
db_gr_total = scalar("SELECT COUNT(*) FROM picks")
db_matches = scalar("SELECT COUNT(*) FROM matches")
db_live = scalar(
    "SELECT COUNT(DISTINCT p.match_id) FROM picks p "
    "JOIN matches m ON m.match_id=p.match_id "
    "WHERE p.result IS NULL AND m.status='running'")

# teams appearing in upcoming picks (trimmed)
db_teams = {r[0].strip() for r in conn.execute(
    "SELECT m.team1 FROM picks p JOIN matches m ON m.match_id=p.match_id "
    "WHERE p.result IS NULL AND m.status='not_started' "
    "UNION SELECT m.team2 FROM picks p JOIN matches m ON m.match_id=p.match_id "
    "WHERE p.result IS NULL AND m.status='not_started'") if r[0].strip()}

# debate statements for the top-5 most-discussed upcoming matches
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

# spot-check team form for 3 sample teams (last-10 / 30d / all-time)
cutoff30 = (ref - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
spot = {}
for team in list(sorted(db_teams))[:3]:
    wins = []
    for r in conn.execute(
            "SELECT begin_at, team1, team2, winner FROM matches "
            "WHERE status='finished' AND begin_at IS NOT NULL "
            "AND (team1=? OR team2=?) ORDER BY begin_at",
            (team, team)):
        wins.append((r["begin_at"], (r["winner"] or "").strip() == team.strip()))
    l10 = [w for _, w in wins[-10:]]
    d30 = [w for d, w in wins if d >= cutoff30]
    spot[team] = {"l10": f"{sum(l10)}-{len(l10) - sum(l10)}",
                  "d30": (f"{sum(d30) / len(d30):.0%}" if d30 else "no matches"),
                  "all": f"{sum(w for _, w in wins)}-{len(wins) - sum(w for _, w in wins)}"}
conn.close()

# count rows in the generated HTML per section
def tbody_rows(table_id):
    for t in tp.tables:
        if t["id"] == table_id:
            return t["rows"]
    return -1


n_team_cards = page.count('<details class="tcard"')
# count debate cards only in the static fallback (the embedded JS source also
# contains the literal '<div class="dcard">' inside renderDebate)
_dstart = page.index('id="debate-static"')
_dend = page.index('<h2>Calibration', _dstart)
n_debate_cards = page.count('<div class="dcard">', _dstart, _dend)
n_calib_rows = tbody_rows("t-calib")
n_live_badges = page.count('<span class="badge live">LIVE</span>')

ok("standings rows == DB", tbody_rows("t-standings") == db_standings,
   f"html={tbody_rows('t-standings')} db={db_standings}")
ok("upcoming rows == DB", tbody_rows("t-upcoming") == db_upcoming,
   f"html={tbody_rows('t-upcoming')} db={db_upcoming}")
ok("pending rows == distinct matches", tbody_rows("t-pending") == db_pending_m,
   f"html={tbody_rows('t-pending')} db={db_pending_m}")
ok("results rows == graded (capped)",
   tbody_rows("t-results") == min(db_results, 120),
   f"html={tbody_rows('t-results')} db={db_results}")
ok("team cards == distinct upcoming teams", n_team_cards == len(db_teams),
   f"html={n_team_cards} db={len(db_teams)}")
ok("debate cards == 5", n_debate_cards == min(len(top5_ids), 5),
   f"html={n_debate_cards} top5={len(top5_ids)}")
ok("calibration rows == agents x 4 buckets",
   n_calib_rows == db_standings * 4, f"html={n_calib_rows} db={db_standings * 4}")
ok("LIVE badges == live pending matches", n_live_badges == db_live,
   f"html={n_live_badges} db={db_live}")

# ---- 3. embedded JSON payload --------------------------------------------
m = re.search(r"<script>const DATA = (.*?);</script>", page, re.S)
ok("DATA payload present", m is not None)
if m:
    try:
        data = json.loads(m.group(1))
        ok("payload is valid JSON", True)
        ok("payload matches len", len(data["matches"]) == db_pending_m,
           f"{len(data['matches'])} vs {db_pending_m}")
        ok("payload picks len", len(data["picks"]) == db_pending_picks,
           f"{len(data['picks'])} vs {db_pending_picks}")
        ok("payload teams len", len(data["teams"]) == len(db_teams),
           f"{len(data['teams'])} vs {len(db_teams)}")
        ok("payload discussion len", len(data["discussion"]) == len(top5_ids))
        ok("payload debate statements",
           sum(d["n_statements"] for d in data["discussion"]) == db_debate_stmts,
           f"{sum(d['n_statements'] for d in data['discussion'])} vs {db_debate_stmts}")
        ok("payload round-1 count",
           sum(d["n_round1"] for d in data["discussion"]) == db_debate_r1)
        cal_n = sum(sum(b["n"] for b in bs) for bs in data["calibration"].values())
        ok("calibration bucket n sums to graded", cal_n == db_results,
           f"{cal_n} vs {db_results}")
        # spot-check team math inside the payload
        for team, exp in spot.items():
            t = data["teams"].get(team)
            if t is None:
                ok(f"team {team!r} in payload", False, "missing")
                continue
            got_l10 = f"{t['last10_wins']}-{t['last10_n'] - t['last10_wins']}"
            got_all = f"{t['all_wins']}-{t['all_played'] - t['all_wins']}"
            got_d30 = (f"{t['d30_wins'] / t['d30_played']:.0%}"
                       if t["d30_played"] else "no matches")
            ok(f"team {team!r} last-10", got_l10 == exp["l10"],
               f"{got_l10} vs {exp['l10']}")
            ok(f"team {team!r} 30d", got_d30 == exp["d30"],
               f"{got_d30} vs {exp['d30']}")
            ok(f"team {team!r} all-time", got_all == exp["all"],
               f"{got_all} vs {exp['all']}")
    except json.JSONDecodeError as e:
        ok("payload is valid JSON", False, str(e))

# ---- 4. no-JS degrade -----------------------------------------------------
ok("core tables present for no-JS",
   all(tbody_rows(i) > 0 for i in
       ("t-standings", "t-upcoming", "t-pending", "t-results")),
   "a core table is empty server-side")
ok("team cards are native <details> (work w/o JS)", n_team_cards > 0)
ok("debate static fallback present", 'id="debate-static"' in page and
   n_debate_cards > 0)
ok("calibration static fallback present", 'id="calib-static"' in page and
   n_calib_rows > 0)
ok("JS-only controls hidden by default", "js-only" in page)
ok("sparklines present", page.count("<svg") >= n_team_cards * 0.5,
   f"svg count={page.count('<svg')}")

# no-JS sanity: strip scripts, confirm content survives
nojs = re.sub(r"<script.*?</script>", "", page, flags=re.S)
ok("readable without JS (team + debate content inline)",
   "Team profiles" in nojs and "Debates" in nojs and n_debate_cards > 0)

print(f"site/index.html: {len(page):,} bytes, {len(tp.tables)} tables")
print(f"DB: {db_matches} matches, {db_gr_total} picks ({db_results} graded, "
      f"{db_pending_picks} pending on {db_pending_m} matches, {db_live} live), "
      f"{len(db_teams)} upcoming teams, {db_debate_stmts} debate statements "
      f"in top-{len(top5_ids)}")
print()
w = max(len(n) for n, _, _ in checks)
npass = sum(1 for _, c, _ in checks if c)
for name, cond, detail in checks:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"  [{detail}]" if detail and not cond else ""))
print(f"\n{npass}/{len(checks)} checks passed")
sys.exit(0 if not errors else 1)