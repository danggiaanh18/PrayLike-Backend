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
import routers.posts as posts  # noqa: E402
import routers.notifications as notifications  # noqa: E402
import routers.interactions as interactions  # noqa: E402
from dependencies import get_current_userid  # noqa: E402
from models import UserFriend, UserProfile  # noqa: E402
import ycoin.ycoin_models  # noqa: E402
from routers.posts import Category, Privacy  # noqa: E402


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
    app.include_router(posts.router)
    app.include_router(notifications.router)
    app.include_router(interactions.router)
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


def seed_friendship(db, user_a: str, user_b: str):
    pair = sorted([user_a, user_b])
    db.add(UserFriend(user_id=pair[0], friend_user_id=pair[1]))
    db.commit()


def create_post(client: TestClient, user_id: str, privacy: Privacy = Privacy.PUBLIC):
    payload = {
        "content": f"hello from {user_id}",
        "title": f"{user_id} title",
        "category": Category.PERSONAL_FAMILY.value,
        "privacy": privacy.value,
    }
    resp = client.post("/api/posts", headers={"X-Test-User": user_id}, json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["docid"]


def test_friend_receives_notification_on_post(client: TestClient):
    db = database.SessionLocal()
    seed_profiles(
        db,
        [
            ("alice@example.com", "alice", 1),
            ("bob@example.com", "bob", None),
            ("charlie@example.com", "charlie", 1),
        ],
    )
    seed_friendship(db, "alice", "bob")
    db.close()

    docid = create_post(client, "alice", privacy=Privacy.FAMILY)

    bob_view = client.get("/api/notifications", headers={"X-Test-User": "bob"})
    assert bob_view.status_code == 200, bob_view.text
    payload = bob_view.json()["data"]
    assert payload["total"] == 1
    note = payload["notifications"][0]
    assert note["post_docid"] == docid
    assert note["reason"] == "friend"
    assert note["is_read"] is False

    # 非朋友且未開啟支派通知者不會收到
    charlie_view = client.get(
        "/api/notifications", headers={"X-Test-User": "charlie"}
    )
    assert charlie_view.status_code == 200
    assert charlie_view.json()["data"]["total"] == 0


def test_same_tribe_member_gets_notification_and_can_mark_read(client: TestClient):
    db = database.SessionLocal()
    seed_profiles(
        db,
        [
            ("dan@example.com", "dan", 3),
            ("erin@example.com", "erin", 3),
            ("frank@example.com", "frank", 4),
        ],
    )
    db.close()

    docid = create_post(client, "dan", privacy=Privacy.TRIBE)

    erin_view = client.get("/api/notifications", headers={"X-Test-User": "erin"})
    assert erin_view.status_code == 200
    data = erin_view.json()["data"]
    assert data["unread_count"] == 1
    note = data["notifications"][0]
    assert note["post_docid"] == docid
    assert note["reason"] == "tribe"

    mark_resp = client.post(
        f"/api/notifications/{note['id']}/read", headers={"X-Test-User": "erin"}
    )
    assert mark_resp.status_code == 200
    updated = mark_resp.json()["data"]
    assert updated["is_read"] is True
    assert updated["read_at"] is not None

    # 再查詢時未讀數應為 0
    refreshed = client.get("/api/notifications", headers={"X-Test-User": "erin"})
    assert refreshed.status_code == 200
    assert refreshed.json()["data"]["unread_count"] == 0

    # 不同支派不會收到通知
    frank_view = client.get("/api/notifications", headers={"X-Test-User": "frank"})
    assert frank_view.status_code == 200
    assert frank_view.json()["data"]["total"] == 0


def test_comment_notification_sent_to_owner(client: TestClient):
    db = database.SessionLocal()
    seed_profiles(
        db,
        [
            ("owner@example.com", "owner", 1),
            ("guest@example.com", "guest", 2),
        ],
    )
    db.close()

    docid = create_post(client, "owner", privacy=Privacy.PUBLIC)

    resp = client.post(
        "/api/comment",
        headers={"X-Test-User": "guest"},
        json={"content": "hi owner", "docid": docid},
    )
    assert resp.status_code == 200, resp.text

    owner_notes = client.get("/api/notifications", headers={"X-Test-User": "owner"})
    assert owner_notes.status_code == 200
    data = owner_notes.json()["data"]
    assert data["total"] == 1
    note = data["notifications"][0]
    assert note["notification_type"] == "commented_on_post"
    assert note["reason"] == "comment"
    assert note["actor_user_id"] == "guest"
    assert note["post_title"] == "owner title"
    assert "hello from owner" in note["post_preview"]

    # 自己留言不應產生通知
    client.post(
        "/api/comment",
        headers={"X-Test-User": "owner"},
        json={"content": "self comment", "docid": docid},
    )
    refreshed = client.get("/api/notifications", headers={"X-Test-User": "owner"})
    assert refreshed.json()["data"]["total"] == 1


def test_amen_notification_sent_to_owner(client: TestClient):
    db = database.SessionLocal()
    seed_profiles(
        db,
        [
            ("poster@example.com", "poster", None),
            ("fan@example.com", "fan", None),
        ],
    )
    db.close()

    docid = create_post(client, "poster", privacy=Privacy.PUBLIC)

    amen_resp = client.post(
        f"/api/posts/{docid}/amen", headers={"X-Test-User": "fan"}
    )
    assert amen_resp.status_code == 200, amen_resp.text

    owner_view = client.get("/api/notifications", headers={"X-Test-User": "poster"})
    assert owner_view.status_code == 200
    payload = owner_view.json()["data"]
    assert payload["total"] == 1
    note = payload["notifications"][0]
    assert note["notification_type"] == "amened_post"
    assert note["reason"] == "amen"
    assert note["actor_user_id"] == "fan"
    assert note["post_title"] == "poster title"
    assert "hello from poster" in note["post_preview"]

    # 自己 Amen 自己不發通知
    client.post(f"/api/posts/{docid}/amen", headers={"X-Test-User": "poster"})
    refreshed = client.get("/api/notifications", headers={"X-Test-User": "poster"})
    assert refreshed.status_code == 200
    assert refreshed.json()["data"]["total"] == 1
