# CVBets — project rules (apply to every agent session in this folder)

- Simulated esports betting only. Never real money, never real wagers.
- Scope: CS2, match-winner market, flat 1-unit stakes until told otherwise.
- Agents write to data/ledger.db ONLY via scripts/ — never direct DB writes.
- Expert output contract: strict JSON — match_id, selection, confidence (0-1), reasoning (<=100 words).
- Log everything with timestamps.
- Ask before installing new packages or editing anything outside this folder.

## Sign-off rule (Vince & Calvin, set 2026-09-11)

BOTH Vince AND Calvin must approve before any of these land in the repo:

- New agents/experts or changes to an expert's decision logic, prompt, or thresholds.
- Changes to betting rules (stakes, sizing, bet selection, EV thresholds).
- Changes to grading/scoring, the picks/bets schema, or AGENTS.md itself.
- Adding an external service or API (e.g. odds feeds) or sharing any credential.
- Pushing to `main` anything other than: nightly loop output (ledger.db, site/, logs),
  scheduled-run commits, or doc/spec DRAFTS clearly marked DRAFT.

Everything else (bug fixes that restore existing behavior, cron/infra repair,
read-only analysis) may proceed without sign-off — announce it in Discord after.
When in doubt, ask. A draft spec may be WRITTEN without sign-off; it may not be
IMPLEMENTED (wired into daily.py, workflows, or agents) without both signatures.
