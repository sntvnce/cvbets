"""Debug: why are past matches being skipped? Count statuses/opponent shapes."""
import sys
from collections import Counter
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ingest import _team_of, get_token

token = get_token()
client = httpx.Client(headers={"Authorization": f"Bearer {token}",
                               "Accept": "application/json"}, timeout=30.0)

for label, params in [
    ("past sort=-begin_at p1", {"per_page": 100, "page": 1, "sort": "-begin_at"}),
    ("past default p1", {"per_page": 100, "page": 1}),
]:
    rows = client.get("https://api.pandascore.co/csgo/matches/past", params=params).json()
    statuses = Counter(m.get("status") for m in rows)
    opp_counts = Counter(len([t for t in (_team_of(o) for o in (m.get("opponents") or [])) if t])
                         for m in rows)
    begins = [m.get("begin_at") for m in rows[:3]] + ["..."] + [rows[-1].get("begin_at")]
    print(f"--- {label}: {len(rows)} rows")
    print("  statuses:", dict(statuses))
    print("  named-opponent counts:", dict(opp_counts))
    print("  begin_at first/last:", begins)

# What does a canceled/postponed row look like, and one with missing opponents?
rows = client.get("https://api.pandascore.co/csgo/matches/past",
                  params={"per_page": 100, "page": 1, "sort": "-begin_at"}).json()
for want in ("canceled", "postponed"):
    m = next((m for m in rows if m.get("status") == want), None)
    if m:
        print(f"--- sample {want}: id={m.get('id')} begin_at={m.get('begin_at')} "
              f"opponents={len(m.get('opponents') or [])} name={m.get('name')!r}")