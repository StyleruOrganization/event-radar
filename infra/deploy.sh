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

# Use a one-off container with the bot's volumes for preflight and backup.
# The existing bot may be restarting or absent.
docker compose run --rm --no-deps -T bot python - <<'PYTHON'
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ValidationError
from eventradar.chatgpt_auth import AuthRequired, ChatGPTCredentials
from eventradar.config import Settings

try:
    settings = Settings()
except ValidationError as error:
    fields = ", ".join(str(entry["loc"][0]) for entry in error.errors() if entry["loc"])
    raise SystemExit("Invalid server .env fields: " + (fields or "configuration")) from None
try:
    profile = ChatGPTCredentials(settings.chatgpt_auth_dir).profile()
except (AuthRequired, OSError):
    raise SystemExit("Cannot read the ChatGPT session in the container; check the runtime mount and permissions") from None
if not all(profile.get(field) for field in ('client_id', 'subject', 'access_token', 'refresh_token')):
    raise SystemExit('Import the ChatGPT session before deployment')
if 'chatgpt.tokens.use.direct' not in profile.get('scopes', []):
    raise SystemExit('The ChatGPT session has no plan-usage permission')
database = settings.database_path
if not database.is_file():
    raise SystemExit('Import the local database before deployment; refusing an empty first start')

# Use SQLite's online backup API to include committed changes in the WAL.
backup_directory = database.parent / 'backups'
backup_directory.mkdir(mode=0o700, exist_ok=True)
backup_name = datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f.sqlite3')
backup_path = backup_directory / backup_name
source_uri = f'{database.absolute().as_uri()}?mode=ro'
with TemporaryDirectory(dir=backup_directory) as temporary_directory:
    temporary_path = Path(temporary_directory) / backup_name
    temporary_path.touch(mode=0o600)
    with (
        closing(sqlite3.connect(source_uri, uri=True)) as source,
        closing(sqlite3.connect(temporary_path)) as target,
    ):
        source.backup(target)
    temporary_path.replace(backup_path)

for stale in sorted(backup_directory.glob('*.sqlite3'))[:-10]:
    stale.unlink()
print(f'SQLite backup created: {backup_name}')
PYTHON

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
