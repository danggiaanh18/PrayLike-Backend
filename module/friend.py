import logging
from enum import Enum
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from database import get_db
from dependencies import get_current_userid
from models import FriendInvitation, UserFriend, UserProfile, format_as_utc, utc_now
from pydantic import BaseModel
from services.notifications import (
    create_friend_accept_notification,
    create_friend_request_notification,
)

router = APIRouter(prefix="/api", tags=["Friends"])
logger = logging.getLogger(__name__)


class InvitationStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"


class UserProfileResult(BaseModel):
    user_id: str
    name: str
    email: Optional[str]
    tribe: Optional[int]
    avatar_url: Optional[str] = None


class UserProfileSearchData(BaseModel):
    results: List[UserProfileResult]


class UserProfileSearchResponse(BaseModel):
    status: Literal["success"]
    data: UserProfileSearchData
    message: str


class AddFriendRequest(BaseModel):
    requester_user_id: str
    target_user_id: str


class FriendInvitationItem(BaseModel):
    id: int
    requester_user_id: str
    target_user_id: str
    status: InvitationStatus
    message: Optional[str]
    created_at: Optional[str]
    responded_at: Optional[str]
    avatar_url: Optional[str] = None


class InviteFriendRequest(BaseModel):
    target_user_id: str
    message: Optional[str] = None


class InviteFriendData(BaseModel):
    invitation: Optional[FriendInvitationItem]
    already_friends: bool = False
    auto_accepted: bool = False


class InviteFriendResponse(BaseModel):
    status: Literal["success"]
    data: InviteFriendData
    message: str


class PendingInvitesData(BaseModel):
    incoming: List[FriendInvitationItem]
    outgoing: List[FriendInvitationItem]


class PendingInvitesResponse(BaseModel):
    status: Literal["success"]
    data: PendingInvitesData
    message: str


class RespondInviteRequest(BaseModel):
    accept: bool


class RespondInviteResponse(BaseModel):
    status: Literal["success"]
    data: FriendInvitationItem
    message: str


class FriendListItem(BaseModel):
    user_id: str
    name: Optional[str]
    email: Optional[str]
    tribe: Optional[int]
    friend_since: Optional[str]
    avatar_url: Optional[str] = None


class FriendListData(BaseModel):
    friends: List[FriendListItem]
    total: int


class FriendListResponse(BaseModel):
    status: Literal["success"]
    data: FriendListData
    message: str


def _create_friendship(db: Session, user_a: str, user_b: str) -> None:
    sorted_pair = sorted([user_a, user_b])
    existing = db.query(UserFriend).filter(
        UserFriend.user_id == sorted_pair[0],
        UserFriend.friend_user_id == sorted_pair[1]
    ).first()
    if existing:
        return
    friend = UserFriend(user_id=sorted_pair[0], friend_user_id=sorted_pair[1])
    db.add(friend)


def _sanitize_message(msg: Optional[str]) -> Optional[str]:
    if msg is None:
        return None
    trimmed = msg.strip()
    return trimmed if trimmed else None


@router.get(
    "/users/search",
    response_model=UserProfileSearchResponse,
    summary="透過 user_id 模糊查詢使用者",
    response_description="找到使用者回傳基本資料，依相似度排序"
)
def search_user_by_id(
    user_id: str = Query(..., description="要查詢的 user_id"),
    limit: int = Query(10, ge=1, le=50, description="最多返回的筆數"),
    db: Session = Depends(get_db)
):
    """依 user_id 模糊查詢使用者資料並按相似度排序"""
    lookup_id = user_id.strip()
    if not lookup_id:
        raise HTTPException(status_code=400, detail="user_id 不可為空")

    normalized_lookup = lookup_id.lower()
    like_pattern = f"%{normalized_lookup}%"
    profiles = db.query(UserProfile).filter(
        func.lower(UserProfile.user_id).like(like_pattern)
    ).all()

    if not profiles:
        raise HTTPException(status_code=404, detail=f"找不到 user_id 包含 {lookup_id} 的使用者")

    def _score(profile: UserProfile):
        candidate = (profile.user_id or "").strip()
        lowered = candidate.lower()
        position = lowered.find(normalized_lookup)
        is_exact = lowered == normalized_lookup
        starts_with = position == 0
        length_gap = abs(len(candidate) - len(lookup_id))
        return (
            0 if is_exact else 1 if starts_with else 2,
            position if position >= 0 else len(candidate),
            length_gap,
            candidate
        )

    sorted_profiles = sorted(profiles, key=_score)[:limit]

    return {
        "status": "success",
        "data": {
            "results": [
                {
                    "user_id": profile.user_id,
                    "name": profile.name,
                    "email": profile.email,
                    "tribe": profile.tribe,
                    "avatar_url": profile.avatar_url
                }
                for profile in sorted_profiles
            ]
        },
        "message": f"找到 {len(sorted_profiles)} 筆符合 {lookup_id} 的使用者"
    }


