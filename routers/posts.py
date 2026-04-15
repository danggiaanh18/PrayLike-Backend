from enum import Enum
import logging
import math
import uuid
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, exists, func, literal, or_
from sqlalchemy.orm import Session

from database import get_db
from dependencies import get_current_userid, get_optional_userid
from models import Post, PostAmen, UserFriend, UserProfile
from services.notifications import create_post_notifications
from ycoin.ycoin_service import safe_award_for_postlike
from pydantic import BaseModel

router = APIRouter(prefix="/api", tags=["Posts"])
logger = logging.getLogger(__name__)

# --- Enums & Schemas ---

class Category(str, Enum):
    PERSONAL_FAMILY = "Personal & Family"
    CHURCH_MINISTRY = "Church & Ministry"
    KINGDOM_PRAYER = "Kingdom Prayer"
    TRIBE_PRAYER = "Tribe Prayer"

class Privacy(str, Enum):
    PUBLIC = "public"
    FAMILY = "family"
    TRIBE = "tribe"
    INDIVIDUAL = "individual"

class Event(str, Enum):
    POST = "post"
    COMMENT = "comment"
    WITNESS = "witness"

class PostCreate(BaseModel):
    content: str
    title: Optional[str] = None
    uuid: Optional[str] = None
    category: Category
    privacy: Privacy = Privacy.PUBLIC

# MỚI: Schema dùng để cập nhật bài viết
class PostUpdate(BaseModel):
    content: str
    title: Optional[str] = None
    category: Optional[Category] = None
    privacy: Optional[Privacy] = None

class PostItem(BaseModel):
    sn: int
    docid: Optional[str]
    userid: str
    content: str
    title: Optional[str]
    datetime: Optional[str]
    uuid: Optional[str]
    parent_docid: Optional[str]
    event: str
    timezone: Optional[str]
    category: Optional[Category]
    privacy: Privacy
    amen_count: int = 0
    amened: bool = False
    witnessed: bool = False
    image_url: Optional[str] = None
    avatar_url: Optional[str] = None

class CreatePostResponse(BaseModel):
    status: Literal["success"]
    data: PostItem
    message: str

class PostFilterDescription(BaseModel):
    userid: Optional[str]
    event: Optional[str]
    category: Optional[str]
    privacy: Optional[str]
    description: str

class PageInfo(BaseModel):
    current_page: int
    total_pages: int
    total_items: int
    per_page: int
    has_next: bool
    has_prev: bool

class PostsPageData(BaseModel):
    posts: List[PostItem]
    pageinfo: PageInfo
    filter: PostFilterDescription

class GetPostsResponse(BaseModel):
    status: Literal["success"]
    data: PostsPageData
    message: str

# --- API Endpoints ---

@router.post(
    "/posts",
    response_model=CreatePostResponse,
    summary="新增貼文",
    response_description="成功返回新增貼文的內容"
)
def create_post(
    post: PostCreate,
    db: Session = Depends(get_db),
    current_user_id: str = Depends(get_current_userid)
):
    """上傳貼文"""
    try:
        generated_docid = str(uuid.uuid4())

        db_post = Post(
            docid=generated_docid,  # 手動設定 docid
            userid=current_user_id,
            content=post.content,
            title=post.title,
            uuid=post.uuid,  # 可選的 uuid，可能是 None
            category=post.category.value,
            privacy=post.privacy.value,
            event=Event.POST.value,
        )
        db.add(db_post)
        db.commit()
        db.refresh(db_post)
        try:
            safe_award_for_postlike(db, post_obj=db_post)
        except Exception:
            pass

        try:
            create_post_notifications(
                db,
                post_docid=db_post.docid,
                author_user_id=current_user_id,
                privacy=db_post.privacy,
            )
        except Exception as exc:  # noqa: PERF203
            logger.warning("Failed to create notifications for post %s: %s", db_post.docid, exc)

        return {
            "status": "success",
            "data": db_post.to_dict(),
            "message": "發文成功"
        }
    except HTTPException as e:
        db.rollback()
        raise e
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"發布失敗: {str(e)}")

