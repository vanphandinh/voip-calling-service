"""SIP controller — raw socket SIP signaling (TLS/TCP/UDP).

Implements REGISTER + INVITE with MD5/SHA-256 digest authentication.
RTP audio is streamed via ffmpeg using PCMU/PCMA (G.711).
Silence padding and realtime pacing are handled by TTSService (padding
is baked into the WAV file so ffmpeg's ``-re`` paces it in real time).

Concurrency model: each call gets its OWN SipConnection + RTP port
(allocated from the configured even-port range), so multiple calls can
run in parallel. A separate long-lived registration connection is
refreshed periodically by a daemon thread and backs the health check.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import socket
import subprocess
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Optional

from .config import SipConfig

logger = logging.getLogger("wcs.sip")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CALL_TIMEOUT_DEFAULT = 30
BUFSIZE = 8192

# Maximum seconds spent waiting for a final response (ringing phase) before
# the call is CANCELled. Bounded so total call time stays predictable.
RINGING_TIMEOUT_MAX = 60

# How far before expiry a registration is refreshed.
REGISTER_REFRESH_MARGIN = 30

# Matches "WWW-Authenticate:" or "Proxy-Authenticate:"
_RE_AUTH = re.compile(r'(?:WWW|Proxy)-Authenticate:\s*Digest\s+([^\r\n]+)', re.I)
_RE_NONCE = re.compile(r'nonce\s*=\s*"([^"]+)"', re.I)
_RE_REALM = re.compile(r'realm\s*=\s*"([^"]+)"', re.I)
_RE_OPAQUE = re.compile(r'opaque\s*=\s*"([^"]*)"', re.I)
_RE_QOP = re.compile(r'qop\s*=\s*"([^"]*)"', re.I)
_RE_ALGORITHM = re.compile(r'algorithm\s*=\s*"?([^",\s]+)"?', re.I)
# Extract status code from response
_RE_STATUS = re.compile(r'^SIP/2\.0\s+(\d{3})', re.M)
# Extract CSeq from any SIP message
_RE_CSEQ = re.compile(r'^CSeq:\s*(\d+)\s+(\w+)', re.M | re.I)
# Extract audio port from SDP (m=audio <port> ...)
_RE_AUDIO_PORT = re.compile(r'^m=audio\s+(\d+)', re.M)
# Extract full SDP media line: m=audio <port> <proto> <payload types...>
_RE_MEDIA_LINE = re.compile(r'^m=audio\s+\d+\s+(\S+)\s+(.+)$', re.M)
# Extract connection address from SDP (c=IN IP4 <addr>)
_RE_CONN_ADDR = re.compile(r'^c=IN\s+IP4\s+([\d.]+)', re.M)
# Extract To tag from SIP response header
_RE_TO_TAG = re.compile(r'^To:\s*[^\r\n]+;tag=([^\s;\r\n]+)', re.M | re.I)
# Extract Contact URI from SIP response header
_RE_CONTACT = re.compile(r'^Contact:\s*<([^>]+)>', re.M | re.I)
# Extract Record-Route URIs from SIP response (may appear multiple times)
_RE_RECORD_ROUTE = re.compile(r'^Record-Route:\s*<([^>]+)>', re.M | re.I)
# Extract Expires from a 200 OK REGISTER response
_RE_EXPIRES = re.compile(r'^Expires:\s*(\d+)', re.M | re.I)
# Request-line method (BYE / CANCEL / INVITE / ACK …)
_RE_REQUEST_LINE = re.compile(r'^([A-Z]+)\s+\S+\s+SIP/2\.0\s*$', re.M)
# Allowed characters for a SIP URI used on the wire (defense in depth —
# the API layer validates too, but make_call() must never emit CRLF).
_TARGET_CHARS_RE = re.compile(r'^[A-Za-z0-9\-._!~*\'()&=+$,;:?@\[\]%]+$')


class CallResult(str, Enum):
    COMPLETED = "completed"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    FAILED = "failed"
    DECLINED = "declined"


def _first_line_method(msg: str) -> Optional[str]:
    """Return the method of the first request line in a SIP message, if any."""
    m = _RE_REQUEST_LINE.search(msg)
    return m.group(1) if m else None


def _sanitize_challenge_value(value: str) -> str:
    """Strip control characters (incl. CRLF) from a challenge parameter."""
    return re.sub(r"[\r\n\0]", "", value) if value else ""


def _sanitize_token(value: str, default: str = "") -> str:
    """Keep only token characters — challenge values get echoed into headers."""
    if not value:
        return default
    cleaned = re.sub(r"[^A-Za-z0-9_\-. ]", "", value)
    return cleaned or default


# ---------------------------------------------------------------------------
# SIP TCP Connection + Signaling
# ---------------------------------------------------------------------------

class SipConnection:
    """One SIP signaling connection (plus its RTP port) with registration state."""

    def __init__(self, config: SipConfig, rtp_port: Optional[int] = None) -> None:
        self._config = config
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._cseq = 1
        self._call_id_prefix = hex(int(time.time() * 1000))[2:]
        self._registered = False
        self._registered_at = 0.0
        self._expires = 600
        self._local_ip = self._detect_local_ip()
        self._transport = self._get_transport()
        self._srtp_key: Optional[str] = None
        self._local_port = 0
        # RTP port for this connection's media stream. Fixed allocation
        # (instead of a hardcoded min+2) lets calls run in parallel.
        self._rtp_port = rtp_port or (config.rtp_port_min + 2)
        # branch/CSeq of the INVITE currently in flight — CANCEL must reuse them
        self._pending_invite_branch: Optional[str] = None
        self._pending_invite_cseq: Optional[int] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_registered(self) -> bool:
        return self._registered

    @property
    def registered_at(self) -> float:
        return self._registered_at

    @property
    def expires(self) -> int:
        return self._expires

    def connect(self) -> None:
        with self._lock:
            if self._sock is not None:
                return
            self._connect()
            self._register()

    def refresh(self) -> None:
        """Re-REGISTER on the existing connection (or reconnect if dead)."""
        with self._lock:
            if self._sock is None:
                self._connect()
            self._register()

    def disconnect(self) -> None:
        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None
            self._registered = False

    def make_call(self, target_sip: str, wav_path: str, timeout: int = CALL_TIMEOUT_DEFAULT,
                  repeat: int = 2, repeat_delay: float = 1.0) -> CallResult:
        with self._lock:
            if not self._registered:
                self._connect()
                self._register()
            return self._invite(target_sip, wav_path, timeout, repeat, repeat_delay)

    # ------------------------------------------------------------------
    # Internal: connection
    # ------------------------------------------------------------------

    def _get_transport(self) -> str:
        """Extract transport from proxy URI: tcp, tls, or udp."""
        proxy = self._config.proxy_uri
        transport = self._config.transport
        if transport == "udp":
            return "udp"
        if transport == "tcp" or ";transport=tcp" in proxy.lower():
            return "tcp"
        if ";transport=tls" in proxy.lower():
            return "tls"
        if ";transport=udp" in proxy.lower():
            return "udp"
        return "tls"

    def _detect_local_ip(self) -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0)
            # Connect to the proxy to discover our local IP on that interface
            proxy = self._config.proxy_uri
            clean = proxy[4:] if proxy.startswith("sip:") else proxy
            clean = clean.split(";")[0]  # "sip.linphone.org:5061"
            parts = clean.rsplit(":", 1)
            host = parts[0]
            port = int(parts[1]) if len(parts) > 1 else 5061
            s.connect((host, port))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def _connect(self) -> None:
        proxy = self._config.proxy_uri
        transport = "tls"
        host = proxy
        port = 5061
        if host.startswith("sip:"):
            host = host[4:]
        if ";transport=" in host:
            parts = host.split(";transport=")
            host = parts[0]
            transport = parts[1].split(";")[0].strip().lower()
        if ";" in host:
            host = host.split(";")[0]
        if ":" in host:
            parts = host.rsplit(":", 1)
            host = parts[0]
            port = int(parts[1])

        try:
            if transport == "tls":
                import ssl
                ctx = ssl.create_default_context()
                raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                raw_sock.settimeout(10)
                self._sock = ctx.wrap_socket(raw_sock, server_hostname=host)
                self._sock.connect((host, port))
                self._sock.settimeout(1)
                self._local_port = self._sock.getsockname()[1]
                logger.info("SIP TLS connected to %s:%d (local port %d)",
                            host, port, self._local_port)
            elif transport == "udp":
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self._sock.bind(("0.0.0.0", 0))
                self._sock.settimeout(1)
                self._proxy_addr = (host, port)
                self._local_port = self._sock.getsockname()[1]
                logger.info("SIP UDP socket ready → %s:%d (local port %d)",
                            host, port, self._local_port)
            else:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._sock.settimeout(10)
                self._sock.connect((host, port))
                self._sock.settimeout(1)
                self._local_port = self._sock.getsockname()[1]
                logger.info("SIP TCP connected to %s:%d (local port %d)",
                            host, port, self._local_port)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to connect to SIP proxy {host}:{port} ({transport}): {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Internal: SIP message helpers
    # ------------------------------------------------------------------

    def _new_call_id(self) -> str:
        return f"{self._call_id_prefix}-{self._cseq}@wcs"

    def _next_cseq(self) -> int:
        n = self._cseq
        self._cseq += 1
        return n

    def _send(self, data: str) -> None:
        if self._sock is None:
            raise RuntimeError("SIP socket is not connected")
        payload = data.encode()
        if self._transport == "udp":
            self._sock.sendto(payload, self._proxy_addr)
        else:
            self._sock.sendall(payload)

    def _recv_until(self, timeout: float = 5.0) -> str:
        """Read SIP response(s) from the socket, return the last complete response.

        For UDP, each ``recv()`` returns one complete SIP datagram.
        For TCP/TLS, we read the stream until ``\\r\\n\\r\\n`` then
        consume any Content-Length body.
        """
        if self._sock is None:
            raise RuntimeError("SIP socket is not connected")

        # --- UDP path: one datagram = one SIP message ---
        if self._transport == "udp":
            self._sock.settimeout(timeout)
            try:
                data, _addr = self._sock.recvfrom(BUFSIZE)
            except socket.timeout:
                data = b""
            self._sock.settimeout(1)
            return data.decode(errors="replace")

        # --- TCP / TLS path: stream-based read ---
        self._sock.settimeout(timeout)
        buf = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = self._sock.recv(BUFSIZE)
                if not chunk:
                    break
                buf += chunk
                if b"\r\n\r\n" in buf:
                    text = buf.decode(errors="replace")
                    cl_match = re.search(r'Content-Length:\s*(\d+)', text, re.I)
                    if cl_match:
                        body_len = int(cl_match.group(1))
                        header_end = buf.find(b"\r\n\r\n") + 4
                        body_so_far = len(buf) - header_end
                        remaining = body_len - body_so_far
                        while remaining > 0:
                            self._sock.settimeout(2)
                            chunk = self._sock.recv(min(remaining, BUFSIZE))
                            if not chunk:
                                break
                            buf += chunk
                            remaining -= len(chunk)
                    break
            except socket.timeout:
                break
        self._sock.settimeout(1)
        return buf.decode(errors="replace")

    def _recv_final(self, method: str, timeout: float = 5.0) -> str:
        """Read messages until a FINAL (>=200) response for *method* arrives.

        Fixes the old behaviour where a provisional 100 Trying and the real
        response in the same TCP read caused the 100 to be mistaken for the
        final answer ("Unexpected REGISTER response: 100").
        """
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            resp = self._recv_until(max(0.5, min(3.0, remaining)))
            if not resp:
                continue
            last = resp
            statuses = list(_RE_STATUS.finditer(resp))
            cseqs = list(_RE_CSEQ.finditer(resp))
            if statuses and cseqs:
                status = int(statuses[-1].group(1))
                cseq_method = cseqs[-1].group(2).upper()
                if status >= 200 and cseq_method == method.upper():
                    return resp
        return last

    def _hash_hex(self, data: str, algorithm: str) -> str:
        """Hash per the digest algorithm (MD5 / SHA-256 / SHA-512)."""
        algo = (algorithm or "MD5").upper().replace("-", "")
        if algo == "SHA256":
            h = hashlib.sha256
        elif algo == "SHA512":
            h = hashlib.sha512
        else:
            h = hashlib.md5
        return h(data.encode()).hexdigest()

    def _compute_digest(self, username: str, password: str, realm: str,
                        nonce: str, method: str, uri: str,
                        opaque: str = "", qop: str = "",
                        algorithm: str = "MD5", nc: str = "00000001",
                        cnonce: str = "abcdef01") -> str:
        """Compute SIP Digest response per RFC 2617 / RFC 8760."""
        ha1 = self._hash_hex(f"{username}:{realm}:{password}", algorithm)
        if "SESS" in (algorithm or "").upper():
            ha1 = self._hash_hex(f"{ha1}:{nonce}:{cnonce}", algorithm)
        ha2 = self._hash_hex(f"{method}:{uri}", algorithm)
        if qop:
            response = self._hash_hex(
                f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}", algorithm
            )
        else:
            response = self._hash_hex(f"{ha1}:{nonce}:{ha2}", algorithm)
        return response

    # ------------------------------------------------------------------
    # Internal: REGISTER
    # ------------------------------------------------------------------

    def _register(self) -> None:
        """Send REGISTER with authentication."""
        username = self._config.username
        password = self._config.password
        domain = self._config.domain
        identity = self._config.identity
        local_ip = self._local_ip
        local_port = self._local_port or 5060

        if identity.startswith("sip:"):
            id_clean = identity[4:]
        else:
            id_clean = identity

        # Step 1: send initial REGISTER
        call_id = self._new_call_id()
        cseq = self._next_cseq()
        branch = f"z9hG4bK-wcs-{int(time.time()*1000)}"
        if self._transport == "udp":
            contact = f"sip:{username}@{local_ip}:{local_port}"
        else:
            contact = f"sip:{username}@{local_ip}:{local_port};transport={self._transport}"

        req = (
            f"REGISTER sip:{domain} SIP/2.0\r\n"
            f"Via: SIP/2.0/{self._transport.upper()} {local_ip}:{local_port};branch={branch};rport\r\n"
            f"From: <sip:{id_clean}>;tag=wcs-reg\r\n"
            f"To: <sip:{id_clean}>\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: {cseq} REGISTER\r\n"
            f"Contact: <{contact}>\r\n"
            f"Max-Forwards: 70\r\n"
            f"Expires: 600\r\n"
            f"User-Agent: WCS/1.0\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )
        logger.info("Sending REGISTER (CSeq %d)", cseq)
        self._send(req)
        resp = self._recv_final("REGISTER", 5.0)

        status_match = _RE_STATUS.search(resp)
        if not status_match:
            raise RuntimeError(f"No SIP response to REGISTER: {resp[:200]}")
        status = int(status_match.group(1))

        if status == 200:
            self._mark_registered(resp)
            logger.info("SIP registration OK (immediate 200) — %s@%s", username, domain)
            return

        if status != 401:
            raise RuntimeError(f"Unexpected REGISTER response: {status}\n{resp[:500]}")

        # Parse WWW-Authenticate
        auth_match = _RE_AUTH.search(resp)
        if not auth_match:
            raise RuntimeError(f"No WWW-Authenticate header in 401 response:\n{resp[:500]}")

        auth_params = auth_match.group(1)
        nonce_match = _RE_NONCE.search(auth_params)
        realm_match = _RE_REALM.search(auth_params)
        opaque_match = _RE_OPAQUE.search(auth_params)
        qop_match = _RE_QOP.search(auth_params)
        algo_match = _RE_ALGORITHM.search(auth_params)

        nonce = _sanitize_challenge_value(nonce_match.group(1) if nonce_match else "")
        realm = _sanitize_challenge_value(realm_match.group(1) if realm_match else domain)
        opaque = _sanitize_challenge_value(opaque_match.group(1) if opaque_match else "")
        qop = _sanitize_token(qop_match.group(1) if qop_match else "")
        algorithm = _sanitize_token(algo_match.group(1) if algo_match else "MD5", "MD5")

        # Step 2: send authenticated REGISTER
        cseq2 = self._next_cseq()
        branch2 = f"z9hG4bK-wcs-{int(time.time()*1000)}"
        nc = "00000001"
        cnonce = hex(int(time.time() * 1000000))[2:16]

        digest_uri = f"sip:{domain}"
        response_digest = self._compute_digest(
            username, password, realm, nonce, "REGISTER",
            digest_uri, opaque, qop, algorithm, nc, cnonce
        )

        auth_header = (
            f'Digest username="{username}",'
            f'realm="{realm}",'
            f'nonce="{nonce}",'
            f'uri="{digest_uri}",'
            f'response="{response_digest}",'
            f'algorithm={algorithm}'
        )
        if opaque:
            auth_header += f',opaque="{opaque}"'
        if qop:
            auth_header += f',qop={qop},nc={nc},cnonce="{cnonce}"'

        req2 = (
            f"REGISTER sip:{domain} SIP/2.0\r\n"
            f"Via: SIP/2.0/{self._transport.upper()} {local_ip}:{local_port};branch={branch2};rport\r\n"
            f"From: <sip:{id_clean}>;tag=wcs-reg2\r\n"
            f"To: <sip:{id_clean}>\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: {cseq2} REGISTER\r\n"
            f"Contact: <{contact}>\r\n"
            f"Max-Forwards: 70\r\n"
            f"Expires: 600\r\n"
            f"Authorization: {auth_header}\r\n"
            f"User-Agent: WCS/1.0\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )
        logger.info("Sending authenticated REGISTER (CSeq %d)", cseq2)
        self._send(req2)
        resp2 = self._recv_final("REGISTER", 5.0)

        status2_match = _RE_STATUS.search(resp2)
        if not status2_match:
            raise RuntimeError(f"No SIP response to authenticated REGISTER: {resp2[:200]}")
        status2 = int(status2_match.group(1))

        if status2 == 200:
            self._mark_registered(resp2)
            logger.info("SIP registration OK — %s@%s", username, domain)
        else:
            raise RuntimeError(f"REGISTER failed with status {status2}:\n{resp2[:500]}")

    def _mark_registered(self, resp: str) -> None:
        """Record registration time + expiry (from Expires header if present)."""
        self._registered = True
        self._registered_at = time.monotonic()
        exp_match = _RE_EXPIRES.search(resp)
        self._expires = int(exp_match.group(1)) if exp_match else 600

    # ------------------------------------------------------------------
    # Internal: INVITE + call
    # ------------------------------------------------------------------

    def _send_invite(self, target_addr: str, id_clean: str, username: str,
                     sdp: str, sdp_len: int, call_id: str, extra_headers: str = ""
                     ) -> tuple[str, str, int]:
        """Send an INVITE request.

        Returns ``(raw_response, via_branch, cseq)`` so the caller can send
        a matching CANCEL later (the CANCEL MUST reuse the INVITE's branch
        and CSeq to match the transaction — RFC 3261 §9.1).
        """
        local_ip = self._local_ip
        cseq = self._next_cseq()
        branch = f"z9hG4bK-wcs-inv-{int(time.time()*1000)}-{cseq}"
        local_port = self._local_port or 5060
        if self._transport == "udp":
            contact_hdr = f"<sip:{username}@{local_ip}:{local_port}>"
        else:
            contact_hdr = f"<sip:{username}@{local_ip}:{local_port};transport={self._transport}>"

        inv = (
            f"INVITE sip:{target_addr} SIP/2.0\r\n"
            f"Via: SIP/2.0/{self._transport.upper()} {local_ip}:{local_port};branch={branch};rport\r\n"
            f"From: <sip:{id_clean}>;tag=wcs-call\r\n"
            f"To: <sip:{target_addr}>\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: {cseq} INVITE\r\n"
            f"Contact: {contact_hdr}\r\n"
            f"Max-Forwards: 70\r\n"
            f"Subject: WCS Announcement\r\n"
            f"User-Agent: WCS/1.0\r\n"
        )
        if extra_headers:
            inv += extra_headers
        inv += (
            f"Content-Type: application/sdp\r\n"
            f"Content-Length: {sdp_len}\r\n"
            f"\r\n"
            f"{sdp}"
        )
        logger.info("Sending INVITE sip:%s (CSeq %d)", target_addr, cseq)
        self._send(inv)
        return self._recv_until(5.0), branch, cseq

    def _invite(self, target_sip: str, wav_path: str, timeout: int,
                repeat: int = 2, repeat_delay: float = 1.0) -> CallResult:
        """Send INVITE, handle response, stream RTP audio via ffmpeg."""
        username = self._config.username
        password = self._config.password
        domain = self._config.domain
        identity = self._config.identity
        id_clean = identity[4:] if identity.startswith("sip:") else identity
        local_ip = self._local_ip

        # Parse target + defense-in-depth character check (the API layer
        # already validates; make_call() must never put CRLF on the wire).
        target_addr = target_sip[4:] if target_sip.startswith("sip:") else target_sip
        if not _TARGET_CHARS_RE.match(target_addr):
            raise ValueError(f"Invalid SIP target (illegal characters): {target_sip!r}")

        # Build SDP offer with RTP info. Linphone iOS (SDK 5.5) rejects
        # unencrypted RTP/AVP with 488 when SRTP is required — offer
        # SDES-SRTP (RTP/SAVP) but accept a plain RTP/AVP answer too.
        rtp_port = self._rtp_port
        srtp_key = base64.b64encode(os.urandom(30)).decode("ascii")
        self._srtp_key = srtp_key
        sdp = (
            f"v=0\r\n"
            f"o={username} {int(time.time())} {int(time.time())} IN IP4 {local_ip}\r\n"
            f"s=WCS Call\r\n"
            f"c=IN IP4 {self._config.nat_address or local_ip}\r\n"
            f"t=0 0\r\n"
            f"m=audio {rtp_port} RTP/SAVP 0 8 101\r\n"
            f"a=crypto:1 AES_CM_128_HMAC_SHA1_80 inline:{srtp_key}\r\n"
            f"a=rtpmap:0 PCMU/8000\r\n"
            f"a=rtpmap:8 PCMA/8000\r\n"
            f"a=rtpmap:101 telephone-event/8000\r\n"
            f"a=sendrecv\r\n"
        )
        sdp_len = len(sdp.encode())

        # Send initial INVITE
        call_id = self._new_call_id()
        resp, invite_branch, invite_cseq = self._send_invite(
            target_addr, id_clean, username, sdp, sdp_len, call_id
        )
        # Remember the in-flight INVITE transaction — CANCEL needs its exact
        # branch + CSeq (RFC 3261 §9.1) to actually reach the far end.
        self._pending_invite_branch = invite_branch
        self._pending_invite_cseq = invite_cseq

        # Handle 407 Proxy Authentication Required (single retry)
        status_match = _RE_STATUS.search(resp)
        if status_match and int(status_match.group(1)) == 407:
            auth_match = _RE_AUTH.search(resp)
            if auth_match:
                auth_params = auth_match.group(1)
                nonce_match = _RE_NONCE.search(auth_params)
                realm_match = _RE_REALM.search(auth_params)
                opaque_match = _RE_OPAQUE.search(auth_params)
                qop_match = _RE_QOP.search(auth_params)
                algo_match = _RE_ALGORITHM.search(auth_params)

                nonce = _sanitize_challenge_value(nonce_match.group(1) if nonce_match else "")
                realm = _sanitize_challenge_value(realm_match.group(1) if realm_match else domain)
                opaque = _sanitize_challenge_value(opaque_match.group(1) if opaque_match else "")
                qop = _sanitize_token(qop_match.group(1) if qop_match else "")
                algorithm = _sanitize_token(algo_match.group(1) if algo_match else "MD5", "MD5")

                # ACK the 407 (To tag from the response, not a placeholder)
                cseq_match = _RE_CSEQ.search(resp)
                if cseq_match:
                    self._send_ack(call_id, int(cseq_match.group(1)),
                                   id_clean, target_addr,
                                   f"z9hG4bK-wcs-ack-{int(time.time()*1000)}",
                                   to_tag=_to_tag(resp))

                nc = "00000001"
                cnonce = hex(int(time.time() * 1000000))[2:16]
                digest_uri = f"sip:{target_addr}"
                response_digest = self._compute_digest(
                    username, password, realm, nonce, "INVITE",
                    digest_uri, opaque, qop, algorithm, nc, cnonce
                )

                auth_hdr = (
                    f'Proxy-Authorization: Digest username="{username}",'
                    f'realm="{realm}",'
                    f'nonce="{nonce}",'
                    f'uri="{digest_uri}",'
                    f'response="{response_digest}",'
                    f'algorithm={algorithm}'
                )
                if opaque:
                    auth_hdr += f',opaque="{opaque}"'
                if qop:
                    auth_hdr += f',qop={qop},nc={nc},cnonce="{cnonce}"'
                auth_hdr += "\r\n"

                logger.info("Retrying INVITE with Proxy-Authorization")
                # The retry's response (final or provisional) flows into the
                # main loop below via `pending` — it used to be dropped, which
                # hung until the deadline when the proxy answered immediately.
                resp, invite_branch, invite_cseq = self._send_invite(
                    target_addr, id_clean, username, sdp, sdp_len,
                    call_id, extra_headers=auth_hdr
                )
                self._pending_invite_branch = invite_branch
                self._pending_invite_cseq = invite_cseq

        # ---- Main loop: provisional responses → final response → call ----
        pending = resp  # first message(s) to process before reading the socket
        # Ringing phase gets its own bounded deadline (max 60 s or `timeout`)
        ring_deadline = time.monotonic() + min(RINGING_TIMEOUT_MAX, max(1, timeout))
        remote_rtp_addr = ""
        remote_rtp_port = 0

        while time.monotonic() < ring_deadline:
            if pending:
                resp = pending
                pending = ""
            else:
                remaining = ring_deadline - time.monotonic()
                try:
                    resp = self._recv_until(max(0.5, min(3, remaining)))
                except Exception:
                    break

            if not resp:
                continue

            # Remote hung up before we answered
            if _first_line_method(resp) == "BYE":
                logger.info("Remote party sent BYE before answering — %s", target_sip)
                self._send_ok_to_bye(resp, call_id, id_clean, target_addr)
                return CallResult.NO_ANSWER

            status_matches = list(_RE_STATUS.finditer(resp))
            if not status_matches:
                continue
            status = int(status_matches[-1].group(1))

            logger.debug("SIP response: %d", status)

            if status == 100:  # Trying
                continue
            elif status in (180, 183):  # Ringing / Session Progress
                logger.info("Call ringing — %s", target_sip)
                port_match = _RE_AUDIO_PORT.search(resp)
                addr_match = _RE_CONN_ADDR.search(resp)
                if port_match:
                    remote_rtp_port = int(port_match.group(1))
                if addr_match:
                    remote_rtp_addr = addr_match.group(1)
                continue
            elif status == 200:  # OK — connected
                # Extract RTP info from SDP in 200 OK
                port_match = _RE_AUDIO_PORT.search(resp)
                addr_match = _RE_CONN_ADDR.search(resp)
                if port_match:
                    remote_rtp_port = int(port_match.group(1))
                if addr_match:
                    remote_rtp_addr = addr_match.group(1)

                logger.info("Call connected to %s — RTP %s:%d",
                            target_sip, remote_rtp_addr, remote_rtp_port)

                # --- Negotiate codec + security from the SDP answer ---
                codec_payload, codec_name, use_srtp = _negotiate_media(resp)
                logger.info("Negotiated media: %s (%s), srtp=%s",
                            codec_name, codec_payload, use_srtp)

                # Extract To tag, Contact, and Record-Route from 200 OK
                to_tag = _to_tag(resp) or "wcs-call"

                contact_match = _RE_CONTACT.search(resp)
                dialog_uri = contact_match.group(1) if contact_match else target_addr
                if dialog_uri.startswith("sip:"):
                    dialog_uri = dialog_uri[4:]

                route_uris = _RE_RECORD_ROUTE.findall(resp)
                # Route headers must appear in REVERSE order of Record-Route
                route_headers = "".join(
                    f"Route: <{uri}>\r\n" for uri in reversed(route_uris)
                )

                # Send ACK for 200 OK (in-dialog: Contact URI + Route headers)
                ok_cseq_match = _RE_CSEQ.search(resp)
                ok_cseq = int(ok_cseq_match.group(1)) if ok_cseq_match else invite_cseq
                self._send_ack(call_id, ok_cseq, id_clean, target_addr,
                               f"z9hG4bK-wcs-ack-{int(time.time()*1000)}",
                               to_tag=to_tag, route_headers=route_headers,
                               request_uri=dialog_uri)

                # Give the phone a moment to open its media port before streaming
                time.sleep(1.0)

                # Playback deadline: `timeout` seconds from connect (bounded —
                # total call time = ringing wait + timeout, never unbounded).
                deadline = time.monotonic() + timeout
                bye_received = False
                logger.info(
                    "Playback starting: deadline=%.1fs timeout=%ds repeat=%d",
                    timeout, timeout, repeat,
                )
                for rep in range(repeat):
                    if bye_received or time.monotonic() >= deadline:
                        if bye_received:
                            logger.info("Skipping repeat %d/%d — remote hung up", rep + 1, repeat)
                        else:
                            logger.info("Deadline reached before repeat %d/%d, stopping",
                                        rep + 1, repeat)
                        break

                    logger.info("Starting playback %d/%d", rep + 1, repeat)
                    rtp_proc = self._stream_rtp(
                        wav_path, remote_rtp_addr, remote_rtp_port,
                        max(1, int(deadline - time.monotonic())),
                        codec=codec_payload, use_srtp=use_srtp,
                    )

                    if rtp_proc is None:
                        # Media failure must NOT be reported as COMPLETED —
                        # the callee heard silence at best.
                        logger.error("RTP streaming unavailable — aborting call %s", call_id)
                        self._send_bye(call_id, id_clean, target_addr, to_tag=to_tag,
                                       route_headers=route_headers,
                                       request_uri=dialog_uri)
                        return CallResult.FAILED

                    # Wait for ffmpeg to finish, deadline, or remote BYE
                    while time.monotonic() < deadline:
                        if rtp_proc and rtp_proc.poll() is not None:
                            logger.info("ffmpeg exited (rc=%d) — playback %d/%d done",
                                        rtp_proc.returncode, rep + 1, repeat)
                            break
                        remaining = min(3, deadline - time.monotonic())
                        try:
                            resp = self._recv_until(max(0.5, remaining))
                            if resp and _first_line_method(resp) == "BYE":
                                logger.info("Remote party hung up — BYE received during playback %d/%d",
                                            rep + 1, repeat)
                                self._send_ok_to_bye(resp, call_id, id_clean, target_addr)
                                bye_received = True
                                break
                        except Exception:
                            logger.warning(
                                "SIP recv error during playback, continuing...",
                                exc_info=True,
                            )

                    # Stop RTP process (only if it didn't exit naturally)
                    if rtp_proc and rtp_proc.poll() is None:
                        try:
                            rtp_proc.terminate()
                            rtp_proc.wait(timeout=3)
                        except Exception:
                            try:
                                rtp_proc.kill()
                            except Exception:
                                pass
                        logger.info("RTP streaming stopped (forced)")

                    if bye_received:
                        break

                    # Break between repeats (skip after the last one)
                    if rep < repeat - 1:
                        logger.info("Waiting %.1fs before next repeat...", repeat_delay)
                        delay_end = time.monotonic() + repeat_delay
                        while time.monotonic() < delay_end:
                            try:
                                resp = self._recv_until(min(0.5, max(0.05, delay_end - time.monotonic())))
                                if resp and _first_line_method(resp) == "BYE":
                                    logger.info("Remote party hung up — BYE received during delay")
                                    self._send_ok_to_bye(resp, call_id, id_clean, target_addr)
                                    bye_received = True
                                    break
                            except Exception:
                                break

                if bye_received:
                    logger.info("Call ended by remote — %s", target_sip)
                    return CallResult.COMPLETED

                # Allow buffered RTP packets to reach the receiver before BYE
                time.sleep(1.0)

                logger.info(
                    "Sending BYE — uri=sip:%s to_tag=%s route=%s",
                    dialog_uri, to_tag, route_uris if route_uris else "(none)",
                )
                self._send_bye(call_id, id_clean, target_addr, to_tag=to_tag,
                               route_headers=route_headers,
                               request_uri=dialog_uri)

                try:
                    bye_resp = self._recv_until(3.0)
                    if bye_resp:
                        bye_status_match = _RE_STATUS.search(bye_resp)
                        bye_status = int(bye_status_match.group(1)) if bye_status_match else 0
                        if bye_status == 200:
                            logger.info("BYE acknowledged (200 OK) — %s", target_sip)
                        else:
                            logger.warning(
                                "BYE response: %d — %s\nResponse:\n%s",
                                bye_status, target_sip, bye_resp[:300],
                            )
                    else:
                        logger.warning("No response to BYE — %s", target_sip)
                except Exception as exc:
                    logger.warning("Error waiting for BYE response: %s", exc)

                logger.info("Call completed — %s", target_sip)
                return CallResult.COMPLETED
            elif status >= 400:
                # Final error response — ALWAYS ACK it (with the To tag from
                # THIS response, not a placeholder) and classify correctly.
                r_cseq_match = _RE_CSEQ.search(resp)
                r_cseq = int(r_cseq_match.group(1)) if r_cseq_match else invite_cseq
                ack_branch = f"z9hG4bK-wcs-ack-{int(time.time()*1000)}"
                self._send_ack(call_id, r_cseq, id_clean, target_addr, ack_branch,
                               to_tag=_to_tag(resp))
                if status == 486 or status == 600:
                    logger.info("Call busy — %s (%d)", target_sip, status)
                    return CallResult.BUSY
                elif status == 487:
                    logger.info("Call cancelled — %s", target_sip)
                    return CallResult.NO_ANSWER
                elif status == 408:
                    logger.info("Call timeout — %s", target_sip)
                    return CallResult.NO_ANSWER
                elif status == 603:
                    logger.info("Call declined — %s", target_sip)
                    return CallResult.DECLINED
                else:
                    logger.warning("Call failed with %d — %s\nResponse:\n%s",
                                   status, target_sip, resp[:500])
                    return CallResult.FAILED

        # Timeout without answer — CANCEL the pending INVITE transaction.
        # The branch and CSeq MUST be the ones from the last INVITE we sent,
        # otherwise the proxy treats the CANCEL as a new (unknown) transaction
        # and the callee keeps ringing.
        try:
            cancel_branch = self._pending_invite_branch or \
                f"z9hG4bK-wcs-cancel-{int(time.time()*1000)}"
            cancel_cseq = self._pending_invite_cseq or invite_cseq
            local_port = self._local_port or 5060
            cancel = (
                f"CANCEL sip:{target_addr} SIP/2.0\r\n"
                f"Via: SIP/2.0/{self._transport.upper()} {local_ip}:{local_port};branch={cancel_branch};rport\r\n"
                f"From: <sip:{id_clean}>;tag=wcs-call\r\n"
                f"To: <sip:{target_addr}>\r\n"
                f"Call-ID: {call_id}\r\n"
                f"CSeq: {cancel_cseq} CANCEL\r\n"
                f"Max-Forwards: 70\r\n"
                f"Content-Length: 0\r\n"
                f"\r\n"
            )
            logger.info("Sending CANCEL (branch=%s cseq=%s)", cancel_branch, cancel_cseq)
            self._send(cancel)
            self._recv_until(2.0)
        except Exception:
            pass

        return CallResult.NO_ANSWER

    def _send_ack(self, call_id: str, cseq: int, from_id: str, to_addr: str, via_branch: str,
                  to_tag: str = "", route_headers: str = "",
                  request_uri: str = "") -> None:
        """Send SIP ACK.

        Args:
            to_tag: tag from the response being acknowledged. Omitted when
                empty (early-dialog case) — never a fabricated placeholder.
            request_uri: SIP URI for the request line. Defaults to *to_addr*.
                For in-dialog ACK (200 OK), this must be the Contact URI from
                the 200 OK response.
        """
        local_port = self._local_port or 5060
        ack_uri = request_uri or to_addr
        to_line = f"<sip:{to_addr}>;tag={to_tag}" if to_tag else f"<sip:{to_addr}>"
        ack = (
            f"ACK sip:{ack_uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/{self._transport.upper()} {self._local_ip}:{local_port};branch={via_branch};rport\r\n"
            f"From: <sip:{from_id}>;tag=wcs-call\r\n"
            f"To: {to_line}\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: {cseq} ACK\r\n"
            f"{route_headers}"
            f"Max-Forwards: 70\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )
        self._send(ack)

    def _send_bye(self, call_id: str, from_id: str, to_addr: str,
                  to_tag: str = "placeholder", route_headers: str = "",
                  request_uri: str = "") -> None:
        """Send SIP BYE.

        Args:
            request_uri: SIP URI for the request line. Defaults to *to_addr*.
                For in-dialog BYE, this must be the Contact URI from the
                200 OK response.
        """
        cseq = self._next_cseq()
        branch = f"z9hG4bK-wcs-bye-{int(time.time()*1000)}"
        local_port = self._local_port or 5060
        bye_uri = request_uri or to_addr
        bye = (
            f"BYE sip:{bye_uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/{self._transport.upper()} {self._local_ip}:{local_port};branch={branch};rport\r\n"
            f"From: <sip:{from_id}>;tag=wcs-call\r\n"
            f"To: <sip:{to_addr}>;tag={to_tag}\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: {cseq} BYE\r\n"
            f"{route_headers}"
            f"Max-Forwards: 70\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )
        self._send(bye)

    def _send_ok_to_bye(self, bye_msg: str, call_id: str, from_id: str, to_addr: str) -> None:
        """Send 200 OK in response to a remote BYE (remote party hung up)."""
        # Extract CSeq from the received BYE
        cseq_match = _RE_CSEQ.search(bye_msg)
        cseq = cseq_match.group(1) if cseq_match else "1"
        # Extract Via for routing
        via_match = re.search(r'^Via:\s*(.+)$', bye_msg, re.M | re.I)
        local_port = self._local_port or 5060
        via_hdr = via_match.group(1) if via_match else \
            f"SIP/2.0/{self._transport.upper()} {self._local_ip}:{local_port};branch=z9hG4bK-dummy"
        # Mirror tags: 200 OK's From = BYE's From (with its tag),
        # 200 OK's To = BYE's To (with our tag).
        to_tag_match = _RE_TO_TAG.search(bye_msg)
        from_tag = ""
        fm_match = re.search(r'^From:\s*[^\r\n]+;tag=([^\s;\r\n]+)', bye_msg, re.M | re.I)
        if fm_match:
            from_tag = fm_match.group(1)
        to_tag = to_tag_match.group(1) if to_tag_match else "wcs-call"
        from_line = f"<sip:{to_addr}>;tag={from_tag}" if from_tag else f"<sip:{to_addr}>"
        ok_resp = (
            f"SIP/2.0 200 OK\r\n"
            f"Via: {via_hdr}\r\n"
            f"From: {from_line}\r\n"
            f"To: <sip:{from_id}>;tag={to_tag}\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: {cseq} BYE\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )
        self._send(ok_resp)
        logger.debug("Sent 200 OK in response to remote BYE")

    # ------------------------------------------------------------------
    # RTP audio streaming (ffmpeg)
    # ------------------------------------------------------------------

    def _stream_rtp(self, wav_path: str, dst_addr: str, dst_port: int,
                    timeout: int = 30, codec: int = 0, use_srtp: bool = True
                    ) -> Optional[subprocess.Popen]:
        """Start streaming WAV as RTP via ffmpeg. Returns the process handle.

        The WAV already contains its trailing silence (padded by TTSService
        at conversion time), so NO apad filter is applied here — `-re` paces
        the whole file in real time.

        Args:
            codec: negotiated payload type (0 = PCMU/pcm_mulaw, 8 = PCMA/pcm_alaw).
            use_srtp: encrypt with SDES-SRTP (only when the answer offered it).
        """
        if not dst_addr or not dst_port:
            logger.warning("No RTP destination, skipping audio")
            return None

        wav = Path(wav_path)
        if not wav.exists() or wav.stat().st_size < 100:
            logger.error("WAV file missing or empty: %s", wav_path)
            return None

        rtp_port = self._rtp_port
        srtp_key = self._srtp_key if use_srtp else None
        scheme = "srtp" if srtp_key else "rtp"
        acodec = "pcm_alaw" if codec == 8 else "pcm_mulaw"

        logger.info(
            "Streaming %s audio (%s) to %s:%d from WAV %s (local RTP port %d, timeout=%ds)",
            scheme.upper(), acodec, dst_addr, dst_port, wav_path, rtp_port, timeout,
        )
        cmd = [
            "ffmpeg", "-y",
            "-re",
            "-i", str(wav),
            "-acodec", acodec,
            "-ar", "8000",
            "-ac", "1",
            "-f", "rtp",
            "-t", str(timeout),
            "-flush_packets", "1",
            "-loglevel", "error",
        ]
        if srtp_key:
            cmd += [
                "-srtp_out_suite", "AES_CM_128_HMAC_SHA1_80",
                "-srtp_out_params", srtp_key,
            ]
        cmd.append(
            f"{scheme}://{dst_addr}:{dst_port}?localrtpport={rtp_port}&pkt_size=160"
        )
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.error("ffmpeg binary not found — cannot stream RTP audio")
            return None
        # Check if ffmpeg started successfully
        time.sleep(0.3)
        if proc.poll() is not None:
            if proc.returncode != 0:
                stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                logger.error("ffmpeg failed to start (rc=%d): %s", proc.returncode, stderr[:500])
                return None
            logger.info("ffmpeg already finished (rc=0), audio streamed in <0.3s")
        # Drain stderr in background so the pipe never fills and blocks ffmpeg
        def _drain_stderr() -> None:
            if proc.stderr:
                for _line in proc.stderr:
                    pass
        threading.Thread(target=_drain_stderr, daemon=True).start()
        return proc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_tag(resp: str) -> str:
    """Extract the To tag from a SIP response ('' when absent)."""
    m = _RE_TO_TAG.search(resp)
    return m.group(1) if m else ""


def _negotiate_media(resp: str) -> tuple[int, str, bool]:
    """Pick codec + security from a 200 OK / 183 SDP answer.

    Returns ``(payload_type, codec_name, use_srtp)``.

    We offered RTP/SAVP with PCMU(0), PCMA(8). If the answer is plain
    RTP/AVP (no a=crypto), we must send PLAIN RTP or the callee hears
    nothing; if the answer only lists PCMA, we must send PCMA.
    """
    media = _RE_MEDIA_LINE.search(resp)
    proto = media.group(1).upper() if media else "RTP/SAVP"
    payloads = []
    if media:
        payloads = [int(p) for p in media.group(2).split() if p.strip().isdigit()]
    answer_has_crypto = "a=crypto" in resp

    use_srtp = "SAVP" in proto or answer_has_crypto

    for pt, name in ((0, "PCMU"), (8, "PCMA")):
        if not payloads or pt in payloads:
            return pt, name, use_srtp
    # Answer lists only unknown codecs — fall back to PCMU and hope.
    logger.warning("SDP answer has no PCMU/PCMA payload; defaulting to PCMU")
    return 0, "PCMU", use_srtp


class _RtpPortAllocator:
    """Thread-safe allocator for even RTP ports within the configured range.

    Each in-flight call leases one port; concurrent calls therefore cap at
    ``(rtp_port_max - rtp_port_min) / 2 + 1`` instead of colliding on the
    old hardcoded ``rtp_port_min + 2``.
    """

    def __init__(self, port_min: int, port_max: int) -> None:
        self._lock = threading.Lock()
        start = port_min if port_min % 2 == 0 else port_min + 1
        self._free: list[int] = list(range(start, port_max + 1, 2))
        self._in_use: set[int] = set()

    def acquire(self) -> int:
        with self._lock:
            while self._free:
                port = self._free.pop(0)
                if port not in self._in_use:
                    self._in_use.add(port)
                    return port
        raise RuntimeError(
            "No free RTP ports in configured range — too many concurrent calls"
        )

    def release(self, port: int) -> None:
        with self._lock:
            self._in_use.discard(port)
            if port not in self._free:
                self._free.append(port)


# ---------------------------------------------------------------------------
# SipController — public API
# ---------------------------------------------------------------------------


class SipController:
    """SIP controller using raw sockets (TLS/TCP/UDP).

    Calls do NOT serialize on a global lock: every make_call() gets its own
    connection + RTP port. A separate registration connection is kept warm
    and refreshed before expiry (backs the /health SIP status).
    """

    def __init__(self, config: SipConfig) -> None:
        self._config = config
        self._reg_conn: Optional[SipConnection] = None
        self._lock = threading.Lock()
        self._ports = _RtpPortAllocator(config.rtp_port_min, config.rtp_port_max)
        self._refresh_stop = threading.Event()
        self._refresh_thread: Optional[threading.Thread] = None

    @property
    def is_registered(self) -> bool:
        """True only while the registration connection is alive AND unexpired."""
        conn = self._reg_conn
        if conn is None or not conn.is_registered:
            return False
        age = time.monotonic() - conn.registered_at
        return age < (conn.expires - REGISTER_REFRESH_MARGIN)

    def connect(self) -> None:
        """Establish (or refresh) the long-lived registration connection."""
        with self._lock:
            if self._reg_conn is not None and self.is_registered:
                return
            if self._reg_conn is not None:
                try:
                    self._reg_conn.disconnect()
                except Exception:
                    pass
            self._reg_conn = SipConnection(self._config)
            self._reg_conn.connect()
            self._ensure_refresh_thread()

    def _ensure_refresh_thread(self) -> None:
        if self._refresh_thread is not None and self._refresh_thread.is_alive():
            return
        self._refresh_stop.clear()

        def _refresh_loop() -> None:
            while not self._refresh_stop.wait(5.0):
                conn = self._reg_conn
                if conn is None:
                    break
                if not conn.is_registered:
                    continue  # disconnected — nothing to refresh
                age = time.monotonic() - conn.registered_at
                if age < (conn.expires - REGISTER_REFRESH_MARGIN):
                    continue
                try:
                    conn.refresh()
                    logger.info("SIP registration refreshed")
                except Exception as exc:
                    logger.warning("SIP re-registration failed: %s", exc)

        self._refresh_thread = threading.Thread(
            target=_refresh_loop, daemon=True, name="sip-registration"
        )
        self._refresh_thread.start()

    def disconnect(self) -> None:
        with self._lock:
            self._refresh_stop.set()
            if self._reg_conn:
                self._reg_conn.disconnect()
                self._reg_conn = None

    def make_call(self, target_sip: str, wav_path: str, timeout: int = CALL_TIMEOUT_DEFAULT,
                  repeat: int = 2, repeat_delay: float = 1.0) -> CallResult:
        """Place a call on a DEDICATED connection (parallel-friendly).

        Raises RuntimeError when no RTP port is free (concurrency cap).
        """
        port = self._ports.acquire()
        conn: Optional[SipConnection] = None
        try:
            conn = SipConnection(self._config, rtp_port=port)
            conn.connect()
            return conn.make_call(target_sip, wav_path, timeout, repeat, repeat_delay)
        finally:
            if conn is not None:
                try:
                    conn.disconnect()
                except Exception:
                    pass
            self._ports.release(port)
