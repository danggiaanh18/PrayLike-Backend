"""Tribe activities module: independent flow for event planning and publishing."""

import logging
import math
import uuid
from datetime import date, time
from pathlib import Path
from typing import List, Literal, Optional, Iterable

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile, Query, status
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session
from sqlalchemy.orm import aliased

from database import get_db
from dependencies import get_current_userid
from models import TribeActivity, UserProfile, utc_now
from services.notifications import create_tribe_activity_notifications
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/activities", tags=["Tribe Activities"])

ACTIVITY_UPLOAD_DIR = Path("uploads") / "activities"
ACTIVITY_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_ACTIVITY_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB，與 Nginx 限制同步

class ActivityBase(BaseModel):
    title: str
    location: str
    event_date: date
    event_time: time


class ActivityCreate(ActivityBase):
    description: Optional[str] = None
    cover_image: Optional[str] = None
    images: List[str] = Field(default_factory=list)


class PageInfo(BaseModel):
    current_page: int
    total_pages: int
    total_items: int
    per_page: int
    has_next: bool
    has_prev: bool


class ActivityItem(BaseModel):
    id: int
    title: str
    location: str
    event_date: date
    event_time: str
    description: Optional[str]
    cover_image: Optional[str]
    images: List[str]
    tribe: Optional[int]
    created_by: str
    is_published: bool
    created_at: Optional[str]
    updated_at: Optional[str]
    published_at: Optional[str]
    creator_avatar_url: Optional[str] = None

    class Config:
        from_attributes = True


class ActivityResponse(BaseModel):
    status: Literal["success"]
    data: ActivityItem
    message: str


class ActivityListData(BaseModel):
    activities: List[ActivityItem]
    pageinfo: PageInfo


class ActivityListResponse(BaseModel):
    status: Literal["success"]
    data: ActivityListData
    message: str


def _format_time_value(value: time) -> str:
    return value.strftime("%H:%M")


def _parse_date_input(raw: Optional[str | date]) -> date:
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw.strip())
        except ValueError:
            raise HTTPException(status_code=400, detail="event_date 格式錯誤，需 YYYY-MM-DD")
    raise HTTPException(status_code=400, detail="event_date 必填")


def _parse_time_input(raw: Optional[str | time]) -> str:
    if isinstance(raw, time):
        return _format_time_value(raw)
    if isinstance(raw, str):
        try:
            parsed = time.fromisoformat(raw.strip())
            return _format_time_value(parsed)
        except ValueError:
            raise HTTPException(status_code=400, detail="event_time 格式錯誤，需 HH:MM")
    raise HTTPException(status_code=400, detail="event_time 必填")


def _resolve_tribe(db: Session, user_id: Optional[str]) -> Optional[int]:
    if not user_id:
        return None
    profile = db.query(UserProfile).filter(UserProfile.user_id == user_id).one_or_none()
    if profile and profile.tribe is not None:
        return int(profile.tribe)
    return None


def _activity_query_with_creator_tribe(db: Session):
    creator_profile = aliased(UserProfile)
    query = db.query(
        TribeActivity,
        creator_profile.tribe.label("creator_tribe"),
        creator_profile.avatar_url.label("creator_avatar_url")
    ).outerjoin(creator_profile, creator_profile.user_id == TribeActivity.created_by)
    return query, creator_profile


def _get_activity_or_404(db: Session, activity_id: int):
    query, _ = _activity_query_with_creator_tribe(db)
    record = query.filter(TribeActivity.id == activity_id).one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="活動不存在")
    return record


def _require_owner(activity: TribeActivity, user_id: str):
    if activity.created_by != user_id:
        raise HTTPException(status_code=403, detail="僅限活動建立者可以執行此操作")


def _to_item(
    activity: TribeActivity, 
    creator_tribe: Optional[int] = None, 
    creator_avatar_url: Optional[str] = None 
) -> ActivityItem:
    if creator_tribe is not None:
        activity._creator_tribe = creator_tribe
    
    data = activity.to_dict()
    data["creator_avatar_url"] = creator_avatar_url
    
    return ActivityItem(**data)

async def _save_upload_files(files: Optional[Iterable[UploadFile]]) -> List[str]:
    saved_paths: List[str] = []
    if not files:
        return saved_paths
    total_size = 0
    try:
        for file in files:
            suffix = Path(file.filename).suffix if file.filename else ""
            filename = f"{uuid.uuid4().hex}{suffix}"
            destination = ACTIVITY_UPLOAD_DIR / filename
            content = await file.read()
            total_size += len(content)
            if total_size > MAX_ACTIVITY_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="圖片檔案太大，總大小需不超過 20MB"
                )
            with destination.open("wb") as buffer:
                buffer.write(content)
            saved_paths.append(str(destination))
        return saved_paths
    except Exception:
        for saved in saved_paths:
            try:
                Path(saved).unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _merge_images(
    existing: List[str],
    new_images: List[str],
    cover_image: Optional[str] = None
) -> List[str]:
    images = [img for img in existing if img]
    if new_images:
        images += [img for img in new_images if img]
    if cover_image:
        if cover_image in images:
            images = [cover_image] + [img for img in images if img != cover_image]
        else:
            images = [cover_image] + images
    return images


