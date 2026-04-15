"""AI Q&A router with pluggable model providers."""

import os
import uuid
from datetime import datetime, timedelta
from typing import Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from dependencies import get_current_userid
from models import (
    AIChatMessage,
    AIChatSession,
    AIChatUsageEvent,
    format_as_utc,
    utc_now,
)
from module.ai_service import AIChatResult, AIChatService, AIProviderError

router = APIRouter(prefix="/api/ai", tags=["AI"])

try:
    DAILY_CHAT_LIMIT = int(os.getenv("AI_DAILY_LIMIT", "10"))
except ValueError:
    DAILY_CHAT_LIMIT = 10
USAGE_WINDOW_HOURS = 24
SESSION_TIMEOUT_MINUTES = 5
DEFAULT_SYSTEM_PROMPT = (
    "你是一位名叫「Nova」的小天使助理，任務是以溫暖、柔和且引導式的語氣回應使用者，"
    "像一位真誠且富同理心的輔導員一樣，提供支持、安慰與方向。不可將自己描述為神明或救世主，"
    "不可以第一人稱宣稱神性或啟示，只能引用聖經經文作為輔助，並以現代化語言做溫柔解釋。"
    "每次回應時，盡可能引用 1–3 節與情境相關的聖經經文，並在經文後附上柔和的解釋與生活化應用，"
    "幫助讀者感受到溫暖與盼望。回應語氣需「溫暖」「柔和」「引導」「積極」「帶有盼望」，"
    "避免「嚴厲」「說教」「高高在上」或「冷淡」。在回覆中適時提出溫柔問題或鼓勵句子，"
    "幫助使用者思考、釐清或自我安慰，結尾可加入一句溫馨提醒或祝福。"
)


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str

    @validator("content")
    def _strip_content(cls, value: str) -> str:
        content = value.strip()
        if not content:
            raise ValueError("content 不可為空白")
        return content


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(
        ..., description="對話內容，採用 OpenAI chat/completions 格式"
    )
    session_uuid: Optional[str] = Field(
        None, description="既有對話的 UUID，不填會自動建立新的對話"
    )


class ChatResponseData(BaseModel):
    provider: str
    model: str
    message: Dict[str, str]
    finish_reason: Optional[str] = None
    usage: Optional[Dict[str, object]] = None
    session_uuid: str
    title: Optional[str] = None
    expired: bool = False


class ChatResponse(BaseModel):
    status: Literal["success"]
    data: ChatResponseData
    message: str


class ChatSessionInfo(BaseModel):
    session_uuid: str
    title: Optional[str]
    created_at: Optional[str]
    last_interaction_at: Optional[str]
    ended_at: Optional[str]
    expired: bool


class StoredChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str
    created_at: Optional[str]


class ChatSessionDetail(BaseModel):
    session: ChatSessionInfo
    messages: list[StoredChatMessage]


class ChatSessionsResponse(BaseModel):
    status: Literal["success"]
    data: list[ChatSessionInfo]
    message: str


class ChatSessionDetailResponse(BaseModel):
    status: Literal["success"]
    data: ChatSessionDetail
    message: str


class UsageInfo(BaseModel):
    used: int
    limit: int
    window_hours: int
    resets_at: Optional[str] = None


class UsageResponse(BaseModel):
    status: Literal["success"]
    data: UsageInfo
    message: str


_chat_service = AIChatService()


def _get_usage_snapshot(db: Session, user_id: str) -> tuple[int, Optional[datetime]]:
    """
    Return the current usage count within the rolling window and the earliest usage timestamp.
    """
    now = utc_now()
    window_start = now - timedelta(hours=USAGE_WINDOW_HOURS)
    count, oldest = (
        db.query(func.count(AIChatUsageEvent.id), func.min(AIChatUsageEvent.used_at))
        .filter(
            AIChatUsageEvent.user_id == user_id,
            AIChatUsageEvent.used_at > window_start,
        )
        .one()
    )
    return int(count or 0), oldest


