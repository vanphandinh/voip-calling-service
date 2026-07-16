"""Application configuration loaded from environment variables."""

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger("wcs.config")


def _env_int(key: str, default: int) -> int:
    """Parse an integer from an env var, returning *default* on failure."""
    val = os.getenv(key)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        logger.warning("Invalid integer %s='%s', using default %d", key, val, default)
        return default


def _env_float(key: str, default: float) -> float:
    """Parse a float from an env var, returning *default* on failure."""
    val = os.getenv(key)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        logger.warning("Invalid float %s='%s', using default %.1f", key, val, default)
        return default


@dataclass
class SipConfig:
    """SIP account and server configuration."""

    domain: str = "sip.linphone.org"
    username: str = "wcs-announcer"
    password: str = ""  # must be set via SIP_PASSWORD env var — validate() catches empty
    transport: str = "tls"
    proxy: str = "sip:sip.linphone.org:5061;transport=tls"
    # NAT traversal — IP advertised in SDP c= line for RTP audio.
    # Leave empty unless behind a known NAT (Linphone handles this automatically).
    nat_address: str = ""
    # Fixed RTP port range (avoids random port assignment).
    rtp_port_min: int = 10000
    rtp_port_max: int = 10020

    @property
    def identity(self) -> str:
        """Full SIP identity URI."""
        return f"sip:{self.username}@{self.domain}"

    @property
    def proxy_uri(self) -> str:
        """SIP proxy URI with transport."""
        proxy = self.proxy or f"sip:{self.domain}"
        if "transport=" not in proxy:
            proxy = f"{proxy};transport={self.transport}"
        return proxy


@dataclass
class TtsConfig:
    """Text-to-Speech configuration."""

    engine: str = "gtts"  # zalo | ttsfree | responsivevoice | gtts | valtec
    zalo_speaker_id: int = 1  # Zalo voice ID (1-6)
    zalo_speed: float = 1.0   # Zalo speed (0.8-1.2)
    # ResponsiveVoice
    rv_api_key: str = ""     # API secret from dashboard → X-API-Secret header
    rv_site_id: str = ""     # site identifier → X-API-Key header (required)
    rv_gender: str = ""      # "male" | "female" | "" (API default)
    rv_rate: float = 1.0     # speech speed 0.0-2.0
    rv_pitch: float = 1.0    # voice pitch 0.0-2.0
    # Valtec
    valtec_voice: str = "NF"  # speaker: NF, SF, NM1, NM2, SM
    valtec_hf_token: str = ""  # HuggingFace token (for gated Spaces)
    valtec_speed: float = 1.0  # speech speed 0.5-2.0 (native length_scale)
    # TTSFree (guest — no API key required)
    ttsfree_voice: str = "vi-VN-HoaiMyNeural"  # voice ID
    ttsfree_voice_service: str = "voice_bin"   # hardcoded, not in env (voice_bin = Microsoft Neural)
    ttsfree_speed: int = 0                     # speech speed range -100 to 100 (sent as volume_range)
    ttsfree_pitch: int = 0                     # pitch range -100 to 100
    # Cache
    tts_cache_enabled: bool = True
    tts_cache_dir: str = ""  # empty = use {audio_dir}/.tts_cache
    tts_cache_max_age_days: int = 30  # files older than this are cleaned up

    @property
    def use_gtts(self) -> bool:
        return self.engine == "gtts"

    @property
    def use_zalo(self) -> bool:
        return self.engine == "zalo"

    @property
    def use_responsivevoice(self) -> bool:
        return self.engine == "responsivevoice"

    @property
    def use_valtec(self) -> bool:
        return self.engine == "valtec"

    @property
    def use_ttsfree(self) -> bool:
        return self.engine == "ttsfree"


@dataclass
class CallConfig:
    """Call behavior configuration."""

    timeout: int = 30  # max call duration in seconds
    audio_dir: str = "/audio"


