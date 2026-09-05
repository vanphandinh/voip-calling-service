"""Call Manager — orchestrates TTS synthesis and SIP call execution.

Maintains in-memory call state tracking and runs call execution as
background tasks so the API can return immediately.
"""

from __future__ import annotations

import aiohttp
import asyncio
import ipaddress
import logging
import math
import socket
import threading
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from uuid import uuid4

from .config import AppConfig, TtsConfig
from .models import CallRequest, CallResponse, CallStatus, CallStatusResponse
from .sip_controller import CallResult, SipController
# APAD_SECS: silence padding baked into every synthesized WAV by TTSService.
# Imported (not redefined) so the timeout math here always matches the audio.
from .tts_service import APAD_SECS, TTSException, TTSService

logger = logging.getLogger("wcs.manager")

# Maximum number of call records kept in memory. Oldest terminal
# records (completed/failed) are evicted when this limit is exceeded.
MAX_CALL_RECORDS = 10000


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
        # Strong references to background tasks — asyncio only keeps weak
        # refs, so tasks without a reference can be garbage-collected
        # mid-flight (see CPython docs on asyncio.create_task).
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit_call(self, request: CallRequest) -> CallResponse:
        """Accept a call request and start background execution.

        Returns immediately with call_id and 'queued' status.
        """
        # Build the record first, then the response FROM the record so the
        # timestamps returned to the caller match the stored ones exactly.
        record = CallRecord(uuid4().hex[:12], request)
        response = CallResponse(
            call_id=record.call_id,
            status=record.status,
            created_at=record.created_at,
        )

        with self._lock:
            self._calls[record.call_id] = record
            self._evict_old_records()

        logger.info(
            "Call %s queued: target=%s, message='%s...'",
            record.call_id, request.target, request.message[:50],
        )

        # Launch background execution
        try:
            task = asyncio.create_task(self._execute(record))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except RuntimeError:
            # No running event loop — spawn a background thread
            threading.Thread(
                target=lambda: asyncio.run(self._execute(record)),
                daemon=True,
            ).start()

        return response

    def get_call(self, call_id: str) -> Optional[CallStatusResponse]:
        """Get the current status of a call by ID."""
        with self._lock:
            record = self._calls.get(call_id)
            return record.to_response() if record else None

    def list_calls(self, offset: int = 0, limit: int = 50) -> tuple[list[CallStatusResponse], int]:
        """List recent calls, newest first.

        Returns:
            ``(responses, total_count)`` tuple.
        """
        with self._lock:
            all_calls = sorted(
                self._calls.values(),
                key=lambda r: r.created_at,
                reverse=True,
            )
            total = len(all_calls)
            return (
                [r.to_response() for r in all_calls[offset:offset + limit]],
                total,
            )

    @property
    def active_call_count(self) -> int:
        active = {CallStatus.CALLING}
        with self._lock:
            return sum(1 for r in self._calls.values() if r.status in active)

    @property
    def is_sip_registered(self) -> bool:
        """Whether the SIP controller is currently registered with the proxy."""
        return self._sip.is_registered

    def connect_sip(self) -> None:
        """Connect and register with the SIP proxy."""
        self._sip.connect()

    def shutdown(self) -> None:
        """Gracefully stop the SIP controller."""
        self._sip.disconnect()

    def update_tts_config(self, config: TtsConfig) -> None:
        """Update TTS configuration at runtime.

        Args:
            config: A :class:`TtsConfig` instance with the new settings.
        """
        self._tts.update_config(config)
        logger.info("TTS config updated: engine=%s", config.engine)

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
    # Eviction
    # ------------------------------------------------------------------

    def _evict_old_records(self) -> None:
        """Evict oldest terminal records when over the max limit.

        Never evicts records with in-flight calls (queued/synthesizing/
        calling) — a 404 on an active call would be worse than slightly
        exceeding the in-memory cap.
        """
        terminal = {
            CallStatus.COMPLETED, CallStatus.FAILED,
            CallStatus.NO_ANSWER, CallStatus.BUSY,
        }
        if len(self._calls) <= MAX_CALL_RECORDS:
            return

        terminal_records = sorted(
            [r for r in self._calls.values() if r.status in terminal],
            key=lambda r: r.updated_at,
        )
        target = int(MAX_CALL_RECORDS * 0.9)
        excess = len(terminal_records) - target
        if excess > 0:
            for r in terminal_records[:excess]:
                del self._calls[r.call_id]
            logger.info(
                "Evicted %d old call record(s) (total=%d, limit=%d)",
                excess, len(self._calls), MAX_CALL_RECORDS,
            )

        # Soft cap: if still over 120% limit, evict oldest records — but
        # STILL only terminal ones. Active calls are never evicted.
        hard_limit = int(MAX_CALL_RECORDS * 1.2)
        if len(self._calls) > hard_limit:
            evictable = sorted(
                [r for r in self._calls.values() if r.status in terminal],
                key=lambda r: r.updated_at,
            )
            overflow = min(len(evictable), len(self._calls) - target)
            for r in evictable[:overflow]:
                del self._calls[r.call_id]
            if overflow:
                logger.warning(
                    "Hard eviction: removed %d records (over 120%% limit, total=%d)",
                    overflow, len(self._calls),
                )

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
                # DECLINED → FAILED: callee actively rejected, not "no answer"
                CallResult.DECLINED: CallStatus.FAILED,
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
        with self._lock:
            record.status = status
            record.updated_at = datetime.now(timezone.utc)
            if error:
                record.error_message = error
        logger.debug("Call %s → %s", record.call_id, status.value)

    async def _fire_webhook(self, record: CallRecord) -> None:
        """POST call status to the callback URL (SSRF-hardened).

        Every hop is resolved and validated BEFORE connecting, and the
        connection is PINNED to the validated IP via a custom aiohttp
        resolver — so a DNS rebinding between validation and connection
        is ineffective. Redirects are NOT followed automatically; we
        follow them ourselves (max 4) and re-validate every hop.
        """
        url = record.callback_url
        payload = record.to_response().model_dump(mode="json")
        loop = asyncio.get_running_loop()
        try:
            current = url
            for _hop in range(4):
                ip = await loop.run_in_executor(None, self._resolve_public_ip, current)
                if ip is None:
                    logger.warning(
                        "Blocked callback to private/internal/unresolvable target: %s",
                        current,
                    )
                    return
                connector = aiohttp.TCPConnector(resolver=_PinnedResolver(ip))
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.post(
                        current,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=10),
                        allow_redirects=False,
                    ) as resp:
                        if resp.status in (301, 302, 303, 307, 308):
                            location = resp.headers.get("Location")
                            if not location:
                                logger.debug("Webhook redirect without Location: %s", current)
                                return
                            current = urllib.parse.urljoin(current, location)
                            continue
                        logger.debug(
                            "Webhook to %s → HTTP %d", current, resp.status
                        )
                        return
            logger.warning("Webhook redirect limit exceeded: %s", url)
        except Exception:
            logger.warning("Webhook to %s failed", url, exc_info=True)

    @staticmethod
    def _resolve_public_ip(url: str) -> Optional[str]:
        """Resolve *url*'s host and return an IP only if it is safe to dial.

        Returns ``None`` (block) when:
        - the hostname cannot be extracted or resolved
        - ANY resolved address is private / loopback / link-local /
          unspecified / multicast / reserved / CGNAT (100.64.0.0/10) /
          an IPv4-mapped IPv6 address
        """
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
            if not hostname:
                return None
            addrs = socket.getaddrinfo(hostname, None)
        except (socket.gaierror, ValueError, OSError):
            return None

        cgnat = ipaddress.ip_network("100.64.0.0/10")
        safe: list[str] = []
        for _family, _, _, _, sockaddr in addrs:
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                continue
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_unspecified
                or ip.is_multicast
                or ip.is_reserved
            ):
                return None
            if ip.version == 6 and ip.ipv4_mapped is not None:
                return None
            if ip.version == 4 and ip in cgnat:
                return None
            safe.append(ip_str)
        return safe[0] if safe else None


class _PinnedResolver(aiohttp.resolver.AbstractResolver):
    """DNS resolver that always returns ONE pre-validated IP address.

    Used by the webhook client so the connection is guaranteed to go to
    the IP that passed the SSRF check (no re-resolution = no rebinding).
    The Host header / TLS SNI still use the URL's original hostname.
    """

    def __init__(self, ip: str) -> None:
        self._ip = ip

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        return [
            {
                "hostname": host,
                "host": self._ip,
                "port": port,
                "family": family,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
        ]

    async def close(self) -> None:  # pragma: no cover — nothing to release
        pass
