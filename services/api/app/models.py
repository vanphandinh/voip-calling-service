"""Pydantic models for API request/response validation."""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field, HttpUrl


# ---------------------------------------------------------------------------
# Call status
# ---------------------------------------------------------------------------

class CallStatus(str, Enum):
    """Possible states of a call."""
    QUEUED = "queued"
    SYNTHESIZING = "synthesizing"  # TTS conversion in progress
    CALLING = "calling"  # SIP call being placed
    COMPLETED = "completed"
    FAILED = "failed"
    NO_ANSWER = "no_answer"
    BUSY = "busy"


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class CallRequest(BaseModel):
    """Request to trigger an announcement call."""

    target: str = Field(
        ...,
        description="Target SIP URI (e.g. 'sip:user@domain' or 'sip:0123456789@domain:5060')",
        examples=["sip:0123456789@sip.linphone.org"],
        min_length=5,
    )
    message: str = Field(
        ...,
        description="Text message to convert to Vietnamese speech and play during the call",
        examples=["Xin chào, đây là thông báo từ hệ thống giám sát."],
        min_length=1,
        max_length=2000,
    )
    repeat: int = Field(
        default=2,
        ge=1,
        le=20,
        description="Number of times to repeat the voice announcement (1 = play once)",
    )
    repeat_delay: float = Field(
        default=1.0,
        ge=0.5,
        le=10.0,
        description="Delay in seconds between repeat loops",
    )
    callback_url: Optional[HttpUrl] = Field(
        default=None,
        description="Optional webhook URL to notify when call status changes",
    )


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class CallResponse(BaseModel):
    """Response returned immediately after submitting a call."""

    call_id: str = Field(default_factory=lambda: uuid4().hex[:12])
    status: CallStatus = CallStatus.QUEUED
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CallStatusResponse(BaseModel):
    """Detailed status of a specific call."""

    call_id: str
    status: CallStatus
    target: str
    message: str
    created_at: datetime
    updated_at: datetime
    duration_seconds: Optional[float] = None
    error_message: Optional[str] = None
    callback_url: Optional[str] = None
    repeat: int = 1
    repeat_delay: float = 1.0


class CallListResponse(BaseModel):
    """Paginated list of calls."""

    calls: list[CallStatusResponse]
    total: int
    offset: int = 0
    limit: int = 50


class HealthResponse(BaseModel):
    """Health check response."""

    status: str = "ok"
    sip_registered: bool = False
    tts_engine: str = ""
    active_calls: int = 0


# ---------------------------------------------------------------------------
# TTS configuration models
# ---------------------------------------------------------------------------

class ZaloVoiceInfo(BaseModel):
    """Information about a Zalo TTS voice."""

    id: int
    gender: str
    accent: str


class TtsConfigUpdate(BaseModel):
    """Request to update TTS configuration at runtime.

    All fields are optional — only the provided fields are changed.
    """

    engine: Optional[str] = Field(
        default=None,
        description="TTS engine: 'gtts', 'zalo', 'espeak', 'responsivevoice', or 'vieneu'",
        pattern="^(gtts|zalo|espeak|responsivevoice|vieneu)$",
    )
    zalo_speaker_id: Optional[int] = Field(
        default=None,
        ge=1,
        le=6,
        description="Zalo voice ID (1-6)",
    )
    zalo_speed: Optional[float] = Field(
        default=None,
        ge=0.8,
        le=1.2,
        description="Speech speed factor (0.8 - 1.2)",
    )
    rv_gender: Optional[str] = Field(
        default=None,
        pattern="^(male|female)$",
        description="ResponsiveVoice gender: 'male' or 'female'",
    )
    rv_rate: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=2.0,
        description="ResponsiveVoice speech rate (0.0 - 2.0)",
    )
    rv_pitch: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=2.0,
        description="ResponsiveVoice voice pitch (0.0 - 2.0)",
    )
    vieneu_voice: Optional[str] = Field(
        default=None,
        min_length=1,
        description="VieNeu preset voice name (e.g. 'Phạm Tuyên', 'Minh Đức')",
    )
    vieneu_speed: Optional[float] = Field(
        default=None,
        ge=0.5,
        le=2.0,
        description="Speech speed (0.5 = half speed, 1.0 = normal, 2.0 = double)",
    )


ZALO_VOICES: list[ZaloVoiceInfo] = [
    ZaloVoiceInfo(id=1, gender="Nu", accent="Mien Nam"),
    ZaloVoiceInfo(id=2, gender="Nu", accent="Mien Bac"),
    ZaloVoiceInfo(id=3, gender="Nam", accent="Mien Nam"),
    ZaloVoiceInfo(id=4, gender="Nam", accent="Mien Bac"),
    ZaloVoiceInfo(id=5, gender="Nu", accent="Mien Bac"),
    ZaloVoiceInfo(id=6, gender="Nu", accent="Mien Nam"),
]


class TtsConfigResponse(BaseModel):
    """Current TTS configuration."""

    engine: str
    zalo_speaker_id: int
    zalo_speed: float
    rv_gender: str = ""
    rv_rate: float = 1.0
    rv_pitch: float = 1.0
    rv_configured: bool = False
    vieneu_voice: str = "Phạm Tuyên"
    vieneu_speed: float = 1.0
    available_engines: list[str] = Field(
        default_factory=lambda: ["gtts", "zalo", "espeak", "responsivevoice", "vieneu"]
    )
    zalo_voices: list[ZaloVoiceInfo] = Field(
        default_factory=lambda: list(ZALO_VOICES)
    )


# ---------------------------------------------------------------------------
# TTS cache models
# ---------------------------------------------------------------------------

class TtsCacheStatsResponse(BaseModel):
    """Statistics about the TTS cache directory."""

    total_files: int
    total_size_bytes: int
    total_size_mb: float
    cache_dir: str


class TtsCacheCleanupResponse(BaseModel):
    """Result of a TTS cache cleanup operation."""

    deleted_files: int
    freed_bytes: int
    freed_mb: float
    max_age_days: int


# ---------------------------------------------------------------------------
# Auth models
# ---------------------------------------------------------------------------


class TokenRequest(BaseModel):
    """Request to exchange master secret key for an access token."""

    secret_key: str = Field(
        ...,
        min_length=8,
        description="Master secret key configured via SECRET_KEY env var",
    )


class TokenResponse(BaseModel):
    """Signed access token response."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds until expiry
