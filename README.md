# VoIP Announcement Calling Service (WCS)

Dịch vụ gọi điện thông báo tự động qua VoIP sử dụng **Linphone Free SIP Service**.
Chuyển đổi văn bản tiếng Việt thành giọng nói và thực hiện cuộc gọi SIP đến
người dùng đang cài ứng dụng **Linphone** trên điện thoại.

**Push notifications hoạt động** — iPhone/Android nhận cuộc gọi ngay cả khi app đóng.

## Kiến trúc

```
External Apps (REST API)
        │
        ▼
┌───────────────────────┐     ┌──────────────────────────┐
│   Calling API (8000)  │────▶│  sip.linphone.org (5061) │
│   FastAPI + SIP socket│     │  SIP Proxy + Push        │
│   + TTS (gTTS/Zalo/   │     └──────────┬───────────────┘
│         espeak)       │                │
└───────────────────────┘                │
                              Push notification + SIP call
                                         │
                                         ▼
                              ┌──────────────────────┐
                              │  Linphone Mobile App │
                              │  (iPhone / Android)  │
                              └──────────────────────┘
```

## Yêu cầu hệ thống

- **Docker** 24+ và **Docker Compose** v2+
- **Linphone** app trên điện thoại
- 2 tài khoản miễn phí `sip.linphone.org` (tạo qua app Linphone)

## Cài đặt nhanh

### 1. Tạo 2 tài khoản Linphone

Mở app Linphone trên điện thoại:
1. **Assistant → Create Account → Linphone Free SIP Service**
2. Tạo tài khoản **API** (để gọi đi): VD: `thongbao`
3. Tạo tài khoản **Phone** (để nhận cuộc gọi): VD: `vanphandinh`

### 2. Cấu hình

```bash
cd voip-calling-service
cp .env.example .env
```

Sửa `.env` — điền credentials của tài khoản API:

```env
SIP_DOMAIN=sip.linphone.org
SIP_TRANSPORT=tls
SIP_PROXY=sip:sip.linphone.org:5061;transport=tls
SIP_USERNAME=thongbao
SIP_PASSWORD=<password-cua-tai-khoan-api>
```

### 3. Cấu hình Linphone trên điện thoại

1. Vào Linphone → **Settings → Accounts → +**
2. Nhập tài khoản **Phone**:

| Trường | Giá trị |
|--------|---------|
| Username | `vanphandinh` |
| Password | `<password>` |
| Domain | `sip.linphone.org` |
| Transport | **TLS** |

3. Nhấn **LOGIN** → thấy **"Connected"**

### 4. Khởi động

```bash
docker compose up -d

# Kiểm tra:
curl http://localhost:8000/api/v1/health
# {"status":"ok","sip_registered":true,...}
```

### 5. Gọi thông báo

```bash
curl -X POST http://localhost:8000/api/v1/call \
  -H "Content-Type: application/json" \
  -d '{
    "target": "sip:vanphandinh@sip.linphone.org",
    "message": "Xin chao, day la thong bao tu he thong. Nhiet do hien tai vuot nguong."
  }'
```

