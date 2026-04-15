from datetime import date
import os
import sys

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from database import Base, get_db
from dependencies import get_current_userid
from models import UserProfile
from routers.activities import router as activities_router


SQLALCHEMY_DATABASE_URL = "sqlite:///file:memdb1?mode=memory&cache=shared"
engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False, "uri": True}
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
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
def setup_database():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(activities_router)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_userid] = override_current_userid
    with TestClient(app) as test_client:
        yield test_client


def seed_profiles():
    db = TestingSessionLocal()
    db.add_all(
        [
            UserProfile(
                email="alice@example.com", name="Alice", user_id="alice", tribe=1
            ),
            UserProfile(
                email="bob@example.com", name="Bob", user_id="bob", tribe=1
            ),
            UserProfile(
                email="carl@example.com", name="Carl", user_id="carl", tribe=2
            ),
        ]
    )
    db.commit()
    db.close()


def create_activity(client: TestClient, user_id: str, title: str):
    payload = {
        "title": title,
        "location": "HQ",
        "event_date": date.today().isoformat(),
        "event_time": "09:00",
        "description": f"{title} description",
    }
    response = client.post(
        "/api/activities", headers={"X-Test-User": user_id}, data=payload
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_same_tribe_members_see_each_other(client: TestClient):
    seed_profiles()
    first = create_activity(client, "alice", "Alice activity")
    second = create_activity(client, "bob", "Bob activity")

    response = client.get("/api/activities", headers={"X-Test-User": "alice"})
    assert response.status_code == 200
    activities = response.json()["data"]["activities"]
    creators = {item["created_by"] for item in activities}

    assert creators == {first["created_by"], second["created_by"]}