@router.put("/posts/{sn}", summary="修改貼文/留言")
def update_post(sn: int, post_data: PostUpdate, db: Session = Depends(get_db), current_user_id: str = Depends(get_current_userid)):
    """Cập nhật nội dung bài viết theo sn"""
    db_post = db.query(Post).filter(Post.sn == sn, Post.userid == current_user_id).first()
    
    if not db_post or getattr(db_post, "is_deleted", 0) == 1:
        raise HTTPException(status_code=404, detail="貼文不存在、已刪除或無權限修改")

    db_post.content = post_data.content
    if post_data.title: db_post.title = post_data.title
    if post_data.category: db_post.category = post_data.category.value
    if post_data.privacy: db_post.privacy = post_data.privacy.value

    db.commit()
    db.refresh(db_post)
    return {"status": "success", "message": "修改成功", "data": db_post.to_dict()}

@router.delete("/posts/{sn}", summary="刪除貼文 (軟刪除)")
def delete_post_soft(sn: int, db: Session = Depends(get_db), current_user_id: str = Depends(get_current_userid)):
    """Xóa mềm bài viết bằng cách đặt is_deleted = 1"""
    db_post = db.query(Post).filter(Post.sn == sn, Post.userid == current_user_id).first()
    
    if not db_post:
        raise HTTPException(status_code=404, detail="貼文不存在或無權限刪除")

    db_post.is_deleted = 1
    db.commit()
    return {"status": "success", "message": "貼文已刪除"}

@router.get("/page", response_model=GetPostsResponse, summary="查詢貼文分頁")
def get_posts(
        userid: Optional[str] = Query(None),
        event: Optional[Event] = Query(None),
        category: Optional[Category] = Query(None),
        privacy: Optional[Privacy] = Query(None),
        page: int = Query(1, ge=1),
        limit: int = Query(10, ge=1, le=100),
        db: Session = Depends(get_db),
        viewer_user_id: Optional[str] = Depends(get_optional_userid)
):
    """Truy vấn danh sách bài viết (Chỉ lấy bài có is_deleted = 0)"""
    try:
        resolved_viewer_id = viewer_user_id
        viewer_tribe = None
        if resolved_viewer_id:
            viewer_tribe = db.query(UserProfile.tribe).filter(UserProfile.user_id == resolved_viewer_id).scalar()

        author_profile_subq = db.query(UserProfile.user_id, UserProfile.tribe, UserProfile.avatar_url).subquery()

        # BỘ LỌC QUAN TRỌNG: Loại bỏ bài viết đã xóa
        base_query = db.query(Post).outerjoin(
            author_profile_subq, Post.userid == author_profile_subq.c.user_id
        ).filter(or_(Post.is_deleted == 0, Post.is_deleted == None))

        if userid: base_query = base_query.filter(Post.userid == userid)
        if event: base_query = base_query.filter(Post.event == event.value)
        if category: base_query = base_query.filter(Post.category == category.value)
        if privacy: base_query = base_query.filter(Post.privacy == privacy.value)

        # Phân quyền hiển thị (Public, Family, Tribe)
        if resolved_viewer_id:
            visibility_conditions = [Post.privacy == Privacy.PUBLIC.value, Post.userid == resolved_viewer_id]
            friend_exists_clause = exists().where(or_(
                and_(UserFriend.user_id == resolved_viewer_id, UserFriend.friend_user_id == Post.userid),
                and_(UserFriend.user_id == Post.userid, UserFriend.friend_user_id == resolved_viewer_id)
            ))
            if viewer_tribe is not None:
                visibility_conditions.append(and_(Post.privacy == Privacy.TRIBE.value, author_profile_subq.c.tribe == viewer_tribe))
            visibility_conditions.append(and_(Post.privacy == Privacy.FAMILY.value, friend_exists_clause))
            base_query = base_query.filter(or_(*visibility_conditions))
        else:
            base_query = base_query.filter(Post.privacy == Privacy.PUBLIC.value)

        total_items = base_query.count()
        total_pages = math.ceil(total_items / limit) if total_items > 0 else 1
        offset = (page - 1) * limit
        
        # Sắp xếp theo thời gian mới nhất
        results = base_query.order_by(Post.datetime.desc()).offset(offset).limit(limit).all()
        
        posts = []
        for post in results:
            post_dict = post.to_dict()
            # Bổ sung logic hiển thị avatar hoặc amen_count nếu cần
            posts.append(post_dict)

        return {
            "status": "success",
            "data": {
                "posts": posts,
                "pageinfo": {"current_page": page, "total_pages": total_pages, "total_items": total_items, "per_page": limit, "has_next": page < total_pages, "has_prev": page > 1},
                "filter": {"userid": userid, "event": event.value if event else None, "category": category.value if category else None, "privacy": privacy.value if privacy else None, "description": "成功取得貼文"}
            },
            "message": "查詢成功"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
