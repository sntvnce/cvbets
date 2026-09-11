#!/bin/bash
# Nightly sage LLM expert (cvbets). Runs on the Hetzner VM via systemd timer
# cvbets-sage.timer at 07:45 UTC — after the 07:00 UTC GitHub Actions pipeline
# has ingested new matches and committed the ledger back to the repo.
# Pull latest ledger -> run sage (picks via scripts/submit_pick.py gate)
# -> push the ledger so GitHub stays the source of truth.
# Push races with other writers are retried: on rejection the local commit is
# dropped and the whole run repeats (sage re-submits its picks through the
# append-only gate; never any direct DB writes).
set -eu

REPO=/root/cvbets
TOKEN_FILE=/root/cron_token/gh_token
export GITHUB_TOKEN_FILE="$TOKEN_FILE"

cd "$REPO"
# scripts/ exec-bit noise (from clones/checkouts) must never block pulls/merges
git config core.fileMode false
# Untracked copies of tracked files (e.g. scripts/run_sage.sh restored from a
# stash) block 'git pull --ff-only' with 'untracked working tree files would be
# overwritten'. Remove them AFTER the pull instead: pull with stale-untracked
# tolerance, then restore missing tracked files from the remote state.
if [ -n "$(git status --porcelain | grep -E '^\?\?')" ]; then
  echo "note: untracked files present; moving them aside before pull"
  mkdir -p /tmp/cvbets-untracked
  git status --porcelain | grep -E '^\?\?' | cut -c4- | while IFS= read -r f; do
    case "$f" in *'*'|*'?'*) continue ;; esac
    mkdir -p "/tmp/cvbets-untracked/$(dirname "$f")"
    mv "$f" "/tmp/cvbets-untracked/$f" 2>/dev/null || true
  done
  git pull --ff-only -q || git pull -q || echo "WARN: git pull failed; continuing with local ledger"
  # restore anything the pull didn't bring in
  git status --porcelain | grep -E '^\?\?' | cut -c4- | while IFS= read -r f; do
    mv "/tmp/cvbets-untracked/$f" "$f" 2>/dev/null || true
  done
else
  git pull --ff-only -q || echo "WARN: git pull --ff-only failed; continuing with local ledger"
fi

PUSHED=0
for attempt in 1 2 3; do
  echo "sage run attempt $attempt $(date -u +%FT%TZ)"
  # 1) fresh ledger: the cloud pipeline commits data/ledger.db to the repo nightly
  git pull --ff-only -q || echo "WARN: git pull --ff-only failed; continuing with local ledger"

  # 2) sage: reads the debate table, asks the LLM relay, submits picks via the gate
  .venv/bin/python -c "import sys; sys.path.insert(0,'scripts'); import expert_sage; expert_sage.run()"

  # 3) push the ledger back (token read fresh from file; helper keeps it out of argv)
  export GITHUB_TOKEN="$(cat "$TOKEN_FILE")"
  git -c credential.helper='!f() { printf "username=x-access-token\npassword=%s\n" "$GITHUB_TOKEN"; }; f' \
      add data/ledger.db
  if git diff --cached --quiet; then
    echo "sage: no ledger changes to push"
    PUSHED=1
    break
  fi
  git -c user.name="sage-bot" -c user.email="sage@users.noreply.github.com" \
      commit -q -m "sage picks: $(date -u +%F)"
  if git -c credential.helper='!f() { printf "username=x-access-token\npassword=%s\n" "$GITHUB_TOKEN"; }; f' \
      push -q origin HEAD:main; then
    echo "sage: ledger pushed to GitHub"
    PUSHED=1
    break
  fi
  echo "sage: push rejected (remote moved); dropping local commit and regenerating"
  git reset -q --hard HEAD^
  sleep 5
done

if [ "$PUSHED" -eq 1 ]; then
  echo "sage nightly run complete $(date -u +%FT%TZ)"
else
  echo "ERROR: sage ledger push failed after 3 attempts $(date -u +%FT%TZ)"
  exit 1
fi