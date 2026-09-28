import json
import logging
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PortfolioSnapshot, Position, PositionSnapshot, Market
from app.services.cost_basis import resolve_cost_basis
from app.services.polymarket import PolymarketClient
from app.services.alerts import AlertService
from app.services.resolution import (
    compute_close_booking,
    parse_resolution,
    should_check_resolution,
)

logger = logging.getLogger(__name__)

# Number of consecutive misses before auto-closing a sold position.
# The counter lives in positions.api_miss_count so it survives a container
# restart (it used to be a module-level dict, which reset progress to zero).
AUTO_CLOSE_MISS_THRESHOLD = 3

# Throttle for end_date-triggered Gamma resolution lookups, keyed by market id.
# A market whose end_date has passed but which UMA hasn't settled (disputes can
# run for days) would otherwise be re-queried on every 60s poll. In-memory only:
# a restart just costs one extra lookup per market. Strong signals (redeemable,
# or the position vanishing from the Data API) bypass the throttle.
_last_resolution_check: Dict[int, datetime] = {}
# Tokens whose Gamma lookup already missed this process; later misses log at
# debug (a worthless still-redeemable token otherwise warns every poll).
_gamma_miss_warned: set = set()
RESOLUTION_RECHECK_SECONDS = 300

# Most resolved positions the resolution sweep will book in a single cycle.
# A negRisk event can legitimately settle several brackets at once (the Nobel
# basket was three), but a double-digit batch means something is wrong with the
# resolution data, not with the portfolio.
RESOLUTION_SWEEP_MAX_CLOSES = 6

# A partial sell is priced from the activity feed only if the recent SELL
# fills cover the sold amount to within this fraction (Data API sizes are
# rounded, so exact equality is too strict). Otherwise the slice is booked at
# the Data API midpoint with a WARNING.
PARTIAL_SELL_COVERAGE_TOLERANCE = Decimal("0.005")


