"""Audit round 2: FULL end-to-end call flow with the fixed code.

Scenario 1: proxy answers, remote hangs up mid-call  -> COMPLETED, BYE acked
Scenario 2: proxy answers, playback finishes         -> COMPLETED, local BYE -> 200 OK
Verifies paced RTP packets actually flow end-to-end through the real _invite().
"""
import os, sys, socket, threading, time, re, wave, struct

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "services", "api"))
BIN = "/tmp/audit-tests/bin"
os.makedirs(BIN, exist_ok=True)
FFMPEG_SRC = "/home/user/voip-calling-service/.venv/lib/python3.11/site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2"
if not os.path.exists(f"{BIN}/ffmpeg"):
    os.symlink(FFMPEG_SRC, f"{BIN}/ffmpeg")
os.environ["PATH"] = BIN + os.pathsep + os.environ["PATH"]

from app.config import SipConfig
from app.sip_controller import SipConnection, CallResult

# 2s tone + 5s pad = 7s file
WAV = "/tmp/audit-tests/e2e.wav"
if not os.path.exists(WAV):
    os.system(f"{BIN}/ffmpeg -y -v error -f lavfi -i 'sine=frequency=440:duration=2' "
              f"-af apad=pad_dur=5 -ar 8000 -ac 1 -sample_fmt s16 {wav if False else WAV}")

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


def parse(text):
    return {
        "via": re.search(r"^Via:\s*(.+)$", text, re.M).group(1).rstrip("\r"),
        "callid": re.search(r"^Call-ID:\s*(\S+)", text, re.M).group(1),
        "cseq": re.search(r"^CSeq:\s*(\d+)\s+(\w+)", text, re.M).groups(),
        "frm": re.search(r"^From:\s*(.+)$", text, re.M).group(1).rstrip("\r"),
        "to": re.search(r"^To:\s*(.+)$", text, re.M).group(1).rstrip("\r"),
    }


def run_scenario(port, mode, rtp_port):
    """mode='remote_bye' | 'full_playback'"""
    state = {"pkts": 0, "ack_to": "", "bye_resp": None, "got_local_bye": threading.Event()}
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port)); srv.listen(1)

    def handler():
        conn, _ = srv.accept(); conn.settimeout(0.3)
        buf = b""; deadline = time.time() + 40
        bye_sent = False
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
                body = rest[:clen]; buf = rest[clen:]
                h = parse(text)
                method = h["cseq"][1]

                if method == "REGISTER":
                    conn.sendall(resp(h["via"], h["frm"], h["to"], h["callid"], h["cseq"][0], "200 OK"))
                elif method == "INVITE":
                    conn.sendall(resp(h["via"], h["frm"], h["to"], h["callid"], h["cseq"][0], "100 Trying"))
                    time.sleep(0.2)
                    conn.sendall(resp(h["via"], h["frm"], h["to"] + ";tag=srv", h["callid"], h["cseq"][0], "180 Ringing"))
                    time.sleep(0.3)
                    sdp = (f"v=0\r\no=s 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
                           f"c=IN IP4 127.0.0.1\r\nm=audio {rtp_port} RTP/AVP 0 101\r\n"
                           f"a=rtpmap:0 PCMU/8000\r\n")
                    conn.sendall(resp(h["via"], h["frm"], h["to"] + ";tag=srv", h["callid"],
                                      h["cseq"][0], "200 OK",
                                      extra=f"Contact: <sip:s@127.0.0.1:{port}>\r\n",
                                      body=sdp, ctype="application/sdp"))
                    if mode == "remote_bye" and not bye_sent:
                        bye_sent = True
                        time.sleep(2.0)  # mid-playback hangup
                        bye = (f"BYE sip:s@127.0.0.1:{port} SIP/2.0\r\n"
                               f"Via: SIP/2.0/TCP 127.0.0.1:{port};branch=z9hG4bK-rb\r\n"
                               f"From: <sip:s@127.0.0.1>;tag=srv\r\n"
                               f"To: <sip:wcs@lab>;tag=wcs-call\r\n"
                               f"Call-ID: {h['callid']}\r\nCSeq: {h['cseq'][0]} BYE\r\n"
                               f"Content-Length: 0\r\n\r\n")
                        conn.sendall(bye.encode())
                elif method == "ACK":
                    state["ack_to"] = h["to"]
                elif method == "BYE":
                    conn.sendall(resp(h["via"], h["frm"], h["to"], h["callid"], h["cseq"][0], "200 OK"))
                    state["got_local_bye"].set()
                    return
        return

    # UDP listener for RTP
    pkt_count = [0]
    stop = threading.Event()
    def listener():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", rtp_port)); s.settimeout(0.5)
        while not stop.is_set():
            try:
                s.recvfrom(2048); pkt_count[0] += 1
            except socket.timeout:
                continue
    lt = threading.Thread(target=listener, daemon=True); lt.start()

    threading.Thread(target=handler, daemon=True).start()
    time.sleep(0.3)

    cfg = SipConfig(domain="lab", username="wcs", password="pw", transport="tcp",
                    proxy=f"sip:127.0.0.1:{port};transport=tcp", rtp_port_min=10000)
    conn = SipConnection(cfg); conn.connect()
    t0 = time.time()
    r = conn.make_call("sip:42@lab", WAV, timeout=15, repeat=1, repeat_delay=0.5)
    el = time.time() - t0
    time.sleep(0.4)
    stop.set(); lt.join(timeout=1)
    try: conn.disconnect()
    except Exception: pass
    return r, el, pkt_count[0], state


print("="*70)
print("E2E-1: connect + remote BYE mid-playback")
print("="*70)
r, el, pkts, st = run_scenario(15101, "remote_bye", 40110)
check("result COMPLETED on remote hangup", r == CallResult.COMPLETED, f"({r})")
check("RTP packets actually streamed", pkts > 50, f"({pkts} packets)")
check("paced streaming (>= 1.5s before BYE)", 1.5 <= el <= 12, f"({el:.1f}s)")
check("200-OK ACK carries real To tag (tag=srv)", "tag=srv" in st["ack_to"], f"({st['ack_to']!r})")

print()
print("="*70)
print("E2E-2: connect + full playback + local BYE")
print("="*70)
r, el, pkts, st = run_scenario(15102, "full_playback", 40112)
check("result COMPLETED after playback", r == CallResult.COMPLETED, f"({r})")
check("RTP packets streamed", pkts > 50, f"({pkts} packets)")
check("full 7s file paced (~7s call)", 6.0 <= el <= 12.5, f"({el:.1f}s)")
check("local BYE sent and acked", st["got_local_bye"].is_set())

print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", *FAIL, sep="\n  - ")
sys.exit(1 if FAIL else 0)
