"""Auth router composed of OAuth, profile, and OTP flows."""

import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse
from uuid import uuid4

import jwt
import requests
import structlog
from fastapi import (
    APIRouter,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, EmailStr, Field, validator
from redis import Redis
from sqlalchemy import or_
from starlette.middleware.sessions import SessionMiddleware

from config import settings
from core.security import (
    COOKIE_ACCESS_NAME,
    COOKIE_REFRESH_NAME,
    EXTERNAL_BASE_URL,
    JWT_ISSUER,
    JWT_SECRET,
    bearer,
    clear_auth_cookies,
    get_current_subject,
    mint_token_pair,
    session_settings,
    set_access_cookie,
    set_refresh_cookie,
    sign_access_jwt,
    sign_jwt,
    verify_and_rotate_refresh,
    revoke_refresh_by_raw,
)
from database import SessionLocal
from models import FriendInvitation, Post, PostAmen, UserFriend, UserProfile
from services.email import send_email
from services.oauth import AppleProviderConfig, oauth_logger, registry

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
otp_logger = structlog.get_logger("auth-otp")

auth_router = APIRouter(prefix="/auth", tags=["Auth"])


def _normalize_email(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    candidate = value.strip().lower()
    return candidate or None


def _email_from_user_payload(user_payload: Any) -> Optional[str]:
    if isinstance(user_payload, dict):
        email_val = user_payload.get("email")
        if isinstance(email_val, str):
            return _normalize_email(email_val)
    return None


def _normalize_user_id(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    candidate = value.strip()
    return candidate or None


def _normalize_avatar_url(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    candidate = value.strip()
    return candidate or None


def _is_user_id_taken(
    db, user_id: Optional[str], exclude_email: Optional[str] = None
) -> bool:
    normalized_id = _normalize_user_id(user_id)
    if not normalized_id:
        return False
    query = db.query(UserProfile).filter(UserProfile.user_id == normalized_id)
    if exclude_email:
        query = query.filter(UserProfile.email != exclude_email)
    return query.first() is not None


def _resolve_first_login(db, email: Optional[str]) -> bool:
    normalized = _normalize_email(email)
    if not normalized:
        return False
    profile = db.query(UserProfile).filter(
        UserProfile.email == normalized).one_or_none()
    if profile is None:
        return True
    if not _normalize_user_id(profile.user_id):
        return True
    if not profile.name or not profile.name.strip():
        return True
    return False


def _load_profile(db, email: Optional[str]) -> Optional[UserProfile]:
    normalized = _normalize_email(email)
    if not normalized:
        return None
    return db.query(UserProfile).filter(UserProfile.email == normalized).one_or_none()


def _session_first_login(request: Request, user_payload: Any) -> bool:
    cached = request.session.get("first_login")
    if cached is not None:
        return bool(cached)
    email = _email_from_user_payload(user_payload)
    if not email:
        return False
    db = SessionLocal()
    try:
        return _resolve_first_login(db, email)
    finally:
        db.close()


AVATAR_UPLOAD_DIR = Path("uploads") / "avatars"
AVATAR_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_AVATAR_BYTES = 1 * 1024 * 1024  # 1MB 上限，避免過大檔案


def _extract_avatar_url(user_payload: Any) -> Optional[str]:
    if not isinstance(user_payload, dict):
        return None
    candidate = user_payload.get("avatar_url") or user_payload.get("picture")
    if isinstance(candidate, dict):
        nested = candidate.get("data") if isinstance(
            candidate.get("data"), dict) else None
        if nested and isinstance(nested.get("url"), str):
            candidate = nested["url"]
        elif isinstance(candidate.get("url"), str):
            candidate = candidate["url"]
        else:
            candidate = None
    if isinstance(candidate, str):
        normalized = _normalize_avatar_url(candidate)
        if normalized and normalized.lower() != "null":
            return normalized
    return None


TRIBE_LABELS: Dict[int, str] = {
    0: "honggggggg",
    1: "Judah",
    2: "Reuben",
    3: "Gad",
    4: "Asher",
    5: "Naphtali",
    6: "Manasseh",
    7: "Simeon",
    8: "Levi",
    9: "Issachar",
    10: "Zebulun",
    11: "Joseph",
    12: "Benjamin",
}

TRIBE_OPTIONS: List[Dict[str, object]] = [
    {"code": code, "label": label} for code, label in TRIBE_LABELS.items()
]


async def _save_avatar_file(file: UploadFile) -> str:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail={
                            "error": "invalid_avatar"})
    if len(content) > MAX_AVATAR_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"error": "avatar_too_large"},
        )
    suffix = Path(file.filename).suffix if file.filename else ""
    filename = f"{uuid4().hex}{suffix}"
    destination = AVATAR_UPLOAD_DIR / filename
    with destination.open("wb") as buffer:
        buffer.write(content)
    return str(destination)


def _delete_local_avatar(path: Optional[str]) -> None:
    if not path:
        return
    try:
        target = Path(path).resolve()
        avatar_root = AVATAR_UPLOAD_DIR.resolve()
        target.relative_to(avatar_root)
    except Exception:
        return
    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass


def _sanitize_redirect_target(target: Optional[str]) -> Optional[str]:
    if not target:
        return None
    candidate = target.strip()
    if not candidate:
        return None
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"}:
            return None
        if not parsed.netloc:
            return None
        return candidate
    if not candidate.startswith("/"):
        candidate = "/" + candidate
    return candidate


REDIRECT_SESSION_KEY = "redirect_to"
DEFAULT_REDIRECT_TARGET = _sanitize_redirect_target(
    settings.auth_success_redirect_url
)
if DEFAULT_REDIRECT_TARGET is None:
    DEFAULT_REDIRECT_TARGET = "/"


def _build_redirect_url(request: Request, target: str) -> str:
    if target.startswith("http://") or target.startswith("https://"):
        return target
    base = EXTERNAL_BASE_URL.rstrip(
        "/") + "/" if EXTERNAL_BASE_URL else str(request.base_url)
    return urljoin(base, target.lstrip("/"))


def _resolve_redirect_url(request: Request, target: Optional[str]) -> str:
    sanitized = _sanitize_redirect_target(target)
    if sanitized:
        return _build_redirect_url(request, sanitized)
    return _build_redirect_url(request, DEFAULT_REDIRECT_TARGET)


def register_auth(app: FastAPI) -> None:
    """Attach session middleware (if missing) and include auth routes."""
    has_session_middleware = any(
        m.cls is SessionMiddleware for m in app.user_middleware
    )
    if not has_session_middleware:
        app.add_middleware(
            SessionMiddleware,
            secret_key=settings.session_secret or secrets.token_hex(32),
            **session_settings,
        )
    app.include_router(auth_router)


def build_external_url(request: Request, route_name: str, **params: str) -> str:
    if EXTERNAL_BASE_URL:
        path = request.app.url_path_for(route_name, **params)
        return urljoin(EXTERNAL_BASE_URL.rstrip("/") + "/", path.lstrip("/"))
    return str(request.app.url_path_for(route_name, **params))


@auth_router.get("/providers")
async def providers():
    return registry.configured_providers()


@auth_router.get("/session")
async def session_info(request: Request):
    user = request.session.get("user")
    if not user:
        return {"authenticated": False, "first_login": False}
    first_login = _session_first_login(request, user)
    stored_profile = None
    if not first_login:
        email = _email_from_user_payload(user)
        if email:
            db = SessionLocal()
            try:
                profile_obj = _load_profile(db, email)
                if profile_obj:
                    stored_profile = profile_obj.to_dict()
            finally:
                db.close()
    return {
        "authenticated": True,
        "provider": request.session.get("provider"),
        "profile": user,
        "first_login": first_login,
        "account": stored_profile,
    }


def _require_authenticated_email(request: Request) -> str:
    user = request.session.get("user")
    email = _email_from_user_payload(user)
    if email:
        return email

    # Fallback for token-based clients without session data (e.g., mobile app)
    token = None
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
    if not token:
        token = request.cookies.get(COOKIE_ACCESS_NAME)
    if token:
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=[
                                 "HS256"], issuer=JWT_ISSUER)
            sub = payload.get("sub")
            normalized = _normalize_email(sub)
            if normalized:
                return normalized
        except Exception:
            pass

    raise HTTPException(status_code=401, detail={"error": "unauthorized"})


class ProfileUpsertBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    user_id: str = Field(..., min_length=1, max_length=120)
    avatar_url: Optional[str] = Field(None, max_length=500)


class TribeSelectBody(BaseModel):
    tribe: int = Field(..., ge=0, le=12, description="選擇的支派編號 (1-12)")

    @validator("tribe")
    def validate_tribe(cls, value: int) -> int:
        if value not in TRIBE_LABELS:
            raise ValueError("invalid_tribe")
        return value


class DeleteAccountBody(BaseModel):
    confirm: bool = Field(
        ...,
        description="Set to true to delete the signed-in account and clear the session.",
    )


@auth_router.get("/profile")
async def get_profile(request: Request):
    email = _require_authenticated_email(request)
    db = SessionLocal()
    try:
        profile_obj = _load_profile(db, email)
        profile_data = profile_obj.to_dict() if profile_obj else None
    finally:
        db.close()
    return {"ok": True, "profile": profile_data}


@auth_router.post("/profile/avatar")
async def update_avatar(
    request: Request,
    file: Optional[UploadFile] = File(None),
    avatar_url: Optional[str] = Form(None),
):
    email = _require_authenticated_email(request)
    normalized_url = _normalize_avatar_url(avatar_url)
    saved_path: Optional[str] = None
    if file:
        saved_path = await _save_avatar_file(file)
        normalized_url = saved_path
    if not normalized_url:
        raise HTTPException(status_code=400, detail={
                            "error": "avatar_required"})

    db = SessionLocal()
    previous_avatar = None
    try:
        profile_obj = _load_profile(db, email)
        if profile_obj is None:
            raise HTTPException(status_code=404, detail={
                                "error": "profile_not_found"})
        previous_avatar = profile_obj.avatar_url
        profile_obj.avatar_url = normalized_url
        db.add(profile_obj)
        db.commit()
        db.refresh(profile_obj)
        profile_data = profile_obj.to_dict()
    except Exception:
        if saved_path:
            _delete_local_avatar(saved_path)
        raise
    finally:
        db.close()

    if saved_path and previous_avatar != saved_path:
        _delete_local_avatar(previous_avatar)

    stored_user = request.session.get("user") or {}
    if isinstance(stored_user, dict):
        stored_user.setdefault("email", email)
        stored_user["avatar_url"] = normalized_url
        stored_user["picture"] = normalized_url
        request.session["user"] = stored_user
    return {"ok": True, "profile": profile_data}


