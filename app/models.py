"""ORM models. All timestamps are stored as naive UTC."""
import enum
from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Role(str, enum.Enum):
    client = "client"
    operator = "operator"
    admin = "admin"


class Quality(str, enum.Enum):
    good = "good"
    usable = "usable"
    bad = "bad"


class RequestStatus(str, enum.Enum):
    submitted = "submitted"
    in_progress = "in_progress"
    delivered = "delivered"
    accepted = "accepted"
    rejected = "rejected"


def _enum(cls: type[enum.Enum], name: str) -> Enum:
    # Store the enum *value* (lower-case string), not the Python member name.
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e], validate_strings=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    organisation: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(_enum(Role, "user_role"))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Robot(Base):
    __tablename__ = "robots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)


class ImportRun(Base):
    __tablename__ = "import_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str | None] = mapped_column(String(255))
    started_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    summary: Mapped[dict | None] = mapped_column(JSON)


class Episode(Base):
    __tablename__ = "episodes"
    __table_args__ = (
        CheckConstraint("duration_seconds > 0", name="ck_episodes_duration_positive"),
        Index("ix_episodes_recorded_at_robot", "recorded_at", "robot_id"),
        Index("ix_episodes_quality_task_recorded", "quality", "task_name", "recorded_at"),
        Index("ix_episodes_task_name", "task_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    episode_id: Mapped[str] = mapped_column(String(64), unique=True)
    robot_id: Mapped[str] = mapped_column(ForeignKey("robots.id"))
    task_name: Mapped[str] = mapped_column(String(255))
    recorded_at: Mapped[datetime] = mapped_column(DateTime)
    duration_seconds: Mapped[float] = mapped_column(Float)
    operator_name: Mapped[str | None] = mapped_column(String(255))
    quality: Mapped[Quality] = mapped_column(_enum(Quality, "episode_quality"))
    import_run_id: Mapped[int | None] = mapped_column(ForeignKey("import_runs.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    assignment: Mapped["Assignment | None"] = relationship(back_populates="episode", uselist=False)


class DatasetRequest(Base):
    __tablename__ = "requests"
    __table_args__ = (
        CheckConstraint("episodes_requested > 0", name="ck_requests_episodes_positive"),
        Index("ix_requests_client_id", "client_id"),
        Index("ix_requests_status", "status"),
        Index("ix_requests_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    task_name: Mapped[str] = mapped_column(String(255))
    episodes_requested: Mapped[int] = mapped_column(Integer)
    deadline: Mapped[date] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[RequestStatus] = mapped_column(_enum(RequestStatus, "request_status"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    client: Mapped[User] = relationship()
    events: Mapped[list["StatusEvent"]] = relationship(
        back_populates="request", order_by="StatusEvent.id", cascade="all, delete-orphan"
    )


class StatusEvent(Base):
    """Append-only audit log of every status change, including creation (from_status NULL)."""

    __tablename__ = "request_status_events"
    __table_args__ = (Index("ix_status_events_request_to", "request_id", "to_status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id", ondelete="CASCADE"))
    from_status: Mapped[RequestStatus | None] = mapped_column(_enum(RequestStatus, "request_status"))
    to_status: Mapped[RequestStatus] = mapped_column(_enum(RequestStatus, "request_status"))
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    request: Mapped[DatasetRequest] = relationship(back_populates="events")
    actor: Mapped[User] = relationship()


class Assignment(Base):
    __tablename__ = "assignments"
    __table_args__ = (Index("ix_assignments_request_id", "request_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("requests.id", ondelete="CASCADE"))
    # unique => an episode belongs to at most one request at a time, enforced by the database.
    episode_id: Mapped[int] = mapped_column(ForeignKey("episodes.id"), unique=True)
    assigned_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    assigned_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    episode: Mapped[Episode] = relationship(back_populates="assignment")
