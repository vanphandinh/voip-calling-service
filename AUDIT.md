# 🔍 Báo cáo Audit toàn diện — VoIP Calling Service (WCS)

> **Ngày audit:** 2026-09-05 · **Commit gốc:** `da16e43` (main)
> **Phạm vi:** toàn bộ mã nguồn `services/api` (7 module Python), Dockerfile, entrypoint, healthcheck, docker-compose, .env.example, README
> **Phương pháp:** đọc tĩnh toàn bộ code + kiểm chứng động (unit probe, fake SIP proxy TCP, capture RTP bằng UDP listener, TestClient FastAPI, pip-audit)
> **Kết quả vòng 1:** 3 Critical, 5 High, 10 Medium + các Low — toàn bộ có bằng chứng tái hiện.
> **Kết quả sau sửa (vòng 2 & 3):** ✅ **66/66 checks PASS — không còn lỗi Critical/High.**
> **Vòng 4 (dọn dead code):** ✅ xóa 5 mục dead code, ruff F401/F841/F811 sạch, full suite vẫn **66/66 PASS**.

## Vòng 4 — Dọn dead code & audit lại (2026-09-05)

Quét bằng `ruff --select F401,F841,F811,F403` + `vulture --min-confidence 60/90` + grep thủ công
từng candidate để phân biệt dead-code thật với false-positive (FastAPI route handlers, pydantic
fields và `_PinnedResolver.resolve` là interface bắt buộc — đều **giữ lại**).

### Đã xóa (5 mục)

| # | Dead code | Lý do chết | File |
|---|---|---|---|
| 1 | `TtsConfig.use_gtts/use_zalo/use_responsivevoice/use_valtec/use_ttsfree` (5 properties) | Không có bất kỳ tham chiếu nào — TTSService so sánh `engine` trực tiếp | `config.py` |
| 2 | `RateLimiter.current_usage()` | Helper thêm ở vòng 2 nhưng không endpoint nào gọi | `routes.py` |
| 3 | `SipConnection._md5()` | Mồ côi sau khi `_compute_digest` chuyển sang `_hash_hex()` (MD5/SHA-256/SHA-512) | `sip_controller.py` |
| 4 | `SipConnection.rtp_port` property | Không ai đọc — chỉ dùng attribute nội bộ `_rtp_port` | `sip_controller.py` |
| 5 | `import shutil` cục bộ trong `_convert_to_wav` | Trùng với module-level import | `tts_service.py` |

### Đã xem xét và GIỮ (không phải dead code)

- `CallResult.DECLINED` — vòng 1 là dead, nhưng sau fix M1 nó được sinh bởi **603 Decline** (đường dẫn thật).
- Tất cả route handlers (`health_check`, `trigger_call`, `create_token`…) — đăng ký qua decorator `@router`.
- `auth_middleware` — đăng ký qua `@app.middleware`.
- `_PinnedResolver.resolve` — triển khai `aiohttp.resolver.AbstractResolver` (gọi bởi aiohttp).
- Pydantic model fields — dùng cho serialization/OpenAPI.

### Kết quả audit lại sau khi xóa

- `ruff --select F401,F841,F811,F403`: **All checks passed** (67 cảnh báo E501 line-too-long là phong cách có sẵn từ code gốc, không phải dead code).
- `vulture --min-confidence 90`: **0 kết quả**.
- Compile + import OK; full test suite **66/66 PASS** (25+19+4+10+8 + concurrency PASS); smoke boot uvicorn: `/health` OK, `PUT /tts/config` hoạt động.

---

## Nhật ký sửa lỗi & xác minh (vòng 2 + 3)

Sau khi áp dụng các fix, toàn bộ code được audit lại bằng 6 bộ test
(`audit-tests/`, chạy: `PYTHONPATH=services/api .venv/bin/python audit-tests/<tên>.py`):

