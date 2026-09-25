from pydantic import BaseModel, field_validator
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from app.models.position import PIPELINE_TAGS, STRATEGY_TAGS


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
    strategy_tag: Optional[str] = None   # pm-rfz.5 attribution: trade shape
    pipeline_tag: Optional[str] = None   # pm-rfz.5 attribution: research depth
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
    strategy_tag: Optional[str] = None
    pipeline_tag: Optional[str] = None

    @field_validator("strategy_tag")
    @classmethod
    def _check_strategy_tag(cls, v):
        if v is not None and v not in STRATEGY_TAGS:
            raise ValueError(f"strategy_tag must be one of {sorted(STRATEGY_TAGS)}, got {v!r}")
        return v

    @field_validator("pipeline_tag")
    @classmethod
    def _check_pipeline_tag(cls, v):
        if v is not None and v not in PIPELINE_TAGS:
            raise ValueError(f"pipeline_tag must be one of {sorted(PIPELINE_TAGS)}, got {v!r}")
        return v


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
