#!/bin/bash
# Nightly Hermes state backup: packs state + memory + skills + config from
# this machine and pushes a dated tarball to sntvnce/hermes-backup (private).
# Runs on the Hetzner VM via cron/systemd timer; the Mac copy runs on demand.
set -eu
STAMP=$(date -u +%F)
DIR=$(mktemp -d)
cd /root/.hermes
# state.db (sessions/transcripts), memories, skills, SOUL, config; exclude caches
tar -czf "$DIR/state_$STAMP.tgz" state.db memories skills SOUL.md config.yaml .env 2>/dev/null
# keep the repo small: local mirror of the backup repo, push one commit per night
BACKUP_DIR=/root/hermes-backup
if [ ! -d "$BACKUP_DIR" ]; then
  git clone -q "https://x-access-token:${GITHUB_TOKEN}@github.com/sntvnce/hermes-backup.git" "$BACKUP_DIR"
fi
cd "$BACKUP_DIR"
git pull -q --ff-only || true
cp "$DIR/state_$STAMP.tgz" .
git add "state_$STAMP.tgz"
git -c user.name="hermes-backup" -c user.email="backup@users.noreply.github.com" \
  commit -q -m "backup: $STAMP ($(du -h "state_$STAMP.tgz" | cut -f1))" || echo "no changes"
git push -q
rm -rf "$DIR"
# keep only the 30 most recent backups locally
ls -t state_*.tgz 2>/dev/null | tail -n +31 | xargs -r git rm -q
git -c user.name="hermes-backup" -c user.email="backup@users.noreply.github.com" commit -q -m "prune old backups" || true
git push -q
echo "backup $STAMP pushed: $(ls -la state_$STAMP.tgz | awk '{print $5}') bytes"