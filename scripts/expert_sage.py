"""Phase 4 LLM expert 'sage' — reads ledger + debate, asks a local LLM to pick.

For each not_started match without an existing 'sage' pick:
  1. Read ledger context: both teams' last-10 form, 30-day win rates,
     all-time H2H, round-1 picks (picks table) and round-2 debate critiques
     (discussion table) for the match.
  2. Compose a compact prompt and ask the local Ollama relay
     (http://127.0.0.1:11434/v1/chat/completions, model glm-5.3-flash:cloud)
     for STRICT JSON: selection / confidence (0.5-1.0) / reasoning (<=100 words).
  3. Parse defensively (first balanced brace block); on any parse failure,
     out-of-range confidence, or a selection that is not one of the two team
     names, fall back to coin-flip team1 @ 0.50 ('sage: LLM output unparseable').
  4. Submit ONLY via experts.submit_via_gate so the JSON gate contract is
     enforced identically to every other agent (append-only, one pick per
     agent per match, not_started only).

Rate limits: one prompt per match, max 40 matches per run, 1s sleep between
calls. If the LLM endpoint is unreachable (e.g. the nightly GitHub Actions
runner, which has no Ollama), run() prints
  'sage skipped: LLM endpoint unreachable'
and returns cleanly — sage must NEVER break daily.py.
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from db import init_db, log  # noqa: E402
from experts import h2h_record, last_n_form, submit_via_gate  # noqa: E402

AGENT = "sage"
MODEL = "glm-5.3-flash:cloud"
ENDPOINT = "http://127.0.0.1:11434/v1/chat/completions"
MAX_MATCHES = 40          # hard cap per run
SLEEP_S = 1.0             # between LLM calls
PROBE_TIMEOUT_S = 10
LLM_TIMEOUT_S = 120
MAX_TOKENS = 3000         # glm-5.3-flash is a reasoner: it burns tokens thinking
TEMPERATURE = 0.2
FORM_N = 10               # last-10 form window
MAX_PICK_CLIP = 160       # chars of each round-1 reasoning kept in the prompt
MAX_CRITIQUES = 8         # round-2 critiques kept in the prompt
FALLBACK_REASON = "sage: LLM output unparseable"
SKIP_MSG = "sage skipped: LLM endpoint unreachable"


# ---------------------------------------------------------------- LLM access

def _post(payload, timeout):
    """POST to the OpenAI-compatible endpoint; return parsed JSON or None."""
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:  # URLError, timeout, refused, bad JSON — all fatal-soft
        log(f"sage: LLM request failed: {type(e).__name__}: {e}")
        return None


def llm_reachable(timeout=PROBE_TIMEOUT_S):
    """Cheap probe: one tiny completion. Content may be empty (reasoner still
    'thinking') — any well-formed choices[] response proves the endpoint works."""
    data = _post(
        {"model": MODEL,
         "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
         "max_tokens": 16},
        timeout,
    )
    return isinstance(data, dict) and "choices" in data


def ask_llm(messages, timeout=LLM_TIMEOUT_S):
    """Send chat messages, return assistant content string, or None on failure."""
    data = _post(
        {"model": MODEL, "messages": messages,
         "temperature": TEMPERATURE, "max_tokens": MAX_TOKENS},
        timeout,
    )
    if not isinstance(data, dict):
        return None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        log("sage: LLM response missing choices[0].message.content")
        return None
    if not isinstance(content, str) or not content.strip():
        log("sage: LLM returned empty content")
        return None
    return content


# -------------------------------------------------------------- prompt build

def _clip(text, n):
    text = (text or "").strip().replace("\n", " ")
    return text[: n - 1] + "…" if len(text) > n else text


def build_messages(conn, match, rates=None):
    """Compact system+user prompt with form / 30d rates / H2H / round-1 picks /
    round-2 critiques. Team names are shown QUOTED so the model copies them
    character-for-character (some stored names have leading spaces)."""
    t1, t2 = match["team1"], match["team2"]
    if rates is None:
        import chalkbot  # repo-root module; imported lazily to keep import cheap
        rates = chalkbot.win_rates(conn)

    def form_line(team):
        r, n = last_n_form(conn, team, FORM_N)
        if n == 0 or r is None:
            return f'- "{team}" last-{FORM_N} form: (no finished matches in ledger)'
        w, l = round(r * n), n - round(r * n)
        return f'- "{team}" last-{FORM_N} form: {w}W-{l}L ({r:.0%})'

    def rate_line(team):
        r, n = rates.get(team, (None, 0))
        if not n or r is None:
            return f'"{team}" 30-day win rate: (no finished matches in last 30 days)'
        w, l = round(r * n), n - round(r * n)
        return f'"{team}" 30-day win rate: {r:.0%} ({w}-{l}, n={n})'

    w1, w2, n = h2h_record(conn, t1, t2)
    h2h_line = (f'- Head-to-head: "{t1}" {w1}-{w2} "{t2}" across {n} meeting(s)'
                if n else '- Head-to-head: no prior meetings in ledger')

    r1_lines = [
        f'- {p["agent"]}: "{p["selection"]}" @ {p["confidence"]:.2f} — {_clip(p["reasoning"], MAX_PICK_CLIP)}'
        for p in conn.execute(
            "SELECT agent, selection, confidence, reasoning FROM picks "
            "WHERE match_id = ? ORDER BY id", (match["match_id"],)
        ).fetchall()
    ] or ["(none recorded)"]

    crit_lines = [
        f'- [{d["agent"]}] {_clip(d["statement"], 240)}'
        for d in conn.execute(
            "SELECT agent, statement FROM discussion "
            "WHERE match_id = ? AND round >= 2 ORDER BY id LIMIT ?",
            (match["match_id"], MAX_CRITIQUES),
        ).fetchall()
    ] or ["(no debate recorded)"]

    fmt = match["format"] or "?"
    system = (
        'You are "sage", an expert CS2 analyst agent in a simulated match-winner '
        "prediction league. Using the ledger facts and the debate below, decide the "
        "winner. Respond with STRICT JSON only — no markdown fences, no prose outside "
        "the JSON — with exactly these three fields:\n"
        '{"selection": "<team name>", "confidence": <float>, "reasoning": "<string>"}\n'
        "Rules: selection must be copied CHARACTER-FOR-CHARACTER from one of the two "
        "team names (including any leading/trailing spaces); confidence is your "
        "probability that the selection wins, a float between 0.5 and 1.0; reasoning "
        "is at most 100 words."
    )
    user = (
        f'CS2 match {match["match_id"]} ({fmt}): "{t1}" vs "{t2}" — '
        f'begins {match["begin_at"]}.\n\n'
        "Ledger facts (from our own results database):\n"
        f"{form_line(t1)}\n{form_line(t2)}\n"
        f"- 30-day win rates: {rate_line(t1)} | {rate_line(t2)}\n"
        f"{h2h_line}\n\n"
        "Round-1 picks (other agents, already locked):\n" + "\n".join(r1_lines) + "\n\n"
        "Round-2 debate critiques (from the discussion table):\n" + "\n".join(crit_lines)
        + "\n\nTask: pick the winner of this match. Reply with STRICT JSON only."
    )
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


# ------------------------------------------------------------ parse + pick

def _first_brace_block(text):
    """Return the first balanced {...} block in text (string-aware), else None.
    Tolerates code fences and prose around the JSON."""
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start: i + 1]
        start = text.find("{", start + 1)  # unbalanced — try the next brace
    return None


def _canonical_team(name, team1, team2):
    """Exact match first; else map whitespace/case variants onto the stored
    name (the gate requires an exact stored team name)."""
    if not isinstance(name, str):
        return None
    if name == team1:
        return team1
    if name == team2:
        return team2
    for cand in (team1, team2):
        if name.strip().casefold() == cand.strip().casefold():
            return cand
    return None


def parse_llm_pick(raw, team1, team2):
    """Defensive parse of the LLM reply -> (selection, confidence, reasoning)
    or None on ANY contract violation."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    block = _first_brace_block(raw)
    if block is None:
        return None
    try:
        obj = json.loads(block)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    sel = _canonical_team(obj.get("selection"), team1, team2)
    if sel is None:
        return None

    conf = obj.get("confidence")
    if isinstance(conf, bool) or not isinstance(conf, (int, float)):
        return None
    conf = float(conf)
    if not 0.5 <= conf <= 1.0:
        return None

    why = obj.get("reasoning")
    if not isinstance(why, str) or not why.strip():
        return None
    words = why.split()
    if len(words) > 100:
        why = " ".join(words[:100])
    return sel, round(conf, 4), why.strip()