| Bộ test | Kiểm chứng | Kết quả |
|---|---|---|
| `test_api_logic.py` | C3 rate limiter chặn đúng; C2 target CRLF → 422; H2 SSRF chặn CGNAT/mapped-IPv6; H3 preflight 200, 401 có CORS, `/auth/token` bị 429 khi brute-force | **25/25 PASS** |
| `test_tts_and_manager.py` | M4 config object được thay thế + session reset hoạt động + revert khi 422; M5 cache chỉ ghi bởi primary engine + atomic; SHA-256 khớp vector chính thức RFC 7616 (cả MD5); RTP port allocator cấp/thu hồi đúng | **19/19 PASS** |
| `test_rtp_pacing.py` | **C1**: pad nằm trong file (3s→8s); 10s nội dung stream trong 9.50s (**1.05× realtime** — trước fix: 0.47s, burst ~14×) | **4/4 PASS** |
| `test_sip_protocol.py` | H1: CANCEL khớp branch+CSeq của INVITE; C2: `make_call` từ chối target CRLF; M1: 486→BUSY + ACK mang tag thật; M3: answer RTP/AVP → stream plain RTP/PCMA; M6: không có media → FAILED (không COMPLETED) | **10/10 PASS** |
| `test_e2e_call.py` | E2E thật: connect → RTP pace đúng → remote BYE (96 packets/2.6s) → COMPLETED; full playback 7s (438 packets) → BYE local → 200 OK | **8/8 PASS** |
| `test_concurrency.py` | **H4**: 2 cuộc gọi 11.5s chạy song song xong trong 16.5s (trước fix: tuần tự 23s); allocator đủ 11 port | **PASS** |

Ngoài ra: compile + import sạch; boot uvicorn thật → `/health` OK, POST `/call`
không token → 401, target độc → 422, target hợp lệ → 202; `pip-audit` trên
`requirements.txt` từ **44 CVE → 1 CVE không thể đạt tới** (click `click.edit()`
— chỉ dùng bởi CLI gtts-cli mà service không gọi; gTTS 2.5.4 pin `click<8.2`).

### Các fix đã áp dụng theo file

- **`models.py`** — [C2] whitelist regex SIP URI chặn CR/LF, dấu cách, ngoặc kép… ở tầng API.
- **`routes.py`** — [C3] `RateLimiter` ghi nhận timestamp trước khi kiểm tra + giới hạn số key; [H3] rate limit riêng `/auth/token` (5/phút) + 503 khi chưa set SECRET_KEY; [M4] `PUT /tts/config` dựng object mới (`dataclasses.replace`) thay vì mutate trực tiếp object dùng chung.
- **`main.py`** — [H3] CORS middleware đăng ký SAU auth (outermost): preflight 200, 401 mang CORS headers; OPTIONS được bỏ qua trong auth; `allow_credentials=False`.
- **`config.py`** — [H3] `CORS_ORIGINS` env; validate dải RTP port.
- **`tts_service.py`** — [C1] im lặng pad vào **file WAV** (`apad` trong `_convert_to_wav` + Zalo), bỏ apad lúc stream; [M5] chỉ cache khi primary engine thành công + ghi cache atomic (`os.replace`); [Low] bỏ `tempfile.mktemp` → UUID; Zalo retry 401/403 với cookie mới.
- **`call_manager.py`** — [H4] giữ strong reference cho task; [M7] eviction không bao giờ xóa cuộc gọi active; [H2] webhook: resolve→validate→**pin IP** (custom aiohttp resolver), tắt auto-redirect, tự theo redirect ≤4 hop kiểm tra từng hop, chặn thêm CGNAT/reserved/multicast; timestamp response khớp record.
- **`sip_controller.py`** — viết lại lớn: [H1] CANCEL dùng đúng branch/CSeq của INVITE; [M1] mọi final error được ACK với To tag thật, 486/600→BUSY, 603→DECLINED, 408 có ACK; [M2] response ngay sau 407-retry được xử lý (không treo); [M8] ringing deadline riêng (≤60s); [M9] REGISTER đọc final response đúng (không nhầm 100 Trying); [M3] đàm phán codec/SRTP từ SDP answer (PCMU/PCMA, SAVP/AVP); [M6] ffmpeg fail → FAILED + BYE; port RTP cấp theo connection; local port lấy từ `getsockname()`; [Low] SHA-256/SHA-512 digest theo RFC 8760 + sanitize tham số challenge; BYE-before-answer được xử lý.
- **`requirements.txt`** — [H5] fastapi ≥0.121, aiohttp ≥3.14 (28 CVE), starlette mới; Dockerfile nâng setuptools/pip.
- **Ops** — healthcheck dùng port trong container (8000) đúng nghĩa; sửa comment Dockerfile; README: docs `/auth/token`, `CORS_ORIGINS`, cảnh báo auth; `.env.example` thêm `CORS_ORIGINS` + cảnh báo.

