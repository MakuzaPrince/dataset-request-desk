# Dataset Request Desk

Internal platform that replaces the dataset-request spreadsheet: clients submit dataset
requests, operators fulfil them by assigning recorded episodes, clients accept or reject
the delivery.

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16
- **Frontend:** a small dependency-free single-page app (vanilla JS), served by the same process.
  It covers the client and operator flows from the brief. Import, analytics and user admin
  are API/CLI features: use them from the interactive docs at `/docs` ("Authorize" with a token from `/api/auth/login`).
- **Stretch item chosen:** real-time. Operators see new requests and status changes live (SSE).

Design notes, trade-offs and answers to the brief's questions are in [NOTES.md](NOTES.md).

## Run it

```bash
docker compose up --build
```

Then open <http://localhost:8000>. On start the app container runs, in order:
migrations, `seed-users`, then an import of `seed/episodes.csv`, then the API/UI server. All three setup
steps are idempotent, so restarting the container is safe.

| Email | Password | Role |
|---|---|---|
| admin@example.com | admin123 | admin |
| ops1@example.com | ops123 | operator |
| ops2@example.com | ops123 | operator |
| client-a@example.com | client123 | client (Acme Robotics) |
| client-b@example.com | client123 | client (Beta Labs) |

Passwords are stored as bcrypt hashes. The seed step only creates missing users, so it never resets a changed password.

Configuration (environment variables, all optional for local use):

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | dev value in compose | JWT signing key. **Set a real one outside local dev.** |
| `POSTGRES_PASSWORD` | `desk-local-only` | Database password used by both containers |
| `APP_PORT` | `8000` | Host port |
| `SEED_USERS` / `SEED_EPISODES` | `true` | Disable seeding on start |
| `TOKEN_TTL_MINUTES` | `480` | Access-token lifetime |
| `LOG_LEVEL` | `INFO` | |

### Without Docker

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
export DATABASE_URL=sqlite:///./desk.db SECRET_KEY=dev-only-secret   # or a postgresql+psycopg:// URL
alembic upgrade head
python -m app.cli seed-users
python -m app.cli import-episodes seed/episodes.csv
uvicorn app.main:app --reload
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

By default the suite runs against a temporary SQLite database built with the real Alembic
migrations. To run the same suite against PostgreSQL:

```bash
TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/desk_test pytest
```

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs the suite on both SQLite and
PostgreSQL, then does `docker compose up` from a clean checkout and smoke-tests health, login,
the seeded data and an idempotent restart.

What the tests cover (most of the domain rules):

- `test_auth.py`: login, hashing, token rejection, deactivation revoking tokens immediately, every endpoint requiring auth, the access-log format.
- `test_authorization.py`: clients only see their own requests (404 for others), clients get 403 on operator endpoints, operators get 403 on user management, role changes take effect on existing tokens.
- `test_transitions.py`: the full lifecycle including rework, every invalid transition (409), the wrong role for a valid step (403), the delivery count guard, a required rejection reason, and the audit trail (who and when).
- `test_assignments.py`: good/usable only, all-or-nothing batches, one request per episode (API check plus the DB unique constraint), frozen outside `in_progress`, filters.
- `test_import.py`: exact report for the messy seed file, re-import creates nothing, normalisation, duplicate and conflict handling, never overwriting existing data.
- `test_analytics.py`: day/robot buckets at range edges, top-5 counting only good episodes, median for odd, even and empty sets, measured to *first* delivery.
- `test_events.py`: the SSE broadcaster, and SSE connections not holding a DB connection.

## Importing episodes

- API: `POST /api/episodes/import` (multipart `file`).
- CLI: `python -m app.cli import-episodes path/to/export.csv [--summary]`

The response reports `imported`, `unchanged` (already present from an earlier run),
`skipped` with counts per reason, and every skipped row with its line number, reason and a detail
message, plus non-fatal warnings. How each kind of messy data is handled is described in
[NOTES.md](NOTES.md#import-rules).

## Analytics at 5 million episodes

`GET /api/analytics` does all aggregation in SQL: `GROUP BY` day and robot, `COUNT` by status,
`percentile_cont` for the median, and `ORDER BY … LIMIT 5`. Only the aggregated rows reach Python.
Measured on PostgreSQL 16 with 200k episodes: a one-month range takes about 13 ms (index scan on
`(recorded_at, robot_id)`), and a full year about 220 ms (a sequential scan, since the range is the
whole table).

At 5M episodes a month-long range still takes tens of milliseconds, but query time grows with the
number of rows in range, so a year would take roughly 5 s. The median is computed over requests,
not episodes, so it stays cheap. The fix is a small daily rollup table
(`day, robot_id, task_name, quality, count`) updated by the importer, which is the only writer of
episodes. That turns year queries into a few thousand rows. Monthly partitioning on `recorded_at`
would help further. Details are in [NOTES.md](NOTES.md#5-scale).

## API overview

All endpoints except `/health` and `/api/auth/login` need `Authorization: Bearer <token>`.
Interactive docs are at <http://localhost:8000/docs>.

| Method & path | Who | |
|---|---|---|
| `POST /api/auth/login`, `GET /api/auth/me` | anyone / any user | |
| `GET /api/requests` · `GET /api/requests/{id}` | all (clients: own only) | filter `?status=`, paginated |
| `POST /api/requests` | client | |
| `POST /api/requests/{id}/transitions` | depends on the step | `{"to_status": "...", "note": "..."}` |
| `GET /api/requests/{id}/assignments` | all (clients: own only) | |
| `POST /api/requests/{id}/assignments` · `DELETE …/{episode_id}` | operator, admin | `{"episode_ids": [...]}` (all-or-nothing) |
| `GET /api/episodes` · `GET /api/episodes/task-names` | operator, admin | filters `task_name`, `quality`, `robot_id`, `unassigned` |
| `POST /api/episodes/import` | operator, admin | |
| `GET /api/analytics?start=YYYY-MM-DD&end=YYYY-MM-DD` | operator, admin | inclusive range, max 366 days |
| `GET /api/events` | operator, admin | Server-Sent Events stream |
| `GET/POST /api/users` · `PATCH /api/users/{id}` | admin | create, change role, deactivate |
| `GET /health` | public | checks the database (503 if unreachable) |

Errors return JSON `{"detail": "...", "code": "..."}` and, where relevant, the offending
`episode_ids`. Status codes: 401 unauthenticated, 403 wrong role, 404 not found or not yours,
409 conflicts with current state, 422 invalid input.

## Project layout

```
app/
  main.py            app factory, error handlers, /health, static UI
  models.py          SQLAlchemy models
  schemas.py         request/response models and validation
  deps.py            authentication and role dependencies
  services/          domain logic: workflow.py, importer.py, analytics.py
  routers/           thin HTTP layer
  events.py          in-process pub/sub for SSE
  logging_setup.py   JSON logs plus a per-request access log
  cli.py             seed-users, import-episodes
  static/            the UI
migrations/          Alembic
seed/                seed users and the sample export
tests/
```
