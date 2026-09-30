# Notes

## 1. Design

**Data model**

```
users ──< requests ──< request_status_events >── users (actor)
             │
             └──< assignments >── episodes >── robots
                                     │
                              import_runs
```

- `users`: email (unique), bcrypt hash, `role` (client/operator/admin), `is_active`.
- `requests`: owned by a client. Holds `task_name`, `episodes_requested` (CHECK > 0), `deadline`, `notes` and the *current* `status`.
- `request_status_events`: an append-only log with one row per status change (`from_status`, `to_status`, `actor_id`, `note`, `created_at`). Creation is logged as `NULL → submitted`. This is the audit trail, and also the source for "time to delivery".
- `episodes`: the business key `episode_id` is UNIQUE, which makes the import idempotent. `robot_id` is a FK to `robots`, the known-robot list seeded by the migration. `quality` is an enum, and `duration_seconds` has CHECK > 0.
- `assignments`: `episode_id` is **UNIQUE**, so the database, not just the code, guarantees that an episode is assigned to at most one request.
- `import_runs`: who imported which file, and the summary of each run.

**Where state lives.** All state lives in PostgreSQL. The API is stateless apart from the SSE subscriber list, which is in memory and only a notification channel, so nothing is lost if it resets. Tokens are stateless JWTs, but the user row is loaded on every request, so deactivating a user or changing a role takes effect immediately. The UI keeps only the token (sessionStorage) and the current view.

**Hardest decisions**

1. **Where to enforce the rules.** Every rule lives in one service module (`services/workflow.py`) as a single `(from, to) → roles` table. The routers and the UI derive from it: `allowed_transitions` in API responses comes from the same table, so the UI never re-implements the rules. The rules that could break under concurrency are also enforced by the database. The UNIQUE constraint on `assignments.episode_id` covers two operators assigning the same episode. `SELECT … FOR UPDATE` on the request row serialises "deliver" against a concurrent unassign, so a delivered request can't drop below its count.
2. **When assignments may change.** The brief doesn't say. I allow assigning and unassigning only while the request is `in_progress`. Once delivered, the set the client is reviewing is frozen, and after a rejection the operator moves the request back to `in_progress` (rework) to change it. This keeps the delivery-count rule true at all times, not just at the moment of delivery. Assignment batches are all-or-nothing: one bad episode fails the whole batch, and the error lists the offending ids.
3. **The import's duplicate policy.** An exact duplicate in the file is skipped. The same id with *different* data in the file is a conflict: the first occurrence wins and the later row is reported with the line number of the one kept. The same id already in the database is compared field by field: identical rows count as `unchanged` (which is what makes re-runs safe), and different data is reported as `conflicts_with_existing` and **never overwritten**. An episode may already be assigned and delivered, so silently changing its quality would be wrong. Deciding what to do in that case is a human call.

### Import rules