**Quyết định thiết kế có chủ ý (không phải lỗi):** auth vẫn tắt khi không có
`SECRET_KEY` (tương thích ngược) nhưng giờ được cảnh báo rõ ở README/.env.example,
`/auth/token` được rate limit, và health endpoint vẫn mở để probe.

---

## Tóm tắt nhanh (phát hiện vòng 1 — đã được sửa toàn bộ)

| # | Mức độ | Lỗi | Vị trí | Trạng thái kiểm chứng |
|---|--------|-----|--------|----------------------|
| C1 | 🔴 CRITICAL | **Âm thanh RTP không phát realtime** — ffmpeg "xả" cả cuộc thông báo nhanh hơn 1.4–14× so với thời gian thực | `sip_controller.py:910–936` | ✅ Đo thực tế |
| C2 | 🔴 CRITICAL | **SIP header injection (CRLF)** qua trường `target` — chèn được header/request tùy ý vào kết nối SIP | `routes.py:111–122`, `sip_controller.py:489–491` | ✅ Tái hiện E2E |
| C3 | 🔴 CRITICAL | **Rate limiter hoàn toàn vô hiệu** — không bao giờ chặn request nào | `routes.py:45–63` | ✅ Tái hiện (15/15 request đều qua) |
| H1 | 🟠 HIGH | **CANCEL sai branch + sai CSeq** → after-timeout API báo `no_answer` nhưng điện thoại **vẫn đổ chuông tiếp** | `sip_controller.py:748–768` | ✅ Tái hiện (branch match = False) |
| H2 | 🟠 HIGH | **SSRF qua webhook**: redirect-following + DNS rebinding vượt qua kiểm tra IP private | `call_manager.py:313–375` | ✅ Phân tích code + test cận |
| H3 | 🟠 HIGH | **Xác thực tắt mặc định + không giới hạn brute-force** `/api/v1/auth/token`; CORS `*` | `main.py:124–127`, `routes.py:292` | ✅ Tái hiện (30 lần đoán key, không 429) |
| H4 | 🟠 HIGH | **Toàn bộ cuộc gọi bị tuần tự hóa** bởi 1 lock → thread pool cạn, API treo khi burst | `sip_controller.py:982–991` | ✅ Phân tích code |
| H5 | 🟠 HIGH | **44 CVE đã biết** trong dependency khóa phiên bản (aiohttp 3.11.10, starlette 0.41.3, click) | `requirements.txt` | ✅ pip-audit |
| M1–M10 | 🟡 MEDIUM | ACK sai/thiếu, mất response sau 407-retry, không đàm phán codec, cache race, update_config chết… | chi tiết bên dưới | ✅ Đa số tái hiện được |

Điểm **đã kiểm tra và đạt**: thuật toán Digest auth đúng theo test vector RFC 2617; luồng cấp/verify token HMAC hoạt động đúng; `localrtpport` của ffmpeg vẫn được chấp nhận (abbreviation matching); compile + import OK trên Python 3.11 + deps trong `requirements.txt`.

---

## 🔴 CRITICAL

### C1. Âm thanh RTP KHÔNG phát realtime — người nghe sẽ nghe nứt/mất tiếng

**Vị trí:** `services/api/app/sip_controller.py` — hàm `_stream_rtp()` (dòng 910–936)

Lệnh ffmpeg hiện tại:

```
ffmpeg -y -re -i message.wav -af apad=pad_dur=5 -acodec pcm_mulaw -ar 8000 -ac 1
       -f rtp -t <timeout> -flush_packets 1 srtp://host:port?localrtpport=10002&pkt_size=160
```

Có **2 vấn đề đo đạc được** (ffmpeg 7.0.2, lệnh y hệt production, đo bằng UDP listener):

