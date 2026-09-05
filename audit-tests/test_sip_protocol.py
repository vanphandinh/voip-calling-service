"""Audit round 2: SIP protocol fixes.

H1: CANCEL must reuse the INVITE's branch + CSeq
M1: final-error ACK must carry the real To tag; 486 must be BUSY
M2: response right after 407-retry must be processed (no hang)
C2: make_call must refuse CRLF targets (defense in depth)
M3: plain RTP/AVP answer → ffmpeg must stream plain rtp (not srtp)
"""
import os, sys, socket, threading, time, re, wave, struct, math

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "services", "api"))

# ffmpeg shim for RTP streaming tests (imageio-ffmpeg binary)
BIN = "/tmp/audit-tests/bin"
os.makedirs(BIN, exist_ok=True)
FFMPEG_SRC = "/home/user/voip-calling-service/.venv/lib/python3.11/site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2"
if not os.path.exists(f"{BIN}/ffmpeg"):
    try:
        os.symlink(FFMPEG_SRC, f"{BIN}/ffmpeg")
    except FileExistsError:
        pass
os.environ["PATH"] = BIN + os.pathsep + os.environ["PATH"]

from app.config import SipConfig
from app.sip_controller import SipConnection, CallResult

WAV = "/tmp/audit-tests/tone.wav"
if not os.path.exists(WAV):
    with wave.open(WAV, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes(b"".join(struct.pack("<h", int(12000*math.sin(2*math.pi*440*i/8000)))
                               for i in range(1600)))

PASS, FAIL = [], []
def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")

def resp(via, frm, to, callid, cseq, status, extra="", body="", ctype=""):
    lines = [f"SIP/2.0 {status}", f"Via: {via}", f"From: {frm}", f"To: {to}",
             f"Call-ID: {callid}", f"CSeq: {cseq}"]
    if extra: lines.append(extra.rstrip("\r\n"))
    if body:
        lines += [f"Content-Type: {ctype}", f"Content-Length: {len(body.encode())}"]
        return ("\r\n".join(lines) + "\r\n\r\n" + body).encode()
    lines.append("Content-Length: 0")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


class Proxy(threading.Thread):
    """Scripted SIP proxy; records everything the client sends."""
    def __init__(self, port, mode):
        super().__init__(daemon=True)
        self.mode = mode
        self.port = port
        self.msgs = []          # (method, cseq, via, to, raw)
        self.done = threading.Event()

    def run(self):
        srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.port)); srv.listen(1)
        conn, _ = srv.accept(); conn.settimeout(0.3)
        buf = b""; deadline = time.time() + 30
        invite_branch2 = None; invite_cseq2 = None; invite_cseq1 = None
        answered = False
        while time.time() < deadline:
            try:
                d = conn.recv(65536)
                if not d: break
                buf += d
            except socket.timeout:
                d = b""
            while b"\r\n\r\n" in buf:
                head, _, rest = buf.partition(b"\r\n\r\n")
                text = head.decode(errors="replace")
                m = re.search(r"Content-Length:\s*(\d+)", text, re.I)
                clen = int(m.group(1)) if m else 0
                while len(rest) < clen:
                    try: rest += conn.recv(65536)
                    except socket.timeout: break
                buf = rest[clen:]
                via = re.search(r"^Via:\s*(.+)$", text, re.M).group(1).rstrip("\r")
                callid = re.search(r"^Call-ID:\s*(\S+)", text, re.M).group(1)
                cseq, method = re.search(r"^CSeq:\s*(\d+)\s+(\w+)", text, re.M).groups()
                frm = re.search(r"^From:\s*(.+)$", text, re.M).group(1).rstrip("\r")
                to = re.search(r"^To:\s*(.+)$", text, re.M).group(1).rstrip("\r")
                self.msgs.append((method, cseq, via, to, text))
                print(f"    [proxy] {method} cseq={cseq}")

                if method == "REGISTER":
                    conn.sendall(resp(via, frm, to, callid, cseq, "200 OK"))
                elif method == "INVITE":
                    if invite_cseq1 is None:
                        invite_cseq1 = cseq
                        if self.mode == "c407":
                            ch = 'Digest realm="lab", nonce="abc123", algorithm=MD5'
                            conn.sendall(resp(via, frm, to, callid, cseq,
                                              "407 Proxy Authentication Required",
                                              extra=f"Proxy-Authenticate: {ch}\r\n"))
                            continue
                    else:
                        invite_cseq2 = cseq
                        invite_branch2 = re.search(r"branch=([^;]+)", via).group(1)
                    # never answer
                    conn.sendall(resp(via, frm, to, callid, cseq, "100 Trying"))
                    if not answered:
                        answered = True
                        time.sleep(0.2)
                        conn.sendall(resp(via, frm, to + ";tag=srv", callid, cseq, "180 Ringing"))
                elif method == "CANCEL":
                    branch = re.search(r"branch=([^;]+)", via).group(1)
                    print(f"    [proxy] CANCEL branch={branch}")
                    self.cancel_branch = branch
                    self.cancel_cseq = cseq
                    self.invite2_branch = invite_branch2
                    self.invite2_cseq = invite_cseq2
                    conn.sendall(resp(via, frm, to, callid, cseq, "200 OK"))
                    conn.sendall(resp(via, frm, to + ";tag=srv", callid,
                                      invite_cseq2 or invite_cseq1, "487 Request Terminated"))
                elif method == "ACK":
                    self.last_ack_to = to
                elif method == "BYE":
                    conn.sendall(resp(via, frm, to, callid, cseq, "200 OK"))
                    self.done.set(); return
        self.done.set()


