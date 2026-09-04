# CVBets — project rules (apply to every agent session in this folder)

- Simulated esports betting only. Never real money, never real wagers.
- Scope: CS2, match-winner market, flat 1-unit stakes until told otherwise.
- Agents write to data/ledger.db ONLY via scripts/ — never direct DB writes.
- Expert output contract: strict JSON — match_id, selection, confidence (0-1), reasoning (<=100 words).
- Log everything with timestamps.
- Ask before installing new packages or editing anything outside this folder.