| Độ dài audio | Thời gian truyền thực tế | Tốc độ so với realtime |
|---|---|---|
| 1 giây | **0.47 s** | **~2.2–14× nhanh hơn** (gồm cả 5s padding) |
| 2 giây | 1.49 s | ~1.4–5× |
| 5 giây | 4.50 s | ~1.1–2.6× |
| 10 giây | 9.49 s | ~1.05× (phần thoại) + 5s padding bị xả tức thời |

Nguyên nhân gốc:
1. **`-re` chỉ điều tốc luồng *input* (demuxer)** — các frame sinh ra bởi filter `apad` không đi qua demuxer nên **không bị pace**, bị bung ra với tốc độ tối đa (5 giây im lặng = hàng trăm gói RTP dồn trong vài chục ms).
2. **Với file ngắn (≤ 2s), chính phần thoại cũng không được pace đủ** (file 1s truyền xong trong 0.46s).

**Tác động thực tế:** jitter buffer của Linphone (~40–300ms) sẽ tràn; số lượng lớn gói RTP bị rơi ⇒ **thông báo ngắn nghe bị "nứt"/chopped hoặc gần như không nghe được**. Thông báo dài (>5s) thì phần thoại gần đúng realtime nhưng padding xả ồ ạt gây phí băng thông và lệch logic thời gian. Việc test localhost "nghe được" là ảo vì loopback không mất gói.

**Sửa đề xuất:** nén im lặng vào **bên trong file WAV** lúc chuyển đổi (thay vì lúc stream), vì sample thật trong file sẽ được `-re` pace đúng:

```python
# trong _convert_to_wav: thay vì apad lúc stream
subprocess.run(["ffmpeg","-y","-v","error","-i",str(input_path),
                "-af","apad=pad_dur=5",       # pad thành PHẦN FILE
                "-ar","8000","-ac","1","-sample_fmt","s16",str(output_path)], check=True)
# trong _stream_rtp: bỏ "-af apad=pad_dur=5" đi, chỉ còn "-re -i file"
```
(Đã kiểm chứng: `apad` ghi vào **file** cho đúng 15s cho input 10s, và `-re -i file` pace chuẩn ~0.95× realtime.)

---

### C2. SIP header injection (CRLF) qua trường `target`

**Vị trí:** `routes.py:111–122` (chỉ kiểm tra `startswith("sip:")`), `sip_controller.py:489–491` rồi nhúng thẳng vào request line + header `To` (dòng 452, 455).

Pydantic chỉ ràng buộc `min_length=5` cho `target` — **không cấm `\r\n`**. Vì auth mặc định TẮT (H3), bất kỳ ai truy cập được API đều khai thác được.

**Bằng chứng (fake SIP proxy TCP, chạy kèm `audit-tests/`):**

Request gửi với `target = "sip:100@lab.local\r\nX-Injected: pwned\r\n"` — proxy nhận được:

```
INVITE sip:100@lab.local
X-Injected: pwned          ← header được chèn vào request
 SIP/2.0
Via: SIP/2.0/TCP 127.0.0.1:5060;branch=z9hG4bK-wcs-inv-...
From: <sip:wcs@lab.local>;tag=wcs-call
To: <sip:100@lab.local
X-Injected: pwned          ← chèn được cả vào header To
>
```

Kẻ tấn công có thể chèn header tùy ý (Route, Diversion…) hoặc **tiêm hẳn một request SIP thứ hai** vào cùng kết nối TLS (request smuggling), giả mạo danh tính người gọi v.v.

**Sửa đề xuất:** validate `target` bằng regex chặt, ví dụ:
`^sip:[A-Za-z0-9_\-\.\!\~\*\'\(\)\&\=\+\$\,]+@[A-Za-z0-9\.\-\[\]:]+$` (không chứa `%`, `\r`, `\n`, dấu cách), đồng thời strip tham số `;tag=` do client gửi.

---

### C3. Rate limiter vô hiệu 100% — không bao giờ chặn

**Vị trí:** `routes.py:45–63`