def make_conn(port):
    cfg = SipConfig(domain="lab", username="wcs", password="pw", transport="tcp",
                    proxy=f"sip:127.0.0.1:{port};transport=tcp", rtp_port_min=10000)
    c = SipConnection(cfg); c.connect(); return c


print("="*70)
print("H1/C2: CANCEL matching + target validation")
print("="*70)
p = Proxy(15091, "c407"); p.start(); time.sleep(0.3)
conn = make_conn(15091)

# C2 defense in depth
try:
    conn.make_call("sip:1@lab\r\nX-Evil: 1\r\n", WAV, timeout=3)
    check("make_call rejects CRLF target", False)
except ValueError:
    check("make_call rejects CRLF target", True)

t0 = time.time()
r = conn.make_call("sip:200@lab", WAV, timeout=4, repeat=1, repeat_delay=0.5)
el = time.time() - t0
check("never-answered 407 call returns NO_ANSWER", r == CallResult.NO_ANSWER, f"({r})")
check("returns within ~4s (no hang)", el < 8, f"({el:.1f}s)")
check("CANCEL branch == retry-INVITE branch",
      getattr(p, "cancel_branch", None) == getattr(p, "invite2_branch", None),
      f"(cancel={getattr(p, 'cancel_branch', '?')}, invite2={getattr(p, 'invite2_branch', '?')})")
check("CANCEL CSeq == retry-INVITE CSeq",
      getattr(p, "cancel_cseq", None) == getattr(p, "invite2_cseq", None))
conn.disconnect()

print()
print("="*70)
print("M1: immediate 486 → BUSY + ACK with real To tag")
print("="*70)
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 15092)); srv.listen(1)
state = {}
def busy_handler():
    conn, _ = srv.accept(); conn.settimeout(0.3)
    buf = b""; deadline = time.time() + 15
    while time.time() < deadline:
        try:
            d = conn.recv(65536)
            if not d: break
            buf += d
        except socket.timeout: d = b""
        while b"\r\n\r\n" in buf:
            head, _, rest = buf.partition(b"\r\n\r\n")
            text = head.decode(errors="replace")
            m = re.search(r"Content-Length:\s*(\d+)", text, re.I); clen = int(m.group(1)) if m else 0
            while len(rest) < clen:
                try: rest += conn.recv(65536)
                except socket.timeout: break
            buf = rest[clen:]
            via = re.search(r"^Via:\s*(.+)$", text, re.M).group(1).rstrip("\r")
            callid = re.search(r"^Call-ID:\s*(\S+)", text, re.M).group(1)
            cseq, method = re.search(r"^CSeq:\s*(\d+)\s+(\w+)", text, re.M).groups()
            frm = re.search(r"^From:\s*(.+)$", text, re.M).group(1).rstrip("\r")
            to = re.search(r"^To:\s*(.+)$", text, re.M).group(1).rstrip("\r")
            if method == "REGISTER":
                conn.sendall(resp(via, frm, to, callid, cseq, "200 OK"))
            elif method == "INVITE":
                time.sleep(0.1)
                conn.sendall(resp(via, frm, to + ";tag=busy486tag", callid, cseq, "486 Busy Here"))
            elif method == "ACK":
                state["ack_to"] = to