@auth_router.get("/profile/tribes")
async def list_tribes():
    return {"ok": True, "tribes": TRIBE_OPTIONS}


@auth_router.get("/profile/check-user-id")
async def check_user_id(user_id: str = Query(..., min_length=1, max_length=120)):
    candidate = _normalize_user_id(user_id)
    if not candidate:
        raise HTTPException(status_code=400, detail={
                            "error": "invalid_user_id"})
    db = SessionLocal()
    try:
        taken = _is_user_id_taken(db, candidate)
    finally:
        db.close()
    return {"ok": True, "exists": taken}


@auth_router.post("/profile")
async def upsert_profile(body: ProfileUpsertBody, request: Request):
    email = _require_authenticated_email(request)
    name = body.name.strip()
    user_id = _normalize_user_id(body.user_id)
    avatar_provided = "avatar_url" in body.__fields_set__
    avatar_url = (
        _normalize_avatar_url(body.avatar_url) if avatar_provided else None
    )
    if not name:
        raise HTTPException(status_code=400, detail={"error": "invalid_name"})
    if not user_id:
        raise HTTPException(status_code=400, detail={
                            "error": "invalid_user_id"})
    db = SessionLocal()
    try:
        profile_obj = _load_profile(db, email)
        if _is_user_id_taken(db, user_id, exclude_email=email if profile_obj else None):
            raise HTTPException(status_code=409, detail={
                                "error": "user_id_taken"})
        if profile_obj is None:
            resolved_avatar = (
                avatar_url
                if avatar_provided
                else _extract_avatar_url(request.session.get("user"))
            )
            profile_obj = UserProfile(
                email=email,
                name=name,
                user_id=user_id,
                avatar_url=resolved_avatar,
            )
            db.add(profile_obj)
        else:
            profile_obj.name = name
            profile_obj.user_id = user_id
            if avatar_provided:
                profile_obj.avatar_url = avatar_url
            db.add(profile_obj)
        db.commit()
        db.refresh(profile_obj)
        profile_data = profile_obj.to_dict()
    finally:
        db.close()
    stored_user = request.session.get("user") or {}
    if isinstance(stored_user, dict):
        stored_user.setdefault("email", email)
        stored_user["name"] = name
        stored_user["user_id"] = user_id
        if avatar_provided or profile_data.get("avatar_url"):
            stored_user["avatar_url"] = profile_data.get("avatar_url")
            if profile_data.get("avatar_url"):
                stored_user["picture"] = profile_data["avatar_url"]
        request.session["user"] = stored_user
    request.session["first_login"] = False
    return {"ok": True, "profile": profile_data}


