"""Shared request-scoped dependencies for authentication helpers."""

from typing import Optional
import structlog
from fastapi import Depends, HTTPException, Request
from core.security import get_current_subject

def get_current_userid(
    request: Request,
    payload: dict = Depends(get_current_subject),
) -> str:
    # Lấy trực tiếp từ payload đã decode ở security.py
    userid = payload.get("user_id")
    
    if not userid:
        raise HTTPException(status_code=401, detail="Token missing user_id")

    # Gán vào state cho logging
    request.state.user_id = str(userid)
    structlog.contextvars.bind_contextvars(user_id=str(userid))

    return str(userid)

def get_optional_userid(request: Request) -> Optional[str]:
    try:
        from core.security import get_current_subject
        payload = get_current_subject(request, None)
        return str(payload.get("user_id"))
    except:
        return None