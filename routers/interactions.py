import logging
import math
import uuid
from pathlib import Path
from typing import List, Literal, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import and_, exists, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from dependencies import get_current_userid, get_optional_userid
from models import Post, PostAmen, UserFriend, UserProfile
from routers.posts import Category, Event, PostItem, Privacy
from services.notifications import (
    create_amen_notification,
    create_comment_notification,
    create_post_notifications,
    WITNESS_CREATED_TYPE
)
from ycoin.ycoin_service import ensure_amen_granted, remove_amen_grant, safe_award_for_postlike
from pydantic import BaseModel

router = APIRouter(prefix="/api", tags=["Interactions"])
WITNESS_UPLOAD_DIR = Path("uploads") / "witnesses"
WITNESS_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_WITNESS_IMAGE_BYTES = 10 * 1024 * 1024  # 10MB 單張限制
logger = logging.getLogger(__name__)


class CommentCreate(BaseModel):
    content: str
    docid: str


class TargetPostSummary(BaseModel):
    sn: int
    docid: Optional[str]
    content: str


class CommentResponseData(BaseModel):
    comment: PostItem
    target_post: TargetPostSummary


class CreateCommentResponse(BaseModel):
    status: Literal["success"]
    data: CommentResponseData
    message: str


class WitnessCreate(BaseModel):
    content: str
    parent_docid: str


class WitnessTargetSummary(BaseModel):
    sn: int
    docid: Optional[str]
    content: str
    category: Optional[Category]


class WitnessResponseData(BaseModel):
    witness: PostItem
    target_post: WitnessTargetSummary


class CreateWitnessResponse(BaseModel):
    status: Literal["success"]
    data: WitnessResponseData
    message: str


class WitnessListEntry(PostItem):
    original_post: Optional[PostItem] = None


class PageInfo(BaseModel):
    current_page: int
    total_pages: int
    total_items: int
    per_page: int
    has_next: bool
    has_prev: bool


class WitnessListData(BaseModel):
    witnesses: List[WitnessListEntry]
    pageinfo: PageInfo


class GetWitnessListResponse(BaseModel):
    status: Literal["success"]
    data: WitnessListData
    message: str


async def _save_witness_image(file: UploadFile) -> str:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="圖片內容不可為空")
    if len(content) > MAX_WITNESS_IMAGE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="見證圖片大小不可超過 10MB",
        )
    suffix = Path(file.filename).suffix if file.filename else ""
    filename = f"{uuid.uuid4().hex}{suffix}"
    destination = WITNESS_UPLOAD_DIR / filename
    with destination.open("wb") as buffer:
        buffer.write(content)
    return str(destination)


def _delete_witness_image(path: Optional[str]) -> None:
    if not path:
        return
    try:
        target = Path(path).resolve()
        base = WITNESS_UPLOAD_DIR.resolve()
        target.relative_to(base)
    except Exception:
        return
    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass


@router.post(
    "/comment",
    response_model=CreateCommentResponse,
    summary="新增留言",
    response_description="成功返回留言和目標貼文摘要"
)
def create_comment(
    comment: CommentCreate,
    db: Session = Depends(get_db),
    current_user_id: str = Depends(get_current_userid)
):
    """新增留言"""
    try:
        docid = comment.docid.strip()
        if not docid:
            raise HTTPException(status_code=400, detail="docid 不可為空")

        # 找到要留言的原貼文（使用 docid 和 event 欄位）
        target_post = db.query(Post).filter(
            Post.docid == docid,
            Post.event.in_([Event.POST.value, Event.WITNESS.value])  # 確保是貼文
        ).first()
        target_event = (
            target_post._resolved_event() if target_post and hasattr(
                target_post, "_resolved_event") else getattr(target_post, "event", "")
        ) or ""

        if not target_post:
            raise HTTPException(
                status_code=404,
                detail=f"找不到 docid {docid} 的貼文"
            )

        generated_docid = str(uuid.uuid4())

        # 建立留言記錄
        db_comment = Post(
            docid=generated_docid,
            userid=current_user_id,
            content=comment.content,
            parent_docid=target_post.docid,  # 指向原貼文的 docid
            category=target_post.category,
            privacy=target_post.privacy,
            event=Event.COMMENT.value  # 設定為 "comment" 事件
        )

        db.add(db_comment)
        db.commit()
        db.refresh(db_comment)
        try:
            safe_award_for_postlike(db, post_obj=db_comment)
        except Exception:
            pass
        if target_event == Event.POST.value:
            try:
                create_comment_notification(
                    db,
                    post_docid=target_post.docid,
                    post_owner_id=target_post.userid,
                    actor_user_id=current_user_id,
                )
            except Exception as exc:  # noqa: PERF203
                logger.warning(
                    "Failed to create comment notification for %s: %s", target_post.docid, exc)

        return {
            "status": "success",
            "data": {
                "comment": db_comment.to_dict(),
                "target_post": {
                    "sn": target_post.sn,
                    "docid": target_post.docid,
                    "content": target_post.content[:50] + "..." if len(
                        target_post.content) > 50 else target_post.content
                }
            },
            "message": f"成功對貼文 {docid} 留言"
        }

    except HTTPException as e:
        db.rollback()
        raise e
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"留言失敗: {str(e)}")