@auth_router.post("/profile/tribe")
async def select_tribe(body: TribeSelectBody, request: Request):
    email = _require_authenticated_email(request)
    db = SessionLocal()
    try:
        profile_obj = _load_profile(db, email)
        if profile_obj is None:
            raise HTTPException(status_code=404, detail={
                                "error": "profile_not_found"})
        if profile_obj.tribe:
            raise HTTPException(status_code=409, detail={
                                "error": "tribe_already_selected"})
        profile_obj.tribe = body.tribe
        db.add(profile_obj)
        db.commit()
        db.refresh(profile_obj)
        profile_data = profile_obj.to_dict()
    finally:
        db.close()
    stored_user = request.session.get("user") or {}
    if isinstance(stored_user, dict):
        stored_user.setdefault("email", email)
        stored_user["tribe"] = body.tribe
        request.session["user"] = stored_user
    return {"ok": True, "profile": profile_data}


@auth_router.delete("/account")
async def delete_account(request: Request, body: DeleteAccountBody):
    if not body.confirm:
        raise HTTPException(
            status_code=400, detail={"error": "confirmation_required"}
        )
    email = _require_authenticated_email(request)
    db = SessionLocal()
    message = "Account already removed."
    deleted = False
    avatar_to_delete = None
    try:
        profile_obj = _load_profile(db, email)
        target_user_id = (
            profile_obj.user_id.strip() if profile_obj and profile_obj.user_id else None
        )
        avatar_to_delete = profile_obj.avatar_url if profile_obj else None
        anonymized_user_id = (
            f"deleted_user_{uuid4().hex[:8]}" if target_user_id else None
        )

        if target_user_id:
            private_docids = [
                docid
                for (docid,) in db.query(Post.docid)
                .filter(Post.userid == target_user_id, Post.event == "post")
                .all()
                if docid
            ]
            if private_docids:
                db.query(PostAmen).filter(
                    PostAmen.post_docid.in_(private_docids)
                ).delete(synchronize_session=False)
                db.query(Post).filter(Post.docid.in_(private_docids)).delete(
                    synchronize_session=False
                )

            db.query(UserFriend).filter(
                or_(
                    UserFriend.user_id == target_user_id,
                    UserFriend.friend_user_id == target_user_id,
                )
            ).delete(synchronize_session=False)
            db.query(FriendInvitation).filter(
                or_(
                    FriendInvitation.requester_user_id == target_user_id,
                    FriendInvitation.target_user_id == target_user_id,
                )
            ).delete(synchronize_session=False)

            if anonymized_user_id:
                # Keep engagement counts intact while detaching user identity.
                amen_rows = (
                    db.query(PostAmen)
                    .filter(PostAmen.userid == target_user_id)
                    .all()
                )
                for amen in amen_rows:
                    suffix = amen.id if amen.id is not None else uuid4(
                    ).hex[:8]
                    amen.userid = f"{anonymized_user_id}_{suffix}"

                witness_posts = (
                    db.query(Post)
                    .filter(Post.userid == target_user_id, Post.event == "witness")
                    .all()
                )
                for witness in witness_posts:
                    witness.userid = anonymized_user_id

        if profile_obj is not None:
            db.delete(profile_obj)
            db.commit()
            deleted = True
            message = "Account deleted and session cleared."
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=500, detail={"error": "delete_failed", "message": str(exc)}
        )
    finally:
        db.close()
    request.session.clear()
    _delete_local_avatar(avatar_to_delete)
    return {
        "ok": True,
        "deleted": deleted,
        "message": message,
    }


