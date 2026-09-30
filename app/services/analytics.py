"""Analytics queries. Every aggregation runs in the database; Python only shapes the result.

Two expressions differ between PostgreSQL and SQLite (day bucketing and interval arithmetic),
and the median uses percentile_cont on PostgreSQL with a window-function fallback on SQLite.
"""
from datetime import date, datetime, time, timedelta

from sqlalchemy import Date, Float, cast, func, select
from sqlalchemy.orm import Session

from app.models import DatasetRequest, Episode, Quality, RequestStatus, StatusEvent


def _day(db: Session, column):
    if db.get_bind().dialect.name == "postgresql":
        return cast(column, Date)
    return func.date(column)


def _seconds_between(db: Session, later, earlier):
    if db.get_bind().dialect.name == "postgresql":
        return func.extract("epoch", later - earlier)
    return (func.julianday(later) - func.julianday(earlier)) * 86400.0


def _bounds(start: date, end: date) -> tuple[datetime, datetime]:
    # Inclusive date range -> half-open datetime range, so an index on the raw column is usable.
    return datetime.combine(start, time.min), datetime.combine(end + timedelta(days=1), time.min)


def episodes_per_day_per_robot(db: Session, start: date, end: date) -> list[dict]:
    lo, hi = _bounds(start, end)
    day = _day(db, Episode.recorded_at).label("day")
    stmt = (
        select(day, Episode.robot_id, func.count().label("episodes"))
        .where(Episode.recorded_at >= lo, Episode.recorded_at < hi)
        .group_by(day, Episode.robot_id)
        .order_by(day, Episode.robot_id)
    )
    return [{"day": str(r.day), "robot_id": r.robot_id, "episodes": r.episodes} for r in db.execute(stmt)]


def request_fulfilment(db: Session, start: date, end: date) -> dict:
    """Requests *submitted* in the range: counts by current status and median submitted->first delivery."""
    lo, hi = _bounds(start, end)
    in_range = (DatasetRequest.created_at >= lo, DatasetRequest.created_at < hi)

    counts = dict(
        db.execute(
            select(DatasetRequest.status, func.count()).where(*in_range).group_by(DatasetRequest.status)
        ).all()
    )
    by_status = {s.value: counts.get(s, 0) for s in RequestStatus}

    first_delivery = (
        select(StatusEvent.request_id, func.min(StatusEvent.created_at).label("delivered_at"))
        .where(StatusEvent.to_status == RequestStatus.delivered)
        .group_by(StatusEvent.request_id)
        .subquery()
    )
    durations = (
        select(_seconds_between(db, first_delivery.c.delivered_at, DatasetRequest.created_at).label("secs"))
        .join(first_delivery, first_delivery.c.request_id == DatasetRequest.id)
        .where(*in_range)
        .subquery()
    )

    if db.get_bind().dialect.name == "postgresql":
        median_stmt = select(
            func.percentile_cont(0.5).within_group(durations.c.secs),
            func.count(),
        )
        median, delivered = db.execute(median_stmt).one()
    else:
        ranked = select(
            durations.c.secs,
            func.row_number().over(order_by=durations.c.secs).label("rn"),
            func.count().over().label("n"),
        ).subquery()
        # Middle row for odd n, mean of the two middle rows for even n.
        median = db.scalar(
            select(func.avg(cast(ranked.c.secs, Float))).where(
                ranked.c.rn.in_([(ranked.c.n + 1) // 2, (ranked.c.n + 2) // 2])
            )
        )
        delivered = db.scalar(select(func.count()).select_from(durations))

    return {
        "total": sum(by_status.values()),
        "by_status": by_status,
        "delivered_count": delivered or 0,
        "median_seconds_to_delivery": round(float(median), 1) if median is not None else None,
    }


def top_tasks_by_good_episodes(db: Session, start: date, end: date, limit: int = 5) -> list[dict]:
    lo, hi = _bounds(start, end)
    good = func.count().label("good_episodes")
    stmt = (
        select(Episode.task_name, good)
        .where(Episode.quality == Quality.good, Episode.recorded_at >= lo, Episode.recorded_at < hi)
        .group_by(Episode.task_name)
        .order_by(good.desc(), Episode.task_name)
        .limit(limit)
    )
    return [{"task_name": r.task_name, "good_episodes": r.good_episodes} for r in db.execute(stmt)]


def summary(db: Session, start: date, end: date) -> dict:
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "episodes_per_day_per_robot": episodes_per_day_per_robot(db, start, end),
        "request_fulfilment": request_fulfilment(db, start, end),
        "top_tasks_by_good_episodes": top_tasks_by_good_episodes(db, start, end),
    }