```python
bucket = _rate_limit_buckets[client_ip]                    # defaultdict tạo []
_rate_limit_buckets[client_ip] = [t for t in bucket if t >= window_start]
if not _rate_limit_buckets[client_ip]:
    del _rate_limit_buckets[client_ip]
    return True            # ← request ĐẦU TIÊN không được ghi nhận
...
_rate_limit_buckets[client_ip].append(now)   # ← chỉ chạy khi bucket ĐÃ có phần tử
```

Logic "chicken-and-egg": phần tử chỉ được `append` khi bucket **đã có** phần tử, mà bucket mới/empty luôn bị `del` và `return True` trước khi kịp append ⇒ **bucket vĩnh viễn rỗng, không request nào bị chặn**.

**Bằng chứng:** gọi `_check_rate_limit()` 15 lần từ 1 IP (giới hạn 10/s): `allowed=15, blocked=0`.

**Sửa đề xuất:** ghi nhận timestamp **trước** khi kiểm tra, ví dụ:

```python
bucket[:] = [t for t in bucket if t >= window_start]
bucket.append(now)
return len(bucket) <= RATE_LIMIT_MAX
```

---

## 🟠 HIGH

### H1. CANCEL sai `branch` và sai `CSeq` — điện thoại vẫn đổ chuông sau khi hệ thống báo `no_answer`

**Vị trí:** `sip_controller.py:748–768` (khối gửi CANCEL khi hết hạn chờ).

RFC 3261 §9.1: CANCEL **phải** dùng lại đúng `branch` của Via trong INVITE (khớp transaction) và cùng CSeq. Hiện code sinh branch mới `z9hG4bK-wcs-cancel-...`. Tệ hơn: sau 407-retry, INVITE được gửi lại với CSeq = snapshot+1, còn CANCEL dùng snapshot ⇒ lệch 1.

**Bằng chứng (proxy giả, cuộc gọi không bao giờ được trả lời):**

```
CANCEL branch=z9hG4bK-wcs-cancel-1788597255241
retry-INVITE branch=z9hG4bK-wcs-inv-1788597251196
CANCEL CSeq=2   retry-INVITE CSeq=3
>> branch match: False   CSeq match: False
result=CallResult.NO_ANSWER        ← API báo no_answer nhưng UA phía sau vẫn kêu
```

**Sửa:** lưu `invite_branch` (và CSeq của INVITE đang chờ) rồi dùng lại cho CANCEL.

### H2. SSRF qua webhook — kiểm tra IP private bị vòng qua

**Vị trí:** `call_manager.py:313–375`

`_is_private_target()` resolve DNS rồi mới chặn, nhưng:
1. **aiohttp mặc định `allow_redirects=True`** — server callback trả 302 về `http://169.254.169.254/...` (hoặc 127.0.0.1) sẽ được theo mà **không qua kiểm tra lần hai** (kiểm tra TOCTOU/DNS rebinding tương tự).
2. Khoảng trống nhỏ đã test: dải CGNAT `100.64.0.0/10` không bị chặn (`blocked=False`), thiếu `is_reserved/is_multicast`.

**Sửa:** tự resolve và pin IP, kết nối thẳng tới IP đã kiểm tra (tắt redirect: `allow_redirects=False`, tự theo dõi từng hop và kiểm tra lại mỗi hop); thêm `ip.is_reserved`, CGNAT, IPv4-mapped IPv6.

### H3. Auth tắt mặc định + brute-force không giới hạn + CORS mở hoàn toàn

- `main.py:124–127`: nếu `SECRET_KEY` rỗng (mặc định trong compose) → **bỏ qua toàn bộ xác thực**; mọi endpoint (gọi điện, đọc lịch sử, đổi TTS config) mở cho bất kỳ ai chạm được port 8000.
- `routes.py:292` `/api/v1/auth/token`: **không có rate limit** — đã test 30 lần đoán key liên tiếp, toàn 401, không 429 ⇒ cho phép brute-force online SECRET_KEY.
- `main.py:105–111`: `allow_origins=["*"]` + `allow_credentials=True`.
- Bonus (test được): middleware auth chạy **ngoài** CORS middleware ⇒ **OPTIONS preflight bị 401** (browser JS không gọi được API dù có token), và **401 không mang header CORS** khiến browser không đọc được lỗi.

