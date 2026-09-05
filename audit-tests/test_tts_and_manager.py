"""Audit round 2: TTS config flow (M4), cache behavior (M5), misc fixes."""
import os, sys, json, time

os.environ.setdefault("SIP_USERNAME", "wcs-test")
os.environ.setdefault("SIP_PASSWORD", "pw-test-123")
os.environ.setdefault("SECRET_KEY", "audit-master-key-123")
os.environ.setdefault("TTS_CACHE_DIR", "/tmp/audit_tts_cache_r2b")
os.environ.setdefault("AUDIO_DIR", "/tmp/audit_audio_r2b")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "services", "api"))

import shutil
shutil.rmtree("/tmp/audit_tts_cache_r2b", ignore_errors=True)

PASS, FAIL = [], []
def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")

def hdr(t):
    print(f"\n{'='*70}\n{t}\n{'='*70}")

# ---------------------------------------------------------------------------
hdr("M4: PUT /tts/config swaps the config OBJECT (change detection works)")
from fastapi.testclient import TestClient
from app.main import app

with TestClient(app) as client:
    # auth is enabled (SECRET_KEY set) — get a token first
    tok = client.post("/api/v1/auth/token", json={"secret_key": "audit-master-key-123"}).json()["access_token"]
    client.headers.update({"Authorization": f"Bearer {tok}"})

    tts_before = app.state.config.tts
    id_before = id(app.state.call_manager._tts._config)
    r = client.put("/api/v1/tts/config", json={"engine": "zalo", "zalo_speaker_id": 4})
    check("PUT /tts/config 200", r.status_code == 200, f"({r.status_code} {r.text[:120]})")
    check("engine changed", r.json()["engine"] == "zalo")
    tts_after = app.state.config.tts
    check("config object REPLACED (identity changed)", tts_after is not tts_before)
    check("TTSService._config == new object", app.state.call_manager._tts._config is tts_after)

    # invalid merged config must be rejected and reverted
    r = client.put("/api/v1/tts/config", json={"zalo_speaker_id": 99})
    check("invalid zalo_speaker_id rejected", r.status_code == 422, f"({r.status_code})")
    r = client.get("/api/v1/tts/config")
    check("config reverted after 422", r.json()["zalo_speaker_id"] == 4)

    # session reset: engine change must clear the ttsfree session state
    svc = app.state.call_manager._tts
    svc._ttsfree_session = object()  # simulate an established session
    svc._ttsfree_process = "abc"
    r = client.put("/api/v1/tts/config", json={"engine": "ttsfree"})
    check("engine→ttsfree 200", r.status_code == 200)
    check("ttsfree session cleared on engine change",
          svc._ttsfree_session is None and svc._ttsfree_process is None)

# ---------------------------------------------------------------------------
hdr("M5: cache — only primary engine result is cached")
from app.config import TtsConfig
from app.tts_service import TTSService
from pathlib import Path
import hashlib

cfg = TtsConfig(engine="gtts", tts_cache_enabled=True,
                tts_cache_dir="/tmp/audit_tts_cache_r2b")
svc = TTSService(cfg)

calls = {"n": 0}
def fake_primary(text, path):
    Path(path).write_bytes(b"PRIMARY" + b"\0" * 200)
def fake_fallback(text, path):
    Path(path).write_bytes(b"FALLBACK" + b"\0" * 200)

# primary fails → fallback succeeds → NOTHING may be cached
orig_gtts = svc._synthesize_gtts
orig_zalo = svc._synthesize_zalo
svc._synthesize_gtts = lambda t, p: (_ for _ in ()).throw(RuntimeError("gtts down"))
svc._synthesize_zalo = fake_fallback
wav = svc.synthesize("xin chao", "/tmp/audit_tests_out/fb.wav")
cache_files = list(Path("/tmp/audit_tts_cache_r2b").glob("*.wav"))
check("fallback output NOT cached under primary key", len(cache_files) == 0,
      f"({[f.name for f in cache_files]})")