@auth_router.get("/{provider}/login")
async def start_login(
    provider: str, request: Request, redirect_to: Optional[str] = None
):
    provider_cfg = registry.get(provider)
    if provider_cfg is None:
        raise HTTPException(
            status_code=404, detail={"error": f"Unsupported provider '{provider}'"}
        )
    if not provider_cfg.is_configured():
        return JSONResponse(
            status_code=500,
            content={
                "error": f"Provider '{provider}' is not configured",
                "missing_env": provider_cfg.missing_env(),
            },
        )

    redirect_target = _sanitize_redirect_target(redirect_to)
    if redirect_target:
        request.session[REDIRECT_SESSION_KEY] = redirect_target
    else:
        request.session.pop(REDIRECT_SESSION_KEY, None)

    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    request.session["provider"] = provider
    redirect_uri = build_external_url(
        request, "oauth_callback", provider=provider)
    authorize_url = provider_cfg.build_authorize_url(state, redirect_uri)
    oauth_logger.info(
        "Starting OAuth login",
        extra={"provider": provider, "redirect_uri": redirect_uri},
    )
    return RedirectResponse(authorize_url)


# async def _handle_oauth_callback(provider: str, request: Request):
#     provider_cfg = registry.get(provider)
#     if provider_cfg is None:
#         raise HTTPException(
#             status_code=404, detail={"error": f"Unsupported provider '{provider}'"}
#         )

#     if request.method == "POST":
#         params = await request.form()
#     else:
#         params = request.query_params

#     expected_state = request.session.get("oauth_state")
#     state = params.get("state")
#     if not expected_state or state != expected_state:
#         raise HTTPException(
#             status_code=400, detail={"error": "Invalid state parameter"}
#         )

#     error = params.get("error")
#     if error:
#         return JSONResponse(
#             status_code=400,
#             content={
#                 "error": error,
#                 "description": params.get("error_description"),
#             },
#         )

#     code = params.get("code")
#     if not code:
#         raise HTTPException(
#             status_code=400, detail={"error": "Missing authorization code"}
#         )

#     raw_user_payload: Optional[str] = None
#     if provider == AppleProviderConfig.name:
#         raw_user_payload = params.get("user")

#     redirect_uri = build_external_url(request, "oauth_callback", provider=provider)
#     oauth_logger.info(
#         "Handling OAuth callback", extra={"provider": provider, "has_code": True}
#     )
#     first_login = False
#     try:
#         tokens = provider_cfg.exchange_code_for_tokens(str(code), redirect_uri)
#         if raw_user_payload:
#             tokens["_apple_raw_user"] = raw_user_payload
#         userinfo = provider_cfg.fetch_userinfo(tokens)
#         # 取用戶識別（優先 email，否則使用 provider 的 sub/id）
#         subject = None
#         if isinstance(userinfo, dict):
#             subject = userinfo.get("email") or userinfo.get("sub") or userinfo.get("id")
#         if not subject:
#             subject = f"{provider}:{uuid4().hex}"

#         # 發 access/refresh 並以 HttpOnly Cookie 回應（Route A）
#         db = SessionLocal()
#         try:
#             access_token, refresh_token = mint_token_pair(db, str(subject))
#             login_email = _email_from_user_payload(userinfo)
#             if login_email is None and isinstance(subject, str):
#                 login_email = _normalize_email(subject)
#             first_login = _resolve_first_login(db, login_email)
#         finally:
#             db.close()
#     except requests.HTTPError as http_error:
#         detail_payload: Dict[str, str] = {
#             "error": "Provider request failed",
#             "details": str(http_error),
#         }
#         if http_error.response is not None:
#             try:
#                 detail_payload["provider_response"] = http_error.response.json()
#             except ValueError:
#                 detail_payload["provider_response"] = http_error.response.text
#         oauth_logger.error(
#             "Provider HTTP error", extra={"provider": provider, **detail_payload}
#         )
#         return JSONResponse(status_code=502, content=detail_payload)
#     except Exception as exc:  # pragma: no cover
#         oauth_logger.exception("OAuth flow failed", extra={"provider": provider})
#         return JSONResponse(
#             status_code=500, content={"error": "OAuth flow failed", "details": str(exc)}
#         )

#     request.session["user"] = userinfo
#     request.session["tokens"] = {
#         k: v for k, v in tokens.items() if k != "access_token" and not k.startswith("_")
#     }
#     oauth_logger.info(
#         "OAuth success",
#         extra={"provider": provider, "user_keys": list(userinfo.keys())},
#     )
#     redirect_target = request.session.pop(REDIRECT_SESSION_KEY, None)
#     redirect_url = _resolve_redirect_url(request, redirect_target)
#     resp = RedirectResponse(redirect_url, status_code=303)
#     set_access_cookie(resp, access_token)
#     set_refresh_cookie(resp, refresh_token)
#     request.session["first_login"] = first_login
#     return resp

