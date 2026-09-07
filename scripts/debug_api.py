"""Debug: inspect raw PandaScore match shapes without leaking the token."""
import json
import sys
from collections import Counter
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ingest import get_token

token = get_token()
client = httpx.Client(headers={"Authorization": f"Bearer {token}",
                               "Accept": "application/json"}, timeout=30.0)

for label, path in [("upcoming", "/csgo/matches/upcoming"), ("past", "/csgo/matches/past")]:
    rows = client.get("https://api.pandascore.co" + path,
                      params={"per_page": 3, "page": 1, "sort": "begin_at"}).json()
    print(f"--- {label}: {len(rows)} sample matches ---")
    for m in rows:
        print(f"keys: {sorted(m.keys())}")
        print(json.dumps({
            "id": m.get("id"),
            "status": m.get("status"),
            "begin_at": m.get("begin_at"),
            "name": m.get("name"),
            "number_of_games": m.get("number_of_games"),
            "opponents": m.get("opponents"),
            "opponent1": m.get("opponent1"),
            "opponent2": m.get("opponent2"),
            "winner": m.get("winner"),
            "results": m.get("results"),
            "games": (m.get("games") or [])[:1],
        }, indent=1, default=str)[:2500])
    print()

# status distribution over a full upcoming page
rows = client.get("https://api.pandascore.co/csgo/matches/upcoming",
                  params={"per_page": 100, "page": 1}).json()
print("upcoming status counts:", Counter(m.get("status") for m in rows))
print("upcoming opponents counts:", Counter(len(m.get("opponents") or []) for m in rows))
opp_shape = Counter(
    json.dumps({"id": (m.get("opponents") or [{}])[0].get("id") if m.get("opponents") else None,
                "type": type((m.get("opponents") or [None])[0]).__name__ if m.get("opponents") else None})
    for m in rows[:20])
print("opponent[0] shape sample:", dict(opp_shape))