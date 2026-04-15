from typing import Optional, List, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import func, desc
from sqlalchemy.exc import IntegrityError
from ycoin.ycoin_models import YCoinTransaction

CENT_PER_Y = 100
# ==== 奬勵設定（以「分」為單位）====
REWARD_POST = 100  # 1.00Y
REWARD_COMMENT = 100  # 1.00Y
REWARD_WITNESS = 100  # 1.00Y
AMEN_REWARD = 20  # 0.20Y


def safe_award_ycoin(
        db: Session,
        *,
        user_id: str,
        event_type: str,  # 'post' | 'comment' | 'witness'
        source_docid: Optional[str] = None,
        context_docid: Optional[str] = None,
        amount: int = 1,
        meta: Optional[str] = None
) -> bool:
    """
    嘗試發放 Y 幣。若觸發唯一鍵（代表已領過），回傳 False；否則 True。
    不會影響主要功能流程（吞掉唯一鍵衝突）。
    """
    tx = YCoinTransaction(
        user_id=user_id,
        event_type=event_type,
        source_docid=source_docid,
        context_docid=context_docid,
        amount=amount,
        meta=meta
    )
    db.add(tx)
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def safe_award_for_postlike(db: Session, *, post_obj) -> bool:
    event = (getattr(post_obj, "event", None) or "post").lower().strip()

    if event == "post":
        return safe_award_ycoin(
            db,
            user_id=post_obj.userid,
            event_type="post",
            source_docid=post_obj.docid,
            amount=REWARD_POST,  # 1Y -> 100 分
        )
    elif event == "comment":
        return safe_award_ycoin(
            db,
            user_id=post_obj.userid,
            event_type="comment",
            source_docid=getattr(post_obj, "docid", None),
            context_docid=getattr(post_obj, "parent_docid", None),
            amount=REWARD_COMMENT,  # 1Y -> 100 分
        )
    elif event == "witness":
        return safe_award_ycoin(
            db,
            user_id=post_obj.userid,
            event_type="witness",
            source_docid=post_obj.docid,
            amount=REWARD_WITNESS,  # 1Y -> 100 分
        )
    else:
        return False


def get_balance(db: Session, user_id: str) -> int:
    total = db.query(func.coalesce(func.sum(YCoinTransaction.amount), 0)) \
        .filter(YCoinTransaction.user_id == user_id).scalar()
    return int(total or 0)


def list_transactions(
        db: Session,
        *,
        user_id: Optional[str] = None,
        event_type: Optional[str] = None,
        page: int = 1,
        limit: int = 20
) -> Tuple[List[YCoinTransaction], int, int]:
    q = db.query(YCoinTransaction)
    if user_id:
        q = q.filter(YCoinTransaction.user_id == user_id)
    if event_type:
        q = q.filter(YCoinTransaction.event_type == event_type)

    total_items = q.count()
    total_pages = (total_items + limit - 1) // limit if total_items > 0 else 1
    rows = q.order_by(desc(YCoinTransaction.created_at)) \
        .offset((page - 1) * limit) \
        .limit(limit).all()
    return rows, total_items, total_pages


def ensure_amen_granted(db: Session, *, user_id: str, post_docid: str) -> bool:
    """
    確保 'amen' 對同一 user+post 只會存在一筆 +20 分的交易。
    若已存在就不重複新增；若不存在就新增一筆。
    回傳 True 表示本次有新增；False 表示原本就有（無需動作）。
    """
    existed = db.query(YCoinTransaction).filter(
        YCoinTransaction.user_id == user_id,
        YCoinTransaction.event_type == "amen",
        YCoinTransaction.context_docid == post_docid,
        YCoinTransaction.amount == AMEN_REWARD
    ).first()
    if existed:
        return False

    tx = YCoinTransaction(
        user_id=user_id,
        event_type="amen",
        context_docid=post_docid,
        amount=AMEN_REWARD,
        meta="amen +0.2Y"
    )
    db.add(tx)
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        # 若是唯一鍵（user_id,event_type,context_docid）已存在但金額不同（理論上不會發生），則忽略
        return False


def remove_amen_grant(db: Session, *, user_id: str, post_docid: str) -> bool:
    """
    取消 Amen：移除原本那筆 +20 分的交易，讓淨效應歸零。
    回傳 True 表示本次有刪除；False 表示原本就沒有（無需動作）。
    """
    row = db.query(YCoinTransaction).filter(
        YCoinTransaction.user_id == user_id,
        YCoinTransaction.event_type == "amen",
        YCoinTransaction.context_docid == post_docid,
        YCoinTransaction.amount == AMEN_REWARD
    ).first()
    if not row:
        return False
    db.delete(row)
    db.commit()
    return True


def get_balance(db: Session, user_id: str) -> float:
    # 以「分」加總後轉為 Y 幣
    total_cents = db.query(func.coalesce(func.sum(YCoinTransaction.amount), 0)) \
                      .filter(YCoinTransaction.user_id == user_id).scalar() or 0
    return round(float(total_cents) / CENT_PER_Y, 2)
