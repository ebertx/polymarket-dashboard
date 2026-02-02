from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.config import get_settings
from app.schemas.portfolio import PortfolioCurrentResponse, PortfolioHistoryResponse, PositionSummary
from app.services.polymarket import PolymarketClient
from app.services.tracker import TrackerService

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


@router.get("/current", response_model=PortfolioCurrentResponse)
async def get_current_portfolio(db: AsyncSession = Depends(get_db)):
    """Get current portfolio state including all open positions."""
    settings = get_settings()
    client = PolymarketClient(settings.polymarket_wallet)
    tracker = TrackerService(db, client)

    try:
        portfolio = await tracker.get_current_portfolio()

        positions = [
            PositionSummary(
                id=p["id"],
                market_title=p["market_title"],
                direction=p["direction"],
                shares=p["shares"],
                entry_price=p["entry_price"],
                current_price=p["current_price"],
                current_value=p["current_value"],
                unrealized_pnl=p["unrealized_pnl"],
                status=p["status"],
            )
            for p in portfolio["positions"]
        ]

        return PortfolioCurrentResponse(
            cash_balance=portfolio["cash_balance"],
            position_value=portfolio["position_value"],
            total_value=portfolio["total_value"],
            unrealized_pnl=portfolio["unrealized_pnl"],
            positions=positions,
            last_updated=portfolio["last_updated"],
        )
    finally:
        await client.close()


@router.get("/history", response_model=PortfolioHistoryResponse)
async def get_portfolio_history(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Get portfolio snapshot history."""
    settings = get_settings()
    client = PolymarketClient(settings.polymarket_wallet)
    tracker = TrackerService(db, client)

    try:
        snapshots = await tracker.get_portfolio_history(limit=limit, offset=offset)
        return PortfolioHistoryResponse(
            snapshots=snapshots,
            count=len(snapshots),
        )
    finally:
        await client.close()


@router.post("/snapshot")
async def take_snapshot(db: AsyncSession = Depends(get_db)):
    """Manually trigger a portfolio snapshot."""
    settings = get_settings()
    client = PolymarketClient(settings.polymarket_wallet)
    tracker = TrackerService(db, client)

    try:
        snapshot = await tracker.take_portfolio_snapshot()
        return {
            "status": "success",
            "snapshot_id": snapshot.id if snapshot else None,
            "total_value": float(snapshot.total_value) if snapshot else None,
        }
    finally:
        await client.close()
