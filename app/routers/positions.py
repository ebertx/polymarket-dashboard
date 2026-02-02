from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timezone
from typing import List

from app.database import get_db
from app.config import get_settings
from app.models import Position, PositionSnapshot, Market
from app.schemas.position import (
    PositionResponse,
    PositionCreate,
    PositionUpdate,
    PositionHistoryResponse,
)
from app.services.polymarket import PolymarketClient
from app.services.tracker import TrackerService

router = APIRouter(prefix="/positions", tags=["positions"])


@router.get("", response_model=List[PositionResponse])
async def list_positions(
    status: str = Query(default="open", description="Filter by status: open, closed, all"),
    db: AsyncSession = Depends(get_db),
):
    """List positions, optionally filtering by status."""
    query = select(Position, Market).outerjoin(Market, Position.market_id == Market.id)

    if status != "all":
        query = query.where(Position.status == status)

    result = await db.execute(query.order_by(Position.entry_date.desc()))
    rows = result.all()

    return [
        PositionResponse(
            id=position.id,
            market_id=position.market_id,
            market_title=market.title if market else None,
            direction=position.direction,
            shares=position.shares,
            entry_price=position.entry_price,
            entry_date=position.entry_date,
            exit_price=position.exit_price,
            exit_date=position.exit_date,
            current_price=position.current_price,
            current_value=position.current_value,
            unrealized_pnl=position.unrealized_pnl,
            realized_pnl=position.realized_pnl,
            cost_basis=position.cost_basis,
            status=position.status,
            thesis_status=position.thesis_status,
            entry_reasoning=position.entry_reasoning,
            exit_reasoning=position.exit_reasoning,
            created_at=position.created_at,
            updated_at=position.updated_at,
        )
        for position, market in rows
    ]


@router.get("/{position_id}", response_model=PositionResponse)
async def get_position(position_id: int, db: AsyncSession = Depends(get_db)):
    """Get a specific position by ID."""
    result = await db.execute(
        select(Position, Market)
        .outerjoin(Market, Position.market_id == Market.id)
        .where(Position.id == position_id)
    )
    row = result.first()

    if not row:
        raise HTTPException(status_code=404, detail="Position not found")

    position, market = row
    return PositionResponse(
        id=position.id,
        market_id=position.market_id,
        market_title=market.title if market else None,
        direction=position.direction,
        shares=position.shares,
        entry_price=position.entry_price,
        entry_date=position.entry_date,
        exit_price=position.exit_price,
        exit_date=position.exit_date,
        current_price=position.current_price,
        current_value=position.current_value,
        unrealized_pnl=position.unrealized_pnl,
        realized_pnl=position.realized_pnl,
        cost_basis=position.cost_basis,
        status=position.status,
        thesis_status=position.thesis_status,
        entry_reasoning=position.entry_reasoning,
        exit_reasoning=position.exit_reasoning,
        created_at=position.created_at,
        updated_at=position.updated_at,
    )


@router.get("/{position_id}/history", response_model=PositionHistoryResponse)
async def get_position_history(
    position_id: int,
    limit: int = Query(default=100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
):
    """Get price history for a specific position."""
    result = await db.execute(select(Position).where(Position.id == position_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Position not found")

    settings = get_settings()
    client = PolymarketClient(settings.polymarket_wallet)
    tracker = TrackerService(db, client)

    try:
        snapshots = await tracker.get_position_history(position_id, limit=limit)
        return PositionHistoryResponse(
            position_id=position_id,
            snapshots=snapshots,
            count=len(snapshots),
        )
    finally:
        await client.close()


@router.post("", response_model=PositionResponse)
async def create_position(
    position_data: PositionCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create a new position manually."""
    result = await db.execute(
        select(Market).where(Market.id == position_data.market_id)
    )
    market = result.scalar_one_or_none()
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")

    cost_basis = position_data.shares * position_data.entry_price

    position = Position(
        market_id=position_data.market_id,
        direction=position_data.direction,
        shares=position_data.shares,
        entry_price=position_data.entry_price,
        entry_date=datetime.now(timezone.utc),
        current_price=position_data.entry_price,
        current_value=cost_basis,
        cost_basis=cost_basis,
        unrealized_pnl=0,
        status="open",
        thesis_status="active",
        entry_reasoning=position_data.entry_reasoning,
    )
    db.add(position)
    await db.commit()
    await db.refresh(position)

    return PositionResponse(
        id=position.id,
        market_id=position.market_id,
        market_title=market.title,
        direction=position.direction,
        shares=position.shares,
        entry_price=position.entry_price,
        entry_date=position.entry_date,
        exit_price=position.exit_price,
        exit_date=position.exit_date,
        current_price=position.current_price,
        current_value=position.current_value,
        unrealized_pnl=position.unrealized_pnl,
        realized_pnl=position.realized_pnl,
        cost_basis=position.cost_basis,
        status=position.status,
        thesis_status=position.thesis_status,
        entry_reasoning=position.entry_reasoning,
        exit_reasoning=position.exit_reasoning,
        created_at=position.created_at,
        updated_at=position.updated_at,
    )


@router.put("/{position_id}", response_model=PositionResponse)
async def update_position(
    position_id: int,
    update_data: PositionUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update an existing position."""
    result = await db.execute(
        select(Position, Market)
        .outerjoin(Market, Position.market_id == Market.id)
        .where(Position.id == position_id)
    )
    row = result.first()

    if not row:
        raise HTTPException(status_code=404, detail="Position not found")

    position, market = row

    if update_data.shares is not None:
        position.shares = update_data.shares
        position.cost_basis = position.shares * position.entry_price
    if update_data.current_price is not None:
        position.current_price = update_data.current_price
    if update_data.status is not None:
        position.status = update_data.status
        if update_data.status == "closed" and not position.exit_date:
            position.exit_date = datetime.now(timezone.utc)
    if update_data.thesis_status is not None:
        position.thesis_status = update_data.thesis_status
    if update_data.exit_price is not None:
        position.exit_price = update_data.exit_price
    if update_data.exit_reasoning is not None:
        position.exit_reasoning = update_data.exit_reasoning
    if update_data.realized_pnl is not None:
        position.realized_pnl = update_data.realized_pnl

    # Recalculate derived values
    if position.current_price and position.shares:
        position.current_value = position.shares * position.current_price
        position.unrealized_pnl = position.current_value - position.cost_basis

    await db.commit()
    await db.refresh(position)

    return PositionResponse(
        id=position.id,
        market_id=position.market_id,
        market_title=market.title if market else None,
        direction=position.direction,
        shares=position.shares,
        entry_price=position.entry_price,
        entry_date=position.entry_date,
        exit_price=position.exit_price,
        exit_date=position.exit_date,
        current_price=position.current_price,
        current_value=position.current_value,
        unrealized_pnl=position.unrealized_pnl,
        realized_pnl=position.realized_pnl,
        cost_basis=position.cost_basis,
        status=position.status,
        thesis_status=position.thesis_status,
        entry_reasoning=position.entry_reasoning,
        exit_reasoning=position.exit_reasoning,
        created_at=position.created_at,
        updated_at=position.updated_at,
    )
