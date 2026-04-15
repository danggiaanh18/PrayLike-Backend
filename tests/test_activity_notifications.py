import os
import sys
from pathlib import Path
from typing import Iterable, Tuple

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
os.environ.setdefault("REFRESH_PEPPER", "test-refresh-pepper")
os.environ.setdefault("OTP_PEPPER", "test-otp-pepper")

from database import Base, get_db  # noqa: E402
import database  # noqa: E402
import routers.activities as activities  # noqa: E402
import routers.notifications as notifications  # noqa: E402
from dependencies import get_current_userid  # noqa: E402
from models import UserProfile  # noqa: E402
import ycoin.ycoin_models  # noqa: E402


def _build_memory_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    return engine, TestingSession


def override_get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()


def override_current_userid(request: Request):
    user_id = request.headers.get("X-Test-User")
    if not user_id:
        raise HTTPException(status_code=401, detail="missing test user header")
    return user_id


@pytest.fixture(autouse=True)
def setup_database(monkeypatch):
    engine, TestingSession = _build_memory_session()
    monkeypatch.setattr(database, "SessionLocal", TestingSession)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(activities.router)
    app.include_router(notifications.router)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_userid] = override_current_userid
    with TestClient(app) as test_client:
        yield test_client


def seed_profiles(db, profiles: Iterable[Tuple[str, str, int | None]]):
    db.add_all(
        [
            UserProfile(email=email, name=user_id, user_id=user_id, tribe=tribe)
            for email, user_id, tribe in profiles
        ]
    )
    db.commit()


def test_same_tribe_receives_activity_notification_on_create(client: TestClient):
    db = database.SessionLocal()
    seed_profiles(
        db,
        [
            ("creator@example.com", "creator", 1),
            ("ally@example.com", "ally", 1),
            ("outsider@example.com", "outsider", 2),
        ],
    )
    db.close()

    payload = {
        "title": "Prayer Walk",
        "location": "City Park",
        "event_date": "2026-02-01",
        "event_time": "09:30",
        "description": "Tribe gathering",
    }
    resp = client.post(
        "/api/activities",
        headers={"X-Test-User": "creator"},
        data=payload,
    )
    assert resp.status_code == 200, resp.text

    ally_notes = client.get("/api/notifications", headers={"X-Test-User": "ally"})
    assert ally_notes.status_code == 200
    data = ally_notes.json()["data"]
    assert data["total"] == 1
    note = data["notifications"][0]
    assert note["notification_type"] == "tribe_activity_published"
    assert note["reason"] == "tribe_activity"
    assert note["actor_user_id"] == "creator"

    # creator 自己不應收到
    creator_notes = client.get("/api/notifications", headers={"X-Test-User": "creator"})
    assert creator_notes.status_code == 200
    assert creator_notes.json()["data"]["total"] == 0

    # 其他支派不會收到
    outsider_notes = client.get(
        "/api/notifications", headers={"X-Test-User": "outsider"}
    )
    assert outsider_notes.status_code == 200
    assert outsider_notes.json()["data"]["total"] == 0
