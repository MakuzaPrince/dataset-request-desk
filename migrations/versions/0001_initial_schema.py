"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-30
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

ENUMS = {
    "user_role": ("client", "operator", "admin"),
    "episode_quality": ("good", "usable", "bad"),
    "request_status": ("submitted", "in_progress", "delivered", "accepted", "rejected"),
}
KNOWN_ROBOTS = ("arm-01", "arm-02", "arm-03", "mobile-01", "humanoid-01")


def _is_pg() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _enum(name: str):
    if _is_pg():
        # Types are created once up front; request_status is shared by two tables.
        return postgresql.ENUM(*ENUMS[name], name=name, create_type=False)
    return sa.Enum(*ENUMS[name], name=name, native_enum=False, create_constraint=True)


def upgrade() -> None:
    if _is_pg():
        for name, values in ENUMS.items():
            postgresql.ENUM(*values, name=name).create(op.get_bind(), checkfirst=True)

    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("organisation", sa.String(255)),
        sa.Column("role", _enum("user_role"), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )

    robots = op.create_table("robots", sa.Column("id", sa.String(64), primary_key=True))
    op.bulk_insert(robots, [{"id": r} for r in KNOWN_ROBOTS])

    op.create_table(
        "import_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("filename", sa.String(255)),
        sa.Column("started_by_id", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("started_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("summary", sa.JSON),
    )

    op.create_table(
        "episodes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("episode_id", sa.String(64), nullable=False, unique=True),
        sa.Column("robot_id", sa.String(64), sa.ForeignKey("robots.id"), nullable=False),
        sa.Column("task_name", sa.String(255), nullable=False),
        sa.Column("recorded_at", sa.DateTime, nullable=False),
        sa.Column("duration_seconds", sa.Float, nullable=False),
        sa.Column("operator_name", sa.String(255)),
        sa.Column("quality", _enum("episode_quality"), nullable=False),
        sa.Column("import_run_id", sa.Integer, sa.ForeignKey("import_runs.id")),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("duration_seconds > 0", name="ck_episodes_duration_positive"),
    )
    # Analytics: per-day/robot counts scan (recorded_at, robot_id) only.
    op.create_index("ix_episodes_recorded_at_robot", "episodes", ["recorded_at", "robot_id"])
    # Analytics top tasks (quality='good', range on recorded_at) and the operator's episode filters.
    op.create_index("ix_episodes_quality_task_recorded", "episodes", ["quality", "task_name", "recorded_at"])
    op.create_index("ix_episodes_task_name", "episodes", ["task_name"])

    op.create_table(
        "requests",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("client_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("task_name", sa.String(255), nullable=False),
        sa.Column("episodes_requested", sa.Integer, nullable=False),
        sa.Column("deadline", sa.Date, nullable=False),
        sa.Column("notes", sa.Text),
        sa.Column("status", _enum("request_status"), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("episodes_requested > 0", name="ck_requests_episodes_positive"),
    )
    op.create_index("ix_requests_client_id", "requests", ["client_id"])
    op.create_index("ix_requests_status", "requests", ["status"])
    op.create_index("ix_requests_created_at", "requests", ["created_at"])

    op.create_table(
        "request_status_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("request_id", sa.Integer, sa.ForeignKey("requests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_status", _enum("request_status")),
        sa.Column("to_status", _enum("request_status"), nullable=False),
        sa.Column("actor_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("note", sa.Text),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_status_events_request_to", "request_status_events", ["request_id", "to_status"])

    op.create_table(
        "assignments",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("request_id", sa.Integer, sa.ForeignKey("requests.id", ondelete="CASCADE"), nullable=False),
        # UNIQUE: an episode can be assigned to at most one request at a time.
        sa.Column("episode_id", sa.Integer, sa.ForeignKey("episodes.id"), nullable=False, unique=True),
        sa.Column("assigned_by_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("assigned_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_assignments_request_id", "assignments", ["request_id"])


def downgrade() -> None:
    for table in ("assignments", "request_status_events", "requests", "episodes", "import_runs", "robots", "users"):
        op.drop_table(table)
    if _is_pg():
        for name in ENUMS:
            postgresql.ENUM(name=name).drop(op.get_bind(), checkfirst=True)
