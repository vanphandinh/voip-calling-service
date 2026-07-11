"""FastAPI application entry point for the VoIP Calling Service."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import AppConfig
from .routes import router

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

config = AppConfig.from_env()

logging.basicConfig(
    level=getattr(logging, config.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("wcs")


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    errors = config.validate()
    if errors:
        for e in errors:
            logger.error("Config error: %s", e)
        raise RuntimeError(f"Invalid configuration: {'; '.join(errors)}")

    logger.info(
        "Starting WCS — SIP: %s@%s, TTS: %s",
        config.sip.username,
        config.sip.domain,
        config.tts.engine,
    )

    # Store config in app state for route access
    app.state.config = config

    yield

    logger.info("Shutting down WCS")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    """Build and return the FastAPI application instance."""
    app = FastAPI(
        title="VoIP Announcement Calling Service",
        description=(
            "REST API for making automated announcement calls via SIP. "
            "Converts Vietnamese text to speech and delivers it as a phone call "
            "to Linphone mobile users."
        ),
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS — allow external apps to call this service
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router)

    return app


app = create_app()
