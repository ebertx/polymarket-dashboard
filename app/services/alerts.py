"""
Alert evaluation service.

Evaluates alert definitions against current position/portfolio data,
handles deduplication (cooldown periods), and sends notifications via ntfy.sh.
"""

import logging
from datetime import datetime, timedelta, timezone, date
from decimal import Decimal
from typing import Optional

import aiohttp
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Position, Market, PortfolioSnapshot
from app.models.alert import AlertDefinition, AlertEvent

logger = logging.getLogger(__name__)

# Cooldown periods by severity — how long before re-alerting the same condition
COOLDOWN_HOURS = {
    "INFO": None,      # alert once, never repeat (until cleared and re-triggered)
    "WARNING": 4,
    "CRITICAL": 1,
}

# Map alert types to severity levels
ALERT_TYPE_SEVERITY = {
    "price_below": "CRITICAL",
    "price_above": "WARNING",
    "drawdown": "WARNING",
    "catalyst": "INFO",
    "resolution_approaching": "INFO",
    "drawdown_any": "WARNING",
    "portfolio_drawdown": "CRITICAL",
    "deployment_high": "WARNING",
}


class AlertService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.settings = get_settings()

    async def evaluate_all_alerts(self) -> list[AlertEvent]:
        """
        Main entry point: evaluate all enabled alert definitions against
        current positions and portfolio state. Returns list of newly triggered events.
        """
        if not self.settings.alerts_enabled:
            return []

        now = datetime.now(timezone.utc)
        today = now.date()

        # Load all enabled alert definitions
        result = await self.db.execute(
            select(AlertDefinition).where(AlertDefinition.enabled == True)  # noqa: E712
        )
        definitions = list(result.scalars().all())

        if not definitions:
            logger.debug("No alert definitions found")
            return []

        # Load current positions with market info
        result = await self.db.execute(
            select(Position, Market)
            .join(Market, Position.market_id == Market.id)
            .where(Position.status == "open")
        )
        position_rows = result.all()

        # Build slug -> (position, market) lookup
        slug_map: dict[str, tuple[Position, Market]] = {}
        for position, market in position_rows:
            if market.slug:
                slug_map[market.slug] = (position, market)

        # Safety net: find closed-position slugs so we can auto-disable stale alerts
        result = await self.db.execute(
            select(Market.slug)
            .join(Position, Position.market_id == Market.id)
            .where(Position.status == "closed")
            .where(Market.slug.isnot(None))
        )
        closed_slugs: set[str] = {row[0] for row in result.all()}

        # Load portfolio summary
        portfolio = await self._get_portfolio_summary()

        # Evaluate each definition
        new_events: list[AlertEvent] = []

        for defn in definitions:
            # Safety net: if this alert is for a closed position, auto-disable it
            if not defn.is_global and defn.market_slug and defn.market_slug in closed_slugs:
                if defn.market_slug not in slug_map:
                    # Position is closed and no open position exists for this slug
                    logger.info(
                        f"Auto-disabling alert '{defn.slug}' — position for "
                        f"'{defn.market_name or defn.market_slug}' is closed"
                    )
                    await self._disable_alerts_for_slug(defn.market_slug, now)
                    continue

            triggered = self._check_definition(defn, slug_map, portfolio, position_rows, today)
            if not triggered:
                continue

            # Check deduplication / cooldown
            should_alert = await self._should_alert(defn, now)
            if not should_alert:
                logger.debug(f"Alert {defn.slug} triggered but in cooldown")
                continue

            # Create event
            event = AlertEvent(
                definition_id=defn.id,
                market_slug=defn.market_slug,
                market_name=defn.market_name,
                alert_type=defn.alert_type,
                severity=defn.severity,
                message=triggered["message"],
                action=defn.action,
                details=triggered.get("details"),
                triggered_at=now,
            )
            self.db.add(event)
            new_events.append(event)

        if new_events:
            await self.db.commit()
            # Refresh to get IDs
            for event in new_events:
                await self.db.refresh(event)

            # Send notifications
            await self._send_notifications(new_events)

        return new_events

    def _check_definition(
        self,
        defn: AlertDefinition,
        slug_map: dict[str, tuple[Position, Market]],
        portfolio: dict,
        all_positions: list,
        today: date,
    ) -> Optional[dict]:
        """
        Check if a single alert definition is triggered.
        Returns dict with 'message' and optional 'details' if triggered, else None.
        """
        alert_type = defn.alert_type

        # ----- Position-level alerts -----
        if not defn.is_global and defn.market_slug:
            pos_data = slug_map.get(defn.market_slug)
            if pos_data is None:
                # Position not found — can only evaluate time-based alerts
                if alert_type == "catalyst" and defn.catalyst_date:
                    return self._check_catalyst(defn, today)
                return None

            position, market = pos_data
            return self._check_position_alert(defn, position, market, today)

        # ----- Global alerts -----
        if defn.is_global:
            return self._check_global_alert(defn, all_positions, portfolio, today)

        return None

    def _check_position_alert(
        self,
        defn: AlertDefinition,
        position: Position,
        market: Market,
        today: date,
    ) -> Optional[dict]:
        """Evaluate a position-level alert."""
        alert_type = defn.alert_type
        current_price = float(position.current_price or 0)

        if alert_type == "drawdown":
            entry = float(defn.entry_price or position.entry_price or 0)
            if entry and current_price:
                dd_pct = (1 - current_price / entry) * 100
                threshold = float(defn.threshold or 0)
                if dd_pct >= threshold:
                    return {
                        "message": f"-{dd_pct:.0f}% from entry ({entry*100:.0f}c -> {current_price*100:.0f}c)",
                        "details": {"drawdown_pct": round(dd_pct, 1), "entry": entry, "current": current_price},
                    }

        elif alert_type == "price_below":
            threshold = float(defn.threshold or 0)
            if current_price and current_price < threshold:
                return {
                    "message": f"Price {current_price*100:.1f}c below {threshold*100:.0f}c",
                    "details": {"current": current_price, "threshold": threshold},
                }

        elif alert_type == "price_above":
            threshold = float(defn.threshold or 0)
            if current_price and current_price > threshold:
                return {
                    "message": f"Price {current_price*100:.1f}c above {threshold*100:.0f}c",
                    "details": {"current": current_price, "threshold": threshold},
                }

        elif alert_type == "catalyst":
            return self._check_catalyst(defn, today)

        elif alert_type == "resolution_approaching":
            end_date = None
            if market.end_date:
                end_date = market.end_date.date() if isinstance(market.end_date, datetime) else market.end_date
            if end_date:
                days_until = (end_date - today).days
                days_threshold = defn.days_before or 7
                if 0 <= days_until <= days_threshold:
                    return {
                        "message": f"Resolves in {days_until} day(s) ({end_date})",
                        "details": {"days_until": days_until, "end_date": str(end_date)},
                    }

        return None

    def _check_catalyst(self, defn: AlertDefinition, today: date) -> Optional[dict]:
        """Check catalyst-type alert."""
        if not defn.catalyst_date:
            return None
        cat_date = defn.catalyst_date.date() if isinstance(defn.catalyst_date, datetime) else defn.catalyst_date
        days_before = defn.days_before or 2
        days_until = (cat_date - today).days
        if 0 <= days_until <= days_before:
            desc = defn.catalyst_description or "Catalyst"
            return {
                "message": f"{desc} in {days_until} day(s) ({cat_date})",
                "details": {"days_until": days_until, "catalyst_date": str(cat_date)},
            }
        return None

    def _check_global_alert(
        self,
        defn: AlertDefinition,
        all_positions: list,
        portfolio: dict,
        today: date,
    ) -> Optional[dict]:
        """Evaluate a global (portfolio-level) alert."""
        alert_type = defn.alert_type

        if alert_type == "drawdown_any":
            threshold = float(defn.threshold or 30)
            worst = None
            worst_dd = 0
            for position, market in all_positions:
                entry = float(position.entry_price or 0)
                current = float(position.current_price or 0)
                if entry and current:
                    dd = (1 - current / entry) * 100
                    if dd >= threshold and dd > worst_dd:
                        worst_dd = dd
                        worst = (position, market)
            if worst:
                pos, mkt = worst
                return {
                    "message": f"{mkt.title}: -{worst_dd:.0f}% drawdown (threshold {threshold:.0f}%)",
                    "details": {"market": mkt.title, "drawdown_pct": round(worst_dd, 1)},
                }

        elif alert_type == "portfolio_drawdown":
            if portfolio.get("suspect"):
                return None  # Don't alert on bad snapshot data
            starting = self.settings.starting_capital
            total = portfolio.get("total_value", 0)
            if total and starting:
                dd = (1 - total / starting) * 100
                threshold = float(defn.threshold or 10)
                if dd >= threshold:
                    return {
                        "message": f"Portfolio at ${total:.2f}, -{dd:.1f}% from starting capital (${starting:.2f})",
                        "details": {"total_value": total, "drawdown_pct": round(dd, 1)},
                    }

        elif alert_type == "deployment_high":
            if portfolio.get("suspect"):
                return None  # Don't alert on bad snapshot data
            total = portfolio.get("total_value", 0)
            pos_value = portfolio.get("position_value", 0)
            if total > 0:
                deployed = pos_value / total * 100
                threshold = float(defn.threshold or 70)
                if deployed >= threshold:
                    return {
                        "message": f"{deployed:.1f}% deployed (threshold {threshold:.0f}%)",
                        "details": {"deployed_pct": round(deployed, 1)},
                    }

        return None

    async def _get_portfolio_summary(self) -> dict:
        """Get latest portfolio summary from DB."""
        result = await self.db.execute(
            select(PortfolioSnapshot)
            .order_by(PortfolioSnapshot.timestamp.desc())
            .limit(1)
        )
        snap = result.scalar_one_or_none()

        # Also get calculated position value
        result = await self.db.execute(
            select(func.coalesce(func.sum(Position.current_value), 0))
            .where(Position.status == "open")
        )
        pos_value = float(result.scalar() or 0)

        if not snap:
            return {"cash": 0, "position_value": pos_value, "total_value": pos_value}

        cash = float(snap.cash_balance or 0)
        pv = pos_value if float(snap.position_value or 0) == 0 else float(snap.position_value)

        # Sanity check: if cash is 0 but positions exist, the snapshot is likely bad
        # (failed to fetch cash balance). Skip alerting on bad data.
        if cash == 0 and pv > 0:
            logger.warning(f"Suspect snapshot: cash=0, positions=${pv:.2f}. Using position value only.")
            return {"cash": 0, "position_value": pv, "total_value": pv, "suspect": True}

        return {"cash": cash, "position_value": pv, "total_value": cash + pv}

    async def _should_alert(self, defn: AlertDefinition, now: datetime) -> bool:
        """
        Check deduplication: should we fire this alert?
        Returns True if:
        - No previous event for this definition, OR
        - Previous event was cleared (cleared_at is set) and re-triggered, OR
        - Previous event was acknowledged and 24h have passed (re-alert if condition persists), OR
        - Cooldown period has elapsed since last notification
        """
        # Get last event for this definition
        result = await self.db.execute(
            select(AlertEvent)
            .where(AlertEvent.definition_id == defn.id)
            .order_by(AlertEvent.triggered_at.desc())
            .limit(1)
        )
        last_event = result.scalar_one_or_none()

        if last_event is None:
            return True

        # If the last event was cleared, allow re-triggering
        if last_event.cleared_at is not None:
            return True

        # If the user acknowledged the alert, silence for 24 hours
        if last_event.acknowledged_at is not None:
            ack_time = last_event.acknowledged_at.replace(tzinfo=timezone.utc) if last_event.acknowledged_at.tzinfo is None else last_event.acknowledged_at
            if now - ack_time < timedelta(hours=24):
                logger.debug(f"Alert {defn.slug} silenced (acknowledged {last_event.acknowledged_at})")
                return False
            # 24h passed since acknowledgment and condition still holds — re-alert
            return True

        # Check cooldown
        cooldown_hours = COOLDOWN_HOURS.get(defn.severity)
        if cooldown_hours is None:
            # INFO severity: never repeat until cleared
            return False

        cooldown_delta = timedelta(hours=cooldown_hours)
        if now - last_event.triggered_at.replace(tzinfo=timezone.utc) >= cooldown_delta:
            return True

        return False

    async def _send_notifications(self, events: list[AlertEvent]) -> None:
        """Send notifications for triggered alerts via ntfy.sh."""
        if not self.settings.ntfy_topic:
            logger.debug("No ntfy_topic configured, skipping notifications")
            # Still mark as notified (no notification channel configured)
            for event in events:
                event.notified = True
            await self.db.commit()
            return

        ntfy_url = f"{self.settings.ntfy_server}/{self.settings.ntfy_topic}"

        async with aiohttp.ClientSession() as session:
            for event in events:
                try:
                    # Build notification
                    severity_emoji = {
                        "CRITICAL": "\U0001f6a8",  # siren
                        "WARNING": "\u26a0\ufe0f",  # warning
                        "INFO": "\u2139\ufe0f",     # info
                    }
                    emoji = severity_emoji.get(event.severity, "")
                    title = f"{emoji} [{event.severity}] {event.market_name or 'Portfolio'}"
                    body = f"{event.message}\n\nAction: {event.action}"

                    # Map severity to ntfy priority
                    priority_map = {
                        "CRITICAL": "urgent",
                        "WARNING": "high",
                        "INFO": "default",
                    }
                    priority = priority_map.get(event.severity, "default")

                    # Map severity to ntfy tags
                    tags_map = {
                        "CRITICAL": "rotating_light,chart_with_downwards_trend",
                        "WARNING": "warning,eyes",
                        "INFO": "information_source",
                    }

                    # Build action buttons for ntfy
                    # Use 'view' instead of 'http' for iOS compatibility
                    dashboard_url = self.settings.dashboard_url.rstrip("/")
                    actions = (
                        f"view, Acknowledge, {dashboard_url}/alerts/acknowledge/{event.id}, clear=true; "
                        f"view, Dashboard, {dashboard_url}"
                    )

                    headers = {
                        "Title": title,
                        "Priority": priority,
                        "Tags": tags_map.get(event.severity, ""),
                        "Actions": actions,
                    }

                    async with session.post(ntfy_url, data=body, headers=headers) as resp:
                        if resp.status == 200:
                            event.notified = True
                            logger.info(f"Notification sent for alert: {event.message}")
                        else:
                            resp_text = await resp.text()
                            event.notification_error = f"HTTP {resp.status}: {resp_text[:200]}"
                            logger.warning(f"Notification failed: {event.notification_error}")

                except Exception as e:
                    event.notification_error = str(e)[:500]
                    logger.error(f"Notification error: {e}")

        await self.db.commit()

    async def clear_alerts_for_closed_position(
        self, market_slug: str, market_name: str
    ) -> int:
        """
        Disable all alert definitions for a closed position and create a final
        AlertEvent recording the automatic clearance.  Returns the number of
        definitions disabled.

        Called from TrackerService._sync_positions when a position transitions
        to 'closed'.
        """
        now = datetime.now(timezone.utc)
        count = await self._disable_alerts_for_slug(market_slug, now)

        if count > 0:
            # Send a single ntfy notification about the clearance
            await self._send_position_closed_notification(market_name or market_slug)

        return count

    async def _disable_alerts_for_slug(
        self, market_slug: str, now: datetime
    ) -> int:
        """
        Disable all enabled AlertDefinitions for a given market_slug.
        Creates a final AlertEvent for each, and clears any open events.
        Returns the count of definitions disabled.
        """
        # Find enabled definitions for this slug
        result = await self.db.execute(
            select(AlertDefinition).where(
                AlertDefinition.market_slug == market_slug,
                AlertDefinition.enabled == True,  # noqa: E712
            )
        )
        definitions = list(result.scalars().all())

        if not definitions:
            return 0

        for defn in definitions:
            defn.enabled = False

            # Create a final informational event
            event = AlertEvent(
                definition_id=defn.id,
                market_slug=defn.market_slug,
                market_name=defn.market_name,
                alert_type=defn.alert_type,
                severity="INFO",
                message="Position closed — alerts cleared automatically",
                action="No action required",
                triggered_at=now,
                cleared_at=now,  # immediately cleared
                notified=True,   # will send a consolidated notification separately
            )
            self.db.add(event)

        # Also clear any open (uncleared) events for this slug
        result = await self.db.execute(
            select(AlertEvent).where(
                AlertEvent.market_slug == market_slug,
                AlertEvent.cleared_at.is_(None),
            )
        )
        open_events = list(result.scalars().all())
        for evt in open_events:
            evt.cleared_at = now

        await self.db.commit()

        logger.info(
            f"Cleared {len(definitions)} alert definition(s) for closed position "
            f"'{market_slug}' (+ {len(open_events)} open event(s) cleared)"
        )
        return len(definitions)

    async def _send_position_closed_notification(self, market_name: str) -> None:
        """Send a single ntfy notification that a position's alerts were cleared."""
        if not self.settings.ntfy_topic:
            return

        ntfy_url = f"{self.settings.ntfy_server}/{self.settings.ntfy_topic}"
        title = "\u2705 Position closed"
        body = f"{market_name} \u2014 alerts cleared automatically"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    ntfy_url,
                    data=body,
                    headers={
                        "Title": title,
                        "Priority": "default",
                        "Tags": "white_check_mark",
                    },
                ) as resp:
                    if resp.status == 200:
                        logger.info(f"Sent position-closed notification for '{market_name}'")
                    else:
                        resp_text = await resp.text()
                        logger.warning(
                            f"Position-closed notification failed: HTTP {resp.status}: {resp_text[:200]}"
                        )
        except Exception as e:
            logger.error(f"Position-closed notification error: {e}")

    async def clear_stale_alerts(self) -> int:
        """
        Mark alerts as cleared if the condition is no longer true.
        Called periodically to allow re-triggering after condition clears.
        Returns count of cleared alerts.
        """
        now = datetime.now(timezone.utc)
        today = now.date()

        # Get all open (uncleared) events
        result = await self.db.execute(
            select(AlertEvent)
            .where(AlertEvent.cleared_at.is_(None))
            .order_by(AlertEvent.triggered_at.desc())
        )
        open_events = list(result.scalars().all())

        if not open_events:
            return 0

        # Load current positions
        result = await self.db.execute(
            select(Position, Market)
            .join(Market, Position.market_id == Market.id)
            .where(Position.status == "open")
        )
        position_rows = result.all()
        slug_map: dict[str, tuple[Position, Market]] = {}
        for position, market in position_rows:
            if market.slug:
                slug_map[market.slug] = (position, market)

        portfolio = await self._get_portfolio_summary()

        cleared_count = 0
        for event in open_events:
            # Load the definition
            result = await self.db.execute(
                select(AlertDefinition).where(AlertDefinition.id == event.definition_id)
            )
            defn = result.scalar_one_or_none()
            if defn is None:
                event.cleared_at = now
                cleared_count += 1
                continue

            # Re-check the condition
            triggered = self._check_definition(defn, slug_map, portfolio, position_rows, today)
            if triggered is None:
                # Condition no longer holds
                event.cleared_at = now
                cleared_count += 1

        if cleared_count > 0:
            await self.db.commit()
            logger.info(f"Cleared {cleared_count} stale alert events")

        return cleared_count

    async def get_active_alerts(self) -> list[dict]:
        """Get currently active (uncleared) alert events."""
        result = await self.db.execute(
            select(AlertEvent)
            .where(AlertEvent.cleared_at.is_(None))
            .order_by(AlertEvent.triggered_at.desc())
        )
        events = result.scalars().all()
        return [self._event_to_dict(e) for e in events]

    async def get_alert_history(self, limit: int = 50) -> list[dict]:
        """Get recent alert event history."""
        result = await self.db.execute(
            select(AlertEvent)
            .order_by(AlertEvent.triggered_at.desc())
            .limit(limit)
        )
        events = result.scalars().all()
        return [self._event_to_dict(e) for e in events]

    @staticmethod
    def _event_to_dict(event: AlertEvent) -> dict:
        return {
            "id": event.id,
            "definition_id": event.definition_id,
            "market_slug": event.market_slug,
            "market_name": event.market_name,
            "alert_type": event.alert_type,
            "severity": event.severity,
            "message": event.message,
            "action": event.action,
            "details": event.details,
            "notified": event.notified,
            "triggered_at": event.triggered_at.isoformat() if event.triggered_at else None,
            "cleared_at": event.cleared_at.isoformat() if event.cleared_at else None,
            "acknowledged_at": event.acknowledged_at.isoformat() if event.acknowledged_at else None,
            "is_active": event.cleared_at is None,
        }