@router.post(
    "/posts/{docid}/amen",
    summary="Amen 或收回 Amen",
    response_description="成功返回 Amen 結果"
)
def amen_post(
    docid: str,
    db: Session = Depends(get_db),
    current_user_id: str = Depends(get_current_userid)
):
    """對指定貼文進行 Amen，若已 Amen 則改為收回"""
    docid = docid.strip() if docid else docid
    if not docid:
        raise HTTPException(status_code=400, detail="docid 不可為空")

    amen_created = False
    target_post = db.query(Post).filter(
        Post.docid == docid,
        Post.event.in_(
            [Event.POST.value, Event.WITNESS.value, Event.COMMENT.value])
    ).first()

    if not target_post:
        raise HTTPException(status_code=404, detail=f"找不到 docid {docid} 的貼文")
    target_event = (target_post._resolved_event() if hasattr(
        target_post, "_resolved_event") else target_post.event) or ""
    is_comment = target_event == Event.COMMENT.value

    # 嘗試先刪除既有 Amen，若有則視為收回操作
    deleted = db.query(PostAmen).filter(
        PostAmen.post_docid == docid,
        PostAmen.userid == current_user_id
    ).delete(synchronize_session=False)

    if deleted:
        target_post.cached_amen_count = max(
            (target_post.cached_amen_count or 0) - deleted, 0)
        try:
            db.add(target_post)
            db.commit()
            db.refresh(target_post)
        except Exception as exc:
            db.rollback()
            raise HTTPException(
                status_code=500, detail=f"收回 Amen 失敗: {str(exc)}")
        if not is_comment:
            try:
                remove_amen_grant(
                    db, user_id=current_user_id, post_docid=docid)
            except Exception:
                # 不讓 Y 幣錯誤影響主流程
                pass

        amened = False
        message = "已收回 Amen"
    else:
        amen_created = False
        amen = PostAmen(post_docid=docid, userid=current_user_id)
        try:
            db.add(amen)
            target_post.cached_amen_count = (
                target_post.cached_amen_count or 0) + 1
            db.add(target_post)
            db.commit()
            db.refresh(amen)
            db.refresh(target_post)
            amen_created = True
        except IntegrityError:
            db.rollback()
            # 競爭情況下再確認是否已存在 Amen
            existing = db.query(PostAmen).filter(
                PostAmen.post_docid == docid,
                PostAmen.userid == current_user_id
            ).first()
            if not existing:
                raise HTTPException(status_code=500, detail="Amen 建立失敗")
            amened = True
        except Exception as exc:
            db.rollback()
            raise HTTPException(status_code=500, detail=f"Amen 失敗: {str(exc)}")
        else:
            amened = True
            if not amen_created:
                db.refresh(target_post)
        if not is_comment:
            try:
                ensure_amen_granted(
                    db, user_id=current_user_id, post_docid=docid)
            except Exception:
                # 不讓 Y 幣錯誤影響主流程
                pass

        message = "Amen 成功" if amened else "已收回 Amen"

    amen_count = target_post.cached_amen_count if target_post and hasattr(
        target_post, "cached_amen_count") else 0

    if (
        amen_created
        and target_event == Event.POST.value
        and current_user_id != target_post.userid
    ):
        try:
            create_amen_notification(
                db,
                post_docid=docid,
                post_owner_id=target_post.userid,
                actor_user_id=current_user_id,
            )
        except Exception as exc:  # noqa: PERF203
            logger.warning(
                "Failed to create amen notification for %s: %s", docid, exc)

    return {
        "status": "success",
        "data": {
            "post_docid": docid,
            "userid": current_user_id,
            "amen_count": amen_count,
            "amened": amened
        },
        "message": message
    }