**Sửa:** bắt buộc SECRET_KEY khi deploy (fail-fast thay vì warning), thêm rate limit cho `/auth/token`, cho phép preflight OPTIONS đi qua auth middleware, siết CORS theo origin tin cậy.

### H4. Mọi cuộc gọi chạy tuần tự + nguy cơ cạn thread pool + task bị GC

- `sip_controller.py:982–991`: `SipController.make_call()` giữ `self._lock` **suốt cuộc gọi** (kể cả đổ chuông + phát nhạc, có thể hàng phút) ⇒ hệ thống chỉ **1 cuộc gọi tại một thời điểm**, dù RTP ports đã map 21 cổng.
- Mỗi request chờ lock nằm trong `asyncio.to_thread` — pool mặc định ~32 thread; burst 10 req/s với cuộc gọi dài ⇒ **toàn bộ API (kể cả TTS dùng to_thread) ngưng đọng**.
- `call_manager.py:109`: `asyncio.create_task(...)` **không giữ reference** — theo docs Python task có thể bị GC giữa chừng.
- Mỗi cuộc gọi còn **ngắt kết nối + REGISTER lại từ đầu** (2 RTT TLS + 2 lượt REGISTER) ⇒ thêm ~1–2s trễ/cuộc và khiến `sip_registered` ở `/health` không phản ánh trạng thái thật (registration đầu tiên hết hạn sau 600s nhưng `is_registered` vẫn `True`).

**Sửa:** 1 connection REGISTER dài hạn + refresh định kỳ; hàng đợi cuộc gọi có giới hạn rõ ràng; giữ reference task (`self._tasks.add(task)` + done callback); nếu muốn song song hóa thì mỗi cuộc gọi cần connection + RTP port riêng (mở rộng range `RTP_PORT_MIN..MAX` — hiện code hardcode `rtp_port_min + 2`, `RTP_PORT_MAX` hoàn toàn không được dùng).

### H5. 44 CVE đã biết trong dependencies

`pip-audit` trên `requirements.txt`:
- **aiohttp 3.11.10: 28 CVE** (PYSEC-2026-…) — sửa bằng ≥ 3.14.x (chỉ dùng cho webhook nhưng là thành phần nhận dữ liệu mạng).
- **starlette 0.41.3: nhiều CVE** (kéo theo fastapi 0.115.6) — sửa bằng fastapi/starlette mới hơn.
- click 8.1.8 (PYSEC-2026-2132) — sửa bằng ≥ 8.3.3.

**Sửa:** nâng pin: `aiohttp>=3.14`, `fastapi>=0.121` (hoặc starlette ≥ 0.49), thêm bước `pip-audit` vào CI.

---

## 🟡 MEDIUM

