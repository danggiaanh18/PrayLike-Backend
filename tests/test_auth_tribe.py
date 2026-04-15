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
from models import UserProfile  # noqa: E402
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


def test_select_tribe_with_bearer_token(test_app):
    db = auth.SessionLocal()
    try:
        profile = UserProfile(
            email="user@example.com",
            name="Tester",
            user_id="tester",
            tribe=None,
        )
        db.add(profile)
        db.commit()
    finally:
        db.close()

    token = auth.sign_access_jwt("user@example.com")

    client = TestClient(test_app)
    resp = client.post(
        "/auth/profile/tribe",
        headers={"Authorization": f"Bearer {token}"},
        json={"tribe": 3},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["profile"]["tribe"] == 3
