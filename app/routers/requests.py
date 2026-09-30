from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.deps import current_user, require_client, require_ops
from app.events import broadcaster
from app.models import Assignment, DatasetRequest, Episode, RequestStatus, StatusEvent, User
from app.schemas import (
    AssignIn,
    AssignOut,
    EpisodeOut,
    Page,
    RequestCreate,
    RequestDetailOut,
    RequestOut,
    StatusEventOut,
    TransitionIn,
)
from app.services import workflow

router = APIRouter(prefix="/api/requests", tags=["requests"])


def _out(req: DatasetRequest, user: User, count: int) -> dict:
    return {
        **{c: getattr(req, c) for c in (
            "id", "client_id", "task_name", "episodes_requested", "deadline", "notes", "status", "created_at", "updated_at"
        )},
        "client_name": req.client.organisation or req.client.name,
        "assigned_count": count,
        "allowed_transitions": workflow.allowed_transitions(req, user),
    }


def _detail(db: Session, req: DatasetRequest, user: User) -> RequestDetailOut:
    db.refresh(req)
    events = db.scalars(
        select(StatusEvent)
        .options(selectinload(StatusEvent.actor))
        .where(StatusEvent.request_id == req.id)
        .order_by(StatusEvent.id)
    ).all()
    count = workflow.assigned_counts(db, [req.id]).get(req.id, 0)
    return RequestDetailOut(
        **_out(req, user, count),
        events=[
            StatusEventOut(
                id=e.id, from_status=e.from_status, to_status=e.to_status, actor_id=e.actor_id,
                actor_name=e.actor.name, note=e.note, created_at=e.created_at,
            )
            for e in events
        ],
    )


def _publish(kind: str, req: DatasetRequest) -> None:
    broadcaster.publish({"type": kind, "request_id": req.id, "status": req.status.value})


@router.get("", response_model=Page[RequestOut])
def list_requests(
    status_filter: RequestStatus | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    stmt = workflow.visible_requests_query(user)
    if status_filter:
        stmt = stmt.where(DatasetRequest.status == status_filter)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    reqs = db.scalars(
        stmt.options(selectinload(DatasetRequest.client))
        .order_by(DatasetRequest.created_at.desc(), DatasetRequest.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    counts = workflow.assigned_counts(db, [r.id for r in reqs])
    return {"items": [_out(r, user, counts.get(r.id, 0)) for r in reqs], "total": total, "limit": limit, "offset": offset}


@router.post("", response_model=RequestDetailOut, status_code=status.HTTP_201_CREATED)
def create_request(body: RequestCreate, db: Session = Depends(get_db), user: User = Depends(require_client)):
    req = workflow.create_request(db, user, body)
    _publish("request_created", req)
    return _detail(db, req, user)


@router.get("/{request_id}", response_model=RequestDetailOut)
def get_request(request_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return _detail(db, workflow.get_visible_request(db, request_id, user), user)


@router.post("/{request_id}/transitions", response_model=RequestDetailOut)
def transition(request_id: int, body: TransitionIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    req = workflow.transition(db, request_id, user, body.to_status, body.note)
    _publish("request_status_changed", req)
    return _detail(db, req, user)


@router.get("/{request_id}/assignments", response_model=list[EpisodeOut])
def list_assignments(request_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    # Clients may review the episodes delivered on their own requests.
    req = workflow.get_visible_request(db, request_id, user)
    episodes = db.scalars(
        select(Episode)
        .join(Assignment, Assignment.episode_id == Episode.id)
        .where(Assignment.request_id == req.id)
        .order_by(Episode.episode_id)
    ).all()
    return [EpisodeOut.model_validate(e).model_copy(update={"assigned_request_id": req.id}) for e in episodes]


@router.post("/{request_id}/assignments", response_model=AssignOut)
def assign(request_id: int, body: AssignIn, db: Session = Depends(get_db), user: User = Depends(require_ops)):
    result = workflow.assign_episodes(db, request_id, user, body.episode_ids)
    broadcaster.publish({"type": "assignments_changed", "request_id": request_id})
    return result


@router.delete("/{request_id}/assignments/{episode_id}", status_code=status.HTTP_204_NO_CONTENT)
def unassign(request_id: int, episode_id: str, db: Session = Depends(get_db), user: User = Depends(require_ops)):
    workflow.unassign_episode(db, request_id, user, episode_id)
    broadcaster.publish({"type": "assignments_changed", "request_id": request_id})
