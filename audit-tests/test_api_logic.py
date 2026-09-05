"""Audit round 2: API logic — verifies the FIXES.

C3: rate limiter must actually block
H3: CORS preflight OK, 401 carries CORS headers, /auth/token rate limited
C2: SIP target with CRLF must be rejected at the API layer (422)
H2: SSRF resolver blocks private + CGNAT + mapped-IPv6
"""
import os, sys

os.environ.setdefault("SIP_USERNAME", "wcs-test")
os.environ.setdefault("SIP_PASSWORD", "pw-test-123")
os.environ.setdefault("SECRET_KEY", "audit-master-key-123")
os.environ.setdefault("TTS_CACHE_DIR", "/tmp/audit_tts_cache_r2")
os.environ.setdefault("AUDIO_DIR", "/tmp/audit_audio")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "services", "api"))

from types import SimpleNamespace

PASS, FAIL = [], []

def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")

def hdr(t):
    print(f"\n{'='*70}\n{t}\n{'='*70}")

# ---------------------------------------------------------------------------
hdr("C3: Rate limiter blocks after 10 requests/s")
from app.routes import CALL_RATE_LIMITER, TOKEN_RATE_LIMITER
allowed = sum(CALL_RATE_LIMITER.allow("9.9.9.9") for _ in range(15))
check("15 requests → only 10 allowed", allowed == 10, f"(allowed={allowed})")
check("6th token request within 60s blocked",
      sum(TOKEN_RATE_LIMITER.allow("8.8.8.8") for _ in range(6)) == 5)
check("different IPs independent", CALL_RATE_LIMITER.allow("7.7.7.7") is True)

# ---------------------------------------------------------------------------
hdr("C2: target validation")
from app.models import CallRequest
from pydantic import ValidationError
for evil in ["sip:a@b\r\nX-Injected: 1\r\n", "sip:a@b\nRoute: <sip:x>", 'sip:a"@b',
             "sip:<a@b>", "sip: a@b", "http://a@b"]:
    try:
        CallRequest(target=evil, message="x")
        ok = False
    except ValidationError:
        ok = True
    check(f"reject {evil!r}", ok)
try:
    CallRequest(target="sip:vanphandinh@sip.linphone.org:5061", message="x")
    check("accept normal SIP URI", True)
except ValidationError as e:
    check("accept normal SIP URI", False, str(e))

# ---------------------------------------------------------------------------
hdr("H2: SSRF resolver")
from app.call_manager import CallManager
cases_blocked = [
    ("http://169.254.169.254/latest/meta-data/", "AWS metadata"),
    ("http://127.0.0.1:8000/", "loopback"),
    ("http://10.0.0.5/x", "private v4"),
    ("http://[::1]/x", "loopback v6"),
    ("http://[::ffff:10.0.0.5]/x", "IPv4-mapped private v6"),
    ("http://100.64.0.5/x", "CGNAT 100.64/10"),
]
for url, desc in cases_blocked:
    check(f"blocked: {desc}", CallManager._resolve_public_ip(url) is None)

# ---------------------------------------------------------------------------
hdr("H3: full app — token, CORS, preflight, rate limits")
from fastapi.testclient import TestClient
from app.main import app

with TestClient(app) as client:
    r = client.post("/api/v1/auth/token", json={"secret_key": "audit-master-key-123"})
    check("token issued", r.status_code == 200)
    tok = r.json()["access_token"]
    auth = {"Authorization": f"Bearer {tok}"}

    r = client.get("/api/v1/calls", headers={"Origin": "https://app.example.com"})
    check("401 without token", r.status_code == 401)
    check("401 carries CORS header",
          r.headers.get("access-control-allow-origin") == "*",
          f"({r.headers.get('access-control-allow-origin')!r})")

    r = client.get("/api/v1/calls", headers=auth)
    check("200 with token", r.status_code == 200)

    r = client.options("/api/v1/call", headers={
        "Origin": "https://app.example.com",
        "Access-Control-Request-Method": "POST",
    })
    check("OPTIONS preflight = 200 (not 401)", r.status_code == 200,
          f"({r.status_code})")
    check("preflight has ACAO", r.headers.get("access-control-allow-origin") is not None)

    # C2 at API level
    r = client.post("/api/v1/call", headers=auth,
                    json={"target": "sip:a@b\r\nX-Evil: 1\r\n", "message": "x"})
    check("POST /call with CRLF target → 422", r.status_code == 422, f"({r.status_code})")

    # created_at consistency
    r = client.post("/api/v1/call", headers=auth,
                    json={"target": "sip:x@y.invalid", "message": "probe"})
    body = r.json()
    from app.routes import _get_manager
    # manager record should exist with same created_at (microsecond equality)
    rec = None
    mgr = app.state.call_manager
    with mgr._lock:
        rec = mgr._calls.get(body["call_id"])
    check("call_id from response matches stored record",
          rec is not None and rec.call_id == body["call_id"])

    # /auth/token rate limit (5/min): we already used 1
    codes = [client.post("/api/v1/auth/token", json={"secret_key": f"g{i}"*4}).status_code
             for i in range(6)]
    check("token brute force → 429 appears", 429 in codes, f"({sorted(set(codes))})")

print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", *FAIL, sep="\n  - ")
sys.exit(1 if FAIL else 0)
