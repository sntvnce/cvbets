# Upset-Hunter Expert — Draft Spec v1 (2026-09-11)

Status: DRAFT for Vince & Calvin review. Not implemented. Nothing here pushes
real-money anything (AGENTS.md rules apply: simulated only, flat 1-unit stakes).

## Purpose

Bet the side our model likes more than the market does — usually the underdog —
when the payout more than compensates for the risk. Accuracy alone doesn't win;
expected value (EV) does.

## The math (computed in Python, never by an LLM)

- Decimal odds `d` (e.g. 2.60) -> implied probability `1/d` (2.60 -> 38.5%).
  Bookmakers bake in a margin (overround), so normalize:
  `p_market = (1/d_i) / (1/d_t1 + 1/d_t2)`.
- Our probability `p_us` = consensus of graded experts (start: simple average of
  available picks for the match; later: Brier-weighted by agent_stats).
- EV of a 1-unit bet on a team = `p_us * d - 1`. Positive EV = bet candidate.
- Edge = `p_us - p_market`. Only bet when BOTH: `EV > 0` and `edge >= THRESHOLD`.

## Decision rule (v1)

- Candidate = team with max EV among the two sides.
- Bet only if `EV >= +5%` AND `edge >= 5pts` (tune after ~100 simulated bets).
- Otherwise: no bet for the match (missing odds -> no bet; never invent odds).
- Stake: flat 1 unit (AGENTS.md). Kelly sizing is a later upgrade.

## Data plumbing

1. **Odds ingest** (`scripts/ingest_odds.py`): OddsPapi free tier, CS2 sportId=17,
   Pinnacle (sharp benchmark) + median across available books. Runs nightly on
   the VM AFTER the GitHub Actions pipeline (same slot as run_sage.sh, 07:45 UTC).
   Store per match: bookmaker, decimal odds both sides, snapshot timestamp.
   Match OddsPapi fixtures -> our match_id by team names + begin_at (fuzzy match
   in Python, log every uncertain join; unmatched = no odds, skip).
2. **Schema**: new `odds` table (match_id, bookmaker, odds_t1, odds_t2,
   captured_at). Read-only for experts; appended only by ingest_odds.py.
3. **Upset-hunter** (`expert_upset.py`): pure math, no LLM. For each not_started
   match with odds: compute p_us, EV, edge; if rule passes, insert pick via the
   existing gate AND a `bets` row (odds, stake=1). grade.py already computes
   pnl/roi for bets rows — the ledger tracks profit automatically.
4. **Sage prompt v2** (same nightly run): add a `Market` block to the prompt —
   decimal odds, implied probabilities, our EV per side — plus each expert's
   graded track record (n, win%, brier) so sage knows who to weight. Sage still
   returns the same 3-field JSON (contract unchanged).
5. **Sage calibration**: rescale confidence before submit:
   `conf_final = 0.5 + 0.5*(conf - 0.5)` (halve overconfidence) until ~200 graded
   sage picks exist, then recalibrate empirically from its own reliability curve
   (bucket win% vs claimed confidence).

## Invariants (unchanged)

- picks/bets append-only via gate; only grade.py mutates results.
- No real money; flat 1-unit stakes; every step logged with timestamps.

## Open questions for review

1. Thresholds: EV >= +5% and edge >= 5pts reasonable to start?
2. Which line is "the market": Pinnacle alone, or median of all books?
3. Should upset-hunter bet EVERY qualifying match or cap per day?
4. When odds are missing: skip (recommended) or fall back to chalkbot pick?