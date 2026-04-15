import os
import sys
from pathlib import Path

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
from module import friend  # noqa: E402
import routers.notifications as notifications  # noqa: E402
from dependencies import get_current_userid  # noqa: E402
from models import UserProfile  # noqa: E402


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
    app.include_router(friend.router)
    app.include_router(notifications.router)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_userid] = override_current_userid
    with TestClient(app) as test_client:
        yield test_client


def seed_profiles(db, profiles):
    db.add_all(
        [
            UserProfile(email=f"{user_id}@example.com", name=user_id, user_id=user_id, tribe=None)
            for user_id in profiles
        ]
    )
    db.commit()


def test_friend_request_triggers_notification(client: TestClient):
    db = database.SessionLocal()
    seed_profiles(db, ["alice", "bob"])
    db.close()

    resp = client.post(
        "/api/friends/invite",
        headers={"X-Test-User": "alice"},
        json={"target_user_id": "bob"},
    )
    assert resp.status_code == 200, resp.text
    invite_id = resp.json()["data"]["invitation"]["id"]

    bob_notes = client.get("/api/notifications", headers={"X-Test-User": "bob"})
    assert bob_notes.status_code == 200
    data = bob_notes.json()["data"]
    assert data["total"] == 1
    note = data["notifications"][0]
    assert note["notification_type"] == "friend_request_received"
    assert note["actor_user_id"] == "alice"
    assert note["post_docid"] == str(invite_id)

    alice_notes = client.get("/api/notifications", headers={"X-Test-User": "alice"})
    assert alice_notes.status_code == 200
    assert alice_notes.json()["data"]["total"] == 0


def test_accept_invite_notifies_requester(client: TestClient):
    db = database.SessionLocal()
    seed_profiles(db, ["carol", "dave"])
    db.close()

    invite = client.post(
        "/api/friends/invite",
        headers={"X-Test-User": "carol"},
        json={"target_user_id": "dave"},
    )
    invite_id = invite.json()["data"]["invitation"]["id"]

    accept = client.post(
        f"/api/friends/{invite_id}/respond",
        headers={"X-Test-User": "dave"},
        json={"accept": True},
    )
    assert accept.status_code == 200, accept.text

    carol_notes = client.get("/api/notifications", headers={"X-Test-User": "carol"})
    assert carol_notes.status_code == 200
    data = carol_notes.json()["data"]
    assert data["total"] == 1
    note = data["notifications"][0]
    assert note["notification_type"] == "friend_request_accepted"
    assert note["actor_user_id"] == "dave"
    assert note["post_docid"] == str(invite_id)


def test_reverse_pending_auto_accept_sends_accept_notification(client: TestClient):
    db = database.SessionLocal()
    seed_profiles(db, ["erin", "frank"])
    db.close()

    first = client.post(
        "/api/friends/invite",
        headers={"X-Test-User": "erin"},
        json={"target_user_id": "frank"},
    )
    first_id = first.json()["data"]["invitation"]["id"]

    # frank sends invite back -> auto accept
    second = client.post(
        "/api/friends/invite",
        headers={"X-Test-User": "frank"},
        json={"target_user_id": "erin"},
    )
    assert second.status_code == 200, second.text

    # frank should already have received request notification earlier
    frank_notes = client.get("/api/notifications", headers={"X-Test-User": "frank"})
    assert frank_notes.status_code == 200
    assert frank_notes.json()["data"]["total"] == 1

    # erin should now receive acceptance notification
    erin_notes = client.get("/api/notifications", headers={"X-Test-User": "erin"})
    assert erin_notes.status_code == 200
    data = erin_notes.json()["data"]
    assert data["total"] == 1
    note = data["notifications"][0]
    assert note["notification_type"] == "friend_request_accepted"
    assert note["actor_user_id"] == "frank"
    assert note["post_docid"] == str(first_id)
