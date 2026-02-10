import logging
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PortfolioSnapshot, Position, PositionSnapshot, Market
from app.services.polymarket import PolymarketClient

logger = logging.getLogger(__name__)


class TrackerService:
    def __init__(self, db: AsyncSession, client: PolymarketClient):
        self.db = db
        self.client = client

    async def take_portfolio_snapshot(self) -> Optional[PortfolioSnapshot]:
        """
        Fetch current wallet state and store a portfolio snapshot.
        Also updates position prices and creates position snapshots.
        """
        try:
            wallet_data = await self.client.get_wallet_balance()

            cash_balance = wallet_data["usdc_balance"]
            position_value = wallet_data["total_position_value"]
            api_positions = wallet_data["positions"]

            total_value = cash_balance + position_value

            # Get previous snapshot for daily PnL calculation
            result = await self.db.execute(
                select(PortfolioSnapshot)
                .order_by(PortfolioSnapshot.timestamp.desc())
                .limit(1)
            )
            prev_snapshot = result.scalar_one_or_none()

            daily_pnl = None
            daily_pnl_pct = None
            if prev_snapshot and prev_snapshot.total_value:
                daily_pnl = total_value - prev_snapshot.total_value
                if prev_snapshot.total_value > 0:
                    daily_pnl_pct = (daily_pnl / prev_snapshot.total_value) * 100

            # Create portfolio snapshot
            snapshot = PortfolioSnapshot(
                timestamp=datetime.now(timezone.utc),
                cash_balance=cash_balance,
                position_value=position_value,
                total_value=total_value,
                daily_pnl=daily_pnl,
                daily_pnl_pct=daily_pnl_pct,
                granularity="minute",
            )
            self.db.add(snapshot)

            # Update positions and create position snapshots
            await self._sync_positions(api_positions)

            await self.db.commit()
            await self.db.refresh(snapshot)

            logger.info(
                f"Portfolio snapshot created: total=${total_value:.2f}, "
                f"positions=${position_value:.2f}"
            )
            return snapshot

        except Exception as e:
            logger.error(f"Failed to take portfolio snapshot: {e}")
            await self.db.rollback()
            raise

    async def _sync_positions(self, api_positions: List[Dict]) -> None:
        """Sync positions from API with database - update prices for open positions."""
        # Get all open positions from DB with their markets
        result = await self.db.execute(
            select(Position, Market)
            .outerjoin(Market, Position.market_id == Market.id)
            .where(Position.status == "open")
        )
        rows = result.all()

        # Build lookup of token_id -> (position, market)
        db_positions = {}
        for row in rows:
            position, market = row[0], row[1]
            if market is not None:
                token_id = market.clob_token_id_yes if position.direction == "yes" else market.clob_token_id_no
                db_positions[token_id] = (position, market)

        # Track which positions we found in API
        found_token_ids = set()

        # Build lookup of API positions by token_id
        api_positions_by_token = {}
        for pos_data in api_positions:
            token_id = pos_data.get("token_id")
            if token_id:
                api_positions_by_token[token_id] = pos_data

        # Update positions found in API
        for pos_data in api_positions:
            token_id = pos_data.get("token_id")
            if not token_id or token_id not in db_positions:
                continue

            found_token_ids.add(token_id)
            position, market = db_positions[token_id]
            current_price = pos_data.get("current_price", Decimal("0"))
            value = pos_data.get("value", Decimal("0"))
            api_size = pos_data.get("size")

            # Sync share count if API reports different size (e.g., partial sell)
            if api_size is not None:
                api_shares = Decimal(str(api_size))
                if api_shares != position.shares and api_shares > 0:
                    old_shares = position.shares
                    shares_sold = old_shares - api_shares
                    # Adjust cost basis proportionally
                    if old_shares > 0:
                        position.cost_basis = position.cost_basis * (api_shares / old_shares)
                    position.shares = api_shares
                    logger.info(
                        f"Position {position.id} shares updated: {old_shares} -> {api_shares} "
                        f"(sold {shares_sold})"
                    )

            # Update position
            position.current_price = current_price
            position.current_value = value
            position.unrealized_pnl = value - position.cost_basis

            # Create position snapshot
            pos_snapshot = PositionSnapshot(
                position_id=position.id,
                timestamp=datetime.now(timezone.utc),
                price=current_price,
                value=value,
            )
            self.db.add(pos_snapshot)

        # Check for positions NOT found in API - these may have resolved
        now = datetime.now(timezone.utc)
        for token_id, (position, market) in db_positions.items():
            if token_id in found_token_ids:
                continue

            # Position not in API - check if market has resolved
            market_resolved = False
            resolution_outcome = None

            if market.resolved_at is not None:
                market_resolved = True
                resolution_outcome = market.resolution_outcome
            elif market.end_date is not None and market.end_date <= now:
                # Market end date has passed - likely resolved
                market_resolved = True
                # Try to determine outcome from market data (we don't have it here,
                # so we'll mark as resolved and set conservative P&L)
                resolution_outcome = None

            if market_resolved:
                logger.info(
                    f"Marking position {position.id} as closed - market '{market.title}' has resolved"
                )

                # Calculate realized P&L
                # If we know the outcome, calculate properly
                # If direction matches outcome, position paid out at $1.00
                # If direction doesn't match, position paid out at $0.00
                if resolution_outcome is not None:
                    won = (position.direction == "yes" and resolution_outcome == "yes") or \
                          (position.direction == "no" and resolution_outcome == "no")
                    payout = position.shares * Decimal("1.0") if won else Decimal("0")
                else:
                    # Outcome unknown - use last known price or 0
                    # Since position disappeared from API, assume it resolved
                    # Conservative: assume we got current_price value (or 0 if unknown)
                    last_price = position.current_price or Decimal("0")
                    payout = position.shares * last_price

                realized_pnl = payout - position.cost_basis

                # Determine exit price
                if resolution_outcome is not None:
                    # Case-insensitive comparison for outcome
                    outcome_lower = resolution_outcome.lower() if resolution_outcome else None
                    direction_lower = position.direction.lower() if position.direction else None
                    won = outcome_lower == direction_lower
                    exit_price = Decimal("1.0") if won else Decimal("0")
                else:
                    # Outcome unknown - use last known price
                    exit_price = position.current_price or Decimal("0")

                # Update position as closed
                position.status = "closed"
                position.exit_date = market.resolved_at or now
                position.exit_price = exit_price
                position.realized_pnl = realized_pnl
                position.current_value = Decimal("0")
                position.unrealized_pnl = Decimal("0")

                logger.info(
                    f"Position {position.id} closed: realized_pnl=${realized_pnl:.2f}, exit_price={exit_price}"
                )
            else:
                # Position not in API and market hasn't resolved = manual exit
                # The user sold all shares outside the tracker
                logger.info(
                    f"Position {position.id} not in API but market '{market.title}' still active - "
                    f"treating as manual exit"
                )

                exit_price = position.current_price or Decimal("0")
                realized_pnl = (position.shares * exit_price) - position.cost_basis

                position.status = "closed"
                position.exit_date = now
                position.exit_price = exit_price
                position.realized_pnl = realized_pnl
                position.current_value = Decimal("0")
                position.unrealized_pnl = Decimal("0")
                position.exit_reasoning = "Auto-detected: position disappeared from API (manual exit)"

                logger.info(
                    f"Position {position.id} closed (manual exit): "
                    f"exit_price={exit_price}, realized_pnl=${realized_pnl:.2f}"
                )

    async def update_position_prices(self) -> int:
        """
        Update current prices for all open positions using CLOB API.
        Returns number of positions updated.
        """
        result = await self.db.execute(
            select(Position, Market)
            .join(Market, Position.market_id == Market.id)
            .where(Position.status == "open")
        )
        rows = result.all()

        updated_count = 0
        for position, market in rows:
            try:
                # Get the right token ID based on direction
                token_id = market.clob_token_id_yes if position.direction == "yes" else market.clob_token_id_no
                if not token_id:
                    continue

                price = await self.client.get_market_price(token_id)
                if price is not None:
                    position.current_price = price
                    position.current_value = position.shares * price
                    position.unrealized_pnl = position.current_value - position.cost_basis
                    updated_count += 1
            except Exception as e:
                logger.warning(f"Failed to update price for position {position.id}: {e}")

        if updated_count > 0:
            await self.db.commit()
            logger.info(f"Updated prices for {updated_count} positions")

        return updated_count

    async def get_current_portfolio(self) -> Dict[str, Any]:
        """Get current portfolio state from database."""
        # Get latest snapshot
        result = await self.db.execute(
            select(PortfolioSnapshot)
            .order_by(PortfolioSnapshot.timestamp.desc())
            .limit(1)
        )
        latest_snapshot = result.scalar_one_or_none()

        # Get open positions with market info
        result = await self.db.execute(
            select(Position, Market)
            .outerjoin(Market, Position.market_id == Market.id)
            .where(Position.status == "open")
        )
        positions_with_markets = result.all()

        positions = []
        total_position_value = Decimal("0")
        total_unrealized_pnl = Decimal("0")

        for position, market in positions_with_markets:
            value = position.current_value or Decimal("0")
            unrealized = position.unrealized_pnl or Decimal("0")

            positions.append({
                "id": position.id,
                "market_title": market.title if market else "Unknown",
                "direction": position.direction,
                "shares": position.shares,
                "entry_price": position.entry_price,
                "current_price": position.current_price,
                "current_value": value,
                "unrealized_pnl": unrealized,
                "status": position.status,
            })

            total_position_value += value
            total_unrealized_pnl += unrealized

        cash_balance = latest_snapshot.cash_balance if latest_snapshot else Decimal("0")

        return {
            "cash_balance": cash_balance,
            "position_value": total_position_value,
            "total_value": cash_balance + total_position_value,
            "unrealized_pnl": total_unrealized_pnl,
            "positions": positions,
            "last_updated": latest_snapshot.timestamp if latest_snapshot else None,
        }

    async def get_portfolio_history(
        self, limit: int = 100, offset: int = 0
    ) -> List[PortfolioSnapshot]:
        """Get portfolio snapshot history."""
        result = await self.db.execute(
            select(PortfolioSnapshot)
            .order_by(PortfolioSnapshot.timestamp.desc())
            .limit(limit)
            .offset(offset)
        )
        return result.scalars().all()

    async def get_position_history(
        self, position_id: int, limit: int = 100
    ) -> List[PositionSnapshot]:
        """Get price history for a specific position."""
        result = await self.db.execute(
            select(PositionSnapshot)
            .where(PositionSnapshot.position_id == position_id)
            .order_by(PositionSnapshot.timestamp.desc())
            .limit(limit)
        )
        return result.scalars().all()