def pick_sage(conn, match, rates=None, ask=None):
    """Full pipeline for one match: prompt -> LLM -> parse (or fallback).
    Returns the pick dict plus the prompt/raw response for auditing."""
    ask = ask or ask_llm
    messages = build_messages(conn, match, rates=rates)
    raw = ask(messages)
    parsed = parse_llm_pick(raw, match["team1"], match["team2"]) if raw else None
    if parsed:
        sel, conf, why = parsed
        reasoning = " ".join(f"sage: {why}".split())
        reasoning = " ".join(reasoning.split()[:100])  # 'sage:' costs a word
        fallback = False
    else:
        sel, conf, reasoning = match["team1"], 0.50, FALLBACK_REASON
        fallback = True
    return {"selection": sel, "confidence": conf, "reasoning": reasoning,
            "messages": messages, "raw_response": raw, "fallback": fallback}


# --------------------------------------------------------------------- run

def run(conn=None, db_path=None, probe=None, ask=None, max_matches=MAX_MATCHES):
    """Pick every eligible not_started match (max max_matches) via the gate.
    Returns {'made', 'eligible'} — or {'skipped': True, ...} when the LLM
    endpoint is unreachable (never raises: sage must not break daily.py)."""
    probe = probe or llm_reachable
    if not probe():
        print(SKIP_MSG)
        log(SKIP_MSG)
        return {"made": 0, "eligible": 0, "skipped": True}
    log(f"sage: LLM endpoint reachable ({MODEL})")

    own = conn or init_db(db_path)
    try:
        import chalkbot
        rates = chalkbot.win_rates(own)
        rows = own.execute(
            "SELECT match_id, team1, team2, format, begin_at FROM matches "
            "WHERE status = 'not_started' AND team1 IS NOT NULL AND team2 IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM picks p WHERE p.match_id = matches.match_id "
            "AND p.agent = ?) ORDER BY begin_at LIMIT ?",
            (AGENT, max_matches),
        ).fetchall()
        made = 0
        for i, r in enumerate(rows):
            pick = None
            try:
                pick = pick_sage(own, r, rates=rates, ask=ask)
            except Exception as e:  # one bad match must never kill the run
                log(f"sage: error on match {r['match_id']}: {type(e).__name__}: {e}")
            if pick:
                ok = submit_via_gate(AGENT, r["match_id"], pick["selection"],
                                     pick["confidence"], pick["reasoning"], db_path)
                if ok:
                    made += 1
                    tag = " (fallback)" if pick["fallback"] else ""
                    log(f"sage: pick match {r['match_id']} -> {pick['selection']} "
                        f"@ {pick['confidence']:.2f}{tag}")
            if i < len(rows) - 1:
                time.sleep(SLEEP_S)
        log(f"sage: {made} picks submitted via gate ({len(rows)} eligible)")
        return {"made": made, "eligible": len(rows)}
    finally:
        if conn is None:
            own.close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="sage LLM expert (default: max 40 matches)")
    ap.add_argument("--limit", type=int, default=MAX_MATCHES,
                    help=f"max matches this run (default {MAX_MATCHES})")
    args = ap.parse_args()
    try:
        run(max_matches=args.limit)
    except Exception as e:  # even a crash must not fail the nightly loop
        log(f"sage: fatal error: {type(e).__name__}: {e}")
    sys.exit(0)