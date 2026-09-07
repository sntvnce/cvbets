CREATE TABLE IF NOT EXISTS matches (
  match_id INTEGER PRIMARY KEY,        -- PandaScore id
  begin_at TEXT,                       -- ISO8601 UTC
  league TEXT, serie TEXT, tournament TEXT,
  team1 TEXT, team2 TEXT,
  team1_id INTEGER, team2_id INTEGER,
  format TEXT,                         -- 'bo1'/'bo3'/'bo5'
  status TEXT,                         -- not_started/running/finished/canceled
  winner TEXT,                         -- team name, NULL until finished
  score TEXT,                          -- e.g. '2-1'
  ingested_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS picks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id INTEGER NOT NULL REFERENCES matches(match_id),
  agent TEXT NOT NULL,                 -- 'chalkbot', later 'chief', experts
  selection TEXT NOT NULL,             -- team name picked to win
  confidence REAL NOT NULL,            -- prob the selection wins, 0.5-1.0
  reasoning TEXT,
  locked_at TEXT NOT NULL DEFAULT (datetime('now')),
  result INTEGER,                      -- NULL pending, 1 win, 0 loss
  brier REAL
);
CREATE TABLE IF NOT EXISTS bets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pick_id INTEGER REFERENCES picks(id),
  match_id INTEGER REFERENCES matches(match_id),
  agent TEXT NOT NULL, selection TEXT NOT NULL,
  odds REAL,                           -- NULL until we have an odds source
  stake REAL DEFAULT 1.0,
  result INTEGER, pnl REAL
);
CREATE TABLE IF NOT EXISTS agent_stats (
  agent TEXT PRIMARY KEY,
  n_picks INTEGER DEFAULT 0, wins INTEGER DEFAULT 0,
  brier_mean REAL, roi REAL,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS discussion (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id INTEGER NOT NULL REFERENCES matches(match_id),
  agent TEXT NOT NULL,                 -- who is speaking
  round INTEGER NOT NULL DEFAULT 1,    -- 1 = state pick, 2 = critique others
  statement TEXT NOT NULL,             -- the argument itself
  created_at TEXT DEFAULT (datetime('now'))
);