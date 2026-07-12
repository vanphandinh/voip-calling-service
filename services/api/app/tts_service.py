"""Vietnamese Text-to-Speech service.

Supports multiple backends:
- gTTS (Google Text-to-Speech): Cloud-based, best quality, requires internet.
- Zalo AI TTS: 6 Vietnamese voices (Northern/Southern, Male/Female).
- espeak-ng: Offline fallback, robotic but always available.
"""

import hashlib
import http.cookiejar
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from .config import TtsConfig

logger = logging.getLogger("wcs.tts")


class TTSException(Exception):
    """Raised when TTS synthesis fails."""


class TTSService:
    """Vietnamese TTS abstraction over multiple backends."""

    # Supported backends
    BACKEND_GTTTS = "gtts"
    BACKEND_ZALO = "zalo"
    BACKEND_ESPEAK = "espeak"

    # Zalo API constants
    _ZALO_PAGE_URL = "https://ai.zalo.solutions/products/text-to-audio-converter"
    _ZALO_API_URL = "https://ai.zalo.solutions/api/demo/v1/tts/synthesize"

    def __init__(self, config: TtsConfig) -> None:
        self._config = config
        self._engine = config.engine
        self._zalo_cookie: Optional[str] = None

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _cache_key(self, text: str) -> str:
        """Build a deterministic cache key from text + current TTS config.

        Only Zalo includes speaker_id/speed in the key because those affect
        the output.  gTTS and espeak have no variable voice parameters.
        """
        engine = self._config.engine
        if engine == "zalo":
            raw = f"{text}|zalo|{self._config.zalo_speaker_id}|{self._config.zalo_speed}"
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

        # Build ordered list of backends to try based on config.
        # Fallback chain: configured primary → gTTS → espeak (last resort)
        backends: list[tuple[str, object]] = []
        if self._config.use_gtts:
            backends.append(("gTTS", self._synthesize_gtts))
        if self._config.use_zalo:
            backends.append(("Zalo", self._synthesize_zalo))

        # Try configured backends in order
        primary_success = False
        for name, method in backends:
            try:
                result = method(text, wav_path)
                # First listed backend is the primary — only that one is cached
                primary_success = (name == backends[0][0])
                break
            except Exception as exc:
                logger.warning("%s failed (%s), falling back", name, exc)

        if not primary_success:
            # Fallback: gTTS (if not already the primary engine)
            if not self._config.use_gtts:
                try:
                    logger.info("Attempting gTTS as fallback")
                    return self._synthesize_gtts(text, wav_path)
                except Exception as exc:
                    logger.warning("gTTS fallback failed (%s)", exc)

            # Last resort: espeak always available offline
            try:
                logger.info("Attempting espeak as last-resort fallback")
                return self._synthesize_espeak(text, wav_path)
            except Exception as exc:
                raise TTSException(
                    f"All TTS backends failed for text: '{text[:50]}...'"
                ) from exc

        # --- Cache: save successful primary-engine result ---
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
        self._engine = config.engine
        if old_engine != config.engine or old_speaker != config.zalo_speaker_id:
            self._zalo_cookie = None
            logger.debug("Zalo cookie cleared due to config change")

    def get_duration(self, wav_path: Path) -> float:
        """Return the duration of a WAV file in seconds.

        Uses ffprobe if available, otherwise estimates from file size.
        """
        # Try ffprobe first (accurate)
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

        # Fallback: estimate from file size
        # WAV 8000 Hz, 16-bit, mono = 16000 bytes/sec
        file_size = wav_path.stat().st_size
        if file_size > 44:  # skip WAV header
            return (file_size - 44) / 16000.0
        return 0.0

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
            except urllib.request.URLError as exc:
                self._zalo_cookie = None
                raise TTSException(f"Zalo API request failed: {exc}") from exc

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
        else:
            # Should be unreachable, but guard against logic errors
            raise TTSException("Zalo TTS API retry loop exhausted")

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
