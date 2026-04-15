"""Authentication helpers for JWT tokens, refresh rotation, and auth cookies."""

import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from uuid import uuid4

import jwt
from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String
from sqlalchemy.orm import Session

from config import settings
from database import Base

# -------- Route A (JWT + Refresh Token) settings --------
ACCESS_TOKEN_MINUTES = settings.access_token_minutes  # 0 代表不設定 exp
REFRESH_TOKEN_DAYS = settings.refresh_token_days  # 0 代表不過期
SESSION_PERSISTENT_DAYS = settings.session_persistent_days
COOKIE_ACCESS_NAME = settings.cookie_access_name
COOKIE_REFRESH_NAME = settings.cookie_refresh_name
COOKIE_DOMAIN = settings.cookie_domain  # 可選，跨子網域時設定
REFRESH_PEPPER = settings.refresh_pepper.encode("utf-8")

EXTERNAL_BASE_URL = settings.base_url
JWT_SECRET = settings.jwt_secret
JWT_ISSUER = settings.jwt_issuer
JWT_EXPIRE_MINUTES = settings.jwt_expire_minutes


def resolve_session_settings(external_url: Optional[str]) -> dict:
    settings: dict = {"same_site": "lax", "https_only": False}
    if external_url and external_url.lower().startswith("https://"):
        settings.update({"same_site": "none", "https_only": True})
    return settings


session_settings = resolve_session_settings(EXTERNAL_BASE_URL)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _hash_refresh(raw: str) -> str:
    # 將 refresh token 以 HMAC-SHA256(pepper, raw) 儲存，避免明碼落地
    return hmac.new(REFRESH_PEPPER, raw.encode("utf-8"), hashlib.sha256).hexdigest()


def _persistent_max_age_seconds() -> Optional[int]:
    if SESSION_PERSISTENT_DAYS <= 0:
        return None
    return SESSION_PERSISTENT_DAYS * 24 * 3600


def _resolve_access_cookie_max_age() -> Optional[int]:
    if ACCESS_TOKEN_MINUTES > 0:
        return ACCESS_TOKEN_MINUTES * 60
    return _persistent_max_age_seconds()


def _resolve_refresh_cookie_max_age() -> Optional[int]:
    if REFRESH_TOKEN_DAYS > 0:
        return REFRESH_TOKEN_DAYS * 24 * 3600
    return _persistent_max_age_seconds()


def _cookie_common_kwargs():
    kwargs = {
        "httponly": True,
        "secure": session_settings.get("https_only", False),
        "samesite": session_settings.get("same_site", "lax"),
        "path": "/",
    }
    if COOKIE_DOMAIN:
        kwargs["domain"] = COOKIE_DOMAIN
    return kwargs


def set_access_cookie(resp: Response, token: str):
    resp.set_cookie(
        key=COOKIE_ACCESS_NAME,
        value=token,
        max_age=_resolve_access_cookie_max_age(),
        **_cookie_common_kwargs(),
    )


def set_refresh_cookie(resp: Response, token: str):
    resp.set_cookie(
        key=COOKIE_REFRESH_NAME,
        value=token,
        max_age=_resolve_refresh_cookie_max_age(),
        **_cookie_common_kwargs(),
    )


def clear_auth_cookies(resp: Response):
    for k in (COOKIE_ACCESS_NAME, COOKIE_REFRESH_NAME):
        resp.delete_cookie(key=k, path="/", domain=COOKIE_DOMAIN)


#
# -------------------- Refresh Token DB Model --------------------
class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id = Column(Integer, primary_key=True, autoincrement=True)
    subject = Column(String(320), nullable=False)  # email 或 provider sub
    jti = Column(String(64), nullable=False)  # 與 access token 對應的 jti
    token_hash = Column(String(64), nullable=False, unique=True)
    issued_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked = Column(Boolean, default=False, nullable=False)
    meta = Column(String(255), nullable=True)  # 可放 UA/IP 等

    __table_args__ = (
        Index("ix_refresh_subject", "subject"),
        Index("ix_refresh_expires_at", "expires_at"),
    )