# primary succeeds → cached
svc._synthesize_gtts = fake_primary
svc.synthesize("xin chao 2", "/tmp/audit_tests_out/ok.wav")
cache_files = list(Path("/tmp/audit_tts_cache_r2b").glob("*.wav"))
check("primary output cached", len(cache_files) == 1, f"({len(cache_files)} files)")

# cache hit returns content
svc._synthesize_gtts = lambda t, p: (_ for _ in ()).throw(RuntimeError("gtts down"))
wav2 = svc.synthesize("xin chao 2", "/tmp/audit_tests_out/hit.wav")
check("cache hit returns cached bytes", wav2.read_bytes().startswith(b"PRIMARY"))

# ---------------------------------------------------------------------------
hdr("Misc: RTP port allocator + digest SHA-256")
from app.sip_controller import _RtpPortAllocator, _negotiate_media

alloc = _RtpPortAllocator(10000, 10006)  # 4 even ports
ports = [alloc.acquire() for _ in range(4)]
check("allocator hands out distinct even ports",
      ports == [10000, 10002, 10004, 10006], f"({ports})")
try:
    alloc.acquire()
    check("allocator exhausted → RuntimeError", False)
except RuntimeError:
    check("allocator exhausted → RuntimeError", True)
alloc.release(10002)
check("released port is reusable", alloc.acquire() == 10002)

# SHA-256 digest no longer identical to MD5
class C:  # minimal mixin for _compute_digest
    _hash_hex = TTSService.__dict__ and None
from app.sip_controller import SipConnection as SC
dummy = SC.__new__(SC)
r_md5 = dummy._compute_digest("u", "p", "r", "n", "REGISTER", "sip:x", algorithm="MD5")
r_sha = dummy._compute_digest("u", "p", "r", "n", "REGISTER", "sip:x", algorithm="SHA-256")
check("SHA-256 digest differs from MD5 now", r_md5 != r_sha)

# RFC 7616 §3.9.1 test vector — official values
r_vec = dummy._compute_digest(
    username="Mufasa", password="Circle of Life",
    realm="http-auth@example.org",
    nonce="7ypf/xlj9XXwfDPEoM4URrv/xwf94BcCAzFZH4GiTo0v",
    method="GET", uri="/dir/index.html", qop="auth",
    nc="00000001", cnonce="f2/wE4q74E6zIJEtWaHKaf5wv/H5QzzpXusqGemxURZJ",
    algorithm="SHA-256")
expected_sha = "753927fa0e85d155564e2e272a28d1802ca10daf4496794697cf8db5856cb6c1"
check("SHA-256 vector exact (RFC 7616)", r_vec == expected_sha, f"(got {r_vec})")
# MD5 variant of the same example
r_md5_vec = dummy._compute_digest(
    username="Mufasa", password="Circle of Life",
    realm="http-auth@example.org",
    nonce="7ypf/xlj9XXwfDPEoM4URrv/xwf94BcCAzFZH4GiTo0v",
    method="GET", uri="/dir/index.html", qop="auth",
    nc="00000001", cnonce="f2/wE4q74E6zIJEtWaHKaf5wv/H5QzzpXusqGemxURZJ",
    algorithm="MD5")
check("MD5 vector exact (RFC 7616)",
      r_md5_vec == "8ca523f5e9506fed4657c9700eebdbec", f"(got {r_md5_vec})")

# media negotiation
sdp_avp = "m=audio 40000 RTP/AVP 8 101\r\n"
sdp_savp = "a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:abc\r\nm=audio 40000 RTP/SAVP 0 101\r\n"
pt, name, srtp = _negotiate_media(sdp_avp)
check("negotiate: AVP+8 → (8, no srtp)", pt == 8 and srtp is False, f"({pt},{name},{srtp})")
pt, name, srtp = _negotiate_media(sdp_savp)
check("negotiate: SAVP+0 → (0, srtp)", pt == 0 and srtp is True, f"({pt},{name},{srtp})")

print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", *FAIL, sep="\n  - ")
sys.exit(1 if FAIL else 0)
