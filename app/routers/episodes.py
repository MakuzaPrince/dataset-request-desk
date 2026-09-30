import io

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import require_ops
from app.errors import Invalid
from app.models import Assignment, Episode, Quality, User
from app.schemas import EpisodeOut, Page, normalize_task_name
from app.services.importer import import_episodes

router = APIRouter(prefix="/api/episodes", tags=["episodes"])


@router.get("", response_model=Page[EpisodeOut])
def list_episodes(
    task_name: str | None = Query(None, max_length=255),
    quality: Quality | None = None,
    robot_id: str | None = Query(None, max_length=64),
    unassigned: bool = False,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: User = Depends(require_ops),
):
    stmt = select(Episode, Assignment.request_id).outerjoin(Assignment, Assignment.episode_id == Episode.id)
    if task_name:
        stmt = stmt.where(Episode.task_name == normalize_task_name(task_name))
    if quality:
        stmt = stmt.where(Episode.quality == quality)
    if robot_id:
        stmt = stmt.where(Episode.robot_id == robot_id.strip().lower())
    if unassigned:
        stmt = stmt.where(Assignment.id.is_(None))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.execute(stmt.order_by(Episode.recorded_at.desc(), Episode.id.desc()).limit(limit).offset(offset)).all()
    items = [EpisodeOut.model_validate(e).model_copy(update={"assigned_request_id": rid}) for e, rid in rows]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/task-names", response_model=list[str])
def task_names(db: Session = Depends(get_db), _: User = Depends(require_ops)):
    return db.scalars(select(Episode.task_name).distinct().order_by(Episode.task_name)).all()


@router.post("/import")
def import_csv(file: UploadFile = File(...), db: Session = Depends(get_db), user: User = Depends(require_ops)):
    if file.size is not None and file.size > settings.max_upload_bytes:
        raise Invalid(f"File is larger than {settings.max_upload_bytes} bytes")
    # Stream-decode the spooled upload; utf-8-sig strips a BOM if the export has one.
    text = io.TextIOWrapper(file.file, encoding="utf-8-sig", errors="strict", newline="")
    try:
        return import_episodes(db, text, filename=file.filename, user_id=user.id)
    except UnicodeDecodeError:
        db.rollback()
        raise Invalid("File is not valid UTF-8 text") from None
