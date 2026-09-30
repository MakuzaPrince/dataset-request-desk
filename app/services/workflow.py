"""Request lifecycle: visibility, status transitions and episode assignment rules."""
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import Conflict, Forbidden, Invalid, NotFound
from app.models import (
    Assignment,
    DatasetRequest,
    Episode,
    Quality,
    RequestStatus,
    Role,
    StatusEvent,
    User,
)
from app.schemas import RequestCreate

S = RequestStatus
OPS = frozenset({Role.operator, Role.admin})
CLIENT = frozenset({Role.client})

# (from, to) -> roles allowed to perform it. Anything not listed is not a valid transition.
TRANSITIONS: dict[tuple[RequestStatus, RequestStatus], frozenset[Role]] = {
    (S.submitted, S.in_progress): OPS,
    (S.in_progress, S.delivered): OPS,
    (S.delivered, S.accepted): CLIENT,
    (S.delivered, S.rejected): CLIENT,
    (S.rejected, S.in_progress): OPS,
}

ASSIGNABLE_QUALITIES = (Quality.good, Quality.usable)
# Assignments are frozen once work is handed over so a delivered count can never drop.
ASSIGNMENT_OPEN_STATUSES = (S.in_progress,)


def _can_act(user: User, req: DatasetRequest, roles: frozenset[Role]) -> bool:
    if user.role not in roles:
        return False
    # Clients may only act on their own requests.
    return user.role != Role.client or req.client_id == user.id


def allowed_transitions(req: DatasetRequest, user: User) -> list[RequestStatus]:
    return [to for (frm, to), roles in TRANSITIONS.items() if frm == req.status and _can_act(user, req, roles)]


def get_visible_request(db: Session, request_id: int, user: User, for_update: bool = False) -> DatasetRequest:
    stmt = select(DatasetRequest).where(DatasetRequest.id == request_id)
    if for_update:
        # Row lock serialises transitions and (un)assignments on the same request (no-op on SQLite).
        stmt = stmt.with_for_update()
    req = db.scalars(stmt).first()
    # Clients get 404, not 403, for other clients' requests so ids don't leak existence.
    if req is None or (user.role == Role.client and req.client_id != user.id):
        raise NotFound("Request not found")
    return req


def visible_requests_query(user: User):
    stmt = select(DatasetRequest)
    if user.role == Role.client:
        stmt = stmt.where(DatasetRequest.client_id == user.id)
    return stmt


def assigned_counts(db: Session, request_ids: list[int]) -> dict[int, int]:
    if not request_ids:
        return {}
    rows = db.execute(
        select(Assignment.request_id, func.count())
        .where(Assignment.request_id.in_(request_ids))
        .group_by(Assignment.request_id)
    )
    return dict(rows.all())


def create_request(db: Session, client: User, data: RequestCreate) -> DatasetRequest:
    req = DatasetRequest(client_id=client.id, status=S.submitted, **data.model_dump())
    db.add(req)
    db.flush()
    db.add(StatusEvent(request_id=req.id, from_status=None, to_status=S.submitted, actor_id=client.id))
    db.commit()
    return req


def transition(db: Session, request_id: int, user: User, to_status: RequestStatus, note: str | None) -> DatasetRequest:
    req = get_visible_request(db, request_id, user, for_update=True)
    roles = TRANSITIONS.get((req.status, to_status))
    if roles is None:
        raise Conflict(f"Cannot move a request from '{req.status.value}' to '{to_status.value}'")
    if not _can_act(user, req, roles):
        raise Forbidden(f"Your role cannot move a request to '{to_status.value}'")
    if to_status == S.delivered:
        count = assigned_counts(db, [req.id]).get(req.id, 0)
        if count < req.episodes_requested:
            raise Conflict(
                f"Request needs at least {req.episodes_requested} assigned episodes before delivery; it has {count}",
                assigned_count=count,
            )
    if to_status == S.rejected and not note:
        raise Invalid("A reason is required when rejecting a delivery")

    db.add(StatusEvent(request_id=req.id, from_status=req.status, to_status=to_status, actor_id=user.id, note=note))
    req.status = to_status
    db.commit()
    return req


def _normalize_episode_ids(ids: list[str]) -> list[str]:
    # Keep order, drop duplicates; the importer stores ids upper-cased.
    return list(dict.fromkeys(i.strip().upper() for i in ids if i.strip()))


def assign_episodes(db: Session, request_id: int, user: User, episode_ids: list[str]) -> dict:
    """All-or-nothing: either every episode is assigned or none is."""
    req = get_visible_request(db, request_id, user, for_update=True)
    if req.status not in ASSIGNMENT_OPEN_STATUSES:
        raise Conflict(f"Episodes can only be assigned while a request is in_progress (it is '{req.status.value}')")

    ids = _normalize_episode_ids(episode_ids)
    if not ids:
        raise Invalid("No episode ids given")
    episodes = {e.episode_id: e for e in db.scalars(select(Episode).where(Episode.episode_id.in_(ids)))}
    missing = [i for i in ids if i not in episodes]
    if missing:
        raise NotFound("Some episodes do not exist", episode_ids=missing)
    bad = [i for i in ids if episodes[i].quality not in ASSIGNABLE_QUALITIES]
    if bad:
        raise Invalid("Only 'good' or 'usable' episodes can be assigned", episode_ids=bad)

    existing = dict(
        db.execute(
            select(Assignment.episode_id, Assignment.request_id).where(
                Assignment.episode_id.in_([e.id for e in episodes.values()])
            )
        ).all()
    )
    elsewhere = [i for i in ids if existing.get(episodes[i].id) not in (None, req.id)]
    if elsewhere:
        raise Conflict("Some episodes are already assigned to another request", episode_ids=elsewhere)

    already = [i for i in ids if existing.get(episodes[i].id) == req.id]
    new = [i for i in ids if episodes[i].id not in existing]
    for i in new:
        db.add(Assignment(request_id=req.id, episode_id=episodes[i].id, assigned_by_id=user.id))
    try:
        db.commit()
    except IntegrityError:
        # Lost a race with a concurrent assignment; the unique constraint is the real guard.
        db.rollback()
        raise Conflict("Some episodes were assigned to another request concurrently; please retry")
    return {
        "request_id": req.id,
        "assigned": new,
        "already_assigned": already,
        "assigned_count": assigned_counts(db, [req.id]).get(req.id, 0),
    }


def unassign_episode(db: Session, request_id: int, user: User, episode_id: str) -> None:
    req = get_visible_request(db, request_id, user, for_update=True)
    if req.status not in ASSIGNMENT_OPEN_STATUSES:
        raise Conflict(f"Episodes can only be unassigned while a request is in_progress (it is '{req.status.value}')")
    assignment = db.scalars(
        select(Assignment)
        .join(Episode, Episode.id == Assignment.episode_id)
        .where(Assignment.request_id == req.id, Episode.episode_id == episode_id.strip().upper())
    ).first()
    if assignment is None:
        raise NotFound("Episode is not assigned to this request")
    db.delete(assignment)
    db.commit()