@router.post(
    "/witness",
    response_model=CreateWitnessResponse,
    summary="新增見證",
    response_description="成功返回見證和原貼文摘要"
)
async def create_witness(
    witness: Optional[WitnessCreate] = Body(None),
    content: Optional[str] = Form(None),
    parent_docid: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    current_user_id: str = Depends(get_current_userid)
):
    """新增見證"""
    payload = witness
    if payload is None:
        if not content or not parent_docid:
            raise HTTPException(
                status_code=400, detail="缺少必要欄位：content、parent_docid")
        payload = WitnessCreate(content=content, parent_docid=parent_docid)
    image_path: Optional[str] = None
    try:
        # 要見證的原貼文
        target_post = db.query(Post).filter(
            Post.docid == payload.parent_docid,
        ).first()

        if not target_post:
            raise HTTPException(
                status_code=404,
                detail=f"找不到 docid {payload.parent_docid} 的貼文"
            )

        if target_post.userid != current_user_id:
            raise HTTPException(status_code=403, detail="只能為自己的貼文添加見證")

        existing_witness = db.query(Post).filter(
            Post.event == Event.WITNESS.value,
            Post.parent_docid == payload.parent_docid,
            Post.userid == current_user_id
        ).first()
        if existing_witness:
            raise HTTPException(status_code=400, detail="已為此貼文建立過見證")

        if file:
            image_path = await _save_witness_image(file)

        # 見證記錄
        db_witness = Post(
            docid=str(uuid.uuid4()),
            userid=current_user_id,
            content=payload.content,
            parent_docid=payload.parent_docid,  # 原貼文的 docid
            event=Event.WITNESS.value,
            category=target_post.category,
            privacy=target_post.privacy,
            image_url=image_path
        )

        db.add(db_witness)
        db.commit()
        db.refresh(db_witness)
        try:
            safe_award_for_postlike(db, post_obj=db_witness)
        except Exception:
            pass

        # === ✅ 新增：發送見證發佈通知給朋友/部落 ===
        # ==========================================
        try:
            create_post_notifications(
                db,
                post_docid=db_witness.docid,
                author_user_id=current_user_id,
                privacy=db_witness.privacy,
                notification_type=WITNESS_CREATED_TYPE
            )
        except Exception as exc:
            logger.warning(
                "Failed to create witness notifications for %s: %s", db_witness.docid, exc)
        # ==========================================

        return {
            "status": "success",
            "data": {
                "witness": db_witness.to_dict(),
                "target_post": {
                    "sn": target_post.sn,
                    "docid": target_post.docid,
                    "content": target_post.content,
                    "category": target_post.category
                }
            },
            "message": "成功對貼文添加見證"
        }

    except HTTPException as e:
        if image_path:
            _delete_witness_image(image_path)
        db.rollback()
        raise e
    except Exception as e:
        if image_path:
            _delete_witness_image(image_path)
        db.rollback()
        raise HTTPException(status_code=500, detail=f"添加見證失敗: {str(e)}")


