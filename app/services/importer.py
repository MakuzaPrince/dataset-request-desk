"""Idempotent CSV import of episode metadata.

Rows are validated and normalised one by one; valid rows are inserted in batches with
INSERT ... ON CONFLICT (episode_id) DO NOTHING, so re-running the same file creates
nothing new. Every row that is not imported is reported with its line and a reason.
"""
import csv
import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TextIO

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from app.errors import Invalid
from app.models import Episode, ImportRun, Quality, Robot, utcnow
from app.schemas import normalize_task_name

REQUIRED_COLUMNS = (
    "episode_id",
    "robot_id",
    "task_name",
    "recorded_at",
    "duration_seconds",
    "operator_name",
    "quality",
)
# Episodes are short clips; anything longer than an hour is treated as a recording-system glitch.
MAX_DURATION_SECONDS = 3600
BATCH_SIZE = 1000
MAX_REPORTED_ROWS = 1000
EPISODE_ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,63}$")
# Naive formats seen in the export; all are interpreted as UTC. Slashed dates are day-first.
DATE_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S")
MISSING_MARKERS = {"", "n/a", "na", "null", "none", "-"}
EPISODES = Episode.__table__


class RowError(Exception):
    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class ParsedRow:
    episode_id: str
    robot_id: str
    task_name: str
    recorded_at: datetime
    duration_seconds: float
    operator_name: str | None
    quality: Quality

    def values(self) -> tuple:
        return (self.robot_id, self.task_name, self.recorded_at, self.duration_seconds, self.operator_name, self.quality)


def _blank(value: str) -> bool:
    return value.strip().lower() in MISSING_MARKERS


def parse_datetime(raw: str) -> datetime:
    raw = raw.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            pass
    try:
        # ISO-8601 with an offset or 'Z' -> convert to naive UTC.
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise RowError("invalid_recorded_at", f"unrecognised date '{raw}'") from None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def parse_row(cells: dict[str, str], now: datetime) -> tuple[ParsedRow, list[str]]:
    """Return the normalised row and any non-fatal warnings; raise RowError if unusable."""
    errors: list[RowError] = []
    warnings: list[str] = []

    episode_id = cells["episode_id"].strip().upper()
    if not episode_id:
        errors.append(RowError("missing_episode_id", "episode_id is blank"))
    elif not EPISODE_ID_RE.match(episode_id):
        errors.append(RowError("invalid_episode_id", f"episode_id '{episode_id}' has an invalid format"))

    robot_id = cells["robot_id"].strip().lower()
    if _blank(robot_id):
        errors.append(RowError("missing_robot_id", "robot_id is blank"))

    task_name = normalize_task_name(cells["task_name"])
    if not task_name:
        errors.append(RowError("missing_task_name", "task_name is blank"))

    recorded_at = None
    if _blank(cells["recorded_at"]):
        errors.append(RowError("missing_recorded_at", "recorded_at is blank"))
    else:
        try:
            recorded_at = parse_datetime(cells["recorded_at"])
            if recorded_at > now + timedelta(days=1):
                errors.append(RowError("recorded_at_in_future", f"recorded_at {recorded_at.isoformat()} is in the future"))
        except RowError as exc:
            errors.append(exc)

    duration = None
    raw_duration = cells["duration_seconds"].strip()
    if _blank(raw_duration):
        errors.append(RowError("missing_duration", f"duration_seconds is blank ('{raw_duration}')"))
    else:
        try:
            duration = float(raw_duration)
        except ValueError:
            errors.append(RowError("invalid_duration", f"duration_seconds '{raw_duration}' is not a number"))
        else:
            if not math.isfinite(duration) or duration <= 0:
                errors.append(RowError("invalid_duration", f"duration_seconds must be positive, got '{raw_duration}'"))
            elif duration > MAX_DURATION_SECONDS:
                errors.append(
                    RowError("duration_out_of_range", f"duration_seconds {raw_duration} exceeds {MAX_DURATION_SECONDS}s")
                )

    operator_name = " ".join(cells["operator_name"].split()) or None
    if operator_name is None:
        warnings.append("operator_name is blank; imported without it")

    raw_quality = cells["quality"].strip().lower()
    quality = None
    if not raw_quality:
        errors.append(RowError("missing_quality", "quality is blank"))
    else:
        try:
            quality = Quality(raw_quality)
        except ValueError:
            errors.append(RowError("invalid_quality", f"quality '{cells['quality'].strip()}' is not good/usable/bad"))

    if errors:
        raise RowError(errors[0].reason, "; ".join(e.detail for e in errors))
    return ParsedRow(episode_id, robot_id, task_name, recorded_at, duration, operator_name, quality), warnings


class Report:
    def __init__(self) -> None:
        self.rows_read = 0
        self.imported = 0
        self.unchanged = 0
        self.skipped_by_reason: Counter[str] = Counter()
        self.skipped: list[dict] = []
        self.warnings: list[dict] = []

    def skip(self, line: int, episode_id: str | None, reason: str, detail: str) -> None:
        self.skipped_by_reason[reason] += 1
        if len(self.skipped) < MAX_REPORTED_ROWS:
            self.skipped.append({"line": line, "episode_id": episode_id or None, "reason": reason, "detail": detail})

    def warn(self, line: int, episode_id: str, detail: str) -> None:
        if len(self.warnings) < MAX_REPORTED_ROWS:
            self.warnings.append({"line": line, "episode_id": episode_id, "detail": detail})

    def as_dict(self) -> dict:
        skipped_total = sum(self.skipped_by_reason.values())
        return {
            "rows_read": self.rows_read,
            "imported": self.imported,
            "unchanged": self.unchanged,
            "skipped": skipped_total,
            "skipped_by_reason": dict(sorted(self.skipped_by_reason.items())),
            "skipped_rows": self.skipped,
            "warnings": self.warnings,
            "details_truncated": skipped_total > len(self.skipped),
        }


