import logging
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.services.polymarket import PolymarketClient
from app.services.tracker import TrackerService

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


def shutdown_scheduler():
    """Gracefully shutdown the scheduler."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler shutdown complete")