@router.post(
    "/friends/invite",
    response_model=InviteFriendResponse,
    summary="送出好友邀請",
    response_description="建立好友邀請或自動接受"
)
def invite_friend(
    payload: InviteFriendRequest,
    db: Session = Depends(get_db),
    current_user_id: str = Depends(get_current_userid)
):
    requester = current_user_id
    target = payload.target_user_id.strip() if payload.target_user_id else ""

    #基本防呆機制
    if not target:
        raise HTTPException(status_code=400, detail="target_user_id 不可為空")
    if requester == target:
        raise HTTPException(status_code=400, detail="不能邀請自己")

    profiles = db.query(UserProfile).filter(UserProfile.user_id.in_([requester, target])).all()
    found_ids = {profile.user_id for profile in profiles}
    missing = [uid for uid in [requester, target] if uid not in found_ids]
    if missing:
        raise HTTPException(status_code=404, detail=f"找不到 user_id: {', '.join(missing)}")

    sorted_pair = sorted([requester, target])
    existing_friend = db.query(UserFriend).filter(
        UserFriend.user_id == sorted_pair[0],
        UserFriend.friend_user_id == sorted_pair[1]
    ).first()
    if existing_friend:
        return {
            "status": "success",
            "data": {
                "invitation": None,
                "already_friends": True,
                "auto_accepted": False
            },
            "message": "已經是好友，無需再次邀請"
        }

    reverse_pending = db.query(FriendInvitation).filter(
        FriendInvitation.requester_user_id == target,
        FriendInvitation.target_user_id == requester,
        FriendInvitation.status == InvitationStatus.PENDING.value
    ).first()
    if reverse_pending:
        reverse_pending.status = InvitationStatus.ACCEPTED.value
        reverse_pending.responded_at = utc_now()
        try:
            db.add(reverse_pending)
            _create_friendship(db, requester, target)
            db.commit()
            db.refresh(reverse_pending)
            try:
                create_friend_accept_notification(
                    db,
                    invitation_id=reverse_pending.id,
                    accepter_id=requester,
                    requester_id=target,
                )
            except Exception as exc:  # noqa: PERF203
                logger.warning(
                    "Failed to create friend accept notification for invitation %s: %s",
                    reverse_pending.id,
                    exc,
                )
        except Exception as exc:
            db.rollback()
            raise HTTPException(status_code=500, detail=f"接受好友邀請失敗: {str(exc)}")
        return {
            "status": "success",
            "data": {
                "invitation": reverse_pending.to_dict(),
                "already_friends": False,
                "auto_accepted": True
            },
            "message": "對方已有邀請，已自動成為好友"
        }

    existing_invite = db.query(FriendInvitation).filter(
        FriendInvitation.requester_user_id == requester,
        FriendInvitation.target_user_id == target,
        FriendInvitation.status == InvitationStatus.PENDING.value
    ).first()
    if existing_invite:
        return {
            "status": "success",
            "data": {
                "invitation": existing_invite.to_dict(),
                "already_friends": False,
                "auto_accepted": False
            },
            "message": "已送出邀請，等待對方回應"
        }

    invite = FriendInvitation(
        requester_user_id=requester,
        target_user_id=target,
        status=InvitationStatus.PENDING.value,
        message=_sanitize_message(payload.message)
    )
    try:
        db.add(invite)
        db.commit()
        db.refresh(invite)
        try:
            create_friend_request_notification(
                db,
                invitation_id=invite.id,
                requester_id=requester,
                target_id=target,
            )
        except Exception as exc:  # noqa: PERF203
            logger.warning(
                "Failed to create friend request notification for invitation %s: %s",
                invite.id,
                exc,
            )
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"建立好友邀請失敗: {str(exc)}")

    # TODO: 通知鈴模組通知對方

    return {
        "status": "success",
        "data": {
            "invitation": invite.to_dict(),
            "already_friends": False,
            "auto_accepted": False
        },
        "message": "已送出好友邀請"
    }


