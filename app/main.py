import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from sqlalchemy import text

from app.config import get_settings
from app.database import engine
from app.routers import portfolio_router, positions_router, exposure_router, alerts_router, freshness_router, auth_router
from app.tasks import start_scheduler, shutdown_scheduler
from app.auth import AuthMiddleware

# Configure logging
settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle."""
    # Startup
    logger.info("Starting Polymarket Tracker service...")

    # Ensure alert tables exist (idempotent)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS alert_definitions (
                    id SERIAL PRIMARY KEY,
                    slug VARCHAR(255) NOT NULL UNIQUE,
                    market_slug VARCHAR(255),
                    market_name VARCHAR(500),
                    direction VARCHAR(10),
                    alert_type VARCHAR(50) NOT NULL,
                    threshold NUMERIC(10, 4),
                    catalyst_date TIMESTAMPTZ,
                    catalyst_description TEXT,
                    days_before INTEGER,
                    action TEXT NOT NULL,
                    severity VARCHAR(20) NOT NULL DEFAULT 'WARNING',
                    entry_price NUMERIC(10, 4),
                    is_global BOOLEAN DEFAULT FALSE,
                    enabled BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """))
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS alert_events (
                    id SERIAL PRIMARY KEY,
                    definition_id INTEGER NOT NULL REFERENCES alert_definitions(id),
                    market_slug VARCHAR(255),
                    market_name VARCHAR(500),
                    alert_type VARCHAR(50) NOT NULL,
                    severity VARCHAR(20) NOT NULL,
                    message TEXT NOT NULL,
                    action TEXT,
                    details JSONB,
                    notified BOOLEAN DEFAULT FALSE,
                    notification_error TEXT,
                    triggered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    cleared_at TIMESTAMPTZ
                )
            """))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_alert_events_definition ON alert_events(definition_id)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_alert_events_cleared ON alert_events(cleared_at)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_alert_events_triggered ON alert_events(triggered_at)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_alert_definitions_enabled ON alert_definitions(enabled)"))
        logger.info("Alert tables ensured")
    except Exception as e:
        logger.warning(f"Could not ensure alert tables (non-fatal): {e}")

    start_scheduler()
    logger.info("Service started successfully")

    yield

    # Shutdown
    logger.info("Shutting down Polymarket Tracker service...")
    shutdown_scheduler()
    logger.info("Service shutdown complete")


app = FastAPI(
    title="Polymarket Tracker",
    description="Real-time Polymarket portfolio tracking service",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Authentication middleware (must be added after CORS)
app.add_middleware(AuthMiddleware)

# Include routers
app.include_router(auth_router)  # Auth routes first (login, logout)
app.include_router(portfolio_router)
app.include_router(positions_router)
app.include_router(exposure_router)
app.include_router(alerts_router)
app.include_router(freshness_router)


@app.get("/health")
async def health_check():
    """Health check endpoint for uptime monitoring."""
    return {
        "status": "healthy",
        "service": "polymarket-tracker",
        "version": "1.0.0",
    }


# Serve dashboard static files
dashboard_path = Path(__file__).parent.parent / "dashboard"
if dashboard_path.exists():
    app.mount("/static", StaticFiles(directory=str(dashboard_path)), name="static")


@app.get("/")
async def root():
    """Serve the dashboard or return API info."""
    dashboard_file = Path(__file__).parent.parent / "dashboard" / "index.html"
    if dashboard_file.exists():
        return FileResponse(dashboard_file)
    return {
        "service": "Polymarket Tracker",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health",
    }
