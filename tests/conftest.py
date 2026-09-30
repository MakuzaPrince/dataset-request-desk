"""Test fixtures.

Tests run against SQLite by default. Set TEST_DATABASE_URL to a PostgreSQL URL to run the
same suite against Postgres (CI does both). The schema is built with the real Alembic
migrations, so migrations are exercised on every run.
"""
import os
from datetime import date, datetime, timedelta
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-long-enough-for-hs256")
os.environ.setdefault("BCRYPT_ROUNDS", "4")

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.db import get_db, make_engine
from app.main import app
from app.models import Assignment, DatasetRequest, Episode, Quality, RequestStatus, Role, StatusEvent, User
from app.security import create_token, hash_password

ROOT = Path(__file__).resolve().parent.parent
SEED_CSV = ROOT / "seed" / "episodes.csv"
PASSWORD = "password123"
TABLES = ("assignments", "request_status_events", "requests", "episodes", "import_runs", "users")


@pytest.fixture(scope="session")
def engine(tmp_path_factory):
    url = os.getenv("TEST_DATABASE_URL") or f"sqlite:///{tmp_path_factory.mktemp('db') / 'test.db'}"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["database_url"] = url
    cfg.attributes["configure_logger"] = False
    if url.startswith("postgresql"):
        command.downgrade(cfg, "base")  # start from an empty schema on a reused database
    command.upgrade(cfg, "head")
    eng = make_engine(url)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    yield session
    session.close()
    # Wipe everything except the robots reference table between tests.
    with engine.begin() as conn:
        for table in TABLES:
            conn.execute(text(f"DELETE FROM {table}"))


@pytest.fixture
def client(engine, db):
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def make_user(db, email: str, role: Role, name: str | None = None, active: bool = True) -> User:
    user = User(
        email=email, name=name or email.split("@")[0], role=role, organisation=None,
        password_hash=hash_password(PASSWORD), is_active=active,
    )
    db.add(user)
    db.commit()
    return user


def auth(user: User) -> dict:
    return {"Authorization": f"Bearer {create_token(user.id)}"}


@pytest.fixture
def users(db):
    return {
        "admin": make_user(db, "admin@test.io", Role.admin),
        "ops": make_user(db, "ops@test.io", Role.operator),
        "client_a": make_user(db, "a@test.io", Role.client),
        "client_b": make_user(db, "b@test.io", Role.client),
    }


@pytest.fixture
def h(users):
    """Auth headers per role."""
    return {k: auth(u) for k, u in users.items()}


_counter = iter(range(1, 10**9))


def make_episode(db, quality: Quality = Quality.good, task: str = "pick cup", robot: str = "arm-01",
                 recorded_at: datetime | None = None, episode_id: str | None = None) -> Episode:
    ep = Episode(
        episode_id=episode_id or f"T-{next(_counter):06d}",
        robot_id=robot,
        task_name=task,
        recorded_at=recorded_at or datetime(2026, 8, 1, 12, 0),
        duration_seconds=30,
        operator_name="Tester",
        quality=quality,
    )
    db.add(ep)
    db.commit()
    return ep


def make_request(db, client: User, status: RequestStatus = RequestStatus.submitted, n: int = 2,
                 task: str = "pick cup", created_at: datetime | None = None) -> DatasetRequest:
    req = DatasetRequest(
        client_id=client.id, task_name=task, episodes_requested=n,
        deadline=date.today() + timedelta(days=30), status=status,
    )
    if created_at:
        req.created_at = created_at
    db.add(req)
    db.flush()
    db.add(StatusEvent(request_id=req.id, from_status=None, to_status=RequestStatus.submitted,
                       actor_id=client.id, created_at=req.created_at))
    db.commit()
    return req


def assign_directly(db, req: DatasetRequest, episodes: list[Episode], by: User) -> None:
    for ep in episodes:
        db.add(Assignment(request_id=req.id, episode_id=ep.id, assigned_by_id=by.id))
    db.commit()
