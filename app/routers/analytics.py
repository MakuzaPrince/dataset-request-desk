from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_ops
from app.errors import Invalid
from app.models import User
from app.services import analytics

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

MAX_RANGE_DAYS = 366


@router.get("")
def get_analytics(
    start: date | None = Query(None, description="Inclusive start date (default: 30 days before end)"),
    end: date | None = Query(None, description="Inclusive end date (default: today, UTC)"),
    db: Session = Depends(get_db),
    _: User = Depends(require_ops),
):
    end = end or datetime.now(timezone.utc).date()
    start = start or end - timedelta(days=30)
    if start > end:
        raise Invalid("start must be on or before end")
    if (end - start).days > MAX_RANGE_DAYS:
        raise Invalid(f"date range cannot exceed {MAX_RANGE_DAYS} days")
    return analytics.summary(db, start, end)
