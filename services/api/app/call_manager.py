"""Call Manager — orchestrates TTS synthesis and SIP call execution.

Maintains in-memory call state tracking and runs call execution as
background tasks so the API can return immediately.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import AppConfig, TtsConfig
from .models import CallRequest, CallResponse, CallStatus, CallStatusResponse
from .sip_controller import CallResult, SipController
from .tts_service import TTSException, TTSService

logger = logging.getLogger("wcs.manager")

# Seconds of silence appended by ffmpeg's apad filter to ensure
# clean end-of-stream and prevent the last audio packets from being
# dropped during ffmpeg shutdown (AVIO buffer flush race).
APAD_SECS = 5


class CallRecord:
    """Tracks the state of a single call through its lifecycle."""

    __slots__ = (
        "call_id", "target", "message", "status", "callback_url",
        "created_at", "updated_at", "duration_seconds", "error_message",
        "repeat", "repeat_delay",
    )

    def __init__(self, call_id: str, request: CallRequest) -> None:
        self.call_id = call_id
        self.target = request.target
        self.message = request.message
        self.callback_url = str(request.callback_url) if request.callback_url else None
        self.repeat = request.repeat
        self.repeat_delay = request.repeat_delay
        self.status = CallStatus.QUEUED
        self.created_at = datetime.now(timezone.utc)
        self.updated_at = self.created_at
        self.duration_seconds: Optional[float] = None
        self.error_message: Optional[str] = None

    def to_response(self) -> CallStatusResponse:
        return CallStatusResponse(
            call_id=self.call_id,
            status=self.status,
            target=self.target,
            message=self.message,
            created_at=self.created_at,
            updated_at=self.updated_at,
            duration_seconds=self.duration_seconds,
            error_message=self.error_message,
            callback_url=self.callback_url,
            repeat=self.repeat,
            repeat_delay=self.repeat_delay,
        )


class CallManager:
    """Orchestrates the full TTS → SIP call pipeline."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._sip = SipController(config.sip)
        self._tts = TTSService(config.tts)
        self._calls: dict[str, CallRecord] = {}
        self._lock = threading.Lock()
        self._running = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit_call(self, request: CallRequest) -> CallResponse:
        """Accept a call request and start background execution.

        Returns immediately with call_id and 'queued' status.
        """
        response = CallResponse()
        record = CallRecord(response.call_id, request)

        with self._lock:
            self._calls[record.call_id] = record

        logger.info(
            "Call %s queued: target=%s, message='%s...'",
            record.call_id, request.target, request.message[:50],
        )

        # Launch background execution
        asyncio.create_task(self._execute(record))

        return response

    def get_call(self, call_id: str) -> Optional[CallStatusResponse]:
        """Get the current status of a call by ID."""
        with self._lock:
            record = self._calls.get(call_id)
        return record.to_response() if record else None

    def list_calls(self, offset: int = 0, limit: int = 50) -> list[CallStatusResponse]:
        """List recent calls, newest first."""
        with self._lock:
            all_calls = sorted(
                self._calls.values(),
                key=lambda r: r.created_at,
                reverse=True,
            )
        return [r.to_response() for r in all_calls[offset:offset + limit]]

    @property
    def active_call_count(self) -> int:
        active = {CallStatus.CALLING, CallStatus.CONNECTED, CallStatus.PLAYING}
        with self._lock:
            return sum(1 for r in self._calls.values() if r.status in active)

    def shutdown(self) -> None:
        """Gracefully stop the SIP controller."""
        self._running = False
        self._sip.disconnect()

    def update_tts_config(self, config: TtsConfig) -> None:
        """Update TTS configuration at runtime.

        Args:
            config: A :class:`TtsConfig` instance with the new settings.
        """
        self._tts.update_config(config)
        logger.info(
            "TTS config updated: engine=%s, speaker=%d, speed=%.1f",
            config.engine,
            config.zalo_speaker_id,
            config.zalo_speed,
        )

    def cleanup_tts_cache(self, max_age_days: int) -> tuple[int, int]:
        """Delete expired TTS cache files.

        Delegates to :meth:`TTSService.cleanup_cache`.
        """
        return self._tts.cleanup_cache(max_age_days)

    def get_tts_cache_stats(self) -> dict:
        """Return TTS cache statistics.

        Delegates to :meth:`TTSService.get_cache_stats`.
        """
        return self._tts.get_cache_stats()

    # ------------------------------------------------------------------
    # Background execution
    # ------------------------------------------------------------------

    async def _execute(self, record: CallRecord) -> None:
        """Run the full call pipeline in the background."""
        wav_path: Optional[Path] = None
        start_time = datetime.now(timezone.utc)

        try:
            # --- Phase 1: TTS synthesis ---
            self._transition(record, CallStatus.SYNTHESIZING)

            wav_path = Path(self._config.call.audio_dir) / f"{record.call_id}.wav"
            wav_path = await asyncio.to_thread(
                self._tts.synthesize, record.message, str(wav_path)
            )

            duration = await asyncio.to_thread(self._tts.get_duration, wav_path)
            logger.info("Call %s: TTS done, duration=%.1fs", record.call_id, duration)

            # --- Phase 2: SIP call ---
            self._transition(record, CallStatus.CALLING)

            # Make the call (blocking I/O run in thread)
            single_play = math.ceil(duration) + APAD_SECS + 10
            total_play = record.repeat * single_play + (record.repeat - 1) * record.repeat_delay
            timeout = max(self._config.call.timeout, total_play + 15)
            result: CallResult = await asyncio.to_thread(
                self._sip.make_call,
                record.target,
                str(wav_path),
                timeout,
                record.repeat,
                record.repeat_delay,
            )

            # --- Phase 3: Outcome ---
            elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
            record.duration_seconds = elapsed

            status_map = {
                CallResult.COMPLETED: CallStatus.COMPLETED,
                CallResult.NO_ANSWER: CallStatus.NO_ANSWER,
                CallResult.BUSY: CallStatus.BUSY,
                CallResult.DECLINED: CallStatus.NO_ANSWER,
                CallResult.FAILED: CallStatus.FAILED,
            }
            final_status = status_map.get(result, CallStatus.FAILED)
            self._transition(record, final_status)

            logger.info(
                "Call %s finished: %s (%.1fs)",
                record.call_id, final_status.value, elapsed,
            )

        except TTSException as exc:
            self._transition(record, CallStatus.FAILED, str(exc))
            logger.error("Call %s TTS error: %s", record.call_id, exc)

        except Exception as exc:
            self._transition(record, CallStatus.FAILED, str(exc))
            logger.exception("Call %s unexpected error", record.call_id)

        finally:
            # Clean up audio file
            if wav_path and wav_path.exists():
                try:
                    wav_path.unlink()
                except OSError:
                    pass

            # Fire webhook if configured
            if record.callback_url:
                await self._fire_webhook(record)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _transition(self, record: CallRecord, status: CallStatus, error: Optional[str] = None) -> None:
        """Update call status with timestamp."""
        record.status = status
        record.updated_at = datetime.now(timezone.utc)
        if error:
            record.error_message = error
        logger.debug("Call %s → %s", record.call_id, status.value)

    async def _fire_webhook(self, record: CallRecord) -> None:
        """POST call status to the callback URL."""
        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                payload = record.to_response().model_dump(mode="json")
                async with session.post(
                    record.callback_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    logger.debug("Webhook to %s → HTTP %d", record.callback_url, resp.status)
        except Exception:
            logger.warning("Webhook to %s failed", record.callback_url, exc_info=True)
