"""API route handlers for the VoIP Calling Service."""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from dataclasses import replace as dataclass_replace

from fastapi import APIRouter, HTTPException, Query, Request

from .call_manager import CallManager
from .models import (
    CallListResponse,
    CallRequest,
    CallResponse,
    CallStatusResponse,
    HealthResponse,
    TokenRequest,
    TokenResponse,
    TtsCacheCleanupResponse,
    TtsCacheStatsResponse,
    TtsConfigResponse,
    TtsConfigUpdate,
)

logger = logging.getLogger("wcs.routes")

router = APIRouter(prefix="/api/v1", tags=["calls"])

# ---------------------------------------------------------------------------
# Rate limiting — sliding window per source IP
# ---------------------------------------------------------------------------


class RateLimiter:
    """Thread-safe sliding-window rate limiter.

    Records the timestamp of every request BEFORE deciding, so the very
    first request is counted (the old implementation lost the first
    timestamp and therefore never blocked anything).

    The key store is bounded: when it grows beyond ``max_keys``, stale
    entries (no events within the window) are purged.
    """

    def __init__(self, max_events: int, window_seconds: float, max_keys: int = 10_000):
        self.max_events = max_events
        self.window = window_seconds
        self.max_keys = max_keys
        self._buckets: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """Record an event for *key* and return True if it is within quota."""
        now = time.monotonic()
        window_start = now - self.window
        with self._lock:
            if len(self._buckets) > self.max_keys:
                stale = [
                    k for k, ts in self._buckets.items()
                    if not ts or ts[-1] < window_start
                ]
                for k in stale:
                    del self._buckets[k]
            bucket = [t for t in self._buckets.get(key, ()) if t >= window_start]
            bucket.append(now)
            self._buckets[key] = bucket
            return len(bucket) <= self.max_events

    def current_usage(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            return sum(1 for t in self._buckets.get(key, ()) if t >= now - self.window)


# Max calls per IP per window
CALL_RATE_LIMITER = RateLimiter(max_events=10, window_seconds=1.0)
# Stricter limit for the token endpoint to slow down online brute-force
TOKEN_RATE_LIMITER = RateLimiter(max_events=5, window_seconds=60.0)

# Token expiry (seconds)
TOKEN_EXPIRY_SECONDS = 86400  # 24 hours


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _check_rate_limit(request: Request) -> bool:
    """Enforce per-IP rate limit for call submissions."""
    allowed = CALL_RATE_LIMITER.allow(_client_ip(request))
    if not allowed:
        logger.warning("Rate limit exceeded for IP %s", _client_ip(request))
    return allowed


# ---------------------------------------------------------------------------
# Manager singleton
# ---------------------------------------------------------------------------

# CallManager singleton — created in main.py lifespan
_manager: CallManager | None = None


def _get_manager(request: Request) -> CallManager:
    """Return the CallManager singleton (set during app lifespan)."""
    global _manager
    if _manager is None:
        _manager = request.app.state.call_manager
    return _manager


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@router.get("/health", response_model=HealthResponse)
async def health_check(request: Request):
    """Service health check including SIP registration status."""
    mgr = _get_manager(request)
    return HealthResponse(
        status="ok",
        sip_registered=mgr.is_sip_registered if mgr else False,
        tts_engine=request.app.state.config.tts.engine,
        active_calls=mgr.active_call_count if mgr else 0,
    )


# ---------------------------------------------------------------------------
# Call endpoints
# ---------------------------------------------------------------------------

@router.post("/call", response_model=CallResponse, status_code=202)
async def trigger_call(request: Request, body: CallRequest):
    """Trigger an announcement call.

    Converts the message to Vietnamese speech and places a SIP call
    to the target user. The call runs asynchronously — use the
    returned `call_id` to poll for status.
    """
    # Rate limiting
    if not _check_rate_limit(request):
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: max {RATE_LIMIT_MAX} calls per "
            f"{RATE_LIMIT_WINDOW}s per IP",
        )

    mgr = _get_manager(request)

    # Validate target format
    if not body.target.startswith("sip:"):
        raise HTTPException(
            status_code=422,
            detail="target must start with 'sip:' (e.g. 'sip:user@domain')",
        )

    return mgr.submit_call(body)


