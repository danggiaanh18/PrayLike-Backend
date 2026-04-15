import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
os.environ.setdefault("REFRESH_PEPPER", "test-refresh-pepper")
os.environ.setdefault("OTP_PEPPER", "test-otp-pepper")

from database import Base  # noqa: E402
import routers.ai as ai  # noqa: E402
from module.ai_service import AIChatResult  # noqa: E402
from models import AIChatSession, AIChatUsageEvent, format_as_utc, utc_now  # noqa: E402


class StubAIService:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        return AIChatResult(
            provider="stub",
            model="stub-model",
            message={"role": "assistant", "content": f"reply-{self.calls}"},
            finish_reason="stop",
            usage={"prompt_tokens": 1, "completion_tokens": 1},
        )


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

    app = FastAPI()

    def _get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[ai.get_db] = _get_db
    app.dependency_overrides[ai.get_current_userid] = lambda: "tester"

    stub = StubAIService()
    monkeypatch.setattr(ai, "_chat_service", stub)

    app.include_router(ai.router)

    client = TestClient(app)
    yield client, stub, TestingSession, engine
    Base.metadata.drop_all(bind=engine)


def test_chat_creates_session_and_history(test_app):
    client, stub, _, _ = test_app
    resp = client.post(
        "/api/ai/chat",
        json={"messages": [{"role": "user", "content": "你好"}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body["data"]
    assert data["provider"] == "stub"
    assert data["session_uuid"]
    assert data["title"]
    assert stub.calls >= 2  # main reply + title generation

    sessions_resp = client.get("/api/ai/sessions")
    assert sessions_resp.status_code == 200, sessions_resp.text
    sessions = sessions_resp.json()["data"]
    assert len(sessions) == 1

    detail_resp = client.get(f"/api/ai/sessions/{data['session_uuid']}")
    assert detail_resp.status_code == 200, detail_resp.text
    messages = detail_resp.json()["data"]["messages"]
    assert len(messages) == 2  # user + assistant
    assert messages[-1]["content"].startswith("reply-")


def test_session_timeout_blocks_chat(test_app):
    client, stub, TestingSession, engine = test_app
    create_resp = client.post(
        "/api/ai/chat",
        json={"messages": [{"role": "user", "content": "first"}]},
    )
    session_uuid = create_resp.json()["data"]["session_uuid"]

    with TestingSession() as db:
        session_obj = db.query(AIChatSession).filter_by(session_uuid=session_uuid).first()
        session_obj.last_interaction_at = session_obj.last_interaction_at - timedelta(minutes=6)
        db.add(session_obj)
        db.commit()

    resp = client.post(
        "/api/ai/chat",
        json={
            "session_uuid": session_uuid,
            "messages": [{"role": "user", "content": "after idle"}],
        },
    )
    assert resp.status_code == 409
    assert "超過 5 分鐘" in resp.json()["detail"]


def test_end_session_prevents_follow_up(test_app):
    client, stub, _, _ = test_app
    create_resp = client.post(
        "/api/ai/chat",
        json={"messages": [{"role": "user", "content": "end me"}]},
    )
    session_uuid = create_resp.json()["data"]["session_uuid"]

    end_resp = client.post(f"/api/ai/sessions/{session_uuid}/end")
    assert end_resp.status_code == 200, end_resp.text
    assert end_resp.json()["data"]["ended_at"] is not None

    follow_resp = client.post(
        "/api/ai/chat",
        json={"session_uuid": session_uuid, "messages": [{"role": "user", "content": "after end"}]},
    )
    assert follow_resp.status_code == 409
    assert "結束" in follow_resp.json()["detail"]


def test_chat_blocks_after_window_limit(test_app):
    client, stub, TestingSession, _ = test_app
    now = utc_now()
    with TestingSession() as db:
        for _ in range(ai.DAILY_CHAT_LIMIT):
            db.add(AIChatUsageEvent(user_id="tester", used_at=now - timedelta(hours=1)))
        db.commit()

    resp = client.post(
        "/api/ai/chat",
        json={"messages": [{"role": "user", "content": "over limit"}]},
    )
    assert resp.status_code == 429
    detail = resp.json()["detail"]
    assert "24 小時內" in detail
    assert str(ai.DAILY_CHAT_LIMIT) in detail


def test_chat_allows_after_oldest_usage_expires(test_app):
    client, stub, TestingSession, _ = test_app
    now = utc_now()
    with TestingSession() as db:
        for i in range(ai.DAILY_CHAT_LIMIT):
            db.add(AIChatUsageEvent(user_id="tester", used_at=now - timedelta(minutes=i)))
        db.commit()

    blocked = client.post(
        "/api/ai/chat",
        json={"messages": [{"role": "user", "content": "should block"}]},
    )
    assert blocked.status_code == 429

    with TestingSession() as db:
        oldest = (
            db.query(AIChatUsageEvent)
            .filter_by(user_id="tester")
            .order_by(AIChatUsageEvent.used_at.asc())
            .first()
        )
        oldest.used_at = oldest.used_at - timedelta(hours=25)
        db.add(oldest)
        db.commit()

    allowed = client.post(
        "/api/ai/chat",
        json={"messages": [{"role": "user", "content": "now allowed"}]},
    )
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "success"

    with TestingSession() as db:
        total_records = db.query(AIChatUsageEvent).filter_by(user_id="tester").count()
        assert total_records == ai.DAILY_CHAT_LIMIT + 1


def test_usage_endpoint_returns_counts_and_reset(test_app):
    client, _, TestingSession, _ = test_app
    now = utc_now()
    oldest = now - timedelta(hours=2)
    with TestingSession() as db:
        db.add(AIChatUsageEvent(user_id="tester", used_at=oldest))
        db.add(AIChatUsageEvent(user_id="tester", used_at=oldest + timedelta(minutes=10)))
        db.commit()

    resp = client.get("/api/ai/usage")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["used"] == 2
    assert data["limit"] == ai.DAILY_CHAT_LIMIT
    assert data["window_hours"] == ai.USAGE_WINDOW_HOURS
    expected_reset = format_as_utc(oldest + timedelta(hours=ai.USAGE_WINDOW_HOURS))
    assert data["resets_at"] == expected_reset


def test_usage_endpoint_handles_no_usage(test_app):
    client, _, _, _ = test_app
    resp = client.get("/api/ai/usage")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["used"] == 0
    assert data["resets_at"] is None