@router.get(
    "/friends/pending",
    response_model=PendingInvitesResponse,
    summary="取得好友邀請列表",
    response_description="取得待處理邀請"
)
def list_pending_invites(
    db: Session = Depends(get_db),
    current_user_id: str = Depends(get_current_userid)
):
    # 1. 查詢邀請資料
    incoming = db.query(FriendInvitation).filter(
        FriendInvitation.target_user_id == current_user_id,
        FriendInvitation.status == InvitationStatus.PENDING.value
    ).order_by(FriendInvitation.created_at.desc()).all()

    outgoing = db.query(FriendInvitation).filter(
        FriendInvitation.requester_user_id == current_user_id,
        FriendInvitation.status == InvitationStatus.PENDING.value
    ).order_by(FriendInvitation.created_at.desc()).all()

    # 2. 收集所有需要查詢頭像的 User ID
    # 對於 incoming (別人邀我)，我們需要 requester_user_id 的頭像
    # 對於 outgoing (我邀別人)，我們需要 target_user_id 的頭像
    related_user_ids = set()
    for inv in incoming:
        related_user_ids.add(inv.requester_user_id)
    for inv in outgoing:
        related_user_ids.add(inv.target_user_id)

    # 3. 批次查詢 UserProfile
    avatar_map = {}
    if related_user_ids:
        profiles = db.query(UserProfile).filter(
            UserProfile.user_id.in_(list(related_user_ids))
        ).all()
        for p in profiles:
            if p.avatar_url:
                avatar_map[p.user_id] = p.avatar_url

    # 4. 組裝資料並填入 avatar_url
    incoming_list = []
    for inv in incoming:
        item = inv.to_dict()
        # 別人邀我 -> 顯示別人的頭像
        item["avatar_url"] = avatar_map.get(inv.requester_user_id)
        incoming_list.append(item)

    outgoing_list = []
    for inv in outgoing:
        item = inv.to_dict()
        # 我邀別人 -> 顯示別人的頭像
        item["avatar_url"] = avatar_map.get(inv.target_user_id)
        outgoing_list.append(item)

    return {
        "status": "success",
        "data": {
            "incoming": incoming_list,
            "outgoing": outgoing_list
        },
        "message": "取得待處理好友邀請"
    }


@router.post(
    "/friends/{invite_id}/respond",
    response_model=RespondInviteResponse,
    summary="回應好友邀請",
    response_description="接受或拒絕好友邀請"
)
def respond_invite(
    invite_id: int,
    payload: RespondInviteRequest,
    db: Session = Depends(get_db),
    current_user_id: str = Depends(get_current_userid)
):
    invite = db.query(FriendInvitation).filter(FriendInvitation.id == invite_id).first()
    if not invite:
        raise HTTPException(status_code=404, detail="找不到邀請")
    if invite.status != InvitationStatus.PENDING.value:
        raise HTTPException(status_code=400, detail="邀請已處理")
    if invite.target_user_id != current_user_id:
        raise HTTPException(status_code=403, detail="無權限回應此邀請")

    invite.status = InvitationStatus.ACCEPTED.value if payload.accept else InvitationStatus.DECLINED.value
    invite.responded_at = utc_now()
    try:
        db.add(invite)
        if payload.accept:
            _create_friendship(db, invite.requester_user_id, invite.target_user_id)
        db.commit()
        db.refresh(invite)
        if payload.accept:
            try:
                create_friend_accept_notification(
                    db,
                    invitation_id=invite.id,
                    accepter_id=current_user_id,
                    requester_id=invite.requester_user_id,
                )
            except Exception as exc:  # noqa: PERF203
                logger.warning(
                    "Failed to create friend accept notification for invitation %s: %s",
                    invite.id,
                    exc,
                )
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"更新邀請狀態失敗: {str(exc)}")

    return {
        "status": "success",
        "data": invite.to_dict(),
        "message": "已接受邀請" if payload.accept else "已拒絕邀請"
    }


@router.get(
    "/friends",
    response_model=FriendListResponse,
    summary="取得目前使用者好友列表",
    response_description="回傳好友清單與總數"
)
def list_user_friends(
    db: Session = Depends(get_db),
    current_user_id: str = Depends(get_current_userid)
):
    normalized_user_id = current_user_id.strip()

    relations = db.query(UserFriend).filter(
        or_(
            UserFriend.user_id == normalized_user_id,
            UserFriend.friend_user_id == normalized_user_id
        )
    ).order_by(UserFriend.created_at.desc()).all()

    friend_since_map = {}
    ordered_friend_ids = []
    for relation in relations:
        other_id = relation.friend_user_id if relation.user_id == normalized_user_id else relation.user_id
        if other_id not in friend_since_map:
            friend_since_map[other_id] = relation.created_at
            ordered_friend_ids.append(other_id)

    profiles = db.query(UserProfile).filter(
        UserProfile.user_id.in_(ordered_friend_ids)
    ).all() if ordered_friend_ids else []
    profile_map = {item.user_id: item for item in profiles}

    friends = []
    for fid in ordered_friend_ids:
        profile = profile_map.get(fid)
        created_at = friend_since_map.get(fid)
        friends.append({
            "user_id": fid,
            "name": profile.name.strip() if profile and profile.name else None,
            "email": profile.email if profile else None,
            "tribe": int(profile.tribe) if profile and profile.tribe is not None else None,
            "friend_since": format_as_utc(created_at),
            "avatar_url": profile.avatar_url.strip() if profile and getattr(profile, "avatar_url", None) else None,
        })

    return {
        "status": "success",
        "data": {
            "friends": friends,
            "total": len(friends)
        },
        "message": "取得好友列表"
    }
