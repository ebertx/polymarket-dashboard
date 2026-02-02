from pydantic import BaseModel
from datetime import datetime
from decimal import Decimal
from typing import List, Optional


class PositionResponse(BaseModel):
    id: int
    market_id: Optional[int] = None
    market_title: Optional[str] = None
    direction: str
    shares: Decimal
    entry_price: Decimal
    entry_date: datetime
    exit_price: Optional[Decimal] = None
    exit_date: Optional[datetime] = None
    current_price: Optional[Decimal] = None
    current_value: Optional[Decimal] = None
    unrealized_pnl: Optional[Decimal] = None
    realized_pnl: Optional[Decimal] = None
    cost_basis: Decimal
    status: Optional[str] = None
    thesis_status: Optional[str] = None
    entry_reasoning: Optional[str] = None
    exit_reasoning: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class PositionCreate(BaseModel):
    market_id: int
    direction: str  # 'yes' or 'no'
    shares: Decimal
    entry_price: Decimal
    entry_reasoning: Optional[str] = None


class PositionUpdate(BaseModel):
    shares: Optional[Decimal] = None
    current_price: Optional[Decimal] = None
    status: Optional[str] = None
    thesis_status: Optional[str] = None
    exit_price: Optional[Decimal] = None
    exit_reasoning: Optional[str] = None
    realized_pnl: Optional[Decimal] = None


class PositionSnapshotResponse(BaseModel):
    id: int
    position_id: int
    timestamp: datetime
    price: Decimal
    value: Decimal
    bid: Optional[Decimal] = None
    ask: Optional[Decimal] = None
    spread: Optional[Decimal] = None

    class Config:
        from_attributes = True


class PositionHistoryResponse(BaseModel):
    position_id: int
    snapshots: List[PositionSnapshotResponse]
    count: int
