"""Shared request-scoped dependencies for authentication helpers."""

from typing import Optional
import structlog
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from database import get_db
from models import UserProfile
from core.security import bearer, get_current_subject


def _optional_subject(
    request: Request, cred: Optional[HTTPAuthorizationCredentials] = Depends(bearer)
) -> Optional[str]:
    """
    Attempt to decode the current JWT subject. Returns None when no token is
    present or decoding fails so callers can fall back to other strategies.
    """
    try:
        return get_current_subject(request, cred)
    except HTTPException:
        return None


def _userid_from_session(request: Request) -> Optional[str]:
    session_user = request.session.get("user") if request and hasattr(request, "session") else None
    if isinstance(session_user, dict):
        candidate = session_user.get("user_id")
        if isinstance(candidate, str):
            candidate = candidate.strip()
            if candidate:
                return candidate
    return None


def resolve_userid(
    request: Request,
    db: Session,
    subject: Optional[str] = None,
    provided_userid: Optional[str] = None,
    allow_anonymous: bool = False,
) -> Optional[str]:
    """
    Resolve the current user's ID from session or JWT subject. Optionally accepts
    a provided_userid for consistency checks and can allow anonymous access.
    """
    resolved_userid = _userid_from_session(request)

    if not resolved_userid and subject:
        profile = db.query(UserProfile).filter(UserProfile.email == subject).one_or_none()
        if profile and profile.user_id:
            resolved_userid = profile.user_id.strip()

    provided = provided_userid.strip() if provided_userid else None
    if resolved_userid and provided and resolved_userid != provided:
        raise HTTPException(status_code=403, detail="使用者身份不一致")

    if not resolved_userid and provided:
        resolved_userid = provided

    if not resolved_userid and not allow_anonymous:
        raise HTTPException(status_code=401, detail="未登入")

    return resolved_userid


def get_current_userid(
    request: Request,
    db: Session = Depends(get_db),
    subject: Optional[str] = Depends(_optional_subject),
) -> str:
    """FastAPI dependency that requires a resolved user ID."""
    userid = resolve_userid(request, db, subject=subject, allow_anonymous=False)
    assert userid is not None

    # 綁定到 request.state，讓全域 Middleware 在 API 結束時能把 user_id 寫進稽核日誌
    request.state.user_id = userid
    #直接綁定到 structlog 的當前 Context
    structlog.contextvars.bind_contextvars(user_id=userid)

    return userid


def get_optional_userid(
    request: Request,
    db: Session = Depends(get_db),
    subject: Optional[str] = Depends(_optional_subject),
) -> Optional[str]:
    """FastAPI dependency that returns None when no user is authenticated."""
    return resolve_userid(request, db, subject=subject, allow_anonymous=True)