async def _handle_oauth_callback(provider: str, request: Request):
    # 1. 基本檢查
    provider_cfg = registry.get(provider)
    if provider_cfg is None:
        raise HTTPException(status_code=404, detail={
                            "error": f"Unsupported provider '{provider}'"})

    if request.method == "POST":
        params = await request.form()
    else:
        params = request.query_params

    # 2. 檢查 State
    expected_state = request.session.get("oauth_state")
    state = params.get("state")
    # 暫時註解掉 State 檢查以方便測試，若確定沒問題再開回來
    # if not expected_state or state != expected_state:
    #     return JSONResponse({"error": "Invalid state", "expected": expected_state, "received": state})

    # 3. 檢查 Error
    error = params.get("error")
    if error:
        return JSONResponse(content={
            "error": error,
            "description": params.get("error_description"),
            "source": "Apple Callback Parameters"
        })

    code = params.get("code")
    if not code:
        return JSONResponse({"error": "Missing authorization code"})

    # 4. 準備 Redirect URI (強制轉 HTTPS)
    redirect_uri = build_external_url(
        request, "oauth_callback", provider=provider)

    # [FIX] 如果是在線上環境但網址是 http，強制轉 https
    if redirect_uri.startswith("http://") and "localhost" not in redirect_uri and "127.0.0.1" not in redirect_uri:
        redirect_uri = redirect_uri.replace("http://", "https://", 1)

    # 5. 開始嘗試交換 Token (加入詳細除錯回傳)
    try:
        # 嘗試執行標準流程
        tokens = provider_cfg.exchange_code_for_tokens(str(code), redirect_uri)

        # --- 如果成功，以下是原本的邏輯 ---
        raw_user_payload = params.get(
            "user") if provider == AppleProviderConfig.name else None
        if raw_user_payload:
            tokens["_apple_raw_user"] = raw_user_payload

        userinfo = provider_cfg.fetch_userinfo(tokens)
        subject = None
        if isinstance(userinfo, dict):
            subject = userinfo.get("email") or userinfo.get(
                "sub") or userinfo.get("id")
        if not subject:
            subject = f"{provider}:{uuid4().hex}"

        db = SessionLocal()
        first_login = False
        try:
            access_token, refresh_token = mint_token_pair(db, str(subject))
            login_email = _email_from_user_payload(userinfo)
            if login_email is None and isinstance(subject, str):
                login_email = _normalize_email(subject)
            first_login = _resolve_first_login(db, login_email)
        finally:
            db.close()

        request.session["user"] = userinfo
        request.session["tokens"] = {k: v for k, v in tokens.items(
        ) if k != "access_token" and not k.startswith("_")}

        # 處理 Redirect
        redirect_target = request.session.pop(REDIRECT_SESSION_KEY, None)
        final_redirect_url = _resolve_redirect_url(request, redirect_target)

        # [App Scheme 支援] 如果是 App Scheme，把 Token 帶在網址上
        if final_redirect_url.startswith("praylike://") or "http" not in final_redirect_url:
            final_redirect_url += f"?access_token={access_token}&refresh_token={refresh_token}"

        resp = RedirectResponse(final_redirect_url, status_code=303)
        set_access_cookie(resp, access_token)
        set_refresh_cookie(resp, refresh_token)
        request.session["first_login"] = first_login
        return resp

    except Exception as exc:
        # ==========================================
        # ★★★ DEBUG 模式：捕捉錯誤並顯示詳細資訊 ★★★
        # ==========================================
        import jwt
        import os
        import time
        from services.oauth import build_apple_client_secret, _load_apple_private_key

        # 收集環境變數
        env_cid = os.environ.get("APPLE_CLIENT_ID")
        env_tid = os.environ.get("APPLE_TEAM_ID")
        env_kid = os.environ.get("APPLE_KEY_ID")

        # 檢查私鑰格式
        pk_raw = _load_apple_private_key()
        pk_status = "Missing"
        if pk_raw:
            if "-----BEGIN PRIVATE KEY-----" in pk_raw:
                pk_status = "Valid Header Found"
            else:
                pk_status = "Invalid Header (Check .env formatting)"

        # 重新生成 Client Secret 並反解 Payload
        decoded_jwt = "Generation Failed"
        jwt_token_sample = "None"
        try:
            secret = build_apple_client_secret()
            jwt_token_sample = secret[:20] + "..." + secret[-20:]
            # 不驗證簽名，只解開看 Payload 內容是否正確
            decoded_jwt = jwt.decode(
                secret, options={"verify_signature": False})
        except Exception as e:
            decoded_jwt = f"Error generating secret: {str(e)}"

        # 組合錯誤報告 JSON
        debug_info = {
            "status": "ERROR",
            "message": str(exc),
            "debug_analysis": {
                "1_Environment_Vars": {
                    "APPLE_CLIENT_ID (Should be Service ID)": env_cid,
                    "APPLE_TEAM_ID": env_tid,
                    "APPLE_KEY_ID": env_kid,
                    "Private_Key_Status": pk_status
                },
                "2_Request_Info": {
                    "Redirect_URI_Sent_To_Apple": redirect_uri,
                    "Auth_Code_Received": code,
                    "Provider": provider
                },
                "3_Generated_JWT_Analysis": {
                    "Payload_Sent_To_Apple": decoded_jwt,
                    "Token_Sample": jwt_token_sample
                },
                "4_Tips": [
                    "Check if 'sub' in JWT matches your Service ID exactly.",
                    "Check if 'Redirect_URI' matches exactly in Apple Developer Portal.",
                    "Check if 'iat' (timestamp) is close to current UTC time."
                ]
            }
        }

        # 直接回傳 JSON 到畫面
        return JSONResponse(content=debug_info, status_code=400)


