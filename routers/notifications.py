from typing import List, Literal, Optional, Dict, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from dependencies import get_current_userid
from models import Notification, Post, utc_now
from services.notifications import (
    AMENED_POST_TYPE,
    COMMENTED_ON_POST_TYPE,
    POST_CREATED_TYPE,
    WITNESS_CREATED_TYPE
)

router = APIRouter(prefix="/api", tags=["Notifications"])


class NotificationItem(BaseModel):
    id: int
    recipient_user_id: str
    actor_user_id: str
    post_docid: str
    notification_type: str
    reason: Optional[str]
    is_read: bool
    created_at: Optional[str]
    read_at: Optional[str]
    post_title: Optional[str] = None
    post_preview: Optional[str] = None


class NotificationListData(BaseModel):
    notifications: List[NotificationItem]
    total: int
    unread_count: int


class NotificationListResponse(BaseModel):
    status: Literal["success"]
    data: NotificationListData
    message: str


class NotificationResponse(BaseModel):
    status: Literal["success"]
    data: NotificationItem
    message: str


@router.get(
    "/notifications",
    response_model=NotificationListResponse,
    summary="取得通知列表",
    response_description="回傳通知清單與未讀數"
)
def list_notifications(
    limit: int = Query(20, ge=1, le=100, description="每頁筆數"),
    offset: int = Query(0, ge=0, description="偏移量"),
    unread_only: bool = Query(False, description="僅列出未讀通知"),
    db: Session = Depends(get_db),
    current_user_id: str = Depends(get_current_userid)
):
    base_query = db.query(Notification).filter(
        Notification.recipient_user_id == current_user_id
    )
    unread_count = base_query.filter(Notification.is_read.is_(False)).count()

    query = base_query
    if unread_only:
        query = query.filter(Notification.is_read.is_(False))

    total_items = query.count()
    notifications = (
        query.order_by(Notification.created_at.desc()).offset(
            offset).limit(limit).all()
    )

    # 取得相關貼文的標題與內容摘要
    post_docids = {
        note.post_docid
        for note in notifications
        if note.notification_type in {POST_CREATED_TYPE, COMMENTED_ON_POST_TYPE, AMENED_POST_TYPE, WITNESS_CREATED_TYPE}
    }
    post_map: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    if post_docids:
        posts = db.query(Post.docid, Post.title, Post.content).filter(
            Post.docid.in_(post_docids)).all()
        post_map = {docid: (title, content) for docid, title, content in posts}

    def _preview(content: Optional[str], limit_len: int = 80) -> Optional[str]:
        if not content:
            return None
        cleaned = content.strip()
        if len(cleaned) <= limit_len:
            return cleaned
        return cleaned[:limit_len] + "..."

    return {
        "status": "success",
        "data": {
            "notifications": [
                {
                    **item.to_dict(),
                    "post_title": post_map.get(item.post_docid, (None, None))[0]
                    if item.post_docid in post_map
                    else None,
                    "post_preview": _preview(post_map.get(item.post_docid, (None, None))[1])
                    if item.post_docid in post_map
                    else None,
                }
                for item in notifications
            ],
            "total": total_items,
            "unread_count": unread_count
        },
        "message": "取得通知列表"
    }


@router.post(
    "/notifications/{notification_id}/read",
    response_model=NotificationResponse,
    summary="標記通知為已讀",
    response_description="回傳更新後的通知"
)
def mark_notification_read(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user_id: str = Depends(get_current_userid)
):
    notification = db.query(Notification).filter(
        Notification.id == notification_id).first()
    if not notification:
        raise HTTPException(status_code=404, detail="通知不存在")
    if notification.recipient_user_id != current_user_id:
        raise HTTPException(status_code=403, detail="無權限操作此通知")

    if not notification.is_read:
        notification.is_read = True
        notification.read_at = utc_now()
        db.add(notification)
        db.commit()
        db.refresh(notification)

    return {
        "status": "success",
        "data": notification.to_dict(),
        "message": "已標記為已讀"
    }
