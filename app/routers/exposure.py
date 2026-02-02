from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List

from app.database import get_db
from app.models import Position, Market, Cluster, Catalyst

router = APIRouter(tags=["exposure"])


@router.get("/exposure/clusters")
async def get_cluster_exposure(db: AsyncSession = Depends(get_db)):
    """Get position exposure grouped by cluster."""
    result = await db.execute(
        select(Position, Market, Cluster)
        .join(Market, Position.market_id == Market.id)
        .outerjoin(Cluster, Market.cluster_id == Cluster.id)
        .where(Position.status == "open")
    )
    rows = result.all()

    cluster_exposure = {}
    unclustered_value = Decimal("0")
    unclustered_positions = []

    for position, market, cluster in rows:
        value = position.current_value or Decimal("0")
        pos_info = {
            "position_id": position.id,
            "market_title": market.title,
            "direction": position.direction,
            "value": float(value),
            "unrealized_pnl": float(position.unrealized_pnl or 0),
        }

        if cluster:
            if cluster.id not in cluster_exposure:
                cluster_exposure[cluster.id] = {
                    "cluster_id": cluster.id,
                    "cluster_name": cluster.name,
                    "max_exposure_pct": float(cluster.max_exposure_pct) if cluster.max_exposure_pct else None,
                    "total_value": Decimal("0"),
                    "total_unrealized_pnl": Decimal("0"),
                    "positions": [],
                }
            cluster_exposure[cluster.id]["total_value"] += value
            cluster_exposure[cluster.id]["total_unrealized_pnl"] += position.unrealized_pnl or Decimal("0")
            cluster_exposure[cluster.id]["positions"].append(pos_info)
        else:
            unclustered_value += value
            unclustered_positions.append(pos_info)

    clusters = []
    for cluster_data in cluster_exposure.values():
        clusters.append({
            "cluster_id": cluster_data["cluster_id"],
            "cluster_name": cluster_data["cluster_name"],
            "max_exposure_pct": cluster_data["max_exposure_pct"],
            "total_value": float(cluster_data["total_value"]),
            "total_unrealized_pnl": float(cluster_data["total_unrealized_pnl"]),
            "position_count": len(cluster_data["positions"]),
            "positions": cluster_data["positions"],
        })

    clusters.sort(key=lambda x: x["total_value"], reverse=True)

    return {
        "clusters": clusters,
        "unclustered": {
            "total_value": float(unclustered_value),
            "position_count": len(unclustered_positions),
            "positions": unclustered_positions,
        },
    }


@router.get("/catalysts/upcoming")
async def get_upcoming_catalysts(
    days: int = Query(default=7, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
):
    """Get upcoming catalysts within the specified number of days."""
    now = datetime.utcnow()
    end_date = now + timedelta(days=days)

    result = await db.execute(
        select(Catalyst, Cluster)
        .outerjoin(Cluster, Catalyst.affected_cluster_id == Cluster.id)
        .where(
            Catalyst.event_date >= now,
            Catalyst.event_date <= end_date,
        )
        .order_by(Catalyst.event_date)
    )
    rows = result.all()

    catalysts = []
    for catalyst, cluster in rows:
        # Get positions in affected cluster
        positions = []
        total_value = Decimal("0")
        if cluster:
            pos_result = await db.execute(
                select(Position)
                .join(Market, Position.market_id == Market.id)
                .where(
                    Market.cluster_id == cluster.id,
                    Position.status == "open",
                )
            )
            positions = pos_result.scalars().all()
            total_value = sum(p.current_value or Decimal("0") for p in positions)

        catalysts.append({
            "id": catalyst.id,
            "title": catalyst.title,
            "description": catalyst.description,
            "event_date": catalyst.event_date.isoformat(),
            "risk_direction": catalyst.risk_direction,
            "recommended_action": catalyst.recommended_action,
            "action_taken": catalyst.action_taken,
            "days_until": (catalyst.event_date.replace(tzinfo=None) - now).days,
            "affected_cluster": cluster.name if cluster else None,
            "affected_position_count": len(positions),
            "affected_position_value": float(total_value),
        })

    return {
        "catalysts": catalysts,
        "count": len(catalysts),
        "days_range": days,
    }
