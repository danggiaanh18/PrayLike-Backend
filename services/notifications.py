import logging
from enum import Enum
from typing import Dict, List, Optional, Set

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import Notification, UserFriend, UserProfile
from services.onesignal_service import send_push_notification

logger = logging.getLogger(__name__)


class NotificationReason(str, Enum):
    # --- 拆分後的細項 ---
    FRIEND_POST = "friend_post"       # 朋友發貼文
    FRIEND_WITNESS = "friend_witness" # 朋友發見證
    TRIBE_POST = "tribe_post"         # 部落發貼文
    TRIBE_WITNESS = "tribe_witness"   # 部落發見證
    # ------------------
    
    TRIBE_ACTIVITY = "tribe_activity"
    FRIEND_INVITATION = "friend_invitation"
    FRIEND_ACCEPT = "friend_accept"


POST_CREATED_TYPE = "post_created"
WITNESS_CREATED_TYPE = "witness_created" 
COMMENTED_ON_POST_TYPE = "commented_on_post"
AMENED_POST_TYPE = "amened_post"
TRIBE_ACTIVITY_PUBLISHED_TYPE = "tribe_activity_published"
FRIEND_REQUEST_RECEIVED_TYPE = "friend_request_received"
FRIEND_REQUEST_ACCEPTED_TYPE = "friend_request_accepted"

PUBLIC_PRIVACY = "public"
FAMILY_PRIVACY = "family"
TRIBE_PRIVACY = "tribe_privacy"
INDIVIDUAL_PRIVACY = "individual"

# 取得該使用者的所有朋友ID
def _get_friend_ids(db: Session, user_id: str) -> Set[str]:
    relations = db.query(UserFriend).filter(
        or_(UserFriend.user_id == user_id, UserFriend.friend_user_id == user_id)
    ).all()
    friend_ids: Set[str] = set()
    for relation in relations:
        other_id = relation.friend_user_id if relation.user_id == user_id else relation.user_id
        if other_id:
            friend_ids.add(other_id)
    return friend_ids

# 查詢該使用者所屬的Tribe
def _get_author_tribe(db: Session, user_id: str) -> Optional[int]:
    profile = (
        db.query(UserProfile.tribe)
        .filter(UserProfile.user_id == user_id)
        .one_or_none()
    )
    if profile is None:
        return None
    tribe_code = profile[0]
    if tribe_code is None:
        return None
    return int(tribe_code)

# 取得該Tribe的所有成員ID
def _get_tribe_member_ids(db: Session, tribe: Optional[int]) -> Set[str]:
    if tribe is None:
        return set()
    rows = db.query(UserProfile.user_id).filter(UserProfile.tribe == tribe).all()
    return {row[0] for row in rows if row and row[0]}

# 計算通知受眾 (已更新：根據 notification_type 決定 reason)
def build_post_notification_targets(
    db: Session, 
    *, 
    author_user_id: str, 
    privacy: str,
    notification_type: str = POST_CREATED_TYPE  # 新增參數
) -> Dict[str, NotificationReason]:
    normalized_privacy = (privacy or "").strip().lower()
    if not normalized_privacy or normalized_privacy == INDIVIDUAL_PRIVACY:
        return {}

    recipients: Dict[str, NotificationReason] = {}

    # 預設使用 Post 相關的 Reason
    friend_reason = NotificationReason.FRIEND_POST
    tribe_reason = NotificationReason.TRIBE_POST

    # 若為見證類型，切換 Reason
    if notification_type == WITNESS_CREATED_TYPE:
        friend_reason = NotificationReason.FRIEND_WITNESS
        tribe_reason = NotificationReason.TRIBE_WITNESS

    # 處理朋友受眾
    if normalized_privacy in {PUBLIC_PRIVACY, FAMILY_PRIVACY}:
        for friend_id in _get_friend_ids(db, author_user_id):
            if friend_id != author_user_id:
                recipients.setdefault(friend_id, friend_reason)

    # 處理部落受眾
    author_tribe = _get_author_tribe(db, author_user_id)
    if normalized_privacy in {PUBLIC_PRIVACY, TRIBE_PRIVACY} and author_tribe is not None:
        for member_id in _get_tribe_member_ids(db, author_tribe):
            if member_id != author_user_id:
                # setdefault 會保留已存在的 key，確保朋友關係優先
                recipients.setdefault(member_id, tribe_reason)

    return recipients

# 資料庫寫入
def _save_notification(db: Session, notification: Notification) -> Optional[Notification]:
    db.add(notification)
    try:
        db.commit()
        db.refresh(notification)
        return notification
    except IntegrityError:
        db.rollback()
        return None   
    except Exception as exc:
        db.rollback()
        logger.warning(
            "Failed to create notification for %s due to %s",
            notification.recipient_user_id,
            exc,
        )
        return None

# 找出誰該收到通知，並將通知寫入資料庫 
def create_post_notifications(
    db: Session, 
    *, 
    post_docid: str, 
    author_user_id: str, 
    privacy: str,
    notification_type: str = POST_CREATED_TYPE 
) -> List[Notification]:
    
    # 傳入類型以取得正確的 reason
    recipients = build_post_notification_targets(
        db, 
        author_user_id=author_user_id, 
        privacy=privacy,
        notification_type=notification_type 
    )
    if not recipients:
        return []

    notifications = [
        Notification(
            recipient_user_id=recipient,
            actor_user_id=author_user_id,
            post_docid=post_docid,
            notification_type=notification_type, 
            reason=reason.value,
        )
        for recipient, reason in recipients.items()
    ]

    db.add_all(notifications)
    try:
        db.commit()
        for item in notifications:
            db.refresh(item)
        
        # 整批觸發推播
        trigger_push_notifications(db, notifications)
        
        return notifications
    except IntegrityError:
        db.rollback()
        saved: List[Notification] = []
        for item in notifications:
            try:
                db.add(item)
                db.commit()
                db.refresh(item)
                saved.append(item)
            except IntegrityError:
                db.rollback()
            except Exception as exc:
                db.rollback()
                logger.warning("Failed to create notification: %s", exc)
        
        # 針對個別寫入成功的通知觸發推播
        if saved:
            trigger_push_notifications(db, saved)
            
        return saved
    except Exception as exc:
        db.rollback()
        logger.warning("Failed to create notifications: %s", exc)
        return []

