# Dataset Request Desk

Clients submit dataset requests, operators fulfil them by assigning episodes, and clients
accept or reject the delivery.

FastAPI, SQLAlchemy, Alembic and PostgreSQL, with a small vanilla-JS UI served by the same app.
**Stretch item:** real-time. Operators see new requests and status changes live (SSE).
Design decisions are in [NOTES.md](NOTES.md).

## Run

```bash
docker compose up --build
```

Open <http://localhost:8000>. On start the container applies migrations, seeds the users and
imports `seed/episodes.csv`. Each step is idempotent, so restarts are safe.

| Email | Password | Role |
|---|---|---|
| admin@example.com | admin123 | admin |
| ops1@example.com / ops2@example.com | ops123 | operator |
| client-a@example.com / client-b@example.com | client123 | client |

`SECRET_KEY` (JWT signing key) has a local-only default in `docker-compose.yml`. Set a real one anywhere else.

Without Docker:

```bash
pip install -r requirements-dev.txt
export DATABASE_URL=sqlite:///./desk.db SECRET_KEY=dev-only   # or a postgresql+psycopg:// URL
alembic upgrade head && python -m app.cli seed-users && python -m app.cli import-episodes seed/episodes.csv
uvicorn app.main:app
```

## Tests

```bash
pytest
```

Runs against a temporary SQLite database built from the migrations. Set
`TEST_DATABASE_URL=postgresql+psycopg://…` to run against PostgreSQL. CI runs both, plus a
`docker compose up` smoke test.

## Usage notes

- The UI covers the client and operator flows. Import, analytics and user admin are API/CLI
  features. Try them at <http://localhost:8000/docs> (log in via `/api/auth/login`, then
  "Authorize" with the token).
- Import: `POST /api/episodes/import` or `python -m app.cli import-episodes <file.csv>`. The
  report gives imported, unchanged and skipped counts, with the line and reason for each
  skipped row. The rules are in [NOTES.md](NOTES.md#import-rules).
- Analytics: `GET /api/analytics?start=YYYY-MM-DD&end=YYYY-MM-DD`.

## Analytics at 5 million episodes

All aggregation happens in SQL (`GROUP BY`, `percentile_cont`, `LIMIT 5`); only the results reach
Python. With 200k episodes on PostgreSQL, a one-month range takes about 13 ms (index on
`(recorded_at, robot_id)`) and a full year about 220 ms. At 5M, monthly ranges stay fast, but a
year of data means scanning about 5M rows, which takes seconds. The fix is a daily rollup table
(`day, robot, task, quality, count`) maintained by the importer, optionally with monthly
partitions. The median runs over requests, not episodes, so it stays cheap.

## Layout

```
app/          main.py, models.py, schemas.py, deps.py (auth), cli.py
  services/   domain rules: workflow, importer, analytics
  routers/    HTTP endpoints
  static/     UI
migrations/   Alembic
seed/         seed users and sample CSV
tests/
```
