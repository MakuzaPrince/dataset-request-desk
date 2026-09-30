# Notes

## 1. Design

```
users ──< requests ──< request_status_events >── users (actor)
             └──< assignments >── episodes >── robots
                                      └── import_runs
```

- `requests` holds the *current* `status`. `request_status_events` is an append-only log with one row per change (`from`, `to`, actor, note, time); creation is logged as `NULL → submitted`. It's the audit trail and the source for "time to delivery".
- `episodes.episode_id` is UNIQUE, which is what makes the import idempotent. `robot_id` is a FK to `robots` (the known robots, seeded by the migration).
- `assignments.episode_id` is UNIQUE, so the database guarantees "one request per episode".
- Enums, CHECK constraints (`duration > 0`, `episodes_requested > 0`) and FKs back up the validation in the API.

**Where state lives.** All state is in PostgreSQL. The API is stateless apart from the in-memory SSE subscriber list, which is only a notification channel. JWTs are stateless, but the user row is loaded on every request, so deactivating a user or changing a role takes effect immediately.

**Hardest decisions**

1. **Where to enforce rules.** All transitions live in one table in `services/workflow.py`: `(from, to) → roles`. The API derives `allowed_transitions` from it, and the UI only renders what the API allows. The rules that concurrency could break are also enforced by the database: the UNIQUE constraint on assignments, and `SELECT … FOR UPDATE` on the request row so "deliver" can't race with an unassign.
2. **When assignments may change.** The brief doesn't say. I allow it only in `in_progress`. A delivered set is frozen while the client reviews it, and rework goes back through `in_progress`. That keeps the delivery-count rule true at all times. Batches are all-or-nothing.
3. **Duplicate policy in the import.** An exact duplicate in the file is skipped. The same id with different data in the file keeps the first row and reports the later one. An id already in the DB is compared field by field: identical rows count as `unchanged` (safe re-runs), and different data is reported as `conflicts_with_existing` and **never overwritten**, because that episode may already have been delivered.

### Import rules

| Problem | Handling |
|---|---|
| Blank line / wrong column count | skipped: `blank_row` / `malformed_row` |
| Casing and whitespace (`" arm-01"`, `"  Pick Cup "`, `USABLE`, `ep-00003`) | normalised (ids upper-case; robot, quality and task names lower-case and trimmed). Requests use the same task-name normaliser |
| `2026-08-14T09:12:00`, `2026-08-14 09:12:00`, `14/08/2026 09:15` | accepted. Slashed dates are **day-first** (`14/08` rules out month-first). All times are UTC |
| Bad date / more than a day in the future | skipped: `invalid_recorded_at` / `recorded_at_in_future` |
| Duration blank or `N/A`, ≤ 0, > 3600 s | skipped (`999999` is a glitch for a short clip). `45.5` is accepted |
| Quality missing or unknown (`excellent`) | skipped. Guessing would make an episode assignable |
| Robot missing / unknown (`arm-99`), id missing | skipped |
| Operator name missing | **imported** with a warning. No rule depends on it |

Sample file: 191 rows → 173 imported, 18 skipped with line and reason. A second run imports 0 and reports 173 unchanged.

**Other decisions.** Only clients create requests, and only the owning client accepts or rejects (admins can't act for a client). Rejection needs a reason. Another client's request returns 404, not 403, so ids don't leak. Clients can see the episodes on their own requests so they can review a delivery. Assigned episodes aren't forced to match the request's task (the picker defaults to it). Deadlines can't be in the past. `/health` is the one unauthenticated endpoint besides login, because load balancers and Docker must reach it; it reveals nothing beyond up/down. Analytics count requests *submitted* in the range, and the median runs to the *first* delivery so rework doesn't inflate it.

## 2. Left out, and the next two days

Left out: refresh tokens, password reset, request cancellation or editing, rate limiting, and automated UI tests (I drove the UI with a Playwright script but didn't commit it). The UI covers only the client and operator flows. Import, analytics and user admin are API/CLI features. SSE fan-out is in-process, which is correct for the single app process compose runs.

Next: (1) large imports as a background job using `COPY` into a staging table plus one `INSERT … ON CONFLICT`; (2) a daily rollup table for analytics; (3) Postgres `LISTEN/NOTIFY` behind the broadcaster for multiple workers; (4) login rate limiting, and an HttpOnly-cookie session with CSRF protection.

## 3. Something that went wrong

The first 200k-row import took **98 s**. `cProfile` showed the time wasn't in parsing but in SQL compilation: I built a fresh `insert().values([...1000 rows...])` per batch, which SQLAlchemy can't cache, and psycopg re-processed each 9,000-parameter query. Switching to `execute(stmt, params)` with `RETURNING` (SQLAlchemy's cached "insertmanyvalues" path), plus a column-only existence check, brought it to **49 s** (re-import: 18 s). The same test reported 400,001 rows for 200,000. On Windows, redirecting the generator's output turned `\r\n` into `\r\r\n`. The importer had correctly reported every blank line, and the report made the problem visible straight away.

A second one: the SSE endpoint held a pooled DB connection for the whole stream. FastAPI cleans up `yield` dependencies only when the response ends, and the auth check had opened a session. About 15 connected operators would have exhausted the pool. It now closes the session before streaming, and a test covers it.

## 4. Security

- **Passwords:** bcrypt (cost 12), never logged or returned. Unknown emails still run a bcrypt check and get the same error, so emails can't be enumerated.
- **Tokens:** HS256 JWT with expiry. The key comes from `SECRET_KEY`. The token is sent in a header, and the SSE stream is read with `fetch`, so the token never appears in a URL or log.
- **Validation:** Pydantic limits on every input, DB constraints behind them, SQL only via SQLAlchemy bound parameters. The upload is size-limited and must be UTF-8. The UI escapes all user data before rendering.
- **Top two worries:** (1) **IDOR**: a client reaching another client's request by id. Every lookup goes through `get_visible_request`, and there are tests per route; the risk is a future endpoint that skips it. (2) **XSS leading to token theft**: a token in `sessionStorage` is readable by any injected script (and CSV content reaches operators' screens). Mitigation today is escape-by-default and no third-party scripts; next would be a strict CSP and HttpOnly cookies.

## 5. Scale

**100× episodes (about 5M+).** Analytics already run in SQL on indexes. With 200k rows, a month takes 13 ms and a full year 220 ms, so a year at 5M is about 5 s. The fix is a daily rollup (`day, robot, task, quality, count`) maintained by the importer, plus monthly partitions. Import through HTTP would time out first, so it should be a background `COPY` job. The episode list's `COUNT(*)` and deep `OFFSET` degrade, which calls for keyset pagination and a partial index on unassigned good/usable episodes.

**10× users.** The API is stateless and scales out, but the **in-process SSE broadcaster** breaks first: with several workers, operators miss events from the other processes. That calls for `LISTEN/NOTIFY` or Redis. Next come DB connections (workers × pool), handled with PgBouncer. bcrypt makes login CPU-heavy by design, and rate limiting also protects it.

## 6. AI tooling

I used Claude Code (an AI coding assistant) throughout: to explore the brief and seed data, draft the code, tests and these notes, and run the checks. Those checks were the test suite on SQLite and PostgreSQL, a browser run of both flows, and the 200k-row load test with profiling. I reviewed the decisions above, and I confirmed the key tests fail when the rule they guard is removed.
