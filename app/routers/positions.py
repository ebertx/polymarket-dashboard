from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import List, Dict

from app.database import get_db
from app.config import get_settings
from app.models import Position, PositionSnapshot, Market, Cluster, AlertDefinition
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


@router.get("/sparklines")
async def get_sparklines(
    days: int = Query(default=7, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
):
    """Get downsampled price history for all open positions (1 point per day).

    Returns a dict keyed by position ID with arrays of {date, price, value}.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Get open position IDs
    open_result = await db.execute(
        select(Position.id).where(Position.status == "open")
    )
    open_ids = [row[0] for row in open_result.all()]

    if not open_ids:
        return {}

    # For each day, take the last snapshot (max timestamp per day per position).
    # We use a subquery to find the max snapshot id per (position_id, date) bucket,
    # then join back to get the actual price/value.
    #
    # Step 1: get max id per (position_id, date)
    date_expr = sa_func.date(PositionSnapshot.timestamp)
    max_id_subq = (
        select(
            sa_func.max(PositionSnapshot.id).label("max_id"),
        )
        .where(
            PositionSnapshot.position_id.in_(open_ids),
            PositionSnapshot.timestamp >= cutoff,
        )
        .group_by(PositionSnapshot.position_id, date_expr)
        .subquery()
    )

    # Step 2: fetch those snapshots
    result = await db.execute(
        select(
            PositionSnapshot.position_id,
            PositionSnapshot.timestamp,
            PositionSnapshot.price,
            PositionSnapshot.value,
        )
        .where(PositionSnapshot.id.in_(select(max_id_subq.c.max_id)))
        .order_by(PositionSnapshot.position_id, PositionSnapshot.timestamp)
    )
    rows = result.all()

    # Build response dict
    sparklines: Dict[str, list] = {}
    for pos_id, ts, price, value in rows:
        key = str(pos_id)
        if key not in sparklines:
            sparklines[key] = []
        sparklines[key].append({
            "date": ts.strftime("%Y-%m-%d"),
            "price": float(price) if price is not None else None,
            "value": float(value) if value is not None else None,
        })

    return sparklines


@router.get("/closed/summary")
async def get_closed_positions_summary(
    db: AsyncSession = Depends(get_db),
):
    """Get summary statistics for all closed positions."""
    result = await db.execute(
        select(Position, Market)
        .outerjoin(Market, Position.market_id == Market.id)
        .where(Position.status == "closed")
        .order_by(Position.exit_date.desc())
    )
    rows = result.all()

    positions_list = []
    wins = 0
    losses = 0
    total_realized_pnl = Decimal("0")
    win_pnls = []
    loss_pnls = []

    for position, market in rows:
        pnl = position.realized_pnl or Decimal("0")
        won = pnl > 0

        if won:
            wins += 1
            win_pnls.append(float(pnl))
        else:
            losses += 1
            loss_pnls.append(float(pnl))

        total_realized_pnl += pnl

        positions_list.append({
            "id": position.id,
            "market_title": market.title if market else "Unknown",
            "direction": position.direction,
            "realized_pnl": float(pnl),
            "won": won,
            "closed_date": position.exit_date.strftime("%Y-%m-%d") if position.exit_date else None,
            "entry_price": float(position.entry_price) if position.entry_price else None,
            "exit_price": float(position.exit_price) if position.exit_price else None,
            "shares": float(position.shares) if position.shares else None,
        })

    total_closed = wins + losses
    win_rate = round((wins / total_closed) * 100, 1) if total_closed > 0 else 0.0
    avg_win = round(sum(win_pnls) / len(win_pnls), 2) if win_pnls else 0.0
    avg_loss = round(sum(loss_pnls) / len(loss_pnls), 2) if loss_pnls else 0.0

    return {
        "total_closed": total_closed,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_realized_pnl": float(total_realized_pnl),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "positions": positions_list,
    }


@router.get("/{position_id}/detail")
async def get_position_detail(
    position_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get comprehensive detail for a single position including market metadata,
    alerts, and 7-day price history."""
    # Fetch position with market and cluster
    result = await db.execute(
        select(Position, Market, Cluster)
        .outerjoin(Market, Position.market_id == Market.id)
        .outerjoin(Cluster, Market.cluster_id == Cluster.id)
        .where(Position.id == position_id)
    )
    row = result.first()

    if not row:
        raise HTTPException(status_code=404, detail="Position not found")

    position, market, cluster = row

    # Calculate derived fields
    cost_basis = float(position.cost_basis) if position.cost_basis else 0.0
    current_value = float(position.current_value) if position.current_value else 0.0
    unrealized_pnl = float(position.unrealized_pnl) if position.unrealized_pnl else 0.0
    pnl_pct = round((unrealized_pnl / cost_basis) * 100, 2) if cost_basis > 0 else 0.0

    days_remaining = None
    if market and market.end_date:
        delta = market.end_date - datetime.now(timezone.utc)
        days_remaining = max(0, delta.days)

    # Fetch alerts for this market
    alerts_list = []
    if market:
        alert_result = await db.execute(
            select(AlertDefinition).where(
                AlertDefinition.market_slug == market.slug,
                AlertDefinition.enabled == True,
            )
        )
        alert_defs = alert_result.scalars().all()
        for ad in alert_defs:
            alerts_list.append({
                "type": ad.alert_type,
                "threshold": float(ad.threshold) if ad.threshold else None,
                "action": ad.action,
                "severity": ad.severity,
            })

    # Fetch 7-day price history (downsampled to 1 per day)
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    date_expr = sa_func.date(PositionSnapshot.timestamp)
    max_id_subq = (
        select(sa_func.max(PositionSnapshot.id).label("max_id"))
        .where(
            PositionSnapshot.position_id == position_id,
            PositionSnapshot.timestamp >= cutoff,
        )
        .group_by(date_expr)
        .subquery()
    )
    snap_result = await db.execute(
        select(PositionSnapshot.timestamp, PositionSnapshot.price)
        .where(PositionSnapshot.id.in_(select(max_id_subq.c.max_id)))
        .order_by(PositionSnapshot.timestamp)
    )
    price_history = [
        {
            "date": ts.strftime("%Y-%m-%d"),
            "price": float(price) if price is not None else None,
        }
        for ts, price in snap_result.all()
    ]

    return {
        "id": position.id,
        "market_title": market.title if market else "Unknown",
        "market_slug": market.slug if market else None,
        "direction": position.direction,
        "shares": float(position.shares) if position.shares else 0,
        "entry_price": float(position.entry_price) if position.entry_price else 0,
        "current_price": float(position.current_price) if position.current_price else 0,
        "current_value": current_value,
        "cost_basis": cost_basis,
        "unrealized_pnl": unrealized_pnl,
        "pnl_pct": pnl_pct,
        "entry_date": position.entry_date.strftime("%Y-%m-%d") if position.entry_date else None,
        "end_date": market.end_date.isoformat() if market and market.end_date else None,
        "days_remaining": days_remaining,
        "thesis_status": position.thesis_status,
        "cluster_name": cluster.name if cluster else "uncorrelated",
        "market_description": market.description if market else None,
        "status": position.status,
        "entry_reasoning": position.entry_reasoning,
        "exit_reasoning": position.exit_reasoning,
        "exit_price": float(position.exit_price) if position.exit_price else None,
        "exit_date": position.exit_date.strftime("%Y-%m-%d") if position.exit_date else None,
        "realized_pnl": float(position.realized_pnl) if position.realized_pnl else None,
        "alerts": alerts_list,
        "price_history_7d": price_history,
    }


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
