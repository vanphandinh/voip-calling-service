"""Audit round 2: C1 fix — RTP audio must be paced in REAL TIME.

Verifies that a padded WAV streams through the production command
(SipConnection._stream_rtp) at ~1x realtime, and that the padding is
INSIDE the file (TTSService._convert_to_wav).
"""
import os, sys, socket, subprocess, threading, time, wave, struct
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "services", "api"))
BIN = "/tmp/audit-tests/bin"
FFMPEG_SRC = "/home/user/voip-calling-service/.venv/lib/python3.11/site-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2"
os.makedirs(BIN, exist_ok=True)
if not os.path.exists(f"{BIN}/ffmpeg"):
    os.symlink(FFMPEG_SRC, f"{BIN}/ffmpeg")
os.environ["PATH"] = BIN + os.pathsep + os.environ["PATH"]

from app.tts_service import TTSService, APAD_SECS

PASS, FAIL = [], []
def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {detail}")

def hdr(t):
    print(f"\n{'='*70}\n{t}\n{'='*70}")

# ---------------------------------------------------------------------------
hdr("C1a: padding is baked into the converted WAV")
src = "/tmp/audit-tests/src3s.wav"
subprocess.run([f"{BIN}/ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                "-i", "sine=frequency=440:duration=3", "-ar", "8000", "-ac", "1",
                "-sample_fmt", "s16", src], check=True)
out = "/tmp/audit-tests/out3s.wav"
TTSService._convert_to_wav(Path(src), Path(out))
with wave.open(out) as w:
    dur = w.getnframes() / w.getframerate()
check(f"3s input -> {3+APAD_SECS}s output (pad inside file)", abs(dur - (3 + APAD_SECS)) < 0.3,
      f"(duration={dur:.2f}s)")

# ---------------------------------------------------------------------------
hdr("C1b: production stream is realtime-paced")
DUR = 5  # 5s tone + APAD 5s baked into the file = 10s of content
wav = "/tmp/audit-tests/paced.wav"
subprocess.run([f"{BIN}/ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                "-i", f"sine=frequency=440:duration={DUR}", "-af", "apad=pad_dur=5",
                "-ar", "8000", "-ac", "1", "-sample_fmt", "s16", wav], check=True)

from app.config import SipConfig
from app.sip_controller import SipConnection
conn_cfg = SipConfig(domain="lab", username="wcs", password="pw", transport="tcp",
                     proxy="sip:127.0.0.1:1;transport=tcp", rtp_port_min=10000)
conn = SipConnection.__new__(SipConnection)
conn._config = conn_cfg
conn._rtp_port = 10002
conn._srtp_key = None
conn._transport = "tcp"

ts: list[float] = []
stop = threading.Event()
def listener(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", port)); s.settimeout(0.5)
    while not stop.is_set():
        try:
            s.recvfrom(2048)
            ts.append(time.time())
        except socket.timeout:
            continue
th = threading.Thread(target=listener, args=(40100,), daemon=True)
th.start()
time.sleep(0.5)

t0 = time.time()
proc = conn._stream_rtp(wav, "127.0.0.1", 40100, timeout=DUR + 10, codec=0, use_srtp=False)
proc.wait(timeout=DUR + 15)
elapsed = time.time() - t0
time.sleep(0.5)
stop.set()
th.join(timeout=2)

total_content = DUR + APAD_SECS  # 10s
span = ts[-1] - ts[0] if ts else 0
speedup = total_content / elapsed if elapsed else 99
check(f"{total_content}s content streamed in ~{total_content}s (elapsed={elapsed:.2f}s)",
      total_content * 0.75 <= elapsed <= total_content * 1.35, f"(elapsed={elapsed:.2f}s)")
check(f"packet span covers the stream", span >= total_content * 0.7, f"(span={span:.2f}s)")
check(f"no burst: speedup ~= 1x (got {speedup:.2f}x)", 0.75 <= speedup <= 1.35)

print(f"\nRESULT: {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", *FAIL, sep="\n  - ")
sys.exit(1 if FAIL else 0)
