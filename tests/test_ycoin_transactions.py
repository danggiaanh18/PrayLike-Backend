from datetime import datetime, timedelta
import os
import sys

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from database import Base, get_db  # noqa: E402
from dependencies import get_current_userid  # noqa: E402
from ycoin.ycoin_models import YCoinTransaction  # noqa: E402
from ycoin.ycoin_router import router as ycoin_router  # noqa: E402


SQLALCHEMY_DATABASE_URL = "sqlite:///file:ycoin_mem?mode=memory&cache=shared"
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
    app.include_router(ycoin_router)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_userid] = override_current_userid
    with TestClient(app) as test_client:
        yield test_client


def seed_transactions():
    db = TestingSessionLocal()
    now = datetime.utcnow()
    db.add_all(
        [
            YCoinTransaction(
                user_id="alice",
                amount=100,
                event_type="post",
                created_at=now - timedelta(minutes=1),
            ),
            YCoinTransaction(
                user_id="alice",
                amount=20,
                event_type="amen",
                context_docid="doc1",
                created_at=now,
            ),
            YCoinTransaction(
                user_id="bob",
                amount=300,
                event_type="post",
                created_at=now,
            ),
        ]
    )
    db.commit()
    db.close()


def test_transactions_amount_matches_balance_format(client: TestClient):
    seed_transactions()
    balance_resp = client.get("/api/ycoin/balance", headers={"X-Test-User": "alice"})
    assert balance_resp.status_code == 200
    assert balance_resp.json()["balance"] == 1.2

    tx_resp = client.get("/api/ycoin/transactions", headers={"X-Test-User": "alice"})
    assert tx_resp.status_code == 200
    body = tx_resp.json()
    amounts = {item["amount"] for item in body["items"]}
    assert amounts == {1.0, 0.2}
    for item in body["items"]:
        assert isinstance(item["amount"], float)