@router.get("/call/{call_id}", response_model=CallStatusResponse)
async def get_call_status(call_id: str, request: Request):
    """Get the current status and details of a call."""
    mgr = _get_manager(request)
    result = mgr.get_call(call_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Call '{call_id}' not found")
    return result


@router.get("/calls", response_model=CallListResponse)
async def list_calls(
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
):
    """List recent calls, newest first."""
    mgr = _get_manager(request)
    calls, total = mgr.list_calls(offset=offset, limit=limit)
    return CallListResponse(
        calls=calls,
        total=total,
        offset=offset,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# TTS configuration
# ---------------------------------------------------------------------------

@router.get("/tts/config", response_model=TtsConfigResponse)
async def get_tts_config(request: Request):
    """Get the current TTS configuration and available voices."""
    tts = request.app.state.config.tts
    return _tts_config_response(tts)


def _tts_config_response(tts) -> TtsConfigResponse:
    """Build the TTS config response from a :class:`TtsConfig`."""
    return TtsConfigResponse(
        engine=tts.engine,
        zalo_speaker_id=tts.zalo_speaker_id,
        zalo_speed=tts.zalo_speed,
        rv_gender=tts.rv_gender,
        rv_rate=tts.rv_rate,
        rv_pitch=tts.rv_pitch,
        rv_configured=bool(tts.rv_api_key and tts.rv_site_id),
        valtec_voice=tts.valtec_voice,
        valtec_speed=tts.valtec_speed,
        ttsfree_voice=tts.ttsfree_voice,
        ttsfree_speed=tts.ttsfree_speed,
        ttsfree_pitch=tts.ttsfree_pitch,
    )


@router.put("/tts/config", response_model=TtsConfigResponse)
async def update_tts_config(request: Request, body: TtsConfigUpdate):
    """Update TTS engine, speaker, or speed at runtime.

    All fields are optional — only the provided fields are changed.
    The change takes effect immediately on the next call.
    """
    tts = request.app.state.config.tts
    mgr = _get_manager(request)

    # Build a NEW TtsConfig instead of mutating the shared one in place.
    # (Mutating first and comparing later made TTSService.update_config's
    # change-detection dead code — old values always equalled new values.)
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    new_tts = dataclass_replace(tts, **changes) if changes else tts

    # Validate the merged config
    old_tts = request.app.state.config.tts
    request.app.state.config.tts = new_tts
    errors = request.app.state.config.validate()
    if errors:
        request.app.state.config.tts = old_tts  # revert on failure
        raise HTTPException(status_code=422, detail="; ".join(errors))

    # Push the (distinct) new config object to CallManager → TTSService
    mgr.update_tts_config(new_tts)

    return _tts_config_response(new_tts)


# ---------------------------------------------------------------------------
# TTS cache management
# ---------------------------------------------------------------------------

@router.delete("/tts/cache", response_model=TtsCacheCleanupResponse)
async def clear_tts_cache(request: Request):
    """Delete TTS cache files older than the configured max age.

    Cache files are identified by their last modification time.
    The cutoff age is defined by ``TTS_CACHE_MAX_AGE_DAYS``
    (default: 30 days).  Files that are still in active use have
    their mtime refreshed on each cache hit, so they survive cleanup.

    This is an on-demand operation — no automatic cleanup runs.
    Call this endpoint periodically to keep the cache from growing
    without bound.
    """
    mgr = _get_manager(request)
    max_age_days = request.app.state.config.tts.tts_cache_max_age_days

    deleted, freed = await asyncio.to_thread(
        mgr.cleanup_tts_cache, max_age_days
    )

    return TtsCacheCleanupResponse(
        deleted_files=deleted,
        freed_bytes=freed,
        freed_mb=round(freed / 1048576, 2),
        max_age_days=max_age_days,
    )


@router.get("/tts/cache", response_model=TtsCacheStatsResponse)
async def get_tts_cache_stats(request: Request):
    """Return current TTS cache statistics without deleting anything."""
    mgr = _get_manager(request)

    stats = await asyncio.to_thread(mgr.get_tts_cache_stats)

    return TtsCacheStatsResponse(
        total_files=stats["total_files"],
        total_size_bytes=stats["total_size_bytes"],
        total_size_mb=round(stats["total_size_bytes"] / 1048576, 2),
        cache_dir=stats["cache_dir"],
    )


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


@router.post("/auth/token", response_model=TokenResponse)
async def create_token(request: Request, body: TokenRequest):
    """Exchange master secret key for a signed HMAC access token.

    The token is valid for 24 hours and is verified on all other
    endpoints via the ``Authorization: Bearer <token>`` header.
    Rate limited per source IP to slow down online brute-force attempts.
    """
    if not TOKEN_RATE_LIMITER.allow(_client_ip(request)):
        logger.warning("Token rate limit exceeded for IP %s", _client_ip(request))
        raise HTTPException(
            status_code=429,
            detail="Too many token requests — retry later",
        )

    config = request.app.state.config
    if not config.secret_key:
        raise HTTPException(
            status_code=503,
            detail="Token endpoint disabled — SECRET_KEY is not configured",
        )
    if not hmac.compare_digest(body.secret_key.encode(), config.secret_key.encode()):
        raise HTTPException(status_code=401, detail="Invalid secret key")

    now = int(time.time())
    payload = {
        "iat": now,
        "exp": now + TOKEN_EXPIRY_SECONDS,
        "jti": hashlib.sha256(
            f"{now}-{config.secret_key}".encode()
        ).hexdigest()[:16],
    }
    payload_b64 = base64.urlsafe_b64encode(
        json.dumps(payload).encode()
    ).rstrip(b"=").decode()

    signature = hmac.new(
        config.secret_key.encode(),
        payload_b64.encode(),
        hashlib.sha256,
    ).hexdigest()

    token = f"{payload_b64}.{signature}"
    return TokenResponse(
        access_token=token,
        expires_in=TOKEN_EXPIRY_SECONDS,
    )
