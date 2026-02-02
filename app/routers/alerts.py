from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List

from app.database import get_db
from app.models import Position, Market, Cluster, Catalyst

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("/positions-needing-attention")
async def get_positions_needing_attention(db: AsyncSession = Depends(get_db)):
    """
    Get positions that need review:
    - Drawdown > 30% from entry
    - Catalyst within 48 hours
    - Thesis status is 'degraded' or 'weakened'
    """
    now = datetime.utcnow()
    catalyst_horizon = now + timedelta(hours=48)

    # Get all open positions with market info
    result = await db.execute(
        select(Position, Market, Cluster)
        .join(Market, Position.market_id == Market.id)
        .outerjoin(Cluster, Market.cluster_id == Cluster.id)
        .where(Position.status == "open")
    )
    rows = result.all()

    # Get upcoming catalysts
    catalyst_result = await db.execute(
        select(Catalyst, Cluster)
        .outerjoin(Cluster, Catalyst.affected_cluster_id == Cluster.id)
        .where(
            Catalyst.event_date >= now,
            Catalyst.event_date <= catalyst_horizon,
        )
    )
    upcoming_catalysts = {
        c.affected_cluster_id: c for c, _ in catalyst_result.all() if c.affected_cluster_id
    }

    alerts = []

    for position, market, cluster in rows:
        reasons = []
        severity = "low"  # low, medium, high, critical

        # Check drawdown
        if position.cost_basis and position.cost_basis > 0:
            drawdown_pct = float((position.cost_basis - (position.current_value or 0)) / position.cost_basis)
            if drawdown_pct > 0.30:
                reasons.append(f"Drawdown: {drawdown_pct*100:.1f}%")
                severity = "high" if drawdown_pct > 0.50 else "medium"

        # Check thesis status
        if position.thesis_status in ("degraded", "invalidated"):
            reasons.append(f"Thesis: {position.thesis_status}")
            severity = "critical" if position.thesis_status == "invalidated" else "high"
        elif position.thesis_status == "weakened":
            reasons.append("Thesis: weakened")
            if severity == "low":
                severity = "medium"

        # Check for upcoming catalyst
        if cluster and cluster.id in upcoming_catalysts:
            catalyst = upcoming_catalysts[cluster.id]
            hours_until = (catalyst.event_date.replace(tzinfo=None) - now).total_seconds() / 3600
            reasons.append(f"Catalyst in {hours_until:.0f}h: {catalyst.title}")
            if severity in ("low", "medium"):
                severity = "high"

        # Check if market is expiring soon
        if market.end_date:
            end_date = market.end_date if isinstance(market.end_date, datetime) else datetime.combine(market.end_date, datetime.min.time())
            days_until_expiry = (end_date - now).days
            if days_until_expiry <= 3:
                reasons.append(f"Expires in {days_until_expiry} days")
                if severity == "low":
                    severity = "medium"

        if reasons:
            alerts.append({
                "position_id": position.id,
                "market_title": market.title,
                "direction": position.direction,
                "current_value": float(position.current_value or 0),
                "unrealized_pnl": float(position.unrealized_pnl or 0),
                "cost_basis": float(position.cost_basis or 0),
                "thesis_status": position.thesis_status,
                "severity": severity,
                "reasons": reasons,
                "cluster": cluster.name if cluster else None,
            })

    # Sort by severity (critical > high > medium > low)
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    alerts.sort(key=lambda x: severity_order.get(x["severity"], 4))

    return {
        "alerts": alerts,
        "count": len(alerts),
        "has_critical": any(a["severity"] == "critical" for a in alerts),
        "has_high": any(a["severity"] == "high" for a in alerts),
        "checked_at": now.isoformat(),
    }


@router.get("/review-status")
async def get_review_status(db: AsyncSession = Depends(get_db)):
    """
    Check if a weekly review is due.
    Returns status and template for the review.
    """
    now = datetime.utcnow()
    today = now.date()
    monday = today - timedelta(days=today.weekday())

    # Get all open positions
    result = await db.execute(
        select(Position, Market)
        .join(Market, Position.market_id == Market.id)
        .where(Position.status == "open")
        .order_by(Position.entry_date)
    )
    positions = result.all()

    # Calculate portfolio stats
    total_value = sum(float(p.current_value or 0) for p, _ in positions)
    total_pnl = sum(float(p.unrealized_pnl or 0) for p, _ in positions)

    # Build review template
    position_summaries = []
    for position, market in positions:
        cost_basis = float(position.cost_basis or 0)
        pnl_pct = (float(position.unrealized_pnl or 0) / cost_basis * 100) if cost_basis > 0 else 0

        position_summaries.append({
            "market": market.title,
            "direction": position.direction,
            "entry_price": float(position.entry_price),
            "current_price": float(position.current_price or 0),
            "pnl_pct": pnl_pct,
            "thesis_status": position.thesis_status or "active",
        })

    return {
        "review_week_of": monday.isoformat(),
        "review_due": today.weekday() == 0,  # Due on Mondays
        "days_until_due": (7 - today.weekday()) % 7,
        "portfolio_summary": {
            "position_count": len(positions),
            "total_value": total_value,
            "total_pnl": total_pnl,
            "pnl_pct": (total_pnl / (total_value - total_pnl) * 100) if (total_value - total_pnl) > 0 else 0,
        },
        "positions": position_summaries,
        "template": f"""## Position Review: Week of {monday.isoformat()}

### Portfolio Summary
- **Total Value:** ${total_value:.2f}
- **Unrealized P&L:** ${total_pnl:+.2f}
- **Position Count:** {len(positions)}

### Position Status

| Market | Dir | Entry | Current | P&L % | Thesis |
|--------|-----|-------|---------|-------|--------|
""" + "\n".join([
            f"| {p['market'][:30]} | {p['direction'].upper()} | {p['entry_price']:.2f} | {p['current_price']:.2f} | {p['pnl_pct']:+.1f}% | {p['thesis_status']} |"
            for p in position_summaries
        ]) + """

### Review Checklist
- [ ] All thesis statuses are current
- [ ] No positions exceed risk limits
- [ ] Upcoming catalysts have action plans
- [ ] Exit criteria are defined for each position
"""
    }


@router.get("/summary")
async def get_alert_summary(db: AsyncSession = Depends(get_db)):
    """
    Quick summary of alert status for dashboard header.
    """
    attention = await get_positions_needing_attention(db)
    review = await get_review_status(db)

    return {
        "attention_count": attention["count"],
        "has_critical": attention["has_critical"],
        "has_high": attention["has_high"],
        "review_due": review["review_due"],
        "days_until_review": review["days_until_due"],
    }
