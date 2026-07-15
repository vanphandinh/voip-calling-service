"""API route handlers for the VoIP Calling Service."""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from collections import defaultdict

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

_rate_limit_buckets: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT_MAX = 10       # max calls per window
RATE_LIMIT_WINDOW = 1.0   # window in seconds

# Token expiry (seconds)
TOKEN_EXPIRY_SECONDS = 86400  # 24 hours


def _check_rate_limit(request: Request) -> bool:
    """Enforce per-IP rate limit. Returns ``True`` if the request is allowed."""
    client_ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    window_start = now - RATE_LIMIT_WINDOW

    bucket = _rate_limit_buckets[client_ip]
    _rate_limit_buckets[client_ip] = [t for t in bucket if t >= window_start]

    if not _rate_limit_buckets[client_ip]:
        del _rate_limit_buckets[client_ip]
        return True

    if len(_rate_limit_buckets[client_ip]) >= RATE_LIMIT_MAX:
        logger.warning("Rate limit exceeded for IP %s", client_ip)
        return False

    _rate_limit_buckets[client_ip].append(now)
    return True


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
    return TtsConfigResponse(
        engine=tts.engine,
        zalo_speaker_id=tts.zalo_speaker_id,
        zalo_speed=tts.zalo_speed,
        rv_gender=tts.rv_gender,
        rv_rate=tts.rv_rate,
        rv_pitch=tts.rv_pitch,
        rv_configured=bool(tts.rv_api_key and tts.rv_site_id),
        vieneu_voice=tts.vieneu_voice,
        vieneu_speed=tts.vieneu_speed,
    )


@router.put("/tts/config", response_model=TtsConfigResponse)
async def update_tts_config(request: Request, body: TtsConfigUpdate):
    """Update TTS engine, speaker, or speed at runtime.

    All fields are optional — only the provided fields are changed.
    The change takes effect immediately on the next call.
    """
    tts = request.app.state.config.tts
    mgr = _get_manager(request)

    # Merge: keep existing values for fields not provided
    if body.engine is not None:
        tts.engine = body.engine
    if body.zalo_speaker_id is not None:
        tts.zalo_speaker_id = body.zalo_speaker_id
    if body.zalo_speed is not None:
        tts.zalo_speed = body.zalo_speed
    if body.rv_gender is not None:
        tts.rv_gender = body.rv_gender
    if body.rv_rate is not None:
        tts.rv_rate = body.rv_rate
    if body.rv_pitch is not None:
        tts.rv_pitch = body.rv_pitch
    if body.vieneu_voice is not None:
        tts.vieneu_voice = body.vieneu_voice
    if body.vieneu_speed is not None:
        tts.vieneu_speed = body.vieneu_speed

    # Validate the merged config
    errors = request.app.state.config.validate()
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))

    # Push to CallManager → TTSService
    mgr.update_tts_config(tts)

    return TtsConfigResponse(
        engine=tts.engine,
        zalo_speaker_id=tts.zalo_speaker_id,
        zalo_speed=tts.zalo_speed,
        rv_gender=tts.rv_gender,
        rv_rate=tts.rv_rate,
        rv_pitch=tts.rv_pitch,
        rv_configured=bool(tts.rv_api_key and tts.rv_site_id),
        vieneu_voice=tts.vieneu_voice,
        vieneu_speed=tts.vieneu_speed,
    )


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
    """
    config = request.app.state.config
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
