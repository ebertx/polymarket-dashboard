import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import portfolio_router, positions_router, exposure_router
from app.tasks import start_scheduler, shutdown_scheduler

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

# Include routers
app.include_router(portfolio_router)
app.include_router(positions_router)
app.include_router(exposure_router)


@app.get("/health")
async def health_check():
    """Health check endpoint for uptime monitoring."""
    return {
        "status": "healthy",
        "service": "polymarket-tracker",
        "version": "1.0.0",
    }


@app.get("/")
async def root():
    """Root endpoint with API info."""
    return {
        "service": "Polymarket Tracker",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health",
    }