@router.get(
    "/witness-list",
    response_model=GetWitnessListResponse,
    summary="查詢見證列表",
    response_description="成功返回見證列表"
)
def get_witness_list(
        docid: Optional[str] = Query(None, description="原貼文 docid，僅查詢對應的見證"),
        category: Optional[Category] = Query(None, description="見證分類"),
        privacy: Optional[Privacy] = Query(None, description="隱私權設定"),
        page: int = Query(1, ge=1, description="頁碼"),
        limit: int = Query(10, ge=1, le=100, description="每頁筆數"),
        db: Session = Depends(get_db),
        viewer_user_id: Optional[str] = Depends(get_optional_userid)
):
    """獲取見證列表"""
    try:
        resolved_viewer_id = viewer_user_id
        viewer_tribe = None
        if resolved_viewer_id:
            viewer_tribe = db.query(UserProfile.tribe).filter(
                UserProfile.user_id == resolved_viewer_id
            ).scalar()

        author_profile_subq = db.query(
            UserProfile.user_id.label("user_id"),
            UserProfile.tribe.label("tribe"),
            UserProfile.avatar_url.label("avatar_url")
        ).subquery()

        cleaned_docid = docid.strip() if docid else None

        # 查詢 event 為 witness 的記錄，支援 docid、分類與隱私篩選
        base_query = db.query(Post).outerjoin(
            author_profile_subq, Post.userid == author_profile_subq.c.user_id
        ).filter(Post.event == Event.WITNESS.value)

        if cleaned_docid:
            base_query = base_query.filter(Post.parent_docid == cleaned_docid)

        if category:
            base_query = base_query.filter(Post.category == category.value)

        if privacy:
            base_query = base_query.filter(Post.privacy == privacy.value)

        if resolved_viewer_id:
            friend_exists_clause = exists().where(
                or_(
                    and_(
                        UserFriend.user_id == resolved_viewer_id,
                        UserFriend.friend_user_id == Post.userid
                    ),
                    and_(
                        UserFriend.user_id == Post.userid,
                        UserFriend.friend_user_id == resolved_viewer_id
                    )
                )
            )

            visibility_conditions = [
                Post.privacy == Privacy.PUBLIC.value,
                Post.userid == resolved_viewer_id
            ]

            if viewer_tribe is not None:
                visibility_conditions.append(
                    and_(
                        Post.privacy == Privacy.TRIBE.value,
                        author_profile_subq.c.tribe == viewer_tribe
                    )
                )

            visibility_conditions.append(
                and_(
                    Post.privacy == Privacy.FAMILY.value,
                    friend_exists_clause
                )
            )

            base_query = base_query.filter(or_(*visibility_conditions))
        else:
            base_query = base_query.filter(
                Post.privacy == Privacy.PUBLIC.value)

        total_items = base_query.count()
        total_pages = math.ceil(total_items / limit) if total_items > 0 else 1
        offset = (page - 1) * limit
        witness_rows = base_query.add_columns(
            author_profile_subq.c.avatar_url.label("avatar_url")
        ).order_by(Post.datetime.desc()).offset(offset).limit(limit).all()

        parent_docids = {
            witness.parent_docid for witness, _ in witness_rows if witness.parent_docid
        }
        parent_profiles_subq = db.query(
            UserProfile.user_id.label("user_id"),
            UserProfile.avatar_url.label("avatar_url")
        ).subquery() if parent_docids else None

        parent_posts_map = {}
        if parent_profiles_subq is not None:
            parent_posts = db.query(
                Post,
                parent_profiles_subq.c.avatar_url.label("avatar_url")
            ).outerjoin(
                parent_profiles_subq, Post.userid == parent_profiles_subq.c.user_id
            ).filter(Post.docid.in_(list(parent_docids))).all()
            parent_posts_map = {post.docid: (
                post, avatar_url) for post, avatar_url in parent_posts}

        # 見證對應的原貼文信息
        result = []
        for witness_obj, author_avatar in witness_rows:
            witness_obj.avatar_url = author_avatar
            witness_dict = witness_obj.to_dict()
            # 原貼文
            if witness_obj.parent_docid:
                original_entry = parent_posts_map.get(witness_obj.parent_docid)
                if original_entry:
                    original_post, original_avatar = original_entry
                    original_post.avatar_url = original_avatar
                    witness_dict["original_post"] = original_post.to_dict()
            result.append(witness_dict)

        return {
            "status": "success",
            "data": {
                "witnesses": result,
                "pageinfo": {
                    "current_page": page,
                    "total_pages": total_pages,
                    "total_items": total_items,
                    "per_page": limit,
                    "has_next": page < total_pages,
                    "has_prev": page > 1
                }
            },
            "message": f"成功獲取見證列表，第 {page} 頁，共 {total_pages} 頁，總計 {total_items} 篇見證"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"獲取見證列表失敗: {str(e)}")