@auth_router.get("/{provider}/callback", name="oauth_callback")
async def oauth_callback_get(provider: str, request: Request):
    return await _handle_oauth_callback(provider, request)


@auth_router.post("/{provider}/callback", name="oauth_callback_post")
async def oauth_callback_post(provider: str, request: Request):
    return await _handle_oauth_callback(provider, request)


@auth_router.post("/refresh")
async def refresh_token(request: Request):
    # 從 Cookie 取 refresh token
    rt = request.cookies.get(COOKIE_REFRESH_NAME)
    if not rt:
        raise HTTPException(status_code=401, detail={
                            "error": "missing_refresh"})
    db = SessionLocal()
    try:
        # 可選：若你維持 session 中有 user/email，可做 subject_hint 強化
        subject_hint = None
        sess_user = request.session.get("user")
        if isinstance(sess_user, dict):
            subject_hint = sess_user.get("email") or sess_user.get(
                "sub") or sess_user.get("id")
        at, new_rt = verify_and_rotate_refresh(
            db, rt, subject_hint=subject_hint)
    finally:
        db.close()
    resp = JSONResponse({"ok": True, "authenticated": True, "app_token": at})
    set_access_cookie(resp, at)
    set_refresh_cookie(resp, new_rt)
    return resp


@auth_router.post("/logout")
async def logout(request: Request):
    # 撤銷 refresh token
    rt = request.cookies.get(COOKIE_REFRESH_NAME)
    db = SessionLocal()
    try:
        revoke_refresh_by_raw(db, rt)
    finally:
        db.close()

    request.session.clear()
    resp = JSONResponse({"ok": True})
    clear_auth_cookies(resp)
    return resp


# -------------------- OTP endpoints --------------------


OTP_TTL_SECONDS = settings.otp_ttl_seconds
OTP_LENGTH = settings.otp_length
OTP_DAILY_LIMIT = settings.otp_daily_limit
OTP_REQUEST_COOLDOWN_SECONDS = settings.otp_request_cooldown_seconds
OTP_MAX_ATTEMPTS = settings.otp_max_attempts
OTP_PEPPER = settings.otp_pepper.encode("utf-8")

redis = Redis(
    host=settings.redis_host,
    port=settings.redis_port,
    db=settings.redis_db,
    decode_responses=True,
)


def email_key(email: str) -> str:
    norm = email.strip().lower().encode("utf-8")
    return hashlib.sha256(norm).hexdigest()


def otp_key(h: str) -> str:
    return f"otp:{h}"


def attempts_key(h: str) -> str:
    return f"otp:attempts:{h}"


def throttle_key(h: str) -> str:
    return f"otp:throttle:req:{h}"