@router.post(
    "",
    response_model=ActivityResponse,
    summary="新增支派活動",
    response_description="成功建立活動資訊"
)
async def create_activity(
    payload: Optional[ActivityCreate] = Body(None),
    title: Optional[str] = Form(None),
    location: Optional[str] = Form(None),
    event_date: Optional[str] = Form(None),
    event_time: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    cover_image: Optional[str] = Form(None),
    images: Optional[List[str]] = Form(None),
    files: Optional[List[UploadFile]] = File(None),
    db: Session = Depends(get_db),
    current_user_id: str = Depends(get_current_userid)
):
    user_profile = db.query(UserProfile).filter(UserProfile.user_id == current_user_id).one_or_none()
    tribe = int(user_profile.tribe) if user_profile and user_profile.tribe is not None else None
    user_avatar = user_profile.avatar_url if user_profile else None # 取得當前用戶頭像

    if tribe is None:
        raise HTTPException(status_code=403, detail="尚未設定支派，請先完成支派設定後再建立活動")

    data = payload
    if data is None:
        if not all([title, location, event_date, event_time]):
            raise HTTPException(status_code=400, detail="缺少必要欄位：title、location、event_date、event_time")
        data = ActivityCreate(
            title=title,
            location=location,
            event_date=_parse_date_input(event_date),
            event_time=_parse_time_input(event_time),
            description=description,
            cover_image=cover_image,
            images=images or []
        )

    new_images = await _save_upload_files(files)
    all_images = _merge_images(data.images or [], new_images, cover_image=data.cover_image)

    activity = TribeActivity(
        title=data.title,
        location=data.location,
        event_date=data.event_date,
        event_time=_format_time_value(data.event_time),
        description=data.description,
        created_by=current_user_id
    )
    if not getattr(activity, "activity_id", None):
        activity.activity_id = str(uuid.uuid4())
    activity.set_images(all_images)
    if tribe is not None:
        activity._creator_tribe = tribe
    # 新增即發布
    activity.is_published = True
    activity.published_at = utc_now()
    try:
        db.add(activity)
        db.commit()
        db.refresh(activity)
        try:
            create_tribe_activity_notifications(
                db,
                activity_id=str(activity.activity_id),
                creator_user_id=current_user_id,
            )
        except Exception as exc:  # noqa: PERF203
            logger = logging.getLogger(__name__)
            logger.warning("Failed to notify tribe activity %s: %s", activity.activity_id, exc)
        return {
            "status": "success",
            "data": _to_item(activity, creator_tribe=tribe, creator_avatar_url=user_avatar),
            "message": "活動已建立"
        }
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"新增活動失敗: {exc}") from exc


@router.get(
    "",
    response_model=ActivityListResponse,
    summary="取得支派活動列表",
    response_description="依條件返回活動資料"
)
def list_activities(
    page: int = Query(1, ge=1, description="頁碼"),
    limit: int = Query(10, ge=1, le=100, description="每頁筆數"),
    db: Session = Depends(get_db),
    current_user_id: str = Depends(get_current_userid)
):
    viewer_tribe = _resolve_tribe(db, current_user_id)
    if viewer_tribe is None:
        raise HTTPException(status_code=403, detail="尚未設定支派，無法查看活動")

    query, creator_profile = _activity_query_with_creator_tribe(db)
    # ✅ 新增邏輯：全域支派 vs 一般支派的可視範圍
    if viewer_tribe == 0:
        # 支派 0 可以看到所有已發布的活動，以及自己建立的草稿/活動
        query = query.filter(
            or_(
                TribeActivity.created_by == current_user_id,
                TribeActivity.status == "published"
            )
        )
    else:
        # 一般支派能看到：自己建立的、同支派已發布的、全域支派(0)已發布的活動
        query = query.filter(
            or_(
                TribeActivity.created_by == current_user_id,
                and_(
                    or_(creator_profile.tribe == viewer_tribe, creator_profile.tribe == 0),
                    TribeActivity.status == "published"
                )
            )
        )

    total_items = query.count()
    total_pages = max(math.ceil(total_items / limit), 1)
    offset = (page - 1) * limit

    activities = query.order_by(
        TribeActivity.event_date.asc(),
        TribeActivity.event_time.asc()
    ).offset(offset).limit(limit).all()

    return {
        "status": "success",
        "data": {
            "activities": [_to_item(activity, creator_tribe, avatar) for activity, creator_tribe, avatar in activities],
            "pageinfo": {
                "current_page": page,
                "total_pages": max(total_pages, 1),
                "total_items": total_items,
                "per_page": limit,
                "has_next": page < total_pages,
                "has_prev": page > 1
            }
        },
        "message": "取得活動列表成功"
    }


@router.get(
    "/{activity_id}",
    response_model=ActivityResponse,
    summary="取得單一活動內容",
    response_description="返回單筆活動資料"
)
def get_activity(
    activity_id: int,
    db: Session = Depends(get_db),
    current_user_id: str = Depends(get_current_userid)
):
    activity, creator_tribe, creator_avatar_url = _get_activity_or_404(db, activity_id)
    if activity.created_by != current_user_id:
        viewer_tribe = _resolve_tribe(db, current_user_id)
        # ✅ 新增邏輯：判斷權限
        is_global_viewer = (viewer_tribe == 0)
        is_same_tribe = (creator_tribe == viewer_tribe)
        is_global_activity = (creator_tribe == 0)
        has_permission = is_global_viewer or is_same_tribe or is_global_activity
        
        if viewer_tribe is None or creator_tribe != viewer_tribe or not activity.is_published:
            raise HTTPException(status_code=403, detail="僅限同支派成員查看")
    return {
        "status": "success",
        "data": _to_item(activity, creator_tribe=creator_tribe, creator_avatar_url=creator_avatar_url),
        "message": "取得活動成功"
    }