class TrackerService:
    def __init__(self, db: AsyncSession, client: PolymarketClient):
        self.db = db
        self.client = client
        self.alert_service = AlertService(db)

    async def take_portfolio_snapshot(self) -> Optional[PortfolioSnapshot]:
        """
        Fetch current wallet state and store a portfolio snapshot.
        Also updates position prices and creates position snapshots.

        Flow:
          1. Sync positions from Data API (discover, share counts, auto-close).
          2. Overwrite prices with CLOB midpoints — source of truth that
             matches the Polymarket UI. Data API curPrice occasionally
             returns glitched values (Apr 20 2026: 2 positions were ~3x
             inflated for ~5 min, baking a $13.50 error into the snapshot).
          3. Compute snapshot totals from DB after the CLOB refresh.
        """
        try:
            wallet_data = await self.client.get_wallet_balance()

            cash_balance = wallet_data["usdc_balance"]
            api_positions = wallet_data["positions"]

            # Step 1: sync position state from Data API (no position_snapshots yet).
            await self._sync_positions(api_positions)

            # Step 2: overwrite prices with CLOB midpoints and create
            # per-position snapshots with the authoritative values.
            clob_refreshed = await self._refresh_prices_from_clob()

            # Step 3: compute totals from DB (CLOB-based).
            result = await self.db.execute(
                select(func.coalesce(func.sum(Position.current_value), 0))
                .where(Position.status == "open")
            )
            position_value = Decimal(str(result.scalar_one() or 0))
            total_value = cash_balance + position_value

            # Previous snapshot for daily PnL calculation
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

            await self.db.commit()
            await self.db.refresh(snapshot)

            logger.info(
                f"Portfolio snapshot created: total=${total_value:.2f}, "
                f"positions=${position_value:.2f} "
                f"({clob_refreshed} positions refreshed from CLOB)"
            )
            return snapshot

        except Exception as e:
            logger.error(f"Failed to take portfolio snapshot: {e}")
            await self.db.rollback()
            raise

    async def _refresh_prices_from_clob(self) -> int:
        """
        Overwrite open position prices with CLOB midpoints (source of truth)
        and create a PositionSnapshot per position using those CLOB values.

        Does NOT commit — caller handles the transaction.
        Returns the number of positions whose CLOB price fetch succeeded.
        """
        result = await self.db.execute(
            select(Position, Market)
            .join(Market, Position.market_id == Market.id)
            .where(Position.status == "open")
        )
        rows = result.all()

        updated = 0
        now = datetime.now(timezone.utc)
        for position, market in rows:
            try:
                # _sync_positions may have closed this position earlier in the
                # same uncommitted transaction (autoflush is off, so the query
                # above still sees it as open). Re-pricing it would undo the
                # zeroed current_value and add a phantom snapshot.
                if position.status != "open":
                    continue

                token_id = (
                    market.clob_token_id_yes if position.direction == "yes"
                    else market.clob_token_id_no
                )
                if not token_id:
                    continue

                price = await self.client.get_market_price(token_id)
                if price is None:
                    continue

                value = position.shares * price
                position.current_price = price
                position.current_value = value
                position.unrealized_pnl = value - position.cost_basis

                self.db.add(PositionSnapshot(
                    position_id=position.id,
                    timestamp=now,
                    price=price,
                    value=value,
                ))
                updated += 1
            except Exception as e:
                logger.warning(
                    f"Failed to refresh CLOB price for position {position.id}: {e}"
                )

        return updated

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
            if position.api_miss_count:
                position.api_miss_count = 0

            current_price = pos_data.get("current_price", Decimal("0"))
            value = pos_data.get("value", Decimal("0"))
            api_size = pos_data.get("size")

            # Sync share count if API reports a different size (add or sell).
            old_shares = position.shares
            old_entry_price = position.entry_price
            if api_size is not None:
                api_shares = Decimal(str(api_size))
                if api_shares != position.shares and api_shares > 0:
                    delta = api_shares - old_shares
                    position.shares = api_shares
                    logger.info(
                        f"Position {position.id} shares updated: {old_shares} -> "
                        f"{api_shares} ({'added' if delta > 0 else 'sold'} {abs(delta)})"
                    )
                    if delta < 0:
                        # Partial sell: book realized P&L for the sold slice
                        # NOW. The later close only books the remaining
                        # shares, so without this every trim / profit-take
                        # vanished from realized P&L (pm-rfz.2).
                        await self._book_partial_sell(
                            position,
                            token_id=token_id,
                            sold=-delta,
                            old_entry_price=old_entry_price,
                            midpoint=current_price,
                        )

            # Re-derive the cost basis from the Data API's avg_price, which is
            # authoritative for the currently-open shares. This self-heals the
            # historical artifact where a frozen entry_price was scaled
            # proportionally on an add (overstating cost_basis) or left stale,
            # and still handles the post-fill lag window (avg_price None/0) via
            # proportional fallback. See resolve_cost_basis() for the rules.
            api_avg_raw = pos_data.get("avg_price")
            api_avg = Decimal(str(api_avg_raw)) if api_avg_raw is not None else None
            prev_cost_basis = position.cost_basis
            new_entry_price, new_cost_basis = resolve_cost_basis(
                api_avg=api_avg,
                shares=position.shares,
                old_shares=old_shares,
                old_entry_price=old_entry_price,
                old_cost_basis=position.cost_basis,
            )
            if new_entry_price is not None:
                position.entry_price = new_entry_price
            if new_cost_basis is not None:
                position.cost_basis = new_cost_basis
            if new_cost_basis is not None and prev_cost_basis != new_cost_basis:
                logger.info(
                    f"Position {position.id} cost-basis resynced: "
                    f"{prev_cost_basis} -> {new_cost_basis} "
                    f"(entry={new_entry_price}, shares={position.shares}, "
                    f"avg_price={api_avg})"
                )

            # Update position with Data API price as an interim value; the
            # caller (take_portfolio_snapshot) overwrites these with CLOB
            # midpoints via _refresh_prices_from_clob, which also creates
            # the authoritative PositionSnapshot.
            position.current_price = current_price
            position.current_value = value
            position.unrealized_pnl = value - position.cost_basis

        # Auto-discover new positions not yet in DB
        if unknown_api_positions:
            await self._auto_discover_positions(unknown_api_positions)

        now = datetime.now(timezone.utc)
        newly_closed: list[tuple[str, str]] = []  # (market_slug, market_title)
        missing_positions = {
            token_id: (position, market)
            for token_id, (position, market) in db_positions.items()
            if token_id not in found_token_ids
        }
        # The Data API is behaving if it returned at least a couple of our
        # positions. Used to gate anything that treats absence as meaningful.
        api_healthy = len(found_token_ids) >= 2

        # Resolution detection, independent of Data API presence. Resolution
        # never removes a position from /positions — only redemption does — so
        # this must be driven by Gamma, not by absence.
        await self._ingest_resolutions(
            db_positions=db_positions,
            api_positions_by_token=api_positions_by_token,
            missing_token_ids=set(missing_positions) if api_healthy else set(),
            now=now,
        )
        newly_closed.extend(self._sweep_resolved_positions(db_positions, now))

        # SAFETY: If ALL (or nearly all) positions disappeared at once, this is
        # almost certainly an API failure, not real position closures.
        # Only proceed with closure logic if at least some positions were matched.
        skip_missing_logic = False
        if missing_positions and not found_token_ids and len(db_positions) > 1:
            logger.warning(
                f"ALL {len(db_positions)} positions missing from API response — "
                f"likely API failure. Skipping position closure logic."
            )
            skip_missing_logic = True
        elif len(missing_positions) > 2 and len(found_token_ids) < len(db_positions) * 0.3:
            logger.warning(
                f"{len(missing_positions)}/{len(db_positions)} positions missing from API "
                f"(only {len(found_token_ids)} matched). Possible API issue — "
                f"skipping closure logic to avoid mass false-closes."
            )
            skip_missing_logic = True

        if not skip_missing_logic:
            for token_id, (position, market) in missing_positions.items():
                if position.status != "open":
                    # Already booked by the resolution sweep this cycle.
                    continue

                # Position not in API - check if market has resolved
                if market.resolved_at is not None:
                    self._close_resolved_position(
                        position,
                        market,
                        now,
                        reason=(
                            "auto-closed: position absent from API and market resolved "
                            f"{market.resolution_outcome or 'outcome unknown'}"
                        ),
                    )
                    newly_closed.append((market.slug, market.title))
                elif market.end_date is not None and market.end_date <= now:
                    # End date passed but Gamma has not confirmed resolution.
                    # The position is gone from the API, so it must be booked;
                    # without a resolved outcome the last price is all we have.
                    self._close_at_last_price(
                        position,
                        now,
                        reason=(
                            "auto-closed: position absent from API and market end_date "
                            "passed, but resolution unconfirmed — booked at last price"
                        ),
                    )
                    newly_closed.append((market.slug, market.title))
                else:
                    # Position not in API and market hasn't resolved.
                    # Track consecutive misses and auto-close after threshold,
                    # but only if enough other positions were found (guards against API outage).
                    miss_count = (position.api_miss_count or 0) + 1
                    position.api_miss_count = miss_count

                    if miss_count >= AUTO_CLOSE_MISS_THRESHOLD and api_healthy:
                        # Position has been absent for 3+ consecutive sync cycles
                        # and the API is returning other positions (not an outage).
                        logger.info(
                            f"Auto-closing position {position.id} ('{market.title}'): "
                            f"absent from API for {miss_count} consecutive sync cycles. "
                            f"Likely sold externally."
                        )
                        self._close_at_last_price(
                            position,
                            now,
                            reason=(
                                f"auto-closed: position absent from API for "
                                f"{miss_count} sync cycles"
                            ),
                        )
                        newly_closed.append((market.slug, market.title))
                    else:
                        logger.warning(
                            f"Position {position.id} ('{market.title}') not found in API but market "
                            f"still active (miss {miss_count}/{AUTO_CLOSE_MISS_THRESHOLD}). "
                            f"Will auto-close after {AUTO_CLOSE_MISS_THRESHOLD} consecutive misses."
                        )

        # Clear alerts for any positions that were just closed
        for market_slug, market_title in newly_closed:
            if market_slug:
                try:
                    cleared = await self.alert_service.clear_alerts_for_closed_position(
                        market_slug, market_title
                    )
                    if cleared:
                        logger.info(
                            f"Cleared {cleared} alert(s) for closed position '{market_title}'"
                        )
                except Exception as e:
                    logger.error(
                        f"Failed to clear alerts for closed position '{market_title}': {e}",
                        exc_info=True,
                    )

    async def _resolve_sell_fill_price(
        self,
        position: Position,
        token_id: str,
        sold: Decimal,
        midpoint: Decimal,
    ) -> tuple[Decimal, str]:
        """Price a sold slice from the activity feed, else the Data API midpoint.

        Takes the most recent SELL fills whose cumulative size covers ``sold``
        (within PARTIAL_SELL_COVERAGE_TOLERANCE) and returns their
        size-weighted average price. The last fill is clipped to the remainder
        so an oversized fill (e.g. two trims settling in one cycle) does not
        skew the average. Returns ``(price, source)`` where source is
        ``"fills"`` or ``"midpoint"``.
        """
        if position.updated_at is not None:
            since_dt = position.updated_at - timedelta(minutes=15)
        else:
            since_dt = datetime.now(timezone.utc) - timedelta(hours=24)
        since_ts = int(since_dt.timestamp())

        try:
            fills = await self.client.get_recent_sell_fills(token_id, since_ts)
        except Exception as e:
            logger.warning(
                f"Position {position.id}: activity feed lookup failed ({e}); "
                f"sold slice will be booked at the midpoint"
            )
            fills = []

        covered = Decimal("0")
        notional = Decimal("0")
        for size, price in fills or []:
            remaining = sold - covered
            if remaining <= 0:
                break
            take = min(Decimal(str(size)), remaining)
            covered += take
            notional += take * Decimal(str(price))

        min_coverage = sold * (Decimal("1") - PARTIAL_SELL_COVERAGE_TOLERANCE)
        if covered > 0 and covered >= min_coverage:
            return notional / covered, "fills"

        logger.warning(
            f"Position {position.id}: activity feed covered {covered} of {sold} "
            f"sold shares (since_ts={since_ts}); booking the slice at the Data "
            f"API midpoint {midpoint} instead of a fill price"
        )
        return midpoint, "midpoint"

    async def _book_partial_sell(
        self,
        position: Position,
        token_id: str,
        sold: Decimal,
        old_entry_price: Optional[Decimal],
        midpoint: Decimal,
    ) -> Decimal:
        """Accumulate realized P&L for a partial sell into ``position.realized_pnl``.

        Avg-cost accounting: the Data API's avgPrice of the remaining shares is
        unchanged by a sell, so the cost of the sold slice is the pre-sell
        entry price. Does not touch cost_basis — resolve_cost_basis re-derives
        it for the remaining shares in the caller as before.
        """
        entry = old_entry_price if old_entry_price is not None else Decimal("0")
        fill_price, source = await self._resolve_sell_fill_price(
            position, token_id=token_id, sold=sold, midpoint=midpoint
        )
        slice_realized = sold * (fill_price - entry)
        position.realized_pnl = (position.realized_pnl or Decimal("0")) + slice_realized

        logger.info(
            f"Position {position.id} partial sell booked: sold {sold} @ "
            f"{fill_price} ({source}) vs entry {entry} -> slice realized "
            f"${slice_realized:.2f}, cumulative realized ${position.realized_pnl:.2f}"
        )
        return slice_realized

    async def _ingest_resolutions(
        self,
        db_positions: Dict[str, tuple],
        api_positions_by_token: Dict[str, Dict],
        missing_token_ids: set,
        now: datetime,
    ) -> int:
        """Populate markets.resolved_at / resolution_outcome from Gamma.

        Only markets that could plausibly have settled are checked (see
        should_check_resolution), so a portfolio of live markets costs zero
        Gamma calls per cycle. Returns the number of lookups performed.
        """
        checked = 0
        seen_markets: set = set()

        for token_id, (position, market) in db_positions.items():
            pos_data = api_positions_by_token.get(token_id) or {}
            redeemable = bool(pos_data.get("redeemable"))
            missing = token_id in missing_token_ids
            if not should_check_resolution(
                resolved_at=market.resolved_at,
                end_date=market.end_date,
                now=now,
                redeemable=redeemable,
                missing_from_api=missing,
            ):
                continue

            # Both sides of the same market share one Market row; one call does.
            market_key = market.id if market.id is not None else id(market)
            if market_key in seen_markets:
                continue
            seen_markets.add(market_key)

            last_checked = _last_resolution_check.get(market_key)
            if (
                not (redeemable or missing)
                and last_checked is not None
                and (now - last_checked).total_seconds() < RESOLUTION_RECHECK_SECONDS
            ):
                continue
            _last_resolution_check[market_key] = now

            try:
                raw = await self.client.get_market_resolution(token_id)
            except Exception as e:
                logger.warning(
                    f"Resolution lookup failed for market '{market.title}' "
                    f"(position {position.id}): {e}"
                )
                continue

            checked += 1
            state = parse_resolution(raw)
            if not state.resolved:
                logger.debug(
                    f"Market '{market.title}' not resolved yet "
                    f"(position {position.id} still open)"
                )
                continue

            market.resolved_at = state.resolved_at or now
            market.resolution_outcome = state.outcome
            if state.outcome is None:
                logger.warning(
                    f"Market '{market.title}' is resolved but the winning outcome "
                    f"could not be determined from Gamma outcomePrices — position "
                    f"will be booked at its last price."
                )
            else:
                logger.info(
                    f"Market '{market.title}' resolved {state.outcome.upper()} at "
                    f"{market.resolved_at.isoformat()} (from Gamma)"
                )

        return checked

    def _sweep_resolved_positions(
        self, db_positions: Dict[str, tuple], now: datetime
    ) -> List[tuple]:
        """Close every open position whose market has resolved.

        This is the fix for the structural bug: closure is driven by resolution
        state, not by the position disappearing from the Data API (which only
        happens on redemption, and never happens for worthless positions nobody
        bothers to redeem). Returns (market_slug, market_title) per close.
        """
        candidates = [
            (position, market)
            for position, market in db_positions.values()
            if position.status == "open" and market.resolved_at is not None
        ]
        if not candidates:
            return []

        if len(candidates) > RESOLUTION_SWEEP_MAX_CLOSES:
            logger.error(
                "Resolution sweep found %d resolved open positions (cap %d) — refusing "
                "to mass-close in one cycle. Review manually: %s",
                len(candidates),
                RESOLUTION_SWEEP_MAX_CLOSES,
                [(p.id, m.slug) for p, m in candidates],
            )
            return []

        closed = []
        for position, market in candidates:
            resolved_at = market.resolved_at.isoformat() if market.resolved_at else "unknown"
            self._close_resolved_position(
                position,
                market,
                now,
                reason=(
                    f"auto-closed: market resolved "
                    f"{market.resolution_outcome or 'outcome unknown'} at {resolved_at}"
                ),
            )
            closed.append((market.slug, market.title))
        return closed

    def _close_resolved_position(
        self, position: Position, market: Market, now: datetime, reason: str
    ) -> Decimal:
        """Book a position against its market's resolution outcome.

        With a known outcome the payout is exactly $1 or $0 per share — not the
        last CLOB midpoint, which froze the July Fed winner at 0.9995.
        """
        exit_price, realized_pnl = compute_close_booking(
            direction=position.direction,
            shares=position.shares,
            cost_basis=position.cost_basis,
            resolution_outcome=market.resolution_outcome,
            last_price=position.current_price,
        )
        self._apply_close(
            position,
            exit_date=market.resolved_at or now,
            exit_price=exit_price,
            realized_pnl=realized_pnl,
            reason=reason,
        )
        return realized_pnl

    def _close_at_last_price(
        self, position: Position, now: datetime, reason: str
    ) -> Decimal:
        """Book a position at its last known price (outcome unknown)."""
        exit_price, realized_pnl = compute_close_booking(
            direction=position.direction,
            shares=position.shares,
            cost_basis=position.cost_basis,
            resolution_outcome=None,
            last_price=position.current_price,
        )
        self._apply_close(
            position,
            exit_date=now,
            exit_price=exit_price,
            realized_pnl=realized_pnl,
            reason=reason,
        )
        return realized_pnl

    def _apply_close(
        self,
        position: Position,
        exit_date: datetime,
        exit_price: Decimal,
        realized_pnl: Decimal,
        reason: str,
    ) -> None:
        """Mark a position closed.

        ``realized_pnl`` is the close-only booking on the shares still open
        (compute_close_booking on the current cost_basis). Any realized P&L
        already accumulated by partial sells is ADDED, not overwritten — the
        overwrite is what erased every 2026 profit-take (pm-rfz.2).
        """
        prior_realized = position.realized_pnl or Decimal("0")
        position.status = "closed"
        position.exit_date = exit_date
        position.exit_price = exit_price
        position.realized_pnl = prior_realized + realized_pnl
        position.current_value = Decimal("0")
        position.unrealized_pnl = Decimal("0")
        position.exit_reasoning = reason
        position.api_miss_count = 0

        logger.info(
            f"Position {position.id} closed: exit_price={exit_price}, "
            f"close_pnl=${realized_pnl:.2f}, prior_partial=${prior_realized:.2f}, "
            f"realized_pnl=${position.realized_pnl:.2f}, "
            f"exit_date={exit_date.isoformat()} — {reason}"
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
                    log = logger.debug if token_id in _gamma_miss_warned else logger.warning
                    _gamma_miss_warned.add(token_id)
                    log(f"Auto-discover: Gamma API returned no data for token {token_id}. Skipping.")
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

                if existing_market is not None and existing_market.resolved_at is not None:
                    # A settled position keeps appearing in the Data API until
                    # it is redeemed, and worthless ones never are. Now that
                    # resolution (not absence) closes positions, re-discovering
                    # this token would re-open the position on every cycle and
                    # close it again on the next sweep.
                    logger.debug(
                        f"Auto-discover: skipping '{title}' {direction} — market resolved "
                        f"{existing_market.resolution_outcome or 'outcome unknown'} at "
                        f"{existing_market.resolved_at.isoformat()}; position already "
                        f"booked, awaiting redemption."
                    )
                    continue

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
                if avg_price == 0:
                    # Data API hasn't computed avgPrice yet (fresh fill). Track
                    # the position now; _sync_positions self-heals the cost
                    # basis once the API reports a real avg_price next cycle.
                    logger.warning(
                        f"Auto-discover: '{title}' {direction} has avg_price=0 "
                        f"(Data API lag) — inserting with cost_basis=0, will "
                        f"self-heal on next sync."
                    )
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
                "end_date": market.end_date if market else None,
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