# 通知發文留言
def create_comment_notification(db: Session, *, post_docid: str, post_owner_id: str, actor_user_id: str) -> Optional[Notification]:
    if actor_user_id == post_owner_id:
        return None
    note = Notification(
        recipient_user_id=post_owner_id,
        actor_user_id=actor_user_id,
        post_docid=post_docid,
        notification_type=COMMENTED_ON_POST_TYPE,
        reason="comment",
    )
    saved = _save_notification(db, note)
    
    if saved:
        trigger_push_notifications(db, [saved])
    return saved

# 通知amen
def create_amen_notification(db: Session, *, post_docid: str, post_owner_id: str, actor_user_id: str) -> Optional[Notification]:
    if actor_user_id == post_owner_id:
        return None
    note = Notification(
        recipient_user_id=post_owner_id,
        actor_user_id=actor_user_id,
        post_docid=post_docid,
        notification_type=AMENED_POST_TYPE,
        reason="amen",
    )
    saved = _save_notification(db, note)
    
    if saved:
        trigger_push_notifications(db, [saved])
        
    return saved

# 通知支派活動
def create_tribe_activity_notifications(db: Session, *, activity_id: str, creator_user_id: str) -> List[Notification]:
    tribe = _get_author_tribe(db, creator_user_id)
    if tribe is None:
        return []
    recipients = [
        row[0]
        for row in db.query(UserProfile.user_id).filter(
            UserProfile.tribe == tribe, UserProfile.user_id != creator_user_id
        )
    ]
    notifications: List[Notification] = []
    for recipient in recipients:
        note = Notification(
            recipient_user_id=recipient,
            actor_user_id=creator_user_id,
            post_docid=activity_id,
            notification_type=TRIBE_ACTIVITY_PUBLISHED_TYPE,
            reason=NotificationReason.TRIBE_ACTIVITY.value,
        )
        saved = _save_notification(db, note)
        if saved:
            notifications.append(saved)
            
    if notifications:
        trigger_push_notifications(db, notifications)
        
    return notifications

# 通知發送好友邀請
def create_friend_request_notification(db: Session, *, invitation_id: int, requester_id: str, target_id: str) -> Optional[Notification]:
    note = Notification(
        recipient_user_id=target_id,
        actor_user_id=requester_id,
        post_docid=str(invitation_id),
        notification_type=FRIEND_REQUEST_RECEIVED_TYPE,
        reason=NotificationReason.FRIEND_INVITATION.value,
    )
    saved = _save_notification(db, note)
    if saved:
        trigger_push_notifications(db, [saved])
    return saved

# 通知確認好友
def create_friend_accept_notification(db: Session, *, invitation_id: int, accepter_id: str, requester_id: str) -> Optional[Notification]:
    note = Notification(
        recipient_user_id=requester_id,
        actor_user_id=accepter_id,
        post_docid=str(invitation_id),
        notification_type=FRIEND_REQUEST_ACCEPTED_TYPE,
        reason=NotificationReason.FRIEND_ACCEPT.value,
    )
    saved = _save_notification(db, note)
    if saved:
        trigger_push_notifications(db, [saved])
    return saved

# onesignal推播通知
def trigger_push_notifications(db: Session, notifications: List[Notification]):
    """
    根據 Notification 列表，統一判斷文案並發送 OneSignal 推播。
    """
    if not notifications:
        return

    for note in notifications:
        actor = db.query(UserProfile).filter(UserProfile.user_id == note.actor_user_id).first()
        actor_name = actor.name if actor and actor.name else "有人"

        heading = ""
        content = ""
        data = {"notification_id": note.id, "type": note.notification_type}

        if note.notification_type == FRIEND_REQUEST_RECEIVED_TYPE:
            heading = "交友邀請"
            content = f"{actor_name} 想加您為好友"
        
        elif note.notification_type == FRIEND_REQUEST_ACCEPTED_TYPE:
            heading = "邀請已接受"
            content = f"{actor_name} 接受了您的交友邀請"
            
        elif note.notification_type == COMMENTED_ON_POST_TYPE:
            heading = "新的留言"
            content = f"{actor_name} 評論了您的貼文"
            
        elif note.notification_type == AMENED_POST_TYPE:
            heading = "收到 Amen"
            content = f"{actor_name} 對您的貼文說了 Amen"

        elif note.notification_type == TRIBE_ACTIVITY_PUBLISHED_TYPE:
            heading = "部落新活動"
            content = "您的部落發佈了新活動，快來看看吧！"
        
        # === 區分 Post 與 Witness 的推播 ===
        elif note.notification_type == POST_CREATED_TYPE:
            heading = "新的動態"
            content = f"{actor_name} 分享了一篇新的貼文"

        elif note.notification_type == WITNESS_CREATED_TYPE:
            heading = "新的見證"
            content = f"{actor_name} 分享了一篇新的見證"
        # ================================

        if heading and content:
            send_push_notification(
                target_user_ids=[note.recipient_user_id],
                heading=heading,
                content=content,
                data=data
            )