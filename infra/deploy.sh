#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Executed remotely by GitHub Actions. Runtime secrets stay in the server's .env.
release_sha="${1:?Expected commit SHA}"
[[ "$release_sha" =~ ^[0-9a-f]{40}$ ]] || { echo 'Invalid commit SHA'; exit 1; }
cd /srv/eventradar
exec 9>.deploy.lock
flock -w 600 9
[[ -s .env || -s .env.pending ]] || { echo 'Provide BOT_ENV or /srv/eventradar/.env'; exit 1; }
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || { echo 'Server checkout has tracked modifications; refusing to overwrite'; exit 1; }

git fetch --prune origin main
git cat-file -e "${release_sha}^{commit}"
git merge-base --is-ancestor "$release_sha" origin/main || { echo 'Commit is not in origin/main'; exit 1; }
# Avoid a queued old workflow deploying over a newer main commit.
[[ "$(git rev-parse origin/main)" == "$release_sha" ]] || { echo 'Superseded release; skipped'; exit 0; }
previous_sha="$(git rev-parse HEAD)"
had_env=false
if [[ -f .env ]]; then
  cp -p .env .env.previous
  had_env=true
fi
restore_checkout() {
  git -c core.hooksPath=/dev/null checkout --detach "$previous_sha"
  if [[ "$had_env" == true ]]; then
    cp -p .env.previous .env
  else
    rm -f .env
  fi
}
trap restore_checkout ERR
previous_image="$(docker inspect --format '{{.Config.Image}}' "$(docker compose ps -aq bot)" 2>/dev/null || true)"
if [[ -s .env.pending ]]; then
  mv .env.pending .env
  chmod 600 .env
fi
git -c core.hooksPath=/dev/null checkout --detach "$release_sha"
export RELEASE_TAG="$release_sha"

# Build before stopping the old container. No image is pushed to a registry.
if ! docker compose build --pull bot; then
  restore_checkout
  exit 1
fi

# Consistent online SQLite backup; never cp a live WAL database.
if [[ -n "$(docker compose ps -q bot)" ]]; then
  docker compose exec -T bot python - <<'PY'
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
path = Path(os.environ.get('DATABASE_PATH', '/data/eventradar.sqlite3'))
directory = path.parent / 'backups'
directory.mkdir(mode=0o700, exist_ok=True)
target = directory / (datetime.now(UTC).strftime('%Y%m%dT%H%M%S') + '.sqlite3')
with sqlite3.connect(path) as source, sqlite3.connect(target) as backup:
    source.backup(backup)
for stale in sorted(directory.glob('*.sqlite3'))[:-10]:
    stale.unlink()
PY
fi

if docker compose up -d --no-build --wait --wait-timeout 180 bot; then
  echo "Deployed $release_sha"
  rm -f .env.previous
  trap - ERR
else
  echo 'New release is unhealthy; attempting previous local image'
  restore_checkout
  if [[ -n "$previous_image" ]]; then
    # Old images remain local; do not prune images as part of deployment.
    export RELEASE_TAG="${previous_image#eventradar:}"
    docker compose up -d --no-build --pull never --wait --wait-timeout 180 bot
  fi
  exit 1
fi
