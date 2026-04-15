import json
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from database import Base

UTC = timezone.utc


def utc_now() -> datetime:
    """Return an aware UTC timestamp for storage."""
    return datetime.now(UTC)


def to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize any datetime (naive or aware) to UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def format_as_utc(dt: Optional[datetime]) -> Optional[str]:
    """Return an ISO 8601 string in UTC with a Z suffix."""
    normalized = to_utc(dt)
    if not normalized:
        return None
    return normalized.isoformat().replace("+00:00", "Z")


def _generate_activity_id() -> str:
    return str(uuid.uuid4())


class Post(Base):
    __tablename__ = "create_post"
    __table_args__ = (
        Index("ix_create_post_userid", "userid"),
        Index("ix_create_post_event", "event"),
        Index("ix_create_post_datetime", "datetime"),
        Index("ix_create_post_parent_docid", "parent_docid"),
    )
    sn = Column(Integer, primary_key=True, autoincrement=True)  # 新的主鍵
    docid = Column(String(255), nullable=True, unique=True)
    userid = Column(String(100), nullable=False)
    content = Column(Text, nullable=False)
    datetime = Column(DateTime(timezone=True), default=utc_now)
    title = Column(String(255), nullable=True)
    uuid = Column(String(255), nullable=True)
    parent_docid = Column(String, nullable=True)  # 如果是留言，指向原貼文的 docid
    event = Column(String, nullable=False)  # "post" 或 "comment"
    category = Column(String(50), nullable=True)  # 新增 category 欄位
    privacy = Column(String(20), nullable=False, default="public")  # 貼文隱私權
    cached_amen_count = Column(Integer, nullable=False, default=0)
    image_url = Column(String(500), nullable=True)  # 見證單張圖片（可選）
    is_deleted = Column(Integer, nullable=False, default=0)
    
    def _resolved_event(self) -> str:
        """Gracefully handle legacy rows missing the event value."""
        if self.event:
            return self.event
        if self.parent_docid:
            if self.docid:
                return "witness"
            return "comment"
        return "post"

    def to_dict(self):
        return {
            "sn": self.sn,  # 新增 sn
            "docid": self.docid,  # 現在是 UUID 字串
            "userid": self.userid,
            "content": self.content,
            "title": self.title,
            "datetime": format_as_utc(self.datetime),
            "uuid": self.uuid,
            "parent_docid": self.parent_docid,
            "event": self._resolved_event(),
            "timezone": "UTC",
            "category": self.category,
            "privacy": self.privacy,
            "amen_count": getattr(self, "cached_amen_count", getattr(self, "amen_count", 0)),
            "amened": getattr(self, "amened", False),
            "witnessed": getattr(self, "witnessed", False),
            "image_url": self.image_url,
            "avatar_url": getattr(self, "avatar_url", None),
        }


class PostAmen(Base):
    __tablename__ = "post_amen"
    __table_args__ = (
        UniqueConstraint("post_docid", "userid", name="uq_post_amen_post_user"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    post_docid = Column(String(255), ForeignKey("create_post.docid"), nullable=False, index=True)
    userid = Column(String(100), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "post_docid": self.post_docid,
            "userid": self.userid,
            "created_at": format_as_utc(self.created_at)
        }


class UserProfile(Base):
    __tablename__ = "user_profile"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(320), nullable=False, unique=True, index=True)
    name = Column(String(120), nullable=False)
    user_id = Column(String(120), nullable=False, unique=True, index=True)
    tribe = Column(Integer, nullable=True)
    avatar_url = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name.strip() if self.name else None,
            "user_id": self.user_id.strip() if self.user_id else None,
            "tribe": int(self.tribe) if self.tribe is not None else None,
            "avatar_url": self.avatar_url.strip() if self.avatar_url else None,
            "created_at": format_as_utc(self.created_at),
            "updated_at": format_as_utc(self.updated_at),
        }


class UserFriend(Base):
    __tablename__ = "user_friend"
    __table_args__ = (
        UniqueConstraint("user_id", "friend_user_id", name="uq_user_friend_pair"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(120), nullable=False, index=True)
    friend_user_id = Column(String(120), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "friend_user_id": self.friend_user_id,
            "created_at": format_as_utc(self.created_at),
        }


class FriendInvitation(Base):
    __tablename__ = "friend_invitation"

    id = Column(Integer, primary_key=True, autoincrement=True)
    requester_user_id = Column(String(120), nullable=False, index=True)
    target_user_id = Column(String(120), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="pending")  # pending/accepted/declined
    message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    responded_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "requester_user_id": self.requester_user_id,
            "target_user_id": self.target_user_id,
            "status": self.status,
            "message": self.message,
            "created_at": format_as_utc(self.created_at),
            "responded_at": format_as_utc(self.responded_at),
        }