| Problem in the export | Handling |
|---|---|
| Blank / whitespace-only line | skipped, `blank_row` |
| Wrong number of columns / unparseable CSV | skipped, `malformed_row` |
| Casing and whitespace (`" arm-01"`, `"  Pick Cup "`, `"USABLE"`, `ep-00003`) | normalised: ids upper-case, robots and quality lower-case, task names trimmed, lower-case and single-spaced (requests use the same normaliser, so filters match) |
| Dates `2026-08-14T09:12:00`, `2026-08-14 09:12:00`, `14/08/2026 09:15` | accepted. Slashed dates are read **day-first** (the sample has `14/08`, so month-first can't be right). All times are treated as UTC |
| Unparseable date / date more than a day in the future | skipped, `invalid_recorded_at` / `recorded_at_in_future` |
| Duration blank or `N/A` / non-numeric or ≤ 0 / > 3600 s | skipped, `missing_duration` / `invalid_duration` / `duration_out_of_range`. Episodes are short clips, so `999999` is a glitch. Fractional seconds (`45.5`) are accepted |
| Missing or unknown quality (`excellent`) | skipped, `missing_quality` / `invalid_quality`. Guessing a quality would make an episode assignable when it shouldn't be |
| Missing / unknown robot (`arm-99`) | skipped, `missing_robot_id` / `unknown_robot` |
| Missing episode id | skipped, `missing_episode_id` |
| Missing operator name | **imported** with a warning. It's descriptive metadata that no rule depends on |

A row with several problems is reported once, with every problem listed in `detail`. On the sample: 191 rows → 173 imported, 18 skipped. A second run → 0 imported, 173 unchanged.

**Other ambiguities decided**

- Only clients create requests, and only the owning client accepts or rejects. Admins can't accept on a client's behalf, because the brief gives that step to clients.
- Rejecting requires a reason, which is stored on the status event.
- A client's view of another client's request returns **404**, not 403, so request ids don't reveal what exists.
- Clients can see which episodes were assigned to their own requests, since they need that to review a delivery.
- Assigned episodes don't have to match the request's `task_name`. The picker defaults to the request's task, but operators may substitute.
- Analytics: "requests by status" and the median use requests **submitted** within the range. The median runs from submission to the **first** delivery, so rework doesn't inflate it. Analytics are visible to operators and admins only.
- Deadlines can't be in the past when a request is created.

## 2. Left out / simplified, and the next two days

- **Left out:** no refresh tokens or logout-revocation list (tokens last 8 h, but deactivation still cuts access immediately), no password reset, no request cancellation or editing, no pagination UI for the episode picker beyond the first 100 per quality, no rate limiting, and the UI has no automated tests (I drove it with a Playwright script during development, but didn't commit it).
- **Simplified:** SSE fan-out is in-process, so it's correct for the single app process that compose runs. Timestamps are naive UTC. SQLite is supported for tests and local runs; production is PostgreSQL (in SQLite, `FOR UPDATE` is a no-op and writes are serialised anyway).
- **Next two days:** (1) move large imports to a background job that uses `COPY` into a staging table followed by one `INSERT … SELECT … ON CONFLICT DO NOTHING`, with progress in the UI; (2) a daily rollup table for analytics (see Scale); (3) Postgres `LISTEN/NOTIFY` behind the broadcaster so it works with several workers; (4) a UI for resolving `conflicts_with_existing` rows; (5) login rate limiting, and moving the token to an HttpOnly cookie with CSRF protection.

## 3. Something that went wrong

The first 200k-row load test took **98 s**, which is too slow for an import meant to run
repeatedly. Instead of guessing, I ran the CLI under `cProfile`. Almost no time went to parsing.
It went to compiling SQL: I was building a new `insert().values([...1000 rows...])` statement
for every batch, which SQLAlchemy can't cache, and psycopg then rewrote each 9,000-parameter
query from scratch. I switched to a Core `execute(stmt, list_of_params)` with `RETURNING`
(SQLAlchemy's cached "insertmanyvalues" path) and selected columns instead of ORM objects for
the existence check. That cut the time to **49 s** (about 4k rows/s; re-import of 200k: 18 s). The profile
shows the remaining time is psycopg parameter handling and index maintenance, which is why
`COPY` is the next step at real volume.

The same test produced another surprise: `rows_read` was 400,001 for 200,000 rows. On Windows,
redirecting the generator's `csv.writer` output turned each `\r\n` into `\r\r\n`, so every
record was followed by a blank line. The importer handled it (every blank line reported as `blank_row`,
nothing lost), and it showed why the report matters: the numbers made the problem
visible straight away.

A third: while writing these notes I realised the SSE endpoint held a pooled DB connection for
the life of each stream. FastAPI only cleans up `yield` dependencies after the response
ends, and the auth check had opened a session. About fifteen connected operators would have
exhausted the pool and hung the API. The endpoint now closes the session before streaming, and a
test checks it.

## 4. Security

- **Passwords:** bcrypt (cost 12), never logged or returned. Login runs a bcrypt check even for unknown emails and returns one generic message, so response time and message don't reveal which emails exist.
- **Tokens:** HS256 JWT with `exp`; the signing key comes from `SECRET_KEY` (random per process if unset, never hard-coded for production). The user is re-loaded on every request, so an inactive user's token is rejected. The UI keeps the token in `sessionStorage` and sends it in a header. The SSE stream is read with `fetch` rather than `EventSource`, so the token never goes in a URL, where it would end up in logs.
- **Authorization:** enforced by dependencies on every route and re-checked in the services (ownership, role per transition). The UI only reflects what the API says is allowed.
- **Input validation:** Pydantic schemas with length and range limits on every body and query parameter. The database has enums, CHECK constraints, FKs and unique keys. SQL goes through SQLAlchemy only (bound parameters). The CSV upload is size-limited, must decode as UTF-8, and each field is validated. The UI escapes every piece of user data before inserting it into HTML (a test request with `<b>` in its notes renders as text).
- **What I'd worry about most:**
  1. **Broken object-level authorization (IDOR)**: a client reading or acting on another client's request by changing an id. Every request lookup goes through one function, `get_visible_request`, which applies the ownership filter, and the tests cover each route. The risk is a future endpoint that bypasses it.
  2. **Token theft through XSS**: a bearer token in `sessionStorage` can be read by any injected script. That's why rendering is escape-by-default and there are no third-party scripts. A strict CSP plus HttpOnly-cookie sessions would be the next hardening step. The CSV import also reaches operators' screens, so its content is treated as untrusted too.

  (Close behind: brute-forcing logins without rate limiting, and a weak or leaked `SECRET_KEY`, which would let anyone mint tokens.)

## 5. Scale

**Measured on a laptop with PostgreSQL 16 and 200k episodes:** analytics for one month takes about 13 ms (index scan on `(recorded_at, robot_id)`). The full-year per-day/per-robot rollup takes about 220 ms (a sequential scan, which is the right plan when the range covers the whole table).

**100× episodes (about 5M+):**
- *Analytics* stay in SQL and use the indexes, but cost grows with the number of rows in range: roughly 5–6 s for a year of data. Fix: a small `episode_daily_stats(day, robot_id, task_name, quality, count)` rollup maintained by the importer (the only writer of episodes), which shrinks year queries to a few thousand rows, and/or monthly partitioning on `recorded_at`. The request median runs over requests, not episodes, so it stays small.
- *Import* would take minutes per million rows and hit HTTP timeouts first. Fix: a background job with `COPY` into a staging table, set-based validation, and one `INSERT … ON CONFLICT`.
- *Episode list:* `COUNT(*)` with filters and deep `OFFSET`s get slow. Fix: keyset pagination on `(recorded_at, id)`, and estimated or capped counts. A partial index on unassigned good/usable episodes would serve the picker.

**10× users:** The API is stateless, so it scales horizontally. What breaks first is the **in-process SSE broadcaster**: with more than one worker or instance, an operator only sees events published by the process they're connected to. Fix: Postgres `LISTEN/NOTIFY` (or Redis pub/sub) feeding each process's broadcaster. Next is DB connections (sync workers × pool size), handled with PgBouncer and a tuned pool. Login is CPU-bound because of bcrypt, which is expected, and rate limiting also protects it.

## 6. AI tooling

I built this with an AI coding assistant (Claude Code) working in the repository. It explored the
brief and seed data, drafted the code, tests and these notes, and ran the verification: the test
suite on SQLite and PostgreSQL, a browser run through the client and operator flows, the
200k-row load test and its profiling. I reviewed the design decisions above and can explain each
of them. The assistant didn't replace verification: every rule has a test, and I confirmed the key tests
fail when the rule they guard is removed.
