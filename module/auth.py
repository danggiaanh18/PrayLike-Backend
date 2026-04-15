"""Backward-compatible shim for the refactored auth router."""

from core.security import (
    COOKIE_ACCESS_NAME,
    COOKIE_REFRESH_NAME,
    bearer,
    clear_auth_cookies,
    get_current_subject,
    mint_token_pair,
    revoke_refresh_by_raw,
    set_access_cookie,
    set_refresh_cookie,
    sign_access_jwt,
    sign_jwt,
    verify_and_rotate_refresh,
)
from database import SessionLocal
from routers.auth import auth_router, build_external_url, register_auth

__all__ = [
    "auth_router",
    "register_auth",
    "build_external_url",
    "bearer",
    "get_current_subject",
    "sign_access_jwt",
    "sign_jwt",
    "set_access_cookie",
    "set_refresh_cookie",
    "clear_auth_cookies",
    "mint_token_pair",
    "verify_and_rotate_refresh",
    "revoke_refresh_by_raw",
    "SessionLocal",
    "COOKIE_ACCESS_NAME",
    "COOKIE_REFRESH_NAME",
]