threading.Thread(target=busy_handler, daemon=True).start()
time.sleep(0.3)
conn = make_conn(15092)
r = conn.make_call("sip:300@lab", WAV, timeout=5, repeat=1, repeat_delay=0.5)
check("486 classified as BUSY (was FAILED)", r == CallResult.BUSY, f"({r})")
time.sleep(0.5)  # give the proxy thread a moment to record the ACK
check("ACK carries real To tag (was placeholder)",
      "busy486tag" in state.get("ack_to", "") and "placeholder" not in state.get("ack_to", ""),
      f"({state.get('ack_to', '?')!r})")
conn.disconnect()

print()
print("="*70)
print("M3: plain RTP/AVP answer → plain rtp stream (no srtp)")
print("="*70)
# capture the ffmpeg command by monkeypatching _stream_rtp
captured = {}
orig = SipConnection._stream_rtp
def spy(self, wav, addr, port, timeout=30, codec=0, use_srtp=True):
    captured["codec"] = codec; captured["srtp"] = use_srtp
    return None  # pretend media failed — fine for this probe
SipConnection._stream_rtp = spy

srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 15093)); srv.listen(1)
def avp_handler():
    conn, _ = srv.accept(); conn.settimeout(0.3)
    buf = b""; deadline = time.time() + 15
    while time.time() < deadline:
        try:
            d = conn.recv(65536)
            if not d: break
            buf += d
        except socket.timeout: d = b""
        while b"\r\n\r\n" in buf:
            head, _, rest = buf.partition(b"\r\n\r\n")
            text = head.decode(errors="replace")
            m = re.search(r"Content-Length:\s*(\d+)", text, re.I); clen = int(m.group(1)) if m else 0
            while len(rest) < clen:
                try: rest += conn.recv(65536)
                except socket.timeout: break
            buf = rest[clen:]
            via = re.search(r"^Via:\s*(.+)$", text, re.M).group(1).rstrip("\r")
            callid = re.search(r"^Call-ID:\s*(\S+)", text, re.M).group(1)
            cseq, method = re.search(r"^CSeq:\s*(\d+)\s+(\w+)", text, re.M).groups()
            frm = re.search(r"^From:\s*(.+)$", text, re.M).group(1).rstrip("\r")
            to = re.search(r"^To:\s*(.+)$", text, re.M).group(1).rstrip("\r")
            if method == "REGISTER":
                conn.sendall(resp(via, frm, to, callid, cseq, "200 OK"))
            elif method == "INVITE":
                time.sleep(0.1)
                sdp = ("v=0\r\no=s 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
                       "c=IN IP4 127.0.0.1\r\nm=audio 40093 RTP/AVP 8 101\r\n"
                       "a=rtpmap:8 PCMA/8000\r\n")
                conn.sendall(resp(via, frm, to + ";tag=t9", callid, cseq, "200 OK",
                                  extra="Contact: <sip:s@127.0.0.1:15093>\r\n",
                                  body=sdp, ctype="application/sdp"))
threading.Thread(target=avp_handler, daemon=True).start()
time.sleep(0.3)
conn = make_conn(15093)
r = conn.make_call("sip:400@lab", WAV, timeout=6, repeat=1, repeat_delay=0.5)
check("negotiated PCMA (payload 8)", captured.get("codec") == 8, f"({captured})")
check("plain RTP when answer is RTP/AVP", captured.get("srtp") is False)
check("no-media call fails instead of COMPLETED", r == CallResult.FAILED, f"({r})")
SipConnection._stream_rtp = orig
conn.disconnect()

print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", *FAIL, sep="\n  - ")
sys.exit(1 if FAIL else 0)
