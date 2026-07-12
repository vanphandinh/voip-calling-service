"""FastAPI application entry point for the VoIP Calling Service."""

import base64
import hashlib
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .call_manager import CallManager
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

_manager: Optional[CallManager] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    global _manager

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

    # Initialize CallManager
    _manager = CallManager(config)
    app.state.call_manager = _manager
    logger.info("CallManager initialized")

    # Attempt SIP registration (non-fatal on failure)
    try:
        _manager.connect_sip()
        logger.info("SIP registration OK")
    except Exception as exc:
        logger.warning(
            "Initial SIP registration failed: %s. "
            "Will retry on first call request.",
            exc,
        )

    yield

    logger.info("Shutting down WCS")
    if _manager:
        _manager.shutdown()


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
        swagger_ui_parameters={"persistAuthorization": True},
    )

    # CORS — allow external apps to call this service
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Auth middleware — verifies HMAC-signed Bearer tokens
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        # Public paths — no token required
        public_paths = {
            "/docs",
            "/redoc",
            "/openapi.json",
            "/api/v1/health",
            "/api/v1/auth/token",
        }
        if request.url.path in public_paths:
            return await call_next(request)

        # If SECRET_KEY is not configured, skip auth entirely (backward compatible)
        if not config.secret_key:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing Bearer token"},
            )

        token = auth_header[7:]
        parts = token.split(".", 1)
        if len(parts) != 2:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid token format"},
            )

        payload_b64, signature = parts
        expected_sig = hmac.new(
            config.secret_key.encode(),
            payload_b64.encode(),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(signature, expected_sig):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid token signature"},
            )

        # Decode payload and check expiry
        try:
            payload_json = base64.urlsafe_b64decode(
                payload_b64 + "=" * ((4 - len(payload_b64) % 4) % 4)
            ).decode()
            payload = json.loads(payload_json)
            if time.time() > payload.get("exp", 0):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Token expired"},
                )
            if payload.get("iat", float("inf")) > time.time():
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Token not yet valid"},
                )
        except Exception:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid token payload"},
            )

        return await call_next(request)

    app.include_router(router)

    # Inject Bearer auth into OpenAPI so Swagger UI shows the Authorize button.
    # Security is applied per-operation: public endpoints get an empty list,
    # all others get the bearerAuth requirement.
    PUBLIC_PATHS = {
        "/api/v1/health",
        "/api/v1/auth/token",
    }

    def _custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = _original_openapi()
        schema.setdefault("components", {}).setdefault("securitySchemes", {})[
            "bearerAuth"
        ] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "HMAC-SHA256 token",
            "description": (
                "Paste your access token here. "
                "Get one via **POST /api/v1/auth/token** with your SECRET_KEY."
            ),
        }
        # Apply security per-path: public = no lock, others = lock
        for path, methods in schema.get("paths", {}).items():
            # /docs, /redoc, /openapi.json don't appear in paths — they're fine
            if path in PUBLIC_PATHS:
                for op in methods.values():
                    op["security"] = []  # explicit "no auth required"
            else:
                for op in methods.values():
                    op["security"] = [{"bearerAuth": []}]
        app.openapi_schema = schema
        return schema

    _original_openapi = app.openapi
    app.openapi = _custom_openapi

    return app


app = create_app()
