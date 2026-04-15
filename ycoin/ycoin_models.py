from sqlalchemy import (
    Column, Integer, String, DateTime, Text,
    UniqueConstraint, Index
)
from database import Base
from models import format_as_utc, utc_now


class YCoinTransaction(Base):
    __tablename__ = "ycoin_transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(100), nullable=False, index=True)
    amount = Column(Integer, nullable=False, default=100)  # amount 以「分（centY）」為單位存放整數：1Y == 100，0.2Y == 20
    event_type = Column(String(20), nullable=False)  # 'post' | 'comment' | 'witness' | 'amen'
    source_docid = Column(String(255), nullable=True)  # 直接觸發的 docid（貼文/留言/見證）
    context_docid = Column(String(255), nullable=True)  # 場景 docid（留言放被留言的貼文 docid）
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    meta = Column(Text, nullable=True)  # Optional JSON string / note

    # 兩條唯一鍵：
    # 1) post / witness：user + event + source_docid 唯一
    # 2) comment：user + event + context_docid 唯一（單篇貼文最多 1Y）
    __table_args__ = (
        UniqueConstraint('user_id', 'event_type', 'source_docid',
                         name='uniq_user_evt_src'),
        UniqueConstraint('user_id', 'event_type', 'context_docid',
                         name='uniq_user_evt_ctx'),
        Index('idx_ycoin_user_created', 'user_id', 'created_at'),
        Index('idx_ycoin_evt_created', 'event_type', 'created_at'),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "amount": self.amount,
            "event_type": self.event_type,
            "source_docid": self.source_docid,
            "context_docid": self.context_docid,
            "created_at": format_as_utc(self.created_at),
        }