@dataclass
class AppConfig:
    """Top-level application configuration."""

    sip: SipConfig = field(default_factory=SipConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    call: CallConfig = field(default_factory=CallConfig)
    log_level: str = "INFO"
    secret_key: str = ""  # master key for signing API tokens — set via SECRET_KEY env var

    @classmethod
    def from_env(cls) -> "AppConfig":
        """Load configuration from environment variables."""
        return cls(
            sip=SipConfig(
                domain=os.getenv("SIP_DOMAIN", "sip.linphone.org"),
                username=os.getenv("SIP_USERNAME", "wcs-announcer"),
                password=os.getenv("SIP_PASSWORD", ""),
                transport=os.getenv("SIP_TRANSPORT", "tls"),
                proxy=os.getenv("SIP_PROXY", "sip:sip.linphone.org:5061;transport=tls"),
                nat_address=os.getenv("SIP_NAT_ADDRESS", ""),
                rtp_port_min=_env_int("RTP_PORT_MIN", 10000),
                rtp_port_max=_env_int("RTP_PORT_MAX", 10020),
            ),
            tts=TtsConfig(
                engine=os.getenv("TTS_ENGINE", "gtts"),
                zalo_speaker_id=_env_int("ZALO_SPEAKER_ID", 1),
                zalo_speed=_env_float("ZALO_SPEED", 1.0),
                rv_api_key=os.getenv("RV_API_KEY", ""),
                rv_site_id=os.getenv("RV_SITE_ID", ""),
                rv_gender=os.getenv("RV_GENDER", ""),
                rv_rate=_env_float("RV_RATE", 1.0),
                rv_pitch=_env_float("RV_PITCH", 1.0),
                valtec_voice=os.getenv("VALTEC_VOICE", "NF"),
                valtec_hf_token=os.getenv("VALTEC_HF_TOKEN", ""),
                valtec_speed=_env_float("VALTEC_SPEED", 1.0),
                ttsfree_voice=os.getenv("TTSFREE_VOICE", "vi-VN-HoaiMyNeural"),
                ttsfree_speed=_env_int("TTSFREE_SPEED", 0),
                ttsfree_pitch=_env_int("TTSFREE_PITCH", 0),
                tts_cache_enabled=os.getenv("TTS_CACHE_ENABLED", "true").lower() != "false",
                tts_cache_dir=os.getenv("TTS_CACHE_DIR", ""),
                tts_cache_max_age_days=_env_int("TTS_CACHE_MAX_AGE_DAYS", 30),
            ),
            call=CallConfig(
                timeout=_env_int("CALL_TIMEOUT", 30),
                audio_dir=os.getenv("AUDIO_DIR", "/audio"),
            ),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            secret_key=os.getenv("SECRET_KEY", ""),
        )

    def validate(self) -> list[str]:
        """Validate required settings. Returns list of error messages."""
        errors = []
        if not self.sip.domain:
            errors.append("SIP_DOMAIN is required")
        if not self.sip.username:
            errors.append("SIP_USERNAME is required")
        if not self.sip.password:
            errors.append("SIP_PASSWORD is required")
        if self.sip.transport.lower() not in ("tls", "tcp", "udp"):
            errors.append(
                f"SIP_TRANSPORT must be 'tls', 'tcp', or 'udp', got '{self.sip.transport}'"
            )
        if self.sip.proxy and not self.sip.proxy.startswith("sip:"):
            errors.append("SIP_PROXY must start with 'sip:'")
        if self.sip.proxy and "transport=" in self.sip.proxy:
            proxy_transport = (
                self.sip.proxy.split("transport=")[1].split(";")[0].strip().lower()
            )
            if proxy_transport != self.sip.transport.lower():
                errors.append(
                    f"SIP transport '{self.sip.transport}' conflicts with proxy transport "
                    f"'{proxy_transport}'. Set SIP_TRANSPORT to '{proxy_transport}' or "
                    f"remove ';transport=' from SIP_PROXY."
                )
        if self.tts.engine not in ("zalo", "ttsfree", "responsivevoice", "gtts", "valtec"):
            errors.append(
                "TTS_ENGINE must be 'zalo', 'ttsfree', 'responsivevoice', 'gtts', or 'valtec'"
            )
        if self.tts.engine == "zalo":
            if not (1 <= self.tts.zalo_speaker_id <= 6):
                errors.append("ZALO_SPEAKER_ID must be between 1 and 6")
            if not (0.8 <= self.tts.zalo_speed <= 1.2):
                errors.append("ZALO_SPEED must be between 0.8 and 1.2")
        if self.tts.engine == "responsivevoice":
            if not self.tts.rv_api_key:
                errors.append("RV_API_KEY is required when TTS_ENGINE=responsivevoice")
            if not self.tts.rv_site_id:
                errors.append("RV_SITE_ID is required when TTS_ENGINE=responsivevoice")
            if self.tts.rv_gender and self.tts.rv_gender not in ("male", "female"):
                errors.append("RV_GENDER must be 'male' or 'female'")
            if not (0.0 <= self.tts.rv_rate <= 2.0):
                errors.append("RV_RATE must be between 0.0 and 2.0")
            if not (0.0 <= self.tts.rv_pitch <= 2.0):
                errors.append("RV_PITCH must be between 0.0 and 2.0")
        if self.tts.engine == "valtec":
            if not self.tts.valtec_voice or not self.tts.valtec_voice.strip():
                errors.append("VALTEC_VOICE must be a non-empty string")
            if not (0.5 <= self.tts.valtec_speed <= 2.0):
                errors.append("VALTEC_SPEED must be between 0.5 and 2.0")
        if self.tts.engine == "ttsfree":
            if not self.tts.ttsfree_voice or not self.tts.ttsfree_voice.strip():
                errors.append("TTSFREE_VOICE must be a non-empty string")
            if not (-100 <= self.tts.ttsfree_speed <= 100):
                errors.append("TTSFREE_SPEED must be between -100 and 100")
            if not (-100 <= self.tts.ttsfree_pitch <= 100):
                errors.append("TTSFREE_PITCH must be between -100 and 100")
        if not self.secret_key:
            logger.warning(
                "SECRET_KEY is not set — API token authentication is disabled. "
                "Set SECRET_KEY to enable token-based auth."
            )
        return errors