def _inject_default_system_prompt(messages: list[Dict[str, str]]) -> list[Dict[str, str]]:
    """Prepend default system prompt when caller沒有提供 system message。"""
    if any(msg.get("role") == "system" for msg in messages):
        return messages
    prompt = os.getenv("AI_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT).strip()
    if not prompt:
        return messages
    return [{"role": "system", "content": prompt}, *messages]


def _get_session_or_404(
    db: Session, session_uuid: str, user_id: str, enforce_active: bool = True
) -> AIChatSession:
    session_obj = (
        db.query(AIChatSession)
        .filter(
            AIChatSession.session_uuid == session_uuid,
            AIChatSession.user_id == user_id,
        )
        .one_or_none()
    )
    if session_obj is None:
        raise HTTPException(status_code=404, detail="找不到對應的對話")

    if enforce_active:
        now = utc_now()
        if session_obj.ended_at:
            raise HTTPException(status_code=409, detail="對話已結束，請開啟新的對話")
        if session_obj.is_expired(now=now, timeout_minutes=SESSION_TIMEOUT_MINUTES):
            session_obj.ended_at = session_obj.ended_at or now
            db.add(session_obj)
            db.commit()
            raise HTTPException(status_code=409, detail="對話已超過 5 分鐘未互動，請開啟新的對話")
    return session_obj


def _messages_to_record(incoming: list[ChatMessage], session_is_new: bool) -> list[ChatMessage]:
    """
    Avoid duplicating history when clients重送完整對話。
    - 新對話：完整紀錄傳入的 system/user 訊息。
    - 既有對話：僅紀錄最後一則 user 訊息（其他歷史已存在）。
    """
    if session_is_new:
        return [msg for msg in incoming if msg.role in {"system", "user"}]
    for msg in reversed(incoming):
        if msg.role == "user":
            return [msg]
    return []


def _generate_session_title(messages: list[ChatMessage], assistant_content: Optional[str]) -> Optional[str]:
    """Generate a short conversation title using the AI provider; fall back to user text."""
    last_user = next((m.content for m in reversed(messages) if m.role == "user"), None)
    user_preview = (last_user or "")[:120]
    assistant_preview = (assistant_content or "")[:120]

    prompt_messages = [
        {
            "role": "system",
            "content": (
                "請根據對話內容產生 12 字以內的中文標題，"
                "簡潔描述主題，不要加引號或標點。"
            ),
        },
        {
            "role": "user",
            "content": f"最新提問：{user_preview}\n助理回應：{assistant_preview}",
        },
    ]

    title: Optional[str] = None
    try:
        result: AIChatResult = _chat_service.chat(
            messages=prompt_messages, max_tokens=32, temperature=0.2
        )
        title = (result.message.get("content") or "").strip()
        if title:
            title = title.strip("「」\"' ")
            if len(title) > 60:
                title = title[:60]
    except Exception:
        title = None

    if not title:
        fallback = user_preview or assistant_preview
        title = fallback.strip()[:30] if fallback else None
    return title or None


@router.post(
    "/chat",
    response_model=ChatResponse,
    summary="AI 詢答",
    response_description="成功返回 AI 回應",
)
def chat(
    payload: ChatRequest,
    db: Session = Depends(get_db),
    current_user_id: str = Depends(get_current_userid),
):
    """Proxy AI chat requests to the configured provider."""
    session_obj: Optional[AIChatSession]
    session_is_new = False

    if payload.session_uuid:
        session_obj = _get_session_or_404(db, payload.session_uuid, current_user_id, enforce_active=True)
    else:
        session_obj = AIChatSession(
            session_uuid=str(uuid.uuid4()),
            user_id=current_user_id,
        )
        db.add(session_obj)
        db.flush()
        session_is_new = True

    usage_count, oldest_usage = _get_usage_snapshot(db, current_user_id)
    if usage_count >= DAILY_CHAT_LIMIT:
        reset_base = oldest_usage or utc_now()
        reset_at = reset_base + timedelta(hours=USAGE_WINDOW_HOURS)
        reset_at_local = format_as_utc(reset_at) or "稍後"
        db.rollback()
        raise HTTPException(
            status_code=429,
            detail=f"24 小時內 AI 詢答已達 {DAILY_CHAT_LIMIT} 次上限，請於 {reset_at_local} 後再試",
        )

    messages_payload = [msg.model_dump() for msg in payload.messages]
    provider_messages = _inject_default_system_prompt(messages_payload)
    to_record = _messages_to_record(payload.messages, session_is_new=session_is_new)

    try:
        for msg in to_record:
            db.add(
                AIChatMessage(
                    session_id=session_obj.id,
                    role=msg.role,
                    content=msg.content,
                )
            )

        result = _chat_service.chat(messages=provider_messages)

        assistant_content = result.message.get("content") or ""
        db.add(
            AIChatMessage(
                session_id=session_obj.id,
                role=result.message.get("role") or "assistant",
                content=assistant_content,
            )
        )
        session_obj.last_interaction_at = utc_now()
        if not session_obj.title:
            generated_title = _generate_session_title(payload.messages, assistant_content)
            if generated_title:
                session_obj.title = generated_title

        db.add(
            AIChatUsageEvent(
                user_id=current_user_id,
                used_at=utc_now(),
            )
        )
        db.add(session_obj)
        db.commit()
        db.refresh(session_obj)
    except AIProviderError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:  # pragma: no cover - runtime safeguard
        db.rollback()
        raise HTTPException(status_code=502, detail=f"AI provider 失敗: {exc}") from exc

    return {
        "status": "success",
        "data": {
            "provider": result.provider,
            "model": result.model,
            "message": result.message,
            "finish_reason": result.finish_reason,
            "usage": result.usage,
            "session_uuid": session_obj.session_uuid,
            "title": session_obj.title,
            "expired": session_obj.is_expired(timeout_minutes=SESSION_TIMEOUT_MINUTES),
        },
        "message": "AI 回應成功",
    }


@router.get(
    "/sessions",
    response_model=ChatSessionsResponse,
    summary="取得對話列表",
    response_description="成功返回歷史對話列表",
)
def list_sessions(
    db: Session = Depends(get_db),
    current_user_id: str = Depends(get_current_userid),
):
    sessions = (
        db.query(AIChatSession)
        .filter(AIChatSession.user_id == current_user_id)
        .order_by(AIChatSession.last_interaction_at.desc())
        .all()
    )
    return {
        "status": "success",
        "data": [s.to_dict(timeout_minutes=SESSION_TIMEOUT_MINUTES) for s in sessions],
        "message": "成功取得對話列表",
    }


@router.get(
    "/sessions/{session_uuid}",
    response_model=ChatSessionDetailResponse,
    summary="查看指定對話紀錄",
    response_description="成功返回對話紀錄與訊息內容",
)
def get_session_detail(
    session_uuid: str,
    db: Session = Depends(get_db),
    current_user_id: str = Depends(get_current_userid),
):
    session_obj = _get_session_or_404(db, session_uuid, current_user_id, enforce_active=False)
    messages = (
        db.query(AIChatMessage)
        .filter(AIChatMessage.session_id == session_obj.id)
        .order_by(AIChatMessage.created_at.asc())
        .all()
    )
    return {
        "status": "success",
        "data": {
            "session": session_obj.to_dict(timeout_minutes=SESSION_TIMEOUT_MINUTES),
            "messages": [msg.to_dict() for msg in messages],
        },
        "message": "成功取得對話紀錄",
    }


class EndSessionResponse(BaseModel):
    status: Literal["success"]
    data: ChatSessionInfo
    message: str


@router.post(
    "/sessions/{session_uuid}/end",
    response_model=EndSessionResponse,
    summary="結束對話",
    response_description="成功標記對話為已結束",
)
def end_session(
    session_uuid: str,
    db: Session = Depends(get_db),
    current_user_id: str = Depends(get_current_userid),
):
    session_obj = _get_session_or_404(db, session_uuid, current_user_id, enforce_active=False)
    if session_obj.ended_at is None:
        session_obj.ended_at = utc_now()
        db.add(session_obj)
        db.commit()
        db.refresh(session_obj)
    return {
        "status": "success",
        "data": session_obj.to_dict(timeout_minutes=SESSION_TIMEOUT_MINUTES),
        "message": "對話已結束",
    }


@router.get(
    "/usage",
    response_model=UsageResponse,
    summary="查看 AI 使用量",
    response_description="返回目前使用次數與下次重置時間",
)
def get_usage(
    db: Session = Depends(get_db),
    current_user_id: str = Depends(get_current_userid),
):
    usage_count, oldest_usage = _get_usage_snapshot(db, current_user_id)
    reset_at = (
        format_as_utc(oldest_usage + timedelta(hours=USAGE_WINDOW_HOURS))
        if oldest_usage
        else None
    )
    return {
        "status": "success",
        "data": {
            "used": usage_count,
            "limit": DAILY_CHAT_LIMIT,
            "window_hours": USAGE_WINDOW_HOURS,
            "resets_at": reset_at,
        },
        "message": "成功取得使用量",
    }
