import json
import logging
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PortfolioSnapshot, Position, PositionSnapshot, Market
from app.services.polymarket import PolymarketClient

logger = logging.getLogger(__name__)

# Module-level miss counter that persists across TrackerService instances
# within the same process. Keyed by position ID.
_api_miss_counts: Dict[int, int] = {}

# Number of consecutive misses before auto-closing a sold position
AUTO_CLOSE_MISS_THRESHOLD = 3


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

        # Collect unknown token_ids for auto-discovery
        unknown_api_positions = []
        for pos_data in api_positions:
            token_id = pos_data.get("token_id")
            if token_id and token_id not in db_positions:
                unknown_api_positions.append(pos_data)

        # Update positions found in API
        for pos_data in api_positions:
            token_id = pos_data.get("token_id")
            if not token_id or token_id not in db_positions:
                continue

            found_token_ids.add(token_id)
            position, market = db_positions[token_id]

            # Reset miss counter — position is present in API
            _api_miss_counts.pop(position.id, None)

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

        # Auto-discover new positions not yet in DB
        if unknown_api_positions:
            await self._auto_discover_positions(unknown_api_positions)

        # Check for positions NOT found in API - these may have resolved
        now = datetime.now(timezone.utc)
        missing_positions = {
            token_id: (position, market)
            for token_id, (position, market) in db_positions.items()
            if token_id not in found_token_ids
        }

        # SAFETY: If ALL (or nearly all) positions disappeared at once, this is
        # almost certainly an API failure, not real position closures.
        # Only proceed with closure logic if at least some positions were matched.
        if missing_positions and not found_token_ids and len(db_positions) > 1:
            logger.warning(
                f"ALL {len(db_positions)} positions missing from API response — "
                f"likely API failure. Skipping position closure logic."
            )
            return

        if len(missing_positions) > 2 and len(found_token_ids) < len(db_positions) * 0.3:
            logger.warning(
                f"{len(missing_positions)}/{len(db_positions)} positions missing from API "
                f"(only {len(found_token_ids)} matched). Possible API issue — "
                f"skipping closure logic to avoid mass false-closes."
            )
            return

        for token_id, (position, market) in missing_positions.items():
            # Position not in API - check if market has resolved
            market_resolved = False
            resolution_outcome = None

            if market.resolved_at is not None:
                market_resolved = True
                resolution_outcome = market.resolution_outcome
            elif market.end_date is not None and market.end_date <= now:
                # Market end date has passed - likely resolved
                market_resolved = True
                resolution_outcome = None

            if market_resolved:
                logger.info(
                    f"Marking position {position.id} as closed - market '{market.title}' has resolved"
                )

                if resolution_outcome is not None:
                    won = (position.direction == "yes" and resolution_outcome == "yes") or \
                          (position.direction == "no" and resolution_outcome == "no")
                    payout = position.shares * Decimal("1.0") if won else Decimal("0")
                else:
                    last_price = position.current_price or Decimal("0")
                    payout = position.shares * last_price

                realized_pnl = payout - position.cost_basis

                if resolution_outcome is not None:
                    outcome_lower = resolution_outcome.lower() if resolution_outcome else None
                    direction_lower = position.direction.lower() if position.direction else None
                    won = outcome_lower == direction_lower
                    exit_price = Decimal("1.0") if won else Decimal("0")
                else:
                    exit_price = position.current_price or Decimal("0")

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
                # Position not in API and market hasn't resolved.
                # Track consecutive misses and auto-close after threshold,
                # but only if enough other positions were found (guards against API outage).
                miss_count = _api_miss_counts.get(position.id, 0) + 1
                _api_miss_counts[position.id] = miss_count

                if miss_count >= AUTO_CLOSE_MISS_THRESHOLD and len(found_token_ids) >= 2:
                    # Position has been absent for 3+ consecutive sync cycles
                    # and the API is returning other positions (not an outage).
                    logger.info(
                        f"Auto-closing position {position.id} ('{market.title}'): "
                        f"absent from API for {miss_count} consecutive sync cycles. "
                        f"Likely sold externally."
                    )
                    last_price = position.current_price or Decimal("0")
                    payout = position.shares * last_price
                    realized_pnl = payout - position.cost_basis

                    position.status = "closed"
                    position.exit_date = now
                    position.exit_price = last_price
                    position.realized_pnl = realized_pnl
                    position.current_value = Decimal("0")
                    position.unrealized_pnl = Decimal("0")
                    position.exit_reasoning = (
                        f"auto-closed: position absent from API for {miss_count} sync cycles"
                    )

                    # Clean up miss counter
                    _api_miss_counts.pop(position.id, None)

                    logger.info(
                        f"Position {position.id} auto-closed: "
                        f"realized_pnl=${realized_pnl:.2f}, exit_price={last_price}"
                    )
                else:
                    logger.warning(
                        f"Position {position.id} ('{market.title}') not found in API but market "
                        f"still active (miss {miss_count}/{AUTO_CLOSE_MISS_THRESHOLD}). "
                        f"Will auto-close after {AUTO_CLOSE_MISS_THRESHOLD} consecutive misses."
                    )

    async def _auto_discover_positions(self, unknown_positions: List[Dict]) -> None:
        """Auto-discover and create DB records for positions found in API but not in DB.

        For each unknown position:
        1. Look up market metadata from Gamma API by token_id
        2. Create Market row if it doesn't exist
        3. Create Position row
        """
        now = datetime.now(timezone.utc)

        for pos_data in unknown_positions:
            token_id = pos_data.get("token_id")
            if not token_id:
                continue

            try:
                # Look up market metadata from Gamma API
                market_data = await self.client.lookup_market_by_token_id(token_id)
                if not market_data:
                    logger.warning(
                        f"Auto-discover: Gamma API returned no data for token {token_id}. Skipping."
                    )
                    continue

                # Parse market metadata
                condition_id = market_data.get("conditionId") or market_data.get("condition_id", "")
                title = market_data.get("question") or market_data.get("title", "Unknown Market")
                slug = market_data.get("slug", f"unknown-{condition_id[:20]}")
                description = market_data.get("description", "")
                end_date_str = market_data.get("endDate") or market_data.get("end_date_iso")

                # Parse clobTokenIds — comes as JSON string like '["yes_token", "no_token"]'
                clob_token_ids_raw = market_data.get("clobTokenIds", "[]")
                if isinstance(clob_token_ids_raw, str):
                    try:
                        clob_token_ids = json.loads(clob_token_ids_raw)
                    except json.JSONDecodeError:
                        clob_token_ids = []
                elif isinstance(clob_token_ids_raw, list):
                    clob_token_ids = clob_token_ids_raw
                else:
                    clob_token_ids = []

                if len(clob_token_ids) < 2:
                    logger.warning(
                        f"Auto-discover: Market '{title}' has {len(clob_token_ids)} token IDs "
                        f"(expected 2). Skipping."
                    )
                    continue

                clob_token_id_yes = clob_token_ids[0]
                clob_token_id_no = clob_token_ids[1]

                # Determine direction based on which token matches
                if token_id == clob_token_id_yes:
                    direction = "yes"
                elif token_id == clob_token_id_no:
                    direction = "no"
                else:
                    logger.warning(
                        f"Auto-discover: Token {token_id} doesn't match either YES ({clob_token_id_yes}) "
                        f"or NO ({clob_token_id_no}) for market '{title}'. Skipping."
                    )
                    continue

                # Parse end_date
                end_date = None
                if end_date_str:
                    try:
                        end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    except (ValueError, AttributeError):
                        pass

                # Check if market already exists by condition_id or slug
                existing_market = None
                if condition_id:
                    result = await self.db.execute(
                        select(Market).where(Market.condition_id == condition_id)
                    )
                    existing_market = result.scalar_one_or_none()

                if existing_market is None:
                    result = await self.db.execute(
                        select(Market).where(Market.slug == slug)
                    )
                    existing_market = result.scalar_one_or_none()

                if existing_market:
                    market = existing_market
                    # Update token IDs if missing
                    if not market.clob_token_id_yes:
                        market.clob_token_id_yes = clob_token_id_yes
                    if not market.clob_token_id_no:
                        market.clob_token_id_no = clob_token_id_no
                    logger.info(
                        f"Auto-discover: Using existing market '{market.title}' (id={market.id})"
                    )
                else:
                    market = Market(
                        slug=slug,
                        title=title,
                        description=description,
                        condition_id=condition_id,
                        clob_token_id_yes=clob_token_id_yes,
                        clob_token_id_no=clob_token_id_no,
                        end_date=end_date,
                    )
                    self.db.add(market)
                    await self.db.flush()  # Get the market.id
                    logger.info(
                        f"Auto-discover: Created new market '{title}' (id={market.id}, slug={slug})"
                    )

                # Check if an open position already exists for this market+direction
                result = await self.db.execute(
                    select(Position).where(
                        Position.market_id == market.id,
                        Position.direction == direction,
                        Position.status == "open",
                    )
                )
                existing_position = result.scalar_one_or_none()
                if existing_position:
                    logger.info(
                        f"Auto-discover: Position already exists for '{title}' {direction} "
                        f"(id={existing_position.id}). Skipping."
                    )
                    continue

                # Create position
                shares = pos_data.get("size", Decimal("0"))
                if isinstance(shares, (int, float, str)):
                    shares = Decimal(str(shares))
                avg_price = pos_data.get("avg_price", Decimal("0"))
                if isinstance(avg_price, (int, float, str)):
                    avg_price = Decimal(str(avg_price))
                current_price = pos_data.get("current_price", Decimal("0"))
                if isinstance(current_price, (int, float, str)):
                    current_price = Decimal(str(current_price))

                cost_basis = shares * avg_price
                current_value = shares * current_price
                unrealized_pnl = current_value - cost_basis

                position = Position(
                    market_id=market.id,
                    direction=direction,
                    shares=shares,
                    entry_price=avg_price,
                    entry_date=now,
                    current_price=current_price,
                    current_value=current_value,
                    cost_basis=cost_basis,
                    unrealized_pnl=unrealized_pnl,
                    status="open",
                    entry_reasoning="auto-discovered: position found in API but not in tracking DB",
                )
                self.db.add(position)

                logger.info(
                    f"Auto-discover: Created position for '{title}' — "
                    f"{shares} {direction.upper()} @ {avg_price} (value=${current_value:.2f})"
                )

            except Exception as e:
                logger.error(
                    f"Auto-discover: Failed to process token {token_id}: {e}",
                    exc_info=True,
                )
                # Continue with next position — don't let one failure stop others
                continue

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
