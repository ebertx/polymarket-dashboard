import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.services.polymarket import PolymarketClient
from app.services.tracker import TrackerService
from app.services.alerts import AlertService

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def poll_portfolio():
    """
    Scheduled task to poll Polymarket API and take a portfolio snapshot.
    """
    settings = get_settings()
    logger.info("Starting scheduled portfolio poll...")

    async with AsyncSessionLocal() as db:
        client = PolymarketClient(settings.polymarket_wallet)
        tracker = TrackerService(db, client)

        try:
            # Take portfolio snapshot
            snapshot = await tracker.take_portfolio_snapshot()
            if snapshot:
                logger.info(
                    f"Snapshot complete: total=${snapshot.total_value:.2f}"
                )

            # Update position prices
            updated = await tracker.update_position_prices()
            logger.info(f"Updated prices for {updated} positions")

        except Exception as e:
            logger.error(f"Portfolio poll failed: {e}", exc_info=True)
        finally:
            await client.close()

    # Run alert evaluation in a separate session (after prices are committed)
    if settings.alerts_enabled:
        await run_alert_check()


async def run_alert_check():
    """
    Evaluate alert definitions against current position/portfolio state.
    Sends notifications for newly triggered alerts via ntfy.sh.
    """
    logger.debug("Starting alert check...")

    async with AsyncSessionLocal() as db:
        alert_service = AlertService(db)
        try:
            # First, clear alerts whose conditions no longer hold
            cleared = await alert_service.clear_stale_alerts()
            if cleared:
                logger.info(f"Cleared {cleared} stale alerts")

            # Evaluate all alert definitions
            new_events = await alert_service.evaluate_all_alerts()
            if new_events:
                for event in new_events:
                    logger.info(
                        f"Alert triggered: [{event.severity}] "
                        f"{event.market_name or 'Portfolio'}: {event.message}"
                    )
                logger.info(f"Total new alerts: {len(new_events)}")
            else:
                logger.debug("No new alerts triggered")

        except Exception as e:
            logger.error(f"Alert check failed: {e}", exc_info=True)


def start_scheduler():
    """Start the APScheduler with the polling job."""
    settings = get_settings()
    interval = settings.poll_interval_seconds

    scheduler.add_job(
        poll_portfolio,
        trigger=IntervalTrigger(seconds=interval),
        id="portfolio_poll",
        name="Poll Polymarket portfolio",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    logger.info(f"Scheduler started with {interval}s polling interval")
    if settings.alerts_enabled:
        logger.info(
            f"Alerts enabled. ntfy topic: "
            f"{'(not configured)' if not settings.ntfy_topic else settings.ntfy_topic}"
        )
    else:
        logger.info("Alerts disabled (ALERTS_ENABLED=false)")


def shutdown_scheduler():
    """Gracefully shutdown the scheduler."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler shutdown complete")