# -------------------- JWT (access) 與 Refresh 發行/驗證 --------------------
def sign_access_jwt(sub: str, jti: Optional[str] = None) -> str:
    now = _now_utc()
    payload = {
        "iss": JWT_ISSUER,
        "sub": sub,
        "auth": "app",
        "iat": int(now.timestamp()),
        "jti": jti or uuid4().hex,
    }
    if ACCESS_TOKEN_MINUTES > 0:
        payload["exp"] = int((now + timedelta(minutes=ACCESS_TOKEN_MINUTES)).timestamp())
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def _new_refresh_record(db: Session, subject: str) -> Tuple[str, RefreshToken]:
    raw = uuid4().hex + uuid4().hex  # 64 hex chars
    now = _now_utc()
    expires_at = (
        now + timedelta(days=REFRESH_TOKEN_DAYS)
        if REFRESH_TOKEN_DAYS > 0
        else datetime.max.replace(tzinfo=timezone.utc)
    )
    rec = RefreshToken(
        subject=subject,
        jti=uuid4().hex,
        token_hash=_hash_refresh(raw),
        issued_at=now,
        expires_at=expires_at,
        revoked=False,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return raw, rec


def mint_token_pair(db: Session, subject: str) -> Tuple[str, str]:
    # 回傳 (access_token, refresh_token)
    at = sign_access_jwt(subject)
    rt_raw, _ = _new_refresh_record(db, subject)
    return at, rt_raw


def verify_and_rotate_refresh(
    db: Session, rt_raw: str, subject_hint: Optional[str] = None
) -> Tuple[str, str]:
    h = _hash_refresh(rt_raw)
    rec: Optional[RefreshToken] = db.query(RefreshToken).filter_by(token_hash=h).one_or_none()
    now = _now_utc()
    if not rec or rec.revoked:
        raise HTTPException(status_code=401, detail={"error": "invalid_refresh"})
    expires_at = rec.expires_at
    if not isinstance(expires_at, datetime):
        raise HTTPException(status_code=401, detail={"error": "invalid_refresh"})
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        raise HTTPException(status_code=401, detail={"error": "invalid_refresh"})
    if subject_hint and rec.subject != subject_hint:
        # 釣魚防護：subject 不一致時拒絕
        raise HTTPException(status_code=401, detail={"error": "mismatched_subject"})
    # 旋轉：撤銷舊的，發新的
    rec.revoked = True
    db.add(rec)
    db.commit()
    new_at = sign_access_jwt(rec.subject)
    new_rt_raw, _ = _new_refresh_record(db, rec.subject)
    return new_at, new_rt_raw


def revoke_refresh_by_raw(db: Session, rt_raw: Optional[str]):
    if not rt_raw:
        return
    h = _hash_refresh(rt_raw)
    rec: Optional[RefreshToken] = db.query(RefreshToken).filter_by(token_hash=h).one_or_none()
    if rec and not rec.revoked:
        rec.revoked = True
        db.add(rec)
        db.commit()


bearer = HTTPBearer(auto_error=False)


def get_current_subject(
    request: Request, cred: Optional[HTTPAuthorizationCredentials] = Depends(bearer)
) -> str:
    # When called as a dependency FastAPI resolves `cred` to HTTPAuthorizationCredentials.
    # When invoked manually (e.g. helper call inside api.py) the default value is the raw
    # `Depends` instance, so guard attribute access to avoid crashes.
    token = getattr(cred, "credentials", None) if cred else None
    if not token:
        token = request.cookies.get(COOKIE_ACCESS_NAME)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"], issuer=JWT_ISSUER)
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return str(sub)


def sign_jwt(email: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "iss": JWT_ISSUER,
        "sub": email,
        "auth": "otp",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=JWT_EXPIRE_MINUTES)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


__all__ = [
    "ACCESS_TOKEN_MINUTES",
    "COOKIE_ACCESS_NAME",
    "COOKIE_REFRESH_NAME",
    "COOKIE_DOMAIN",
    "EXTERNAL_BASE_URL",
    "JWT_EXPIRE_MINUTES",
    "JWT_ISSUER",
    "JWT_SECRET",
    "REFRESH_TOKEN_DAYS",
    "SESSION_PERSISTENT_DAYS",
    "bearer",
    "clear_auth_cookies",
    "get_current_subject",
    "mint_token_pair",
    "resolve_session_settings",
    "session_settings",
    "set_access_cookie",
    "set_refresh_cookie",
    "sign_access_jwt",
    "sign_jwt",
    "verify_and_rotate_refresh",
    "revoke_refresh_by_raw",
]
