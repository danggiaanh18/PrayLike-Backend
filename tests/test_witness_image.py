import os
import sys
from pathlib import Path
from uuid import uuid4
from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker
from starlette.middleware.sessions import SessionMiddleware

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
os.environ.setdefault("REFRESH_PEPPER", "test-refresh-pepper")
os.environ.setdefault("OTP_PEPPER", "test-otp-pepper")

from database import Base  # noqa: E402
import database  # noqa: E402
import routers.interactions as interactions  # noqa: E402
import routers.auth as auth  # noqa: E402
from models import Post, UserProfile  # noqa: E402
from routers.posts import Category, Event, Privacy  # noqa: E402


def _build_memory_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    return engine, TestingSession


@pytest.fixture
def app_and_upload_dir(monkeypatch, tmp_path):
    engine, TestingSession = _build_memory_session()
    monkeypatch.setattr(database, "SessionLocal", TestingSession)
    monkeypatch.setattr(interactions, "WITNESS_UPLOAD_DIR", tmp_path / "witnesses")
    interactions.WITNESS_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(interactions.router)
    yield app, interactions.WITNESS_UPLOAD_DIR
    Base.metadata.drop_all(bind=engine)


def _seed_profile_and_post(email: str, user_id: str = "tester", post_userid: Optional[str] = None) -> str:
    db = database.SessionLocal()
    try:
        profile = UserProfile(
            email=email,
            name="Tester",
            user_id=user_id,
        )
        parent_docid = str(uuid4())
        post = Post(
            docid=parent_docid,
            userid=post_userid or user_id,
            content="target post",
            event=Event.POST.value,
            category=Category.PERSONAL_FAMILY.value,
            privacy=Privacy.PUBLIC.value,
        )
        db.add(profile)
        db.add(post)
        db.commit()
        return parent_docid
    finally:
        db.close()


def test_create_witness_with_image_upload(app_and_upload_dir):
    app, upload_dir = app_and_upload_dir
    email = "witness@example.com"
    parent_docid = _seed_profile_and_post(email)
    token = auth.sign_access_jwt(email)
    client = TestClient(app)

    resp = client.post(
        "/api/witness",
        headers={"Authorization": f"Bearer {token}"},
        data={"content": "my witness", "parent_docid": parent_docid},
        files={"file": ("photo.png", b"binarydata", "image/png")},
    )
    assert resp.status_code == 200, resp.text
    witness = resp.json()["data"]["witness"]
    assert witness["image_url"]
    assert Path(witness["image_url"]).exists()
    assert str(upload_dir) in witness["image_url"]


def test_witness_image_over_limit_rejected(app_and_upload_dir):
    app, _ = app_and_upload_dir
    email = "witness2@example.com"
    parent_docid = _seed_profile_and_post(email)
    token = auth.sign_access_jwt(email)
    client = TestClient(app)

    oversized = b"0" * (interactions.MAX_WITNESS_IMAGE_BYTES + 1)
    resp = client.post(
        "/api/witness",
        headers={"Authorization": f"Bearer {token}"},
        data={"content": "too big", "parent_docid": parent_docid},
        files={"file": ("big.bin", oversized, "application/octet-stream")},
    )
    assert resp.status_code == 413


def test_witness_must_match_post_owner(app_and_upload_dir):
    app, _ = app_and_upload_dir
    email = "intruder@example.com"
    parent_docid = _seed_profile_and_post(email, user_id="viewer", post_userid="owner123")
    token = auth.sign_access_jwt(email)
    client = TestClient(app)

    resp = client.post(
        "/api/witness",
        headers={"Authorization": f"Bearer {token}"},
        data={"content": "should fail", "parent_docid": parent_docid},
    )
    assert resp.status_code == 403


def test_cannot_witness_same_post_twice(app_and_upload_dir):
    app, _ = app_and_upload_dir
    email = "repeat@example.com"
    parent_docid = _seed_profile_and_post(email, user_id="owner")
    token = auth.sign_access_jwt(email)
    client = TestClient(app)

    first = client.post(
        "/api/witness",
        headers={"Authorization": f"Bearer {token}"},
        data={"content": "first", "parent_docid": parent_docid},
    )
    assert first.status_code == 200, first.text

    second = client.post(
        "/api/witness",
        headers={"Authorization": f"Bearer {token}"},
        data={"content": "second", "parent_docid": parent_docid},
    )
    assert second.status_code == 400


def test_witness_list_returns_original_post(app_and_upload_dir):
    app, _ = app_and_upload_dir
    email = "list@example.com"
    parent_docid = _seed_profile_and_post(email, user_id="owner")
    token = auth.sign_access_jwt(email)
    client = TestClient(app)

    created = client.post(
        "/api/witness",
        headers={"Authorization": f"Bearer {token}"},
        data={"content": "list witness", "parent_docid": parent_docid},
    )
    assert created.status_code == 200, created.text

    resp = client.get("/api/witness-list")
    assert resp.status_code == 200, resp.text
    payload = resp.json()["data"]
    assert payload["pageinfo"]["total_items"] == 1
    witness_entry = payload["witnesses"][0]
    assert witness_entry["original_post"]["docid"] == parent_docid
    assert witness_entry["content"] == "list witness"
