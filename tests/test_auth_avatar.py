import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
os.environ.setdefault("REFRESH_PEPPER", "test-refresh-pepper")
os.environ.setdefault("OTP_PEPPER", "test-otp-pepper")

from database import Base  # noqa: E402
import routers.auth as auth  # noqa: E402


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
def test_app(monkeypatch):
    engine, TestingSession = _build_memory_session()
    monkeypatch.setattr(auth, "SessionLocal", TestingSession)
    app = FastAPI()
    auth.register_auth(app)
    yield app
    Base.metadata.drop_all(bind=engine)


def test_upsert_profile_accepts_avatar_url(test_app):
    client = TestClient(test_app)
    token = auth.sign_access_jwt("user@example.com")
    resp = client.post(
        "/auth/profile",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "Tester",
            "user_id": "tester",
            "avatar_url": "https://example.com/avatar.png",
        },
    )
    assert resp.status_code == 200, resp.text
    profile = resp.json()["profile"]
    assert profile["avatar_url"] == "https://example.com/avatar.png"


def test_update_avatar_uploads_file(monkeypatch, tmp_path, test_app):
    upload_dir = tmp_path / "avatars"
    upload_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(auth, "AVATAR_UPLOAD_DIR", upload_dir)

    client = TestClient(test_app)
    token = auth.sign_access_jwt("user2@example.com")

    create_resp = client.post(
        "/auth/profile",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": "Uploader", "user_id": "uploader"},
    )
    assert create_resp.status_code == 200, create_resp.text

    resp = client.post(
        "/auth/profile/avatar",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("avatar.png", b"binarydata", "image/png")},
    )
    assert resp.status_code == 200, resp.text
    profile = resp.json()["profile"]
    assert profile["avatar_url"].startswith(str(upload_dir))
    assert Path(profile["avatar_url"]).exists()
