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

# --- CONFIGURATIONS ---
ACCESS_TOKEN_MINUTES = settings.access_token_minutes
REFRESH_TOKEN_DAYS = settings.refresh_token_days
SESSION_PERSISTENT_DAYS = settings.session_persistent_days
COOKIE_ACCESS_NAME = settings.cookie_access_name
COOKIE_REFRESH_NAME = settings.cookie_refresh_name
COOKIE_DOMAIN = settings.cookie_domain
REFRESH_PEPPER = settings.refresh_pepper.encode("utf-8")

EXTERNAL_BASE_URL = settings.base_url
JWT_SECRET = settings.jwt_secret
JWT_ISSUER = settings.jwt_issuer
JWT_EXPIRE_MINUTES = settings.jwt_expire_minutes

def resolve_session_settings(external_url: Optional[str]) -> dict:
    settings_dict = {"same_site": "lax", "https_only": False}
    if external_url and external_url.lower().startswith("https://"):
        settings_dict.update({"same_site": "none", "https_only": True})
    return settings_dict

session_settings = resolve_session_settings(EXTERNAL_BASE_URL)

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _hash_refresh(raw: str) -> str:
    return hmac.new(REFRESH_PEPPER, raw.encode("utf-8"), hashlib.sha256).hexdigest()

# --- REFRESH TOKEN MODEL ---
class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id = Column(Integer, primary_key=True, autoincrement=True)
    subject = Column(String(320), nullable=False)
    jti = Column(String(64), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)
    issued_at = Column(DateTime(timezone=True), nullable=False, default=_now_utc)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked = Column(Boolean, default=False, nullable=False)

    __table_args__ = (
        Index("ix_refresh_subject", "subject"),
        Index("ix_refresh_expires_at", "expires_at"),
    )

# --- KHÔI PHỤC ĐẦY ĐỦ CÁC HÀM SIGN ---
def sign_access_jwt(sub: str, user_id: Optional[str] = None, jti: Optional[str] = None) -> str:
    now = _now_utc()
    payload = {
        "iss": JWT_ISSUER,
        "sub": sub,
        "user_id": user_id,
        "auth": "app",
        "iat": int(now.timestamp()),
        "jti": jti or uuid4().hex,
    }
    if ACCESS_TOKEN_MINUTES > 0:
        payload["exp"] = int((now + timedelta(minutes=ACCESS_TOKEN_MINUTES)).timestamp())
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def sign_jwt(email: str) -> str:
    now = _now_utc()
    payload = {
        "iss": JWT_ISSUER,
        "sub": email,
        "auth": "otp",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=JWT_EXPIRE_MINUTES)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def _new_refresh_record(db: Session, subject: str) -> Tuple[str, RefreshToken]:
    raw = uuid4().hex + uuid4().hex
    now = _now_utc()
    expires_at = now + timedelta(days=REFRESH_TOKEN_DAYS)
    rec = RefreshToken(
        subject=subject,
        jti=uuid4().hex,
        token_hash=_hash_refresh(raw),
        issued_at=now,
        expires_at=expires_at,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return raw, rec

def mint_token_pair(db: Session, subject: str, user_id: Optional[str] = None) -> Tuple[str, str]:
    at = sign_access_jwt(subject, user_id)
    rt_raw, _ = _new_refresh_record(db, subject)
    return at, rt_raw

# --- KHÔI PHỤC HÀM VERIFY VÀ ROTATE ---
def verify_and_rotate_refresh(db: Session, rt_raw: str, subject_hint: Optional[str] = None) -> Tuple[str, str]:
    h = _hash_refresh(rt_raw)
    rec = db.query(RefreshToken).filter_by(token_hash=h, revoked=False).one_or_none()
    if not rec or rec.expires_at.replace(tzinfo=timezone.utc) < _now_utc():
        raise HTTPException(status_code=401, detail="invalid_refresh")
    
    rec.revoked = True
    db.commit()
    return mint_token_pair(db, rec.subject)

def revoke_refresh_by_raw(db: Session, rt_raw: Optional[str]):
    if not rt_raw: return
    h = _hash_refresh(rt_raw)
    rec = db.query(RefreshToken).filter_by(token_hash=h).one_or_none()
    if rec:
        rec.revoked = True
        db.commit()

bearer = HTTPBearer(auto_error=False)

def get_current_subject(request: Request, cred: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> dict:
    token = getattr(cred, "credentials", None) if cred else request.cookies.get(COOKIE_ACCESS_NAME)
    if not token: raise HTTPException(status_code=401)
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"], issuer=JWT_ISSUER)
    except:
        raise HTTPException(status_code=401)

def set_access_cookie(resp: Response, token: str):
    resp.set_cookie(key=COOKIE_ACCESS_NAME, value=token, httponly=True, secure=session_settings.get("https_only"), samesite=session_settings.get("same_site"), path="/")

def set_refresh_cookie(resp: Response, token: str):
    resp.set_cookie(key=COOKIE_REFRESH_NAME, value=token, httponly=True, secure=session_settings.get("https_only"), samesite=session_settings.get("same_site"), path="/")

def clear_auth_cookies(resp: Response):
    for k in (COOKIE_ACCESS_NAME, COOKIE_REFRESH_NAME):
        resp.delete_cookie(key=k, path="/")