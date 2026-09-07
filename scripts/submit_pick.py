#!/usr/bin/env python3
"""Expert pick submission gate (Phase 2 doorway) — the ONLY way agents add picks.

Strict JSON contract (AGENTS.md):
  {
    "agent": "chief",            // required, non-empty string
    "match_id": 1658040,         // required, must be an upcoming match
    "selection": "yologang",     // required, exact team1/team2 name
    "confidence": 0.62,          // required, P(selection wins), 0.5 <= c <= 1.0
    "reasoning": "..."           // required, <= 100 words
  }

Usage:
  python scripts/submit_pick.py pick.json        # from a file
  cat pick.json | python scripts/submit_pick.py -  # from stdin
  python scripts/submit_pick.py pick.json --db /tmp/test.db   # throwaway db for tests

Rejections (exit 1, reason printed): malformed JSON, unknown/extra keys, match not
found or no longer not_started, selection not one of the two named teams,
confidence outside [0.5, 1.0], reasoning missing or > 100 words, or the agent
already has a pick for the match (append-only: one pick per agent per match,
inserted via db.insert_pick's guarded INSERT). Every decision is logged with a
timestamp.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db import connect, init_db, insert_pick, log  # noqa: E402

REQUIRED_KEYS = {"agent", "match_id", "selection", "confidence", "reasoning"}
MAX_WORDS = 100


def fail(reasons):
    for r in reasons:
        log(f"submit: REJECTED — {r}")
    print(f"REJECTED ({len(reasons)} problem(s)):")
    for r in reasons:
        print(f"  - {r}")
    return 1


def validate(conn, pick):
    """Return (None) if valid, else a list of rejection reasons."""
    errors = []

    if not isinstance(pick, dict):
        return ["payload is not a JSON object"]
    keys = set(pick)
    missing = REQUIRED_KEYS - keys
    extra = keys - REQUIRED_KEYS
    if missing:
        errors.append(f"missing key(s): {sorted(missing)}")
    if extra:
        errors.append(f"unknown key(s): {sorted(extra)} — contract is exactly "
                      f"{sorted(REQUIRED_KEYS)}")
    if missing or extra:
        return errors

    agent = pick["agent"]
    if not isinstance(agent, str) or not agent.strip():
        errors.append("agent must be a non-empty string")

    match_id = pick["match_id"]
    if not isinstance(match_id, int) or isinstance(match_id, bool):
        errors.append(f"match_id must be an integer, got {type(match_id).__name__}")
        return errors

    row = conn.execute(
        "SELECT team1, team2, status FROM matches WHERE match_id = ?", (match_id,)
    ).fetchone()
    if row is None:
        errors.append(f"match_id {match_id} not found in matches table")
        return errors
    if row["status"] != "not_started":
        errors.append(f"match {match_id} is '{row['status']}', not 'not_started' — "
                      f"picks lock before the match begins")

    selection = pick["selection"]
    if not isinstance(selection, str) or not selection.strip():
        errors.append("selection must be a non-empty team name")
    elif selection not in (row["team1"], row["team2"]):
        errors.append(f"selection '{selection}' is not one of the opponents "
                      f"('{row['team1']}' vs '{row['team2']}')")

    confidence = pick["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        errors.append(f"confidence must be a number, got {type(confidence).__name__}")
    elif not 0.5 <= confidence <= 1.0:
        errors.append(f"confidence {confidence} outside [0.5, 1.0] "
                      f"(P(selection wins); if you favor the other team, pick them instead)")

    reasoning = pick["reasoning"]
    if not isinstance(reasoning, str) or not reasoning.strip():
        errors.append("reasoning must be a non-empty string")
    else:
        words = len(reasoning.split())
        if words > MAX_WORDS:
            errors.append(f"reasoning is {words} words — contract max is {MAX_WORDS}")

    if not errors:
        existing = conn.execute(
            "SELECT id FROM picks WHERE match_id = ? AND agent = ?",
            (match_id, agent),
        ).fetchone()
        if existing:
            errors.append(f"agent '{agent}' already has pick id {existing['id']} for "
                          f"match {match_id} — picks are append-only, one per agent per match")
    return errors


def main(argv):
    if len(argv) < 2 or len(argv) > 4:
        print(__doc__)
        return 2
    source, db_path = argv[1], None
    if len(argv) == 4 and argv[2] == "--db":
        db_path = argv[3]

    if source == "-":
        raw = sys.stdin.read()
    else:
        path = Path(source)
        if not path.exists():
            log(f"submit: REJECTED — file not found: {path}")
            print(f"file not found: {path}")
            return 1
        raw = path.read_text()

    try:
        pick = json.loads(raw)
    except json.JSONDecodeError as e:
        log(f"submit: REJECTED — invalid JSON: {e}")
        print(f"invalid JSON: {e}")
        return 1

    if db_path:
        conn = init_db(db_path)
    else:
        conn = init_db()

    errors = validate(conn, pick)
    if errors:
        return fail(errors)

    agent, match_id = pick["agent"].strip(), pick["match_id"]
    inserted = insert_pick(
        conn, match_id, agent, pick["selection"],
        float(pick["confidence"]), pick["reasoning"].strip(),
    )
    if not inserted:  # race-safe double check
        return fail([f"pick for (agent={agent}, match_id={match_id}) already exists"])

    pid = conn.execute(
        "SELECT id FROM picks WHERE match_id = ? AND agent = ?", (match_id, agent)
    ).fetchone()["id"]
    conn.close()
    log(f"submit: ACCEPTED pick id {pid} — agent={agent} match={match_id} "
        f"selection={pick['selection']} confidence={pick['confidence']}")
    print(f"ACCEPTED: pick id {pid} — {agent} picks {pick['selection']} "
          f"@ {pick['confidence']} for match {match_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))