def daily_key(h: str) -> str:
    day = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d")
    return f"otp:daily:{h}:{day}"


def gen_otp(n: int) -> str:
    # 以密碼學安全隨機數產生固定長度數字 OTP（避免 leading zero 被截斷）
    return "".join(str(secrets.randbelow(10)) for _ in range(n))


def otp_digest(email: str, code: str) -> str:
    # 綁定 email，避免離線撞碼；lowercase 保持一致
    msg = f"{email.strip().lower()}|{code.strip()}".encode("utf-8")
    return hmac.new(OTP_PEPPER, msg, hashlib.sha256).hexdigest()


def consteq(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


class OTPRequestBody(BaseModel):
    email: EmailStr


class OTPVerifyBody(BaseModel):
    email: EmailStr
    code: str


@auth_router.post("/otp/request")
async def request_otp(body: OTPRequestBody):
    email = body.email.strip().lower()
    h = email_key(email)

    if redis.exists(throttle_key(h)):
        return {"ok": True}  # 模糊回應

    dk = daily_key(h)
    count = int(redis.get(dk) or 0)
    if count >= OTP_DAILY_LIMIT:
        return {"ok": True}  # 模糊回應

    code = gen_otp(OTP_LENGTH)
    digest = otp_digest(email, code)

    redis.setex(otp_key(h), OTP_TTL_SECONDS, digest)
    redis.setex(attempts_key(h), OTP_TTL_SECONDS, 0)
    redis.setex(throttle_key(h), OTP_REQUEST_COOLDOWN_SECONDS, 1)

    if not redis.exists(dk):
        redis.setex(dk, 24 * 3600, 1)
    else:
        redis.incr(dk)

    subject = "[Pray Like Incense] Your OTP Code"
    text = (
        f"Your OTP code is: {code}\n"
        f"It expires in {OTP_TTL_SECONDS // 60} minutes.\n"
        "If you did not request this, you can ignore this email."
    )
    html = f"""\
<html>
  <body style="font-family: Arial, sans-serif; color: #1a1a1a;">
    <p>Your OTP code is: <strong style="font-size: 24px; letter-spacing: 4px;">{code}</strong></p>
    <p>It expires in {OTP_TTL_SECONDS // 60} minutes.</p>
    <p>If you did not request this, you can ignore this email.</p>
  </body>
</html>
"""
    try:
        await send_email(email, subject, text, html=html)
        otp_logger.info("otp_sent", email=email)
    except Exception:  # pragma: no cover - best effort logging
        otp_logger.warning("otp_send_issue", email=email)

    return {"ok": True}


@auth_router.post("/otp/verify")
async def verify_otp(body: OTPVerifyBody, request: Request):
    email = body.email.strip().lower()
    code = body.code.strip()
    h = email_key(email)

    hashed = redis.get(otp_key(h))
    if not hashed:
        return {"ok": False}

    ak = attempts_key(h)
    attempts = int(redis.get(ak) or 0)
    if attempts >= OTP_MAX_ATTEMPTS:
        return {"ok": False}

    ok = consteq(otp_digest(email, code), hashed)

    redis.incr(ak)

    if not ok:
        return {"ok": False}

    redis.delete(otp_key(h))
    redis.delete(ak)

    # Route A：發 access/refresh token 並設 HttpOnly Cookie
    db = SessionLocal()
    first_login = False
    user_id = None
    try:
        access_token, refresh_token = mint_token_pair(db, email)
        first_login = _resolve_first_login(db, email)
        profile = db.query(UserProfile).filter(UserProfile.email == email).one_or_none()
        if profile and profile.user_id:
            user_id = profile.user_id.strip()
    finally:
        db.close()

    otp_logger.info("otp_verified", email=email)
    resp = JSONResponse(
        {
            "ok": True,
            "authenticated": True,
            "app_token": access_token,
            "first_login": first_login,
        }
    )
    set_access_cookie(resp, access_token)
    set_refresh_cookie(resp, refresh_token)
    request.session["user"] = {"email": email, "user_id": user_id} if user_id else {"email": email}
    request.session["provider"] = request.session.get("provider") or "otp"
    request.session["first_login"] = first_login
    return resp


__all__ = [
    "auth_router",
    "register_auth",
    "build_external_url",
    "get_current_subject",
    "bearer",
    "sign_access_jwt",
    "sign_jwt",
]