> **Mặc định voice được lặp lại 2 lần**, cách nhau 1 giây. Xem [POST /api/v1/call](#post-apiv1call) để tùy chỉnh `repeat` và `repeat_delay`.

Điện thoại sẽ đổ chuông (kể cả khi app đang tắt!) và phát thông báo tiếng Việt.

> **Push notifications**: Linphone.org tự động gửi push notification khi có cuộc gọi đến. Điện thoại sẽ thức dậy và đổ chuông ngay cả khi app bị đóng.

## API Endpoints

| Method | Path | Mô tả |
|--------|------|-------|
| `POST` | `/api/v1/call` | Thực hiện cuộc gọi thông báo |
| `GET` | `/api/v1/call/{id}` | Xem trạng thái cuộc gọi |
| `GET` | `/api/v1/calls` | Danh sách cuộc gọi gần đây |
| `GET` | `/api/v1/health` | Health check |
| `GET` | `/api/v1/tts/config` | Xem cấu hình TTS hiện tại |
| `PUT` | `/api/v1/tts/config` | Đổi engine/giọng nói/tốc độ runtime |
| `GET` | `/api/v1/tts/cache` | Xem thống kê cache TTS |
| `DELETE` | `/api/v1/tts/cache` | Xóa file cache TTS cũ hơn N ngày |
| `GET` | `/docs` | Swagger UI |

### POST /api/v1/call

```json
{
  "target": "sip:vanphandinh@sip.linphone.org",
  "message": "Nội dung thông báo tiếng Việt (tối đa 2000 ký tự)",
  "repeat": 2,
  "repeat_delay": 1.0,
  "callback_url": "https://myapp.example.com/webhook"
}
```

| Tham số | Loại | Mặc định | Mô tả |
|---------|------|----------|-------|
| `target` | string | *(bắt buộc)* | SIP URI của người nhận |
| `message` | string | *(bắt buộc)* | Nội dung thông báo (1-2000 ký tự) |
| `repeat` | int | `2` | Số lần lặp lại voice (1-20, 1 = phát 1 lần) |
| `repeat_delay` | float | `1.0` | Thời gian nghỉ giữa các lần lặp (0.5-10.0 giây) |
| `callback_url` | string | `null` | Webhook URL nhận callback khi trạng thái thay đổi |

## Chọn giọng nói & Engine TTS

Dịch vụ hỗ trợ 3 engine TTS, có thể đổi runtime qua API mà không cần restart:

| Engine | Chất lượng | Internet | Ghi chú |
|--------|-----------|----------|---------|
| `zalo` | ⭐⭐⭐⭐⭐ | Cần | 6 giọng tự nhiên (Nam/Nữ × Bắc/Nam), tốt nhất |
| `gtts` | ⭐⭐⭐⭐ | Cần | Google TTS tiếng Việt |
| `espeak` | ⭐⭐ | Không | Offline, giọng robot |

### 6 giọng Zalo AI

| ID | Giới tính | Vùng miền |
|----|-----------|-----------|
| 1 | Nữ | Miền Nam |
| 2 | Nữ | Miền Bắc |
| 3 | Nam | Miền Nam |
| 4 | Nam | Miền Bắc |
| 5 | Nữ | Miền Bắc |
| 6 | Nữ | Miền Nam |

```bash
# Xem cấu hình TTS hiện tại
curl http://localhost:8000/api/v1/tts/config

# Đổi sang Zalo + giọng nam miền Bắc
curl -X PUT http://localhost:8000/api/v1/tts/config \
  -H "Content-Type: application/json" \
  -d '{"engine": "zalo", "zalo_speaker_id": 4}'

# Đổi sang Google TTS
curl -X PUT http://localhost:8000/api/v1/tts/config \
  -H "Content-Type: application/json" \
  -d '{"engine": "gtts"}'

# Chỉ đổi tốc độ nói
curl -X PUT http://localhost:8000/api/v1/tts/config \
  -H "Content-Type: application/json" \
  -d '{"zalo_speed": 1.2}'
```

> **Fallback chain**: Khi engine chính lỗi, hệ thống tự động thử: engine đã config → gTTS → espeak. Điều này đảm bảo cuộc gọi luôn được thực hiện ngay cả khi mất internet.

## Quản lý cache TTS

Audio đã synthesize được cache để tránh gọi lại TTS API. Cache nằm trong Docker volume
`audio_data` → tồn tại qua các lần rebuild.

File cache có `mtime` được reset mỗi lần được dùng lại (cache hit) → chỉ file thực sự
không còn dùng mới bị xóa khi cleanup.

### Xem thống kê cache

```bash
curl http://localhost:8000/api/v1/tts/cache
# {"total_files":42,"total_size_bytes":5242880,"total_size_mb":5.0,"cache_dir":"/audio/.tts_cache"}
```

### Dọn dẹp cache cũ

```bash
# Xóa file cache cũ hơn TTS_CACHE_MAX_AGE_DAYS (mặc định 30 ngày)
curl -X DELETE http://localhost:8000/api/v1/tts/cache
# {"deleted_files":5,"freed_bytes":640000,"freed_mb":0.61,"max_age_days":30}
```

> **Gợi ý**: Đặt cron job gọi `DELETE /api/v1/tts/cache` hàng tuần để giữ cache gọn gàng.

## Trạng thái cuộc gọi

| Trạng thái | Ý nghĩa |
|-----------|---------|
| `queued` | Đang chờ xử lý |
| `synthesizing` | Đang chuyển văn bản → giọng nói |
| `calling` | Đang gọi |
| `completed` | Hoàn thành |
| `no_answer` | Không trả lời |
| `busy` | Máy bận |
| `failed` | Lỗi |

## Biến môi trường

| Biến | Mặc định | Mô tả |
|------|----------|-------|
| `SIP_DOMAIN` | `sip.linphone.org` | SIP provider domain |
| `SIP_USERNAME` | *(bắt buộc)* | Tài khoản API trên linphone.org |
| `SIP_PASSWORD` | *(bắt buộc)* | Mật khẩu tài khoản API |
| `SIP_TRANSPORT` | `tls` | TLS (bắt buộc cho linphone.org) |
| `SIP_PROXY` | `sip:sip.linphone.org:5061;transport=tls` | SIP proxy |
| `RTP_PORT_MIN` | `10000` | Port RTP bắt đầu |
| `RTP_PORT_MAX` | `10020` | Port RTP kết thúc |
| `TTS_ENGINE` | `gtts` | Engine TTS: `gtts`, `zalo`, `espeak` |
| `ZALO_SPEAKER_ID` | `1` | Giọng Zalo (1-6), chỉ dùng khi `TTS_ENGINE=zalo` |
| `ZALO_SPEED` | `1.0` | Tốc độ nói Zalo (0.8 - 1.2) |
| `CALL_TIMEOUT` | `30` | Thời gian tối đa mỗi cuộc gọi (giây) |
| `TTS_CACHE_ENABLED` | `true` | Bật/tắt cache audio TTS (`true`/`false`) |
| `TTS_CACHE_DIR` | *(hệ thống)* | Thư mục cache, mặc định `%TEMP%/wcs_tts_cache/` |
| `TTS_CACHE_MAX_AGE_DAYS` | `30` | Số ngày tối đa file cache được giữ, file cũ hơn bị xóa khi gọi `DELETE /api/v1/tts/cache` |
| `API_PORT` | `8000` | Cổng API HTTP |
| `LOG_LEVEL` | `INFO` | Log level |

## Tích hợp từ app khác

### Python
```python
import requests

def send_announcement(message: str, target_sip: str,
                      repeat: int = 2, repeat_delay: float = 1.0) -> dict:
    resp = requests.post(
        "http://localhost:8000/api/v1/call",
        json={
            "target": target_sip,
            "message": message,
            "repeat": repeat,
            "repeat_delay": repeat_delay,
        },
        timeout=5,
    )
    return resp.json()

# Usage
result = send_announcement(
    "Cảnh báo: nhiệt độ vượt ngưỡng!",
    "sip:vanphandinh@sip.linphone.org",
    repeat=3,
    repeat_delay=2.0,
)
print(result["call_id"])
```

### cURL
```bash
curl -s -X POST http://localhost:8000/api/v1/call \
  -H "Content-Type: application/json" \
  -d "{\"target\":\"sip:vanphandinh@sip.linphone.org\",\"message\":\"Canh bao luc $(date +%H:%M)\",\"repeat\":3,\"repeat_delay\":2.0}"
```

## Webhook (callback)

```json
POST https://myapp.example.com/webhook
{
  "call_id": "a1b2c3d4e5f6",
  "status": "completed",
  "target": "sip:vanphandinh@sip.linphone.org",
  "message": "Xin chao...",
  "duration_seconds": 12.5,
  "error_message": null
}
```

## Cấu trúc dự án

```
voip-calling-service/
├── docker-compose.yml
├── .env.example
├── README.md
├── audio/                          # Temp WAV files (volume)
├── services/
│   └── api/
│       ├── Dockerfile
│       ├── requirements.txt
│       └── app/
│           ├── main.py             # FastAPI entry
│           ├── config.py           # Configuration
│           ├── models.py           # Pydantic models
│           ├── routes.py           # API endpoints
│           ├── call_manager.py     # Call orchestration
│           ├── sip_controller.py   # SIP signaling (Python socket + TLS)
│           └── tts_service.py      # TTS (gTTS + Zalo + espeak)
```

## Xử lý lỗi thường gặp

| Lỗi | Nguyên nhân | Cách khắc phục |
|-----|------------|----------------|
| `sip_registered: false` | Sai password hoặc username | Kiểm tra `SIP_USERNAME`/`SIP_PASSWORD` trong `.env` |
| `call failed: 404` | Target chưa đăng ký trên linphone.org | Kiểm tra target đúng username, iPhone đang Connected |
| `call failed: 408` | Không trả lời | iPhone không online hoặc không nhận push |
| `gTTS failed` | Không có internet | Đổi `TTS_ENGINE=zalo` hoặc `espeak` |
| `Zalo failed` | Cookie hết hạn hoặc API quá tải | Tự động retry + fallback xuống gTTS → espeak |
| **Điện thoại đổ chuông nhưng không nghe âm thanh** | RTP port bị chặn | Mở UDP ports 10000-10020 trên firewall |

## License

MIT
