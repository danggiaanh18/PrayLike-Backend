from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from database import get_db
from ycoin.ycoin_service import CENT_PER_Y, get_balance, list_transactions
from dependencies import get_current_userid
from ycoin.ycoin_models import YCoinTransaction

router = APIRouter(prefix="/api/ycoin", tags=["ycoin"])


class BalanceResponse(BaseModel):
    user_id: str
    balance: float


@router.get("/balance", response_model=BalanceResponse)
def get_user_balance(
    db: Session = Depends(get_db),
    current_user_id: str = Depends(get_current_userid),
):
    try:
        bal = get_balance(db, current_user_id)
        return {"user_id": current_user_id, "balance": bal}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查詢餘額失敗: {e}")


class TxItem(BaseModel):
    id: int
    user_id: str
    amount: float
    event_type: str
    source_docid: Optional[str] = None
    context_docid: Optional[str] = None
    created_at: Optional[str] = None
    description: str


class TxListResponse(BaseModel):
    items: list[TxItem]
    pageinfo: dict


def _describe_tx(tx: YCoinTransaction) -> str:
    """Human-readable description for a YCoin transaction."""
    labels = {
        "post": "發佈貼文獎勵",
        "comment": "留言獎勵",
        "witness": "見證獎勵",
        "amen": "Amen 獎勵",
    }
    label = labels.get(tx.event_type, tx.event_type)
    amount_y = (tx.amount or 0) / CENT_PER_Y
    parts = [f"{label} {amount_y:.2f}Y"]
    # Amen 不顯示 meta 提示
    if tx.meta and tx.event_type != "amen":
        parts.append(str(tx.meta))
    return " - ".join(parts)


@router.get("/transactions", response_model=TxListResponse)
def get_transactions(
    event_type: Optional[str] = Query(None, pattern="^(post|comment|witness|amen)$"), # <-- Đổi regex thành pattern
    page: int = Query(1, ge=1),
        limit: int = Query(20, ge=1, le=100),
        db: Session = Depends(get_db),
        current_user_id: str = Depends(get_current_userid),
):
    items, total_items, total_pages = list_transactions(
        db, user_id=current_user_id, event_type=event_type, page=page, limit=limit
    )
    return {
        "items": [
            {
                **i.to_dict(),
                "amount": round((i.amount or 0) / CENT_PER_Y, 2),
                "description": _describe_tx(i)
            } for i in items
        ],
        "pageinfo": {
            "current_page": page,
            "total_pages": total_pages,
            "total_items": total_items,
            "per_page": limit,
            "has_next": page < total_pages,
            "has_prev": page > 1
        }
    }
