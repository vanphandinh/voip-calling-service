"""Vietnamese Text-to-Speech service.

Supports multiple backends:
- Valtec: HuggingFace Space (Valtec), 5 Vietnamese voices, GPU-powered, native speed control.
- ResponsiveVoice: Cloud API, Vietnamese voices via responsivevoice.org.
- Zalo AI TTS: 6 Vietnamese voices (Northern/Southern, Male/Female).
- gTTS (Google Text-to-Speech): Cloud-based, best quality, requires internet.
- espeak-ng: Offline fallback, robotic but always available.
"""

import hashlib
import http.cookiejar
import json
import logging
import re
import shutil
import struct
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Optional

from .config import TtsConfig

logger = logging.getLogger("wcs.tts")


class TTSException(Exception):
    """Raised when TTS synthesis fails."""


class TTSService:
    """Vietnamese TTS abstraction over multiple backends."""

    # Zalo API constants
    _ZALO_PAGE_URL = "https://ai.zalo.solutions/products/text-to-audio-converter"
    _ZALO_API_URL = "https://ai.zalo.solutions/api/demo/v1/tts/synthesize"

    # ResponsiveVoice API constants
    _RV_API_URL = "https://texttospeech.responsivevoice.org/v2/text/synthesize"

    def __init__(self, config: TtsConfig) -> None:
        self._config = config
        self._zalo_cookie: Optional[str] = None

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _cache_key(self, text: str) -> str:
        """Build a deterministic cache key from text + current TTS config.

        Zalo includes speaker_id/speed; ResponsiveVoice includes
        gender/rate/pitch; Valtec includes voice because those
        affect the output.  gTTS and espeak have no variable voice parameters.
        """
        engine = self._config.engine
        if engine == "zalo":
            raw = f"{text}|zalo|{self._config.zalo_speaker_id}|{self._config.zalo_speed}"
        elif engine == "responsivevoice":
            raw = (
                f"{text}|responsivevoice|{self._config.rv_gender}"
                f"|{self._config.rv_rate}|{self._config.rv_pitch}"
            )
        elif engine == "valtec":
            raw = f"{text}|valtec|{self._config.valtec_voice}|{self._config.valtec_speed}"
        else:
            raw = f"{text}|{engine}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _cache_dir(self) -> Path:
        """Return the cache directory (creating it if needed)."""
        if self._config.tts_cache_dir:
            cache_dir = Path(self._config.tts_cache_dir)
        else:
            cache_dir = Path(tempfile.gettempdir()) / "wcs_tts_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir

    def _cache_path(self, key: str) -> Path:
        """Return the path for a given cache key."""
        return self._cache_dir() / f"{key}.wav"

    def _build_backend_order(self) -> list[tuple[str, Callable[[str, Path], Path]]]:
        """Return (name, method) pairs in priority: primary engine, then fallbacks."""
        engine_map: dict[str, tuple[str, Callable[[str, Path], Path]]] = {
            "gtts": ("gTTS", self._synthesize_gtts),
            "zalo": ("Zalo", self._synthesize_zalo),
            "espeak": ("espeak", self._synthesize_espeak),
            "responsivevoice": ("ResponsiveVoice", self._synthesize_responsivevoice),
            "valtec": ("Valtec", self._synthesize_valtec),
        }
        primary = self._config.engine
        order = [primary]
        for eng in ("zalo", "valtec", "responsivevoice", "gtts", "espeak"):
            if eng not in order:
                order.append(eng)
        return [engine_map[eng] for eng in order if eng in engine_map]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def synthesize(self, text: str, output_path: Optional[str] = None) -> Path:
        """Convert Vietnamese text to a WAV audio file.

        Args:
            text: The Vietnamese text to speak.
            output_path: Optional destination path. Auto-generated in temp dir if None.

        Returns:
            Path to the generated WAV file (16-bit PCM, 8000 Hz mono).

        Raises:
            TTSException: If all backends fail.
        """
        if output_path:
            wav_path = Path(output_path)
        else:
            wav_path = Path(tempfile.mktemp(suffix=".wav"))

        wav_path.parent.mkdir(parents=True, exist_ok=True)

        # --- Cache: check if we already have this exact text + config ---
        cache_key = self._cache_key(text)

        if self._config.tts_cache_enabled:
            cached = self._cache_path(cache_key)
            if cached.exists() and cached.stat().st_size >= 100:
                logger.info("TTS cache HIT for key=%s (engine=%s)", cache_key, self._config.engine)
                shutil.copy2(cached, wav_path)
                cached.touch()  # reset mtime so frequently-used files survive cleanup
                return wav_path

        # Build ordered list of backends: configured primary first, then fallbacks
        backends = self._build_backend_order()

        # Try backends in order until one succeeds
        last_exception: Optional[Exception] = None
        for name, method in backends:
            try:
                method(text, wav_path)
                logger.info("TTS succeeded with %s", name)
                break
            except Exception as exc:
                logger.warning(
                    "%s failed (%s: %s), trying next backend",
                    name, type(exc).__name__, exc,
                )
                last_exception = exc
        else:
            # Loop completed without break — all backends failed
            raise TTSException(
                f"All TTS backends failed for text: '{text[:50]}...'"
            ) from last_exception

        # --- Cache: save successful result (regardless of which backend produced it) ---
        if self._config.tts_cache_enabled:
            cached = self._cache_path(cache_key)
            try:
                shutil.copy2(wav_path, cached)
                logger.debug("TTS cached to %s (key=%s)", cached, cache_key)
            except OSError as exc:
                logger.warning("Failed to write TTS cache: %s", exc)

        return wav_path

    def cleanup_cache(self, max_age_days: int) -> tuple[int, int]:
        """Delete cached WAV files older than *max_age_days*.

        Scans the cache directory, checks each ``.wav`` file's modification
        time, and removes files whose age exceeds the cutoff.  Files that
        are still being used (cache hits) have their mtime refreshed by
        :meth:`synthesize`, so they survive cleanup.

        Args:
            max_age_days: Files whose mtime is older than this many days
                are removed.

        Returns:
            ``(files_deleted, total_bytes_freed)``.
        """
        cache_dir = self._cache_dir()
        if not cache_dir.is_dir():
            logger.info(
                "Cache directory %s does not exist, nothing to clean", cache_dir
            )
            return (0, 0)

        now = time.time()
        cutoff = now - (max_age_days * 86400)
        deleted = 0
        freed = 0

        for entry in cache_dir.iterdir():
            if not entry.is_file() or entry.suffix != ".wav":
                continue
            try:
                stat = entry.stat()
                if stat.st_mtime < cutoff:
                    size = stat.st_size
                    entry.unlink()
                    deleted += 1
                    freed += size
                    logger.debug(
                        "Deleted expired cache file %s (age=%.1f days)",
                        entry.name,
                        (now - stat.st_mtime) / 86400,
                    )
            except OSError as exc:
                logger.warning("Failed to clean cache file %s: %s", entry.name, exc)

        if deleted:
            logger.info(
                "Cache cleanup: deleted %d files, freed %d bytes (%.1f MB)",
                deleted, freed, freed / 1048576,
            )
        else:
            logger.info(
                "Cache cleanup: no expired files found (max_age=%d days)", max_age_days
            )

        return (deleted, freed)

    def get_cache_stats(self) -> dict:
        """Return statistics about the TTS cache.

        Returns:
            A dict with keys ``total_files``, ``total_size_bytes``,
            and ``cache_dir``.
        """
        # Resolve path *without* creating the directory (read-only operation).
        # _cache_dir() calls mkdir() which is undesirable for a stats endpoint.
        if self._config.tts_cache_dir:
            cache_dir = Path(self._config.tts_cache_dir)
        else:
            cache_dir = Path(tempfile.gettempdir()) / "wcs_tts_cache"

        if not cache_dir.is_dir():
            return {
                "total_files": 0,
                "total_size_bytes": 0,
                "cache_dir": str(cache_dir),
            }

        total_files = 0
        total_size = 0

        try:
            for entry in cache_dir.iterdir():
                if entry.is_file() and entry.suffix == ".wav":
                    total_files += 1
                    try:
                        total_size += entry.stat().st_size
                    except OSError:
                        pass
        except PermissionError as exc:
            logger.warning("Cannot read cache directory %s: %s", cache_dir, exc)

        return {
            "total_files": total_files,
            "total_size_bytes": total_size,
            "cache_dir": str(cache_dir),
        }

    def update_config(self, config: TtsConfig) -> None:
        """Update TTS configuration at runtime (engine, speaker, speed).

        Clears the cached Zalo cookie when the engine or speaker changes
        so the next call fetches a fresh one.
        """
        old_engine = self._config.engine
        old_speaker = self._config.zalo_speaker_id
        self._config = config
        if old_engine != config.engine or old_speaker != config.zalo_speaker_id:
            self._zalo_cookie = None
            logger.debug("Zalo cookie cleared due to config change")

    def get_duration(self, wav_path: Path) -> float:
        """Return the duration of a WAV file in seconds.

        Uses ffprobe if available, then RIFF header parsing,
        and finally file-size estimation as fallback.
        """
        # 1. Try ffprobe first (most accurate)
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(wav_path),
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return float(result.stdout.strip())
        except (FileNotFoundError, ValueError, subprocess.TimeoutExpired):
            pass

        # 2. Parse RIFF WAV header (accurate for standard WAV files)
        try:
            duration = self._parse_wav_header(wav_path)
            if duration is not None and duration > 0:
                return duration
        except (OSError, struct.error):
            pass

        # 3. Fallback: estimate from file size
        #    Assume 8 kHz, 16-bit, mono = 16,000 bytes/sec
        file_size = wav_path.stat().st_size
        if file_size > 44:
            return (file_size - 44) / 16000.0
        return 0.0

    @staticmethod
    def _parse_wav_header(wav_path: Path) -> Optional[float]:
        """Parse WAV RIFF header to compute exact duration.

        Returns duration in seconds, or ``None`` if the header cannot be parsed.
        """
        with open(wav_path, "rb") as f:
            header = f.read(44)
        if len(header) < 44:
            return None
        if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            return None
        channels = struct.unpack("<H", header[22:24])[0]
        sample_rate = struct.unpack("<I", header[24:28])[0]
        bits_per_sample = struct.unpack("<H", header[34:36])[0]
        data_size = struct.unpack("<I", header[40:44])[0]

        bytes_per_second = sample_rate * channels * (bits_per_sample // 8)
        if bytes_per_second == 0 or data_size == 0:
            return None
        return data_size / bytes_per_second

    # ------------------------------------------------------------------
    # Zalo AI TTS backend
    # ------------------------------------------------------------------

    def _fetch_zalo_cookie(self) -> str:
        """Obtain a fresh ``zai_did`` cookie from Zalo's TTS demo page.

        The cookie is set server-side via ``Set-Cookie`` when visiting the
        page.  We use :mod:`http.cookiejar` to extract it automatically.

        Returns:
            The ``zai_did`` cookie value string.

        Raises:
            TTSException: If the cookie cannot be obtained.
        """
        cookie_jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cookie_jar)
        )

        req = urllib.request.Request(
            self._ZALO_PAGE_URL,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )

        try:
            with opener.open(req, timeout=15) as resp:
                resp.read()  # consume response body
        except urllib.request.URLError as exc:
            raise TTSException(
                f"Failed to reach Zalo page for cookie: {exc}"
            ) from exc

        for cookie in cookie_jar:
            if cookie.name == "zai_did":
                logger.info("Fetched fresh zai_did cookie")
                return cookie.value

        raise TTSException(
            "zai_did cookie not found in Zalo response headers"
        )

    def _synthesize_zalo(self, text: str, output_path: Path) -> Path:
        """Synthesize using Zalo AI TTS (6 Vietnamese voices).

        1. Ensures we have a valid ``zai_did`` cookie.
        2. POSTs the text to Zalo's demo TTS API.
        3. Downloads & parses the m3u8 playlist (handles both standard
           HLS and LL-HLS preload hints).
        4. Downloads raw AAC segments with retry on transient errors.
        5. Concatenates segments and transcodes to 8 kHz 16-bit mono WAV.
        """
        # Ensure we have a cookie
        if not self._zalo_cookie:
            self._zalo_cookie = self._fetch_zalo_cookie()

        logger.info(
            "Synthesizing with Zalo TTS (voice=%d, speed=%.1f): '%s...' (%d chars)",
            self._config.zalo_speaker_id,
            self._config.zalo_speed,
            text[:60],
            len(text),
        )

        # 1. Call Zalo TTS API (retry once with fresh cookie on auth error)
        post_data = urllib.parse.urlencode({
            "input": text,
            "speaker_id": str(self._config.zalo_speaker_id),
            "speed": str(self._config.zalo_speed),
            "dict_id": "0",
            "quality": "0",
        }).encode()

        for _attempt in range(2):  # initial + 1 retry with fresh cookie
            api_req = urllib.request.Request(
                self._ZALO_API_URL,
                data=post_data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "origin": "https://ai.zalo.solutions",
                    "referer": self._ZALO_PAGE_URL,
                    "Cookie": f"zai_did={self._zalo_cookie}",
                },
            )

            try:
                with urllib.request.urlopen(api_req, timeout=30) as resp:
                    body = resp.read().decode()
            except urllib.request.HTTPError as exc:
                if exc.code in (401, 403):
                    self._zalo_cookie = None
                raise TTSException(
                    f"Zalo API request failed (HTTP {exc.code}): {exc}"
                ) from exc
            except urllib.request.URLError as exc:
                raise TTSException(
                    f"Zalo API request failed (network): {exc}"
                ) from exc

            # Parse response
            try:
                result = json.loads(body)
            except json.JSONDecodeError as exc:
                self._zalo_cookie = None
                raise TTSException(
                    f"Zalo API returned invalid JSON: {body[:200]}"
                ) from exc

            error_code = result.get("error_code")
            if error_code == 0:
                break  # success — exit retry loop

            # Auth/quota error — refresh cookie and retry once
            self._zalo_cookie = None
            if _attempt == 0 and error_code in (-429,):
                logger.debug(
                    "Zalo API error %d, refreshing cookie and retrying",
                    error_code,
                )
                self._zalo_cookie = self._fetch_zalo_cookie()
                continue

            raise TTSException(
                f"Zalo TTS error {error_code}: "
                f"{result.get('error_message', 'unknown')}"
            )

        m3u8_url = result.get("data", {}).get("url")
        if not m3u8_url:
            raise TTSException("Zalo TTS returned no audio URL in response")

        logger.info("Zalo TTS audio URL: %s...", m3u8_url[:80])

        # 3. Download and parse m3u8 playlist
        #    Zalo uses non-standard HLS: segments are raw AAC files
        #    with no file extension and protocol-relative URLs.
        #    ffmpeg's HLS demuxer can't handle this, so we download
        #    segments manually, concatenate, then transcode.
        try:
            with urllib.request.urlopen(m3u8_url, timeout=15) as resp:
                playlist = resp.read().decode()
        except urllib.request.URLError as exc:
            raise TTSException(
                f"Failed to download m3u8 playlist: {exc}"
            ) from exc

        # 4. Parse segment URLs from playlist
        #    Zalo uses two HLS formats interchangeably:
        #    - Standard HLS:  #EXTINF tag followed by a standalone URL line
        #    - LL-HLS:         #EXT-X-PRELOAD-HINT:TYPE=PART,URI="//host/path"
        base_url = m3u8_url.rsplit("/", 1)[0]
        segment_urls: list[str] = []
        for line in playlist.splitlines():
            line = line.strip()
            if not line:
                continue

            # LL-HLS preload hint — extract URI from tag attribute
            if line.startswith("#EXT-X-PRELOAD-HINT"):
                match = re.search(r'URI="([^"]+)"', line)
                if match:
                    line = match.group(1)
                else:
                    continue

            # Skip other comment/tag lines
            if line.startswith("#"):
                continue

            # Resolve protocol-relative URLs (//host/path)
            if line.startswith("//"):
                line = f"https:{line}"
            elif not line.startswith("http"):
                line = f"{base_url}/{line}"
            segment_urls.append(line)

        if not segment_urls:
            raise TTSException("No audio segments found in m3u8 playlist")

        logger.debug(
            "Downloading %d AAC segment(s) from Zalo CDN", len(segment_urls)
        )

        # 5. Download all segments → concatenate into one AAC file.
        #    LL-HLS preload hints may reference PARTs that are still being
        #    generated — retry with backoff on 404/503.
        tmp_aac = output_path.with_suffix(".aac")
        try:
            with open(tmp_aac, "wb") as out_fh:
                for i, seg_url in enumerate(segment_urls, 1):
                    seg_data = self._download_segment(seg_url, i, len(segment_urls))
                    out_fh.write(seg_data)

            # 6. Convert concatenated AAC → WAV (8 kHz, 16-bit PCM, mono)
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error",
                    "-i", str(tmp_aac),
                    "-ar", "8000",
                    "-ac", "1",
                    "-sample_fmt", "s16",
                    str(output_path),
                ],
                check=True,
                timeout=60,
            )
        except FileNotFoundError as exc:
            raise TTSException(
                "ffmpeg is required for Zalo TTS but was not found"
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise TTSException(
                f"ffmpeg failed to convert Zalo AAC audio: {exc}"
            ) from exc
        finally:
            # Clean up temporary AAC file
            try:
                tmp_aac.unlink()
            except OSError:
                pass

        if not output_path.exists() or output_path.stat().st_size < 100:
            raise TTSException("Zalo TTS produced empty audio")

        logger.info("Zalo TTS WAV written to %s", output_path)
        return output_path

    # ------------------------------------------------------------------
    # Zalo helpers
    # ------------------------------------------------------------------

    def _download_segment(
        self, url: str, index: int, total: int
    ) -> bytes:
        """Download a single AAC segment with retry on transient errors.

        Zalo uses LL-HLS where ``#EXT-X-PRELOAD-HINT`` references a PART
        that may still be finalising on the server.  We retry a few times
        with a short backoff to handle 404 / 503 responses.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(1, 5):  # up to 4 attempts
            try:
                seg_req = urllib.request.Request(url)
                with urllib.request.urlopen(seg_req, timeout=15) as resp:
                    data = resp.read()
                if attempt > 1:
                    logger.debug(
                        "Segment %d/%d succeeded on attempt %d",
                        index, total, attempt,
                    )
                return data
            except urllib.request.HTTPError as exc:
                last_exc = exc
                if exc.code in (404, 503):
                    delay = 0.25 * (2 ** (attempt - 1))  # 0.25, 0.5, 1.0, 2.0s
                    logger.debug(
                        "Segment %d/%d HTTP %d, retrying in %.1fs (attempt %d/4)",
                        index, total, exc.code, delay, attempt,
                    )
                    time.sleep(delay)
                    continue
                raise TTSException(
                    f"Failed to download segment {index}/{total}: {exc}"
                ) from exc
            except urllib.request.URLError as exc:
                last_exc = exc
                delay = 0.25 * (2 ** (attempt - 1))
                logger.debug(
                    "Segment %d/%d network error, retrying in %.1fs (attempt %d/4)",
                    index, total, delay, attempt,
                )
                time.sleep(delay)
                continue

        raise TTSException(
            f"Failed to download segment {index}/{total} "
            f"after 4 attempts: {last_exc}"
        ) from last_exc

    # ------------------------------------------------------------------
    # Backend implementations
    # ------------------------------------------------------------------

    def _synthesize_gtts(self, text: str, output_path: Path) -> Path:
        """Synthesize using Google TTS (gTTS)."""
        from gtts import gTTS

        logger.info("Synthesizing with gTTS: '%s...' (%d chars)", text[:60], len(text))

        # gTTS generates MP3; we convert to WAV with ffmpeg if available
        mp3_path = output_path.with_suffix(".mp3")

        tts = gTTS(text=text, lang="vi", slow=False)
        tts.save(str(mp3_path))

        # Convert MP3 → WAV (8000 Hz, 16-bit, mono — required for RTP PCMU)
        self._convert_to_wav(mp3_path, output_path)

        # Clean up MP3
        try:
            mp3_path.unlink()
        except OSError:
            pass

        logger.info("gTTS WAV written to %s", output_path)
        return output_path

    def _synthesize_espeak(self, text: str, output_path: Path) -> Path:
        """Synthesize using espeak-ng (offline)."""
        logger.info("Synthesizing with espeak-ng: '%s...'", text[:60])

        # espeak-ng writes WAV directly
        cmd = [
            "espeak-ng",
            "-v", "vi",  # Vietnamese voice
            "-w", str(output_path),
            "-s", "140",  # speed (words per minute)
            "-p", "50",   # pitch
            "-a", "100",  # amplitude
            "--", text,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        if result.returncode != 0:
            raise TTSException(f"espeak-ng failed: {result.stderr}")

        if not output_path.exists() or output_path.stat().st_size < 100:
            raise TTSException("espeak-ng produced empty audio")

        logger.info("espeak-ng WAV written to %s", output_path)
        return output_path

    def _synthesize_responsivevoice(self, text: str, output_path: Path) -> Path:
        """Synthesize using ResponsiveVoice TTS API.

        Uses POST /v2/text/synthesize with JSON body.
        Language is hardcoded to Vietnamese (vi-VN).
        Returns 8kHz 16-bit mono WAV.
        """
        logger.info(
            "Synthesizing with ResponsiveVoice (gender=%s): '%s...' (%d chars)",
            self._config.rv_gender or "default",
            text[:60],
            len(text),
        )

        # Build payload — lang is hardcoded to vi-VN
        payload: dict = {
            "text": text,
            "lang": "vi-VN",
            "format": "mp3",
        }
        if self._config.rv_gender:
            payload["gender"] = self._config.rv_gender
        if self._config.rv_rate != 1.0:
            payload["rate"] = self._config.rv_rate
        if self._config.rv_pitch != 1.0:
            payload["pitch"] = self._config.rv_pitch

        headers = {
            "X-API-Key": self._config.rv_site_id,
            "X-API-Secret": self._config.rv_api_key,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._RV_API_URL,
            data=data,
            headers=headers,
            method="POST",
        )

        for attempt in range(2):  # initial + 1 retry on 429
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    body = resp.read()
            except urllib.request.HTTPError as exc:
                if exc.code == 429:
                    retry_after = exc.headers.get("Retry-After")
                    delay = int(retry_after) if retry_after else 5
                    if attempt == 0:
                        logger.warning(
                            "ResponsiveVoice rate limited (429), "
                            "retrying after %ds",
                            delay,
                        )
                        time.sleep(delay)
                        continue
                    raise TTSException(
                        f"ResponsiveVoice rate limited (429) — "
                        f"retry failed after {delay}s"
                    ) from exc
                # Try to parse error body
                try:
                    err_json = json.loads(exc.read().decode())
                    err_msg = err_json.get("error", {}).get("message", str(exc))
                except Exception:
                    err_msg = str(exc)
                raise TTSException(
                    f"ResponsiveVoice API error (HTTP {exc.code}): {err_msg}"
                ) from exc
            except urllib.request.URLError as exc:
                raise TTSException(
                    f"ResponsiveVoice API request failed (network): {exc}"
                ) from exc

            # Success — write mp3 then convert to WAV
            mp3_path = output_path.with_suffix(".mp3")
            try:
                mp3_path.write_bytes(body)
                self._convert_to_wav(mp3_path, output_path)
            finally:
                try:
                    mp3_path.unlink()
                except OSError:
                    pass

            if not output_path.exists() or output_path.stat().st_size < 100:
                raise TTSException(
                    "ResponsiveVoice returned empty or invalid audio"
                )

            logger.info("ResponsiveVoice WAV written to %s", output_path)
            return output_path

        # Should not reach here (retry loop exits via return or raise)
        raise TTSException("ResponsiveVoice synthesis failed after retries")

    # ------------------------------------------------------------------
    # Valtec TTS backend (HuggingFace Space — Valtec Vietnamese TTS)
    # ------------------------------------------------------------------

    def _synthesize_valtec(self, text: str, output_path: Path) -> Path:
        """Synthesize using Valtec Vietnamese TTS via Gradio client.

        Calls the HuggingFace Space ``valtecAI-team/valtec-vietnamese-tts``
        using the ``gradio_client`` library.  The model supports native
        speed control (length_scale), 5 speakers, and runs on GPU.

        Only voice (speaker) and speed are configurable — noise_scale,
        noise_scale_w, and sdp_ratio use the model defaults.
        """
        logger.info(
            "Synthesizing with Valtec (speaker=%s, speed=%.1f): '%s...' (%d chars)",
            self._config.valtec_voice, self._config.valtec_speed, text[:60], len(text),
        )

        try:
            from gradio_client import Client  # type: ignore[import-untyped]
        except ImportError as exc:
            raise TTSException(
                "gradio_client is required for Valtec TTS. "
                "Install it with: pip install gradio-client"
            ) from exc

        try:
            hf_token = self._config.valtec_hf_token or None
            client = Client(
                "valtecAI-team/valtec-vietnamese-tts",
                hf_token=hf_token,
            )
        except Exception as exc:
            raise TTSException(
                f"Failed to connect to Valtec Space: {exc}"
            ) from exc

        try:
            result = client.predict(
                text,
                self._config.valtec_voice,   # speaker
                self._config.valtec_speed,    # speed (length_scale)
                0.667,   # noise_scale
                0.8,     # noise_scale_w
                0.0,     # sdp_ratio
                api_name="/synthesize",
            )
        except Exception as exc:
            raise TTSException(
                f"Valtec synthesis failed: {exc}"
            ) from exc

        # result is (audio_file_path, sample_rate)
        audio_path = Path(str(result[0]))

        # Convert to 8 kHz 16-bit mono PCM WAV
        self._convert_to_wav(audio_path, output_path)

        if not output_path.exists() or output_path.stat().st_size < 100:
            raise TTSException("Valtec produced empty or invalid audio")

        logger.info("Valtec WAV written to %s", output_path)
        return output_path

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _convert_to_wav(input_path: Path, output_path: Path) -> None:
        """Convert audio file to RTP-compatible WAV format (8kHz mono PCM).

        8000 Hz, 16-bit PCM, 1 channel (mono).
        Tries ffmpeg first, falls back to a basic copy if audio is already WAV.
        """
        # Try ffmpeg
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-v", "error",
                    "-i", str(input_path),
                    "-ar", "8000",       # sample rate
                    "-ac", "1",           # mono
                    "-sample_fmt", "s16", # 16-bit PCM
                    str(output_path),
                ],
                check=True,
                timeout=30,
            )
            return
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass

        # If input is already WAV and ffmpeg unavailable, copy as-is
        if input_path.suffix.lower() == ".wav":
            import shutil
            shutil.copy2(input_path, output_path)
            return

        raise TTSException(
            "ffmpeg is required to convert audio formats. "
            "Install ffmpeg or use the espeak backend."
        )