def _insert_ignore(db: Session):
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        return postgresql.insert(EPISODES)
    if dialect == "sqlite":
        return sqlite.insert(EPISODES)
    raise RuntimeError(f"unsupported database dialect: {dialect}")


def _flush(db: Session, batch: list[tuple[int, ParsedRow]], report: Report, run_id: int) -> None:
    if not batch:
        return
    ids = [row.episode_id for _, row in batch]
    c = EPISODES.c
    existing = {
        row[0]: tuple(row[1:])
        for row in db.execute(
            select(c.episode_id, c.robot_id, c.task_name, c.recorded_at, c.duration_seconds, c.operator_name, c.quality)
            .where(c.episode_id.in_(ids))
        )
    }
    to_insert: list[tuple[int, ParsedRow]] = []
    for line, row in batch:
        if row.episode_id not in existing:
            to_insert.append((line, row))
        elif existing[row.episode_id] == row.values():
            report.unchanged += 1  # already imported by a previous run: this is what makes re-runs safe
        else:
            report.skip(line, row.episode_id, "conflicts_with_existing",
                        "an episode with this id already exists with different data; not overwritten")

    if to_insert:
        now = utcnow()
        # Core executemany with RETURNING: SQLAlchemy batches it into multi-row INSERTs
        # ("insertmanyvalues") while reusing one cached compiled statement.
        stmt = (
            _insert_ignore(db)
            .on_conflict_do_nothing(index_elements=["episode_id"])
            .returning(EPISODES.c.episode_id)
        )
        params = [
            {
                "episode_id": r.episode_id,
                "robot_id": r.robot_id,
                "task_name": r.task_name,
                "recorded_at": r.recorded_at,
                "duration_seconds": r.duration_seconds,
                "operator_name": r.operator_name,
                "quality": r.quality,
                "import_run_id": run_id,
                "created_at": now,
            }
            for _, r in to_insert
        ]
        inserted = set(db.connection().execute(stmt, params).scalars().all())
        for line, row in to_insert:
            if row.episode_id in inserted:
                report.imported += 1
            else:
                report.skip(line, row.episode_id, "conflicts_with_existing", "inserted concurrently by another import")
    db.commit()
    batch.clear()


def import_episodes(db: Session, stream: TextIO, filename: str | None = None, user_id: int | None = None) -> dict:
    reader = csv.reader(stream)
    header = next(reader, None)
    if header is None:
        raise Invalid("The file is empty")
    columns = [h.strip().lower().lstrip("﻿") for h in header]
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        raise Invalid(f"CSV header is missing required columns: {', '.join(missing)}")
    index = {c: columns.index(c) for c in REQUIRED_COLUMNS}

    run = ImportRun(filename=filename, started_by_id=user_id)
    db.add(run)
    db.commit()

    report = Report()
    known_robots = set(db.scalars(select(Robot.id)))
    seen: dict[str, tuple[int, tuple]] = {}  # episode_id -> (first line, values) within this file
    batch: list[tuple[int, ParsedRow]] = []
    now = utcnow()

    for line, row, csv_error in _rows(reader):
        report.rows_read += 1
        if csv_error is not None:
            report.skip(line, None, "malformed_row", f"unparseable CSV: {csv_error}")
            continue
        if not any(c.strip() for c in row):
            report.skip(line, None, "blank_row", "row is empty")
            continue
        if len(row) != len(columns):
            report.skip(line, row[0].strip() or None, "malformed_row",
                        f"expected {len(columns)} columns, got {len(row)}")
            continue
        try:
            parsed, warnings = parse_row({c: row[i] for c, i in index.items()}, now)
        except RowError as exc:
            report.skip(line, row[index["episode_id"]].strip() or None, exc.reason, exc.detail)
            continue
        if parsed.robot_id not in known_robots:
            report.skip(line, parsed.episode_id, "unknown_robot", f"robot '{parsed.robot_id}' is not a known robot")
            continue
        if parsed.episode_id in seen:
            first_line, first_values = seen[parsed.episode_id]
            if first_values == parsed.values():
                report.skip(line, parsed.episode_id, "duplicate_in_file", f"exact duplicate of line {first_line}")
            else:
                report.skip(line, parsed.episode_id, "conflicting_duplicate_in_file",
                            f"same episode_id as line {first_line} but different data; the first occurrence was kept")
            continue
        seen[parsed.episode_id] = (line, parsed.values())
        for w in warnings:
            report.warn(line, parsed.episode_id, w)
        batch.append((line, parsed))
        if len(batch) >= BATCH_SIZE:
            _flush(db, batch, report, run.id)
    _flush(db, batch, report, run.id)

    result = report.as_dict()
    result["import_run_id"] = run.id
    run.summary = {k: v for k, v in result.items() if k not in ("skipped_rows", "warnings")}
    db.commit()
    return result


def _rows(reader) -> Iterable[tuple[int, list[str], str | None]]:
    """Yield (line number, cells, csv error); a broken row is reported instead of aborting the import."""
    while True:
        try:
            row = next(reader)
        except StopIteration:
            return
        except csv.Error as exc:
            yield reader.line_num, [], str(exc)
            continue
        yield reader.line_num, row, None