| # | Lỗi | Vị trí | Ghi chú |
|---|-----|--------|---------|
| M1 | **ACK cho final response đầu tiên bị bỏ qua**: nếu proxy trả 4xx/5xx/6xx ngay (không có 100 Trying trước) thì code rẽ nhánh `>= 400 → return FAILED` **không gửi ACK**, và **486 Busy bị phân loại sai thành `failed` thay vì `busy`** (nhánh 486 nằm trong vòng lặp sau). Đồng thời ACK cho các lỗi trong vòng lặp (486/487/…) dùng cứng `To: tag=placeholder` thay vì tag thật của response ⇒ ACK không khớp dialog, peer retransmit. 408 thì return **không ACK**. | `sip_controller.py:569–572, 655, 776, 856–880` | ✅ Test C: proxy gửi 486 với `tag=busy486tag` → nhận kết quả `FAILED` (không phải BUSY), **không hề nhận ACK nào**; test 487 sau CANCEL nhận ACK `tag=placeholder` |
| M2 | **Response đầu tiên sau 407-retry bị "ăn mất"**: `_send_invite()` thứ hai trả về response nhưng code không xét lại mà chờ message tiếp theo. Nếu response ngay lập tức là final (403/404/200) thì bị bỏ, treo tới deadline, trả kết quả sai (thường `no_answer`). | `sip_controller.py:567 vs 575+` | Phân tích tĩnh, cùng họ lỗi với M1 (`_RE_STATUS.search` chỉ lấy message đầu trong buffer TCP) |
| M3 | **Không đàm phán codec/SRTP theo SDP answer**: SDP offer `RTP/SAVP 0 8 101` nhưng ffmpeg luôn encode **PCMU + SRTP** bất kể answer trả gì. Peer chỉ hỗ trợ PCMA ⇒ im lặng; answer downgrade `RTP/AVP` ⇒ ffmpeg vẫn gửi SRTP ⇒ im lặng. | `sip_controller.py:495–507, 910–936` | Phân tích tĩnh |
| M4 | **`TTSService.update_config` là code chết**: routes mutate chính object `app.state.config.tts` (cùng identity với `self._config`) **trước** khi gọi update ⇒ mọi so sánh `old != new` luôn False, session Zalo/TTSFree không bao giờ được reset như thiết kế. | `tts_service.py:330–350`, `routes.py:242–280` | ✅ Test: `svc._config is cfg == True`, `old_engine` luôn == giá trị mới |
| M5 | **Race ghi cache TTS**: 2 request cùng text đồng thời (cache-miss) cùng `shutil.copy2` vào 1 file cache → reader có thể đọc file đang ghi dở (không atomic). Cache key cũng **không phản ánh engine thật** đã synth (fallback gTTS được lưu dưới key của Zalo). | `tts_service.py:142–186` | Phân tích tĩnh |
| M6 | **ffmpeg fail ⇒ cuộc gọi vẫn "COMPLETED"**: `_stream_rtp` trả `None` (file lỗi/không có ffmpeg — `FileNotFoundError` thậm chí không bắt) nhưng `_invite` vẫn ngồi chờ hết timeout rồi BYE + trả COMPLETED ⇒ trạng thái sai + giữ máy Hz. | `sip_controller.py:660–680, 936–960` | Phân tích tĩnh (thấy rõ trong test: ffmpeg thoát sớm, gọi vẫn COMPLETED) |
| M7 | **Eviction có thể xóa record đang `calling`**: nhánh hard-cap (>120%) xóa theo `updated_at` **mọi trạng thái** ⇒ GET /call/{id} trả 404 giữa chừng cuộc gọi. | `call_manager.py:189–232` | Phân tích tĩnh |
| M8 | **Deadline reset sau 200 OK** (`deadline = now + timeout`) ⇒ worst-case tổng thời gian ~2× timeout (ring timeout + playback timeout); kèm `408` không ACK, `CallResult.DECLINED` là dead-code (không bao giờ được sinh ra). | `sip_controller.py:648, 776; call_manager.py:276` | Phân tích tĩnh |
| M9 | **REGISTER/TCP parse chỉ message đầu**: nếu 100 Trying và 401 về chung một đợt đọc TCP, `_RE_STATUS.search` bắt `100` ⇒ raise "Unexpected REGISTER response: 100". Contact/Via cho TCP/TLS khai port cứng 5060 (thực tế là port ephemeral) — chạy được nhờ rport nhưng sai chuẩn. | `sip_controller.py:214–230, 292–297` | Phân tích tĩnh |
| M10 | **NAT/Docker**: SDP `c=` quảng bá IP container (172.17.x) — mobile gửi RTP về địa chỉ này sẽ không tới được nếu không đặt `SIP_NAT_ADDRESS`; README không nhấn mạnh điều này. Firewall cần đúng 1 port RTP (10002) chứ không phải dải 10000–10020 như compose map. | `sip_controller.py:495, 503; docker-compose.yml` | Phân tích tĩnh |

---

## ⚪ LOW (tổng hợp nhanh)

