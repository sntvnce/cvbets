"""SQLite data layer for cvbets (Phase 1 sportsbook core).

HARD RULE: picks are APPEND-ONLY. After insert, the only mutation ever
allowed is grade.py setting result/brier once per pick. No other code path
may UPDATE or DELETE a pick.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "ledger.db"
SCHEMA_PATH = ROOT / "schema.sql"


def log(msg):
    """Timestamped log line (UTC)."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}")


def connect(db_path=None):
    conn = sqlite3.connect(str(db_path or DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(db_path=None):
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
    return conn


def insert_pick(conn, match_id, agent, selection, confidence, reasoning=None):
    """Append-only pick insert. Refuses a second pick by the same agent for
    the same match (enforced here AND by callers checking eligibility).

    Returns True when a new pick row was written, False when one already
    existed for (match_id, agent).
    """
    cur = conn.execute(
        "INSERT INTO picks (match_id, agent, selection, confidence, reasoning) "
        "SELECT ?, ?, ?, ?, ? "
        "WHERE NOT EXISTS (SELECT 1 FROM picks WHERE match_id = ? AND agent = ?)",
        (match_id, agent, selection, confidence, reasoning, match_id, agent),
    )
    conn.commit()
    return cur.rowcount == 1