class Notification(Base):
    __tablename__ = "notification"
    __table_args__ = (
        UniqueConstraint(
            "recipient_user_id",
            "actor_user_id",
            "post_docid",
            "notification_type",
            name="uq_notification_recipient_actor_post_type",
        ),
        Index("ix_notification_recipient_created", "recipient_user_id", "created_at"),
        Index("ix_notification_post", "post_docid"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    recipient_user_id = Column(String(120), nullable=False, index=True)
    actor_user_id = Column(String(120), nullable=False)
    post_docid = Column(String(255), nullable=False, index=True)
    notification_type = Column(String(50), nullable=False, default="post_created")
    reason = Column(String(20), nullable=True)  # friend 或 tribe
    is_read = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    read_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "recipient_user_id": self.recipient_user_id,
            "actor_user_id": self.actor_user_id,
            "post_docid": self.post_docid,
            "notification_type": self.notification_type,
            "reason": self.reason,
            "is_read": bool(self.is_read),
            "created_at": format_as_utc(self.created_at),
            "read_at": format_as_utc(self.read_at),
        }


class TribeActivity(Base):
    __tablename__ = "tribe_activities"
    __table_args__ = (
        Index("ix_tribe_activities_event_date", "event_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    activity_id = Column(String(36), nullable=False, index=True, default=_generate_activity_id)
    title = Column("name", String(200), nullable=False)
    location = Column(String(200), nullable=False)
    event_date = Column(Date, nullable=False)
    event_time = Column(String(20), nullable=False)
    description = Column(Text, nullable=True)
    images_json = Column("image_urls", Text, nullable=True)
    created_by = Column("creator_userid", String(120), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="draft")
    published_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    def _image_list(self):
        try:
            parsed = json.loads(self.images_json) if self.images_json else []
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        except ValueError:
            return []
        return []

    def set_images(self, paths):
        self.images_json = json.dumps(list(paths)) if paths else None

    @property
    def cover_image(self):
        images = self._image_list()
        return images[0] if images else None

    @property
    def is_published(self) -> bool:
        return (self.status or "").lower() == "published"

    @is_published.setter
    def is_published(self, value: bool):
        self.status = "published" if value else "draft"

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "location": self.location,
            "event_date": self.event_date.isoformat() if isinstance(self.event_date, date) else None,
            "event_time": self.event_time,
            "description": self.description,
            "cover_image": self.cover_image,
            "images": self._image_list(),
            "tribe": int(getattr(self, "_creator_tribe", None)) if getattr(self, "_creator_tribe", None) is not None else None,
            "created_by": self.created_by,
            "is_published": bool(self.is_published),
            "published_at": format_as_utc(self.published_at),
            "created_at": format_as_utc(self.created_at),
            "updated_at": format_as_utc(self.updated_at),
        }


class AIChatUsageEvent(Base):
    __tablename__ = "ai_chat_usage_event"
    __table_args__ = (
        Index("ix_ai_chat_usage_event_user_id", "user_id"),
        Index("ix_ai_chat_usage_event_used_at", "used_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(120), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class AIChatSession(Base):
    __tablename__ = "ai_chat_session"
    __table_args__ = (
        Index("ix_ai_chat_session_user_id", "user_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_uuid = Column(String(36), nullable=False, unique=True)
    user_id = Column(String(120), nullable=False)
    title = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    last_interaction_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)

    def is_expired(self, now: Optional[datetime] = None, timeout_minutes: int = 5) -> bool:
        """Return True when the session is ended or has been idle past the timeout."""
        if self.ended_at:
            return True
        last_ts = to_utc(self.last_interaction_at or self.created_at)
        if not last_ts:
            return False
        now = to_utc(now) or utc_now()
        return (now - last_ts) > timedelta(minutes=timeout_minutes)

    def to_dict(self, timeout_minutes: int = 5):
        return {
            "session_uuid": self.session_uuid,
            "title": self.title,
            "created_at": format_as_utc(self.created_at),
            "last_interaction_at": format_as_utc(self.last_interaction_at),
            "ended_at": format_as_utc(self.ended_at),
            "expired": self.is_expired(timeout_minutes=timeout_minutes),
        }


class AIChatMessage(Base):
    __tablename__ = "ai_chat_message"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("ai_chat_session.id"), nullable=False, index=True)
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "role": self.role,
            "content": self.content,
            "created_at": format_as_utc(self.created_at),
        }
