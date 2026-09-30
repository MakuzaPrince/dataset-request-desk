import re
from datetime import date, datetime, timezone
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import Quality, RequestStatus, Role

T = TypeVar("T")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_task_name(value: str) -> str:
    """Canonical task name: trimmed, lower-case, single spaces. Shared by requests and the importer."""
    return " ".join(value.split()).lower()


class Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True, str_strip_whitespace=True)


class Page(Schema, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


# --- auth / users ---------------------------------------------------------


class LoginIn(Schema):
    email: str = Field(max_length=255)
    password: str = Field(max_length=256)


class UserOut(Schema):
    id: int
    email: str
    name: str
    organisation: str | None
    role: Role
    is_active: bool


class TokenOut(Schema):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class UserCreate(Schema):
    email: str = Field(max_length=255)
    name: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=8, max_length=256)
    role: Role
    organisation: str | None = Field(default=None, max_length=255)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.lower()
        if not _EMAIL_RE.match(v):
            raise ValueError("invalid email address")
        return v


class UserUpdate(Schema):
    role: Role | None = None
    is_active: bool | None = None
    name: str | None = Field(default=None, min_length=1, max_length=255)


# --- requests ---------------------------------------------------------------


class RequestCreate(Schema):
    task_name: str = Field(min_length=1, max_length=255)
    episodes_requested: int = Field(gt=0, le=1_000_000)
    deadline: date
    notes: str | None = Field(default=None, max_length=5000)

    @field_validator("task_name")
    @classmethod
    def _task(cls, v: str) -> str:
        return normalize_task_name(v)

    @field_validator("deadline")
    @classmethod
    def _deadline(cls, v: date) -> date:
        if v < datetime.now(timezone.utc).date():
            raise ValueError("deadline cannot be in the past")
        return v


class StatusEventOut(Schema):
    id: int
    from_status: RequestStatus | None
    to_status: RequestStatus
    actor_id: int
    actor_name: str
    note: str | None
    created_at: datetime


class RequestOut(Schema):
    id: int
    client_id: int
    client_name: str
    task_name: str
    episodes_requested: int
    deadline: date
    notes: str | None
    status: RequestStatus
    created_at: datetime
    updated_at: datetime
    assigned_count: int
    allowed_transitions: list[RequestStatus]


class RequestDetailOut(RequestOut):
    events: list[StatusEventOut]


class TransitionIn(Schema):
    to_status: RequestStatus
    note: str | None = Field(default=None, max_length=2000)


# --- episodes / assignments ---------------------------------------------------


class EpisodeOut(Schema):
    episode_id: str
    robot_id: str
    task_name: str
    recorded_at: datetime
    duration_seconds: float
    operator_name: str | None
    quality: Quality
    assigned_request_id: int | None = None


class AssignIn(Schema):
    episode_ids: list[str] = Field(min_length=1, max_length=1000)


class AssignOut(Schema):
    request_id: int
    assigned: list[str]
    already_assigned: list[str]
    assigned_count: int
