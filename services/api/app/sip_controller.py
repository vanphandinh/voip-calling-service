"""SIP controller — raw socket SIP signaling (TLS/TCP/UDP).

Implements REGISTER + INVITE with MD5 digest authentication.
RTP audio is streamed via ffmpeg using PCMU (G.711).
"""
from __future__ import annotations

import hashlib
import logging
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
# Extract connection address from SDP (c=IN IP4 <addr>)
_RE_CONN_ADDR = re.compile(r'^c=IN\s+IP4\s+([\d.]+)', re.M)

class CallResult(str, Enum):
    COMPLETED = "completed"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    FAILED = "failed"
    DECLINED = "declined"


# ---------------------------------------------------------------------------
# SIP TCP Connection + Signaling
# ---------------------------------------------------------------------------

class SipConnection:
    """Persistent TCP connection to the SIP proxy with registration state."""

    def __init__(self, config: SipConfig) -> None:
        self._config = config
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._cseq = 1
        self._call_id_prefix = hex(int(time.time() * 1000))[2:]
        self._registered = False
        self._local_ip = self._detect_local_ip()
        self._transport = self._get_transport()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_registered(self) -> bool:
        return self._registered

    def connect(self) -> None:
        with self._lock:
            if self._sock is not None:
                return
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
            # Extract host and port from proxy URI (e.g. "sip:sip.linphone.org:5061;transport=tls")
            clean = proxy[4:] if proxy.startswith("sip:") else proxy
            clean = clean.split(";")[0]  # "sip.linphone.org:5061"
            parts = clean.rsplit(":", 1)
            host = parts[0]  # "sip.linphone.org"
            port = int(parts[1]) if len(parts) > 1 else 5061
            s.connect((host, port))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def _connect(self) -> None:
        proxy = self._config.proxy_uri
        # Parse sip:host:port;transport=... → host, port, transport
        # e.g. "sip:sip.linphone.org:5061;transport=tls" → sip.linphone.org, 5061, tls
        transport = "tls"
        host = proxy
        port = 5061
        if host.startswith("sip:"):
            host = host[4:]
        # Extract transport
        if ";transport=" in host:
            parts = host.split(";transport=")
            host = parts[0]
            transport = parts[1].split(";")[0].strip().lower()
        # Extract other params
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
                logger.info("SIP TLS connected to %s:%d", host, port)
            elif transport == "udp":
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                # Bind to a specific port so Contact header is accurate
                self._sock.bind(("0.0.0.0", 0))
                self._sock.settimeout(1)
                self._proxy_addr = (host, port)
                self._udp_port = self._sock.getsockname()[1]
                logger.info("SIP UDP socket ready → %s:%d (local port %d)", host, port, self._udp_port)
            else:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._sock.settimeout(10)
                self._sock.connect((host, port))
                self._sock.settimeout(1)
                logger.info("SIP TCP connected to %s:%d", host, port)
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
                # Check for end of SIP response (double CRLF)
                if b"\r\n\r\n" in buf:
                    # If there's Content-Length, read the body too
                    text = buf.decode(errors="replace")
                    cl_match = re.search(r'Content-Length:\s*(\d+)', text, re.I)
                    if cl_match:
                        body_len = int(cl_match.group(1))
                        # Find header end at BYTE level for accurate offset
                        header_end = buf.find(b"\r\n\r\n") + 4
                        body_so_far = len(buf) - header_end
                        # Read remaining body with loop for TCP fragmentation
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

    def _md5(self, data: str) -> str:
        return hashlib.md5(data.encode()).hexdigest()

    def _compute_digest(self, username: str, password: str, realm: str,
                        nonce: str, method: str, uri: str,
                        opaque: str = "", qop: str = "",
                        algorithm: str = "MD5", nc: str = "00000001",
                        cnonce: str = "abcdef01") -> str:
        """Compute SIP Digest response per RFC 2617."""
        if algorithm.upper() == "MD5" or algorithm.upper() == "MD5-SESS":
            a1 = f"{username}:{realm}:{password}"
        else:
            a1 = f"{username}:{realm}:{password}"
        ha1 = self._md5(a1)
        if algorithm.upper() == "MD5-SESS":
            ha1 = self._md5(f"{ha1}:{nonce}:{cnonce}")
        a2 = f"{method}:{uri}"
        ha2 = self._md5(a2)
        if qop:
            response = self._md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
        else:
            response = self._md5(f"{ha1}:{nonce}:{ha2}")
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

        # Handle identity format: sip:user@domain → extract user and domain parts
        if identity.startswith("sip:"):
            id_clean = identity[4:]
        else:
            id_clean = identity

        # Step 1: send initial REGISTER
        call_id = self._new_call_id()
        cseq = self._next_cseq()
        branch = f"z9hG4bK-wcs-{int(time.time()*1000)}"
        local_port = getattr(self, '_udp_port', 5060)
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
        resp = self._recv_until(5.0)

        # Check for 401
        status_match = _RE_STATUS.search(resp)
        if not status_match:
            raise RuntimeError(f"No SIP response to REGISTER: {resp[:200]}")
        status = int(status_match.group(1))

        if status == 200:
            # Already registered? Unusual but handle it.
            self._registered = True
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

        nonce = nonce_match.group(1) if nonce_match else ""
        realm = realm_match.group(1) if realm_match else domain
        opaque = opaque_match.group(1) if opaque_match else ""
        qop = qop_match.group(1) if qop_match else ""
        algorithm = algo_match.group(1) if algo_match else "MD5"

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
        resp2 = self._recv_until(5.0)

        status2_match = _RE_STATUS.search(resp2)
        if not status2_match:
            raise RuntimeError(f"No SIP response to authenticated REGISTER: {resp2[:200]}")
        status2 = int(status2_match.group(1))

        if status2 == 200:
            self._registered = True
            logger.info("SIP registration OK — %s@%s", username, domain)
        else:
            raise RuntimeError(f"REGISTER failed with status {status2}:\n{resp2[:500]}")

    # ------------------------------------------------------------------
    # Internal: INVITE + call
    # ------------------------------------------------------------------

    def _send_invite(self, target_addr: str, id_clean: str, username: str,
                     sdp: str, sdp_len: int, call_id: str, extra_headers: str = "") -> str:
        """Send an INVITE request, return the raw SIP response."""
        local_ip = self._local_ip
        cseq = self._next_cseq()
        branch = f"z9hG4bK-wcs-inv-{int(time.time()*1000)}"
        local_port = getattr(self, '_udp_port', 5060)
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
        return self._recv_until(5.0)

    def _invite(self, target_sip: str, wav_path: str, timeout: int,
                repeat: int = 2, repeat_delay: float = 1.0) -> CallResult:
        """Send INVITE, handle response, stream RTP audio via ffmpeg."""
        username = self._config.username
        password = self._config.password
        domain = self._config.domain
        identity = self._config.identity
        if identity.startswith("sip:"):
            id_clean = identity[4:]
        else:
            id_clean = identity
        local_ip = self._local_ip

        # Parse target
        target_addr = target_sip
        if target_addr.startswith("sip:"):
            target_addr = target_addr[4:]

        # Build SDP with RTP info
        rtp_port = self._config.rtp_port_min + 2
        sdp = (
            f"v=0\r\n"
            f"o={username} {int(time.time())} {int(time.time())} IN IP4 {local_ip}\r\n"
            f"s=WCS Call\r\n"
            f"c=IN IP4 {self._config.nat_address or local_ip}\r\n"
            f"t=0 0\r\n"
            f"m=audio {rtp_port} RTP/AVP 0 8 101\r\n"
            f"a=rtpmap:0 PCMU/8000\r\n"
            f"a=rtpmap:8 PCMA/8000\r\n"
            f"a=rtpmap:101 telephone-event/8000\r\n"
            f"a=sendrecv\r\n"
        )
        sdp_len = len(sdp.encode())

        # Send initial INVITE
        call_id = self._new_call_id()
        cseq = self._cseq  # snapshot before send (send_invite increments)
        resp = self._send_invite(target_addr, id_clean, username, sdp, sdp_len, call_id)

        # Handle 407 Proxy Authentication Required
        status_match = _RE_STATUS.search(resp)
        if status_match and int(status_match.group(1)) == 407:
            # Extract Proxy-Authenticate challenge
            auth_match = _RE_AUTH.search(resp)
            if auth_match:
                auth_params = auth_match.group(1)
                nonce_match = _RE_NONCE.search(auth_params)
                realm_match = _RE_REALM.search(auth_params)
                opaque_match = _RE_OPAQUE.search(auth_params)
                qop_match = _RE_QOP.search(auth_params)
                algo_match = _RE_ALGORITHM.search(auth_params)

                nonce = nonce_match.group(1) if nonce_match else ""
                realm = realm_match.group(1) if realm_match else domain
                opaque = opaque_match.group(1) if opaque_match else ""
                qop = qop_match.group(1) if qop_match else ""
                algorithm = algo_match.group(1) if algo_match else "MD5"

                # Send ACK for 407
                cseq_match = _RE_CSEQ.search(resp)
                if cseq_match:
                    self._send_ack(call_id, int(cseq_match.group(1)),
                                   id_clean, target_addr, f"z9hG4bK-wcs-ack-{int(time.time()*1000)}")

                # Compute digest and resend INVITE with Proxy-Authorization
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
                resp = self._send_invite(target_addr, id_clean, username, sdp, sdp_len,
                                          call_id, extra_headers=auth_hdr)
        elif status_match and int(status_match.group(1)) >= 400:
            # Other error without 407 challenge
            logger.warning("INVITE failed with %d — %s", int(status_match.group(1)), target_addr)
            return CallResult.FAILED

        # Wait for provisional responses and final response
        deadline = time.monotonic() + timeout
        call_connected = False
        remote_rtp_addr = ""
        remote_rtp_port = 0

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                resp = self._recv_until(min(3, max(0.5, remaining)))
            except Exception:
                break

            if not resp:
                continue

            status_matches = list(_RE_STATUS.finditer(resp))
            if not status_matches:
                continue
            status = int(status_matches[-1].group(1))

            logger.debug("SIP response: %d", status)

            if status == 100:  # Trying
                continue
            elif status == 180 or status == 183:  # Ringing / Session Progress
                logger.info("Call ringing — %s", target_sip)
                # Extract RTP info from SDP if present
                port_match = _RE_AUDIO_PORT.search(resp)
                addr_match = _RE_CONN_ADDR.search(resp)
                if port_match:
                    remote_rtp_port = int(port_match.group(1))
                if addr_match:
                    remote_rtp_addr = addr_match.group(1)
                continue
            elif status == 200:  # OK
                call_connected = True
                # Extract RTP info from SDP in 200 OK
                port_match = _RE_AUDIO_PORT.search(resp)
                addr_match = _RE_CONN_ADDR.search(resp)
                if port_match:
                    remote_rtp_port = int(port_match.group(1))
                if addr_match:
                    remote_rtp_addr = addr_match.group(1)

                logger.info("Call connected to %s — RTP %s:%d", target_sip, remote_rtp_addr, remote_rtp_port)

                # Send ACK for 200 OK
                ok_cseq_match = _RE_CSEQ.search(resp)
                ok_cseq = int(ok_cseq_match.group(1)) if ok_cseq_match else cseq
                self._send_ack(call_id, ok_cseq, id_clean, target_addr,
                               f"z9hG4bK-wcs-ack-{int(time.time()*1000)}")

                # Give the phone a moment to open its media port before streaming
                time.sleep(1.0)

                # Loop playback for `repeat` times within the same SIP session
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
                    )

                    # Wait for ffmpeg to finish, deadline, or remote BYE
                    while time.monotonic() < deadline:
                        if rtp_proc and rtp_proc.poll() is not None:
                            logger.info("ffmpeg exited (rc=%d) — playback %d/%d done",
                                       rtp_proc.returncode, rep + 1, repeat)
                            break
                        remaining = min(3, deadline - time.monotonic())
                        try:
                            resp = self._recv_until(max(0.5, remaining))
                            # Detect remote BYE (recipient hung up)
                            if resp and "BYE " in resp[:80].upper():
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
                        # Check for BYE during the delay too
                        delay_end = time.monotonic() + repeat_delay
                        while time.monotonic() < delay_end:
                            try:
                                resp = self._recv_until(min(0.5, delay_end - time.monotonic()))
                                if resp and "BYE " in resp[:80].upper():
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
                drain_start = time.monotonic()
                time.sleep(1.0)
                logger.info(
                    "RTP drain complete after %.1fs",
                    time.monotonic() - drain_start,
                )

                # Send BYE
                self._send_bye(call_id, id_clean, target_addr)

                # Wait for 200 OK to BYE
                try:
                    self._recv_until(3.0)
                except Exception:
                    pass

                logger.info("Call completed — %s", target_sip)
                return CallResult.COMPLETED
            elif status >= 400:
                # Get CSeq from response for proper ACK
                r_cseq_match = _RE_CSEQ.search(resp)
                r_cseq = int(r_cseq_match.group(1)) if r_cseq_match else cseq
                ack_branch = f"z9hG4bK-wcs-ack-{int(time.time()*1000)}"
                if status == 486:
                    logger.info("Call busy — %s", target_sip)
                    self._send_ack(call_id, r_cseq, id_clean, target_addr, ack_branch)
                    return CallResult.BUSY
                elif status == 487:
                    logger.info("Call cancelled — %s", target_sip)
                    self._send_ack(call_id, r_cseq, id_clean, target_addr, ack_branch)
                    return CallResult.NO_ANSWER
                elif status == 408:
                    logger.info("Call timeout — %s", target_sip)
                    return CallResult.NO_ANSWER
                else:
                    logger.warning("Call failed with %d — %s\nResponse:\n%s", status, target_sip, resp[:500])
                    self._send_ack(call_id, r_cseq, id_clean, target_addr, ack_branch)
                    return CallResult.FAILED

        # Timeout without answer
        # Send CANCEL
        try:
            local_port = getattr(self, '_udp_port', 5060)
            cancel = (
                f"CANCEL sip:{target_addr} SIP/2.0\r\n"
                f"Via: SIP/2.0/{self._transport.upper()} {local_ip}:{local_port};branch=z9hG4bK-wcs-cancel-{int(time.time()*1000)};rport\r\n"
                f"From: <sip:{id_clean}>;tag=wcs-call\r\n"
                f"To: <sip:{target_addr}>\r\n"
                f"Call-ID: {call_id}\r\n"
                f"CSeq: {cseq} CANCEL\r\n"
                f"Max-Forwards: 70\r\n"
                f"Content-Length: 0\r\n"
                f"\r\n"
            )
            self._send(cancel)
            self._recv_until(2.0)
        except Exception:
            pass

        return CallResult.NO_ANSWER

    def _send_ack(self, call_id: str, cseq: int, from_id: str, to_addr: str, via_branch: str) -> None:
        local_port = getattr(self, '_udp_port', 5060)
        ack = (
            f"ACK sip:{to_addr} SIP/2.0\r\n"
            f"Via: SIP/2.0/{self._transport.upper()} {self._local_ip}:{local_port};branch={via_branch};rport\r\n"
            f"From: <sip:{from_id}>;tag=wcs-call\r\n"
            f"To: <sip:{to_addr}>;tag=placeholder\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: {cseq} ACK\r\n"
            f"Max-Forwards: 70\r\n"
            f"Content-Length: 0\r\n"
            f"\r\n"
        )
        self._send(ack)

    def _send_bye(self, call_id: str, from_id: str, to_addr: str) -> None:
        cseq = self._next_cseq()
        branch = f"z9hG4bK-wcs-bye-{int(time.time()*1000)}"
        local_port = getattr(self, '_udp_port', 5060)
        bye = (
            f"BYE sip:{to_addr} SIP/2.0\r\n"
            f"Via: SIP/2.0/{self._transport.upper()} {self._local_ip}:{local_port};branch={branch};rport\r\n"
            f"From: <sip:{from_id}>;tag=wcs-call\r\n"
            f"To: <sip:{to_addr}>;tag=placeholder\r\n"
            f"Call-ID: {call_id}\r\n"
            f"CSeq: {cseq} BYE\r\n"
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
        local_port = getattr(self, '_udp_port', 5060)
        via_hdr = via_match.group(1) if via_match else \
            f"SIP/2.0/{self._transport.upper()} {self._local_ip}:{local_port};branch=z9hG4bK-dummy"
        ok_resp = (
            f"SIP/2.0 200 OK\r\n"
            f"Via: {via_hdr}\r\n"
            f"From: <sip:{to_addr}>;tag=placeholder\r\n"
            f"To: <sip:{from_id}>;tag=wcs-call\r\n"
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
                    timeout: int = 30) -> Optional[subprocess.Popen]:
        """Start streaming WAV as RTP PCMU via ffmpeg. Returns the process handle."""
        if not dst_addr or not dst_port:
            logger.warning("No RTP destination, skipping audio")
            return None

        wav = Path(wav_path)
        if not wav.exists() or wav.stat().st_size < 100:
            logger.error("WAV file missing or empty: %s", wav_path)
            return None

        rtp_port = self._config.rtp_port_min + 2

        logger.info("Streaming RTP audio to %s:%d from WAV %s (local RTP port %d, timeout=%ds)",
                   dst_addr, dst_port, wav_path, rtp_port, timeout)
        proc = subprocess.Popen(
            [
                "ffmpeg", "-y",
                "-re",
                "-i", str(wav),
                "-af", "apad=pad_dur=5",
                "-acodec", "pcm_mulaw",
                "-ar", "8000",
                "-ac", "1",
                "-f", "rtp",
                "-t", str(timeout),
                "-flush_packets", "1",
                "-loglevel", "error",
                f"rtp://{dst_addr}:{dst_port}?localrtpport={rtp_port}&pkt_size=160"
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
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
# SipController — public API
# ---------------------------------------------------------------------------


class SipController:
    """SIP controller using raw sockets (TLS/TCP/UDP)."""

    def __init__(self, config: SipConfig) -> None:
        self._config = config
        self._conn: Optional[SipConnection] = None
        self._lock = threading.Lock()

    @property
    def is_registered(self) -> bool:
        if self._conn is None:
            return False
        return self._conn.is_registered

    def connect(self) -> None:
        with self._lock:
            if self._conn is not None and self._conn.is_registered:
                return
            self._conn = SipConnection(self._config)
            self._conn.connect()

    def disconnect(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.disconnect()
                self._conn = None

    def make_call(self, target_sip: str, wav_path: str, timeout: int = CALL_TIMEOUT_DEFAULT,
                  repeat: int = 2, repeat_delay: float = 1.0) -> CallResult:
        with self._lock:
            # Create a fresh connection for each call to avoid loop detection
            if self._conn:
                self._conn.disconnect()
            self._conn = SipConnection(self._config)
            self._conn.connect()
            return self._conn.make_call(target_sip, wav_path, timeout, repeat, repeat_delay)
