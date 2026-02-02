from pydantic import BaseModel
from datetime import datetime
from decimal import Decimal
from typing import List, Optional


class PortfolioSnapshotResponse(BaseModel):
    id: int
    timestamp: datetime
    cash_balance: Decimal
    position_value: Decimal
    total_value: Decimal
    daily_pnl: Optional[Decimal] = None
    daily_pnl_pct: Optional[Decimal] = None
    granularity: Optional[str] = None

    class Config:
        from_attributes = True


class PositionSummary(BaseModel):
    id: int
    market_title: str
    direction: str
    shares: Decimal
    entry_price: Decimal
    current_price: Optional[Decimal] = None
    current_value: Optional[Decimal] = None
    unrealized_pnl: Optional[Decimal] = None
    status: Optional[str] = None

    class Config:
        from_attributes = True


class PortfolioCurrentResponse(BaseModel):
    cash_balance: Decimal
    position_value: Decimal
    total_value: Decimal
    unrealized_pnl: Decimal
    positions: List[PositionSummary]
    last_updated: Optional[datetime] = None


class PortfolioHistoryResponse(BaseModel):
    snapshots: List[PortfolioSnapshotResponse]
    count: int