1. `tempfile.mktemp()` (không an toàn, deprecated) tại `tts_service.py:142, 1096`.
2. `CallResponse.created_at` sinh riêng khi POST ⇒ lệch микро-giây so với `CallRecord` lưu thật (`call_manager.py:66`).
3. `APAD_SECS = 5` trùng lặp magic number với `"apad=pad_dur=5"` (`call_manager.py:31` vs `sip_controller.py:920`) — dễ lệch nhau khi sửa.
4. `_compute_digest`: nhánh MD5/MD5-SESS/else **giống hệt nhau** (SHA-256 bị fallback về MD5 âm thầm) — ✅ test xác nhận 2 output identical; `algorithm` từ server được nội suy thẳng vào header (server đáng ngờ ⇒ header injection).
5. Zalo: comment nói "retry once with fresh cookie on auth error" nhưng nhánh `HTTPError 401/403` **raise ngay** không retry (`tts_service.py:425–432`).
6. `jti` của token băm từ chính secret key — vô ích, nên dùng random.
7. `_rate_limit_buckets` (sau khi sửa C3) cần dọn IP cũ để tránh tăng trưởng không giới hạn.
8. `API_PORT`: compose map `${API_PORT}:8000` nhưng CMD hardcode `--port 8000`, healthcheck đọc `API_PORT` bên trong container ⇒ đặt `API_PORT≠8000` sẽ unhealthy + entrypoint echo sai.
9. Dockerfile: comment "HEALTHCHECK … still runs as wcs" **sai** — HEALTHCHECK chạy theo USER cuối cùng (root); vô hại nhưng gây hiểu nhầm.
10. README: thiếu tài liệu endpoint `/api/v1/auth/token` (auth chỉ xuất hiện ở bảng env); ghi "TTS_CACHE_DIR mặc định %TEMP%" trong khi compose đặt `/audio/.tts_cache`; `TTSFREE` giới hạn 500 ký tự nhưng `message` cho tới 2000 ký tự (chỉ log warning, không tự cắt/chọn engine phù hợp); "48+ giọng" nhưng liệt kê 6.
11. `_register` không tự re-register (Expires: 600) — nhờ "fresh connection mỗi cuộc gọi" che lỗi, nhưng `/health` `sip_registered` vẫn `true` sau khi registration hết hạn.
12. SSRF `_is_private_target`: CGNAT 100.64/10 không bị chặn (✅ test), thiếu `is_reserved`.
13. `CallRequest.callback_url` cho phép http:// (không ép https) — token/trạng thái gọi bị gửi plain-text.

---

## Đã kiểm tra và BÌNH THƯỜNG ✅

- **Digest auth**: khớp test vector RFC 2617 (`6629fae49393a05397450978507c4ef1`) cả trường hợp có/không qop.
- **ffmpeg option `localrtpport`**: hợp lệ (ffmpeg khớp abbreviation với `local_rtpport`), stream RTP gửi/nhận gói bình thường.
- **Luồng token**: issue → verify → hết hạn hoạt động đúng; chữ ký HMAC kiểm bằng `compare_digest`.
- **SSRF guard** chặn đúng: 127.0.0.1, 10.0.0.0/8, 169.254.169.254, ::1, IPv4-mapped IPv6.
- Compile sạch, import sạch (Python 3.11.8 + các phiên bản pin trong requirements).

---

## Thứ tự ưu tiên khắc phục đề xuất

1. **Ngay lập tức:** C2 (validate `target` — 1 dòng regex), C3 (sửa rate limiter — 3 dòng), H3 (bật bắt buộc SECRET_KEY + rate limit `/auth/token`).
2. **Trước khi prod:** C1 (pad silence vào file WAV, bỏ apad lúc stream), H1 (CANCEL dùng lại branch/CSeq của INVITE), M1/M2 (xử lý ACK + response sau 407), H5 (nâng deps).
3. **Cải thiện kiến trúc:** H4 (connection dài hạn + hàng đợi + giữ task reference), H2 (SSRF pin-IP), M3 (đàm phán codec theo answer), M5 (ghi cache atomic qua `os.replace`).

## Cách tái hiện các test

```bash
cd /home/user/voip-calling-service
python3 -m venv .venv && .venv/bin/pip install -r services/api/requirements.txt httpx
PYTHONPATH=services/api .venv/bin/python audit-tests/test_api_logic.py      # C3, H3, CORS, digest, SSRF
PYTHONPATH=services/api .venv/bin/python audit-tests/test_sip_injection.py  # C2 + hành vi BYE
PYTHONPATH=services/api .venv/bin/python audit-tests/test_sip_cancel_ack.py # H1, M1
```

*(Test scripts nằm trong thư mục `audit-tests/`, không ảnh hưởng mã nguồn.)*
