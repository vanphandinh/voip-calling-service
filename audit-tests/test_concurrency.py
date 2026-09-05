"""H4: concurrent calls must not serialize."""
import os, sys, socket, threading, time, re
sys.path.insert(0, "/home/user/voip-calling-service/services/api")
BIN = "/tmp/audit-tests/bin"
os.environ["PATH"] = BIN + os.pathsep + os.environ["PATH"]
from app.config import SipConfig
from app.sip_controller import SipController, SipConnection, CallResult

def resp(via, frm, to, callid, cseq, status, extra="", body="", ctype=""):
    lines = [f"SIP/2.0 {status}", f"Via: {via}", f"From: {frm}", f"To: {to}",
             f"Call-ID: {callid}", f"CSeq: {cseq}"]
    if extra: lines.append(extra.rstrip("\r\n"))
    if body:
        lines += [f"Content-Type: {ctype}", f"Content-Length: {len(body.encode())}"]
        return ("\r\n".join(lines) + "\r\n\r\n" + body).encode()
    lines.append("Content-Length: 0")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()

def proxy(port, rtp):
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port)); srv.listen(1)
    def handler():
        conn, _ = srv.accept(); conn.settimeout(0.3)
        buf = b""; t_end = time.time() + 25
        while time.time() < t_end:
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
                    conn.sendall(resp(via, frm, to, callid, cseq + " " + method, "200 OK"))
                elif method == "INVITE":
                    time.sleep(0.2)
                    sdp = (f"v=0\r\no=s 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
                           f"c=IN IP4 127.0.0.1\r\nm=audio {rtp} RTP/AVP 0 101\r\na=rtpmap:0 PCMU/8000\r\n")
                    conn.sendall(resp(via, frm, to + ";tag=s", callid, cseq + " " + method, "200 OK",
                                      extra=f"Contact: <sip:s@127.0.0.1:{port}>\r\n",
                                      body=sdp, ctype="application/sdp"))
                elif method == "BYE":
                    conn.sendall(resp(via, frm, to, callid, cseq + " " + method, "200 OK"))
                    return
    threading.Thread(target=handler, daemon=True).start()

proxy(15110, 40120)
proxy(15111, 40122)
cfg = SipConfig(domain="lab", username="wcs", password="pw", transport="tcp",
                proxy="sip:127.0.0.1:15110;transport=tcp", rtp_port_min=10000, rtp_port_max=10004)
# NOTE: SipController uses ONE proxy from config; both calls go to the same proxy port.
results = {}
def do_call(i):
    results[i] = None
sc = SipController(cfg)
t = {}
def call(i, port):
    try:
        c = SipConnection(SipConfig(domain="lab", username="wcs", password="pw", transport="tcp",
                         proxy=f"sip:127.0.0.1:{port};transport=tcp", rtp_port_min=10000 + i*10,
                         rtp_port_max=10000 + i*10 + 4))
        c.connect()
        t0 = time.time()
        r = c.make_call(f"sip:{i}@lab", "/tmp/audit-tests/e2e.wav", timeout=15, repeat=1, repeat_delay=0.5)
        t[i] = (time.time() - t0, r)
        c.disconnect()
    except Exception as e:
        t[i] = (0.0, f"EXC: {e}")

th1 = threading.Thread(target=call, args=(1, 15110))
th2 = threading.Thread(target=call, args=(2, 15111))
t0 = time.time()
th1.start(); th2.start(); th1.join(); th2.join()
total = time.time() - t0
d1, d2 = t[1], t[2]
overlap_ok = total < d1[0] + d2[0] - 1.0
print(f"call1: {d1[0]:.1f}s {d1[1]}, call2: {d2[0]:.1f}s {d2[1]}, total: {total:.1f}s")
print("PASS" if (overlap_ok and d1[1] == CallResult.COMPLETED and d2[1] == CallResult.COMPLETED)
      else "FAIL", "- calls ran concurrently and both completed")

# port allocator capacity
from app.sip_controller import _RtpPortAllocator
a = _RtpPortAllocator(10000, 10020)
got = sorted(a.acquire() for _ in range(11))
print("allocator capacity 11 ports:", got == list(range(10000, 10021, 2)))
