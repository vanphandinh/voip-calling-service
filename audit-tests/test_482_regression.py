"""Regression test for the production 482 Loop Detected failure.

Real-world scenario (sip.linphone.org / Flexisip):
  1. INVITE -> 407 Proxy Authentication Required
  2. Client must ACK the 407 reusing the INVITE's Via branch
     (RFC 3261 §17.1.1.3). A fresh branch made Flexisip treat the ACK
     as a stray request, forward it into itself and reply
     "482 Loop Detected" with CSeq: <n> ACK.
  3. The client's main loop then mistook that 482 for the INVITE's
     final response -> call FAILED after ~1.9s -> no audio ever played.

This test asserts:
  - the 407-ACK reuses the INVITE's Via branch, and
  - a 482 addressed to the ACK no longer kills the call: the client
    filters it out (CSeq method mismatch) and continues to 200 OK.
"""
import os, sys, socket, threading, time, re, wave, struct, math

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "services", "api"))
BIN = "/tmp/audit-tests/bin"
os.makedirs(BIN, exist_ok=True)
# Sửa đường dẫn này nếu venv nằm chỗ khác (binary từ gói imageio-ffmpeg):
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

PORT = 15120
state = {
    "invite_branch": None,     # branch of the FIRST (407-challenged) INVITE
    "ack407_branch": None,     # branch the client used on its 407-ACK
    "retry_invite_branch": None,
    "sent_482_to_ack": False,  # Flexisip-style reaction to a stray ACK branch
    "connected": False,
    "stream_called": False,
}
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", PORT)); srv.listen(1)


def handler():
    conn, _ = srv.accept(); conn.settimeout(0.3)
    buf = b""; deadline = time.time() + 25
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
            branch = re.search(r"branch=([^;\r]+)", via).group(1)
            print(f"    [proxy] {method} cseq={cseq} branch={branch}")

            if method == "REGISTER":
                conn.sendall(resp(via, frm, to, callid, cseq + " " + method, "200 OK"))
            elif method == "INVITE":
                if state["invite_branch"] is None:
                    state["invite_branch"] = branch
                    ch = 'Digest realm="linphone.org", nonce="n0nce123", algorithm=MD5'
                    conn.sendall(resp(via, frm, to, callid, cseq + " " + method,
                                      "407 Proxy Authentication Required",
                                      extra=f"Proxy-Authenticate: {ch}\r\n"))
                else:
                    state["retry_invite_branch"] = branch
                    # Flexisip timing: a 482-to-the-ACK (old bug) reaches the
                    # client BEFORE the 100 Trying of the retried INVITE, so
                    # it is the first message the INVITE loop sees. Without
                    # this gap the 482 could be buried in the same TCP read
                    # as the 100 and the old code would pass by accident.
                    time.sleep(0.3)
                    conn.sendall(resp(via, frm, to, callid, cseq + " " + method, "100 Trying"))
                    time.sleep(0.15)
                    conn.sendall(resp(via, frm, to + ";tag=6Nmj2N2ymF2SS", callid, cseq + " " + method, "180 Ringing"))
                    time.sleep(0.25)
                    sdp = ("v=0\r\no=vp 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
                           "c=IN IP4 127.0.0.1\r\nm=audio 40120 RTP/AVP 0 101\r\n"
                           "a=rtpmap:0 PCMU/8000\r\n")
                    conn.sendall(resp(via, frm, to + ";tag=6Nmj2N2ymF2SS", callid, cseq + " " + method,
                                      "200 OK",
                                      extra=f"Contact: <sip:vp@127.0.0.1:{PORT}>\r\n",
                                      body=sdp, ctype="application/sdp"))
                    state["connected"] = True
            elif method == "ACK":
                if state["retry_invite_branch"] is None:
                    # This is the ACK for the 407 (before the retry INVITE)
                    state["ack407_branch"] = branch
                    if branch != state["invite_branch"]:
                        # OLD BUG: stray branch -> Flexisip loop-detects it
                        state["sent_482_to_ack"] = True
                        conn.sendall(resp(via, frm, to, callid, f"{cseq} ACK",
                                          "482 Loop Detected"))
                # ACK for 200 OK: no response (correct SIP behaviour)
            elif method == "BYE":
                conn.sendall(resp(via, frm, to, callid, cseq + " " + method, "200 OK"))
                return
    return


# Fake streamer: pretend media streamed fine (this test is about signaling)
def fake_stream(self, wav, addr, port, timeout=30, codec=0, use_srtp=True):
    state["stream_called"] = True
    class P:
        def poll(self): return 0
        returncode = 0
    return P()


SipConnection._stream_rtp = fake_stream
threading.Thread(target=handler, daemon=True).start()
time.sleep(0.3)

cfg = SipConfig(domain="sip.linphone.org", username="thongbao", password="pw",
                transport="tcp", proxy=f"sip:127.0.0.1:{PORT};transport=tls".replace("tls", "tcp"),
                rtp_port_min=10000)
conn = SipConnection(cfg); conn.connect()
t0 = time.time()
result = conn.make_call("sip:vanphandinh@sip.linphone.org", WAV,
                        timeout=12, repeat=1, repeat_delay=0.5)
el = time.time() - t0
time.sleep(0.3)
conn.disconnect()

print()
check("407-ACK reuses the INVITE's Via branch (RFC 3261 §17.1.1.3)",
      state["ack407_branch"] == state["invite_branch"],
      f"(ack={state['ack407_branch']}, invite={state['invite_branch']})")
check("proxy did NOT loop-detect the ACK",
      state["sent_482_to_ack"] is False)
check("call CONNECTED (200 OK processed, media started)",
      state["connected"] and state["stream_called"])
check("result COMPLETED (was FAILED in production)", result == CallResult.COMPLETED,
      f"({result}, {el:.1f}s)")

print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", *FAIL, sep="\n  - ")
sys.exit(1 if FAIL else 0)
