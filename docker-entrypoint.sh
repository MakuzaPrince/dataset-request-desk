#!/bin/sh
# Every step is idempotent, so restarting the container is always safe.
set -e

echo "Applying database migrations"
alembic upgrade head

if [ "${SEED_USERS:-true}" = "true" ]; then
  echo "Seeding users"
  python -m app.cli seed-users --file seed/users.json
fi

if [ "${SEED_EPISODES:-true}" = "true" ]; then
  echo "Importing seed episodes"
  python -m app.cli import-episodes seed/episodes.csv --summary
fi

exec "$@"
