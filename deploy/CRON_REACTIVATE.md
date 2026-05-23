# Cron auto re-activate (Locket Gold)

Script `cron_reactivate.py` ở repo root. Tương đương file PHP
`cron_re_activate.php` — chạy định kỳ để gọi RevenueCat refresh Gold cho các
locket username đã có trong database, tránh việc Apple thu hồi receipt sau
24-72h.

Đọc trực tiếp từ SQLite (`gold_activations` ở nhánh PR mới; fallback
`gold_purchases` ở nhánh main). Tự ghi vào bảng `reactivation_log` để skip
các username đã được refresh trong N giờ qua (mặc định 6h).

## Mode mặc định: SINGLE RECEIPT (an toàn)

Codebase này có 1 fetch_token cứng trong `locket/locket_api.py::HARDCODED_PAYLOAD`.
Một receipt chỉ "thuộc về" 1 UID trên RevenueCat tại 1 thời điểm — refresh
nhiều UID liên tiếp sẽ gây ping-pong. Vì vậy mặc định cron chỉ refresh
**username được kích hoạt gần nhất**, đảm bảo user mới nhất luôn có Gold.

Nếu bạn đã chuyển sang nhiều receipt riêng cho từng UID, chạy thêm `--all`
để scan toàn bộ.

## Cách 1: cron Linux (đơn giản nhất)

```cron
# Mỗi 10 phút
*/10 * * * * cd /home/locket/LocketGoldUsername && \
    /home/locket/LocketGoldUsername/.venv/bin/python cron_reactivate.py \
    >> /var/log/locket-reactivate.log 2>&1
```

## Cách 2: systemd timer (khuyến nghị)

Copy 2 file vào `/etc/systemd/system/`:

```bash
sudo cp deploy/locket-reactivate.service /etc/systemd/system/
sudo cp deploy/locket-reactivate.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now locket-reactivate.timer
```

Kiểm tra:

```bash
systemctl list-timers locket-reactivate.timer
journalctl -u locket-reactivate -f
```

Chạy thủ công 1 lần (test):

```bash
sudo systemctl start locket-reactivate.service
journalctl -u locket-reactivate -n 50
```

## Cách 3: daemon process (không cần cron / timer)

```bash
python cron_reactivate.py --daemon --interval 600
```

Bật bằng `nohup` hoặc gói trong service riêng. Daemon mode giữ process
sống mãi, mỗi 10 phút (cấu hình qua `--interval`) gọi `run_once()`. Bắt
SIGINT/SIGTERM → exit gọn.

## Tham số quan trọng

| Flag                       | Mặc định  | Tác dụng |
| -------------------------- | --------- | -------- |
| `--all`                    | off       | Multi-receipt mode (scan toàn bộ DB) |
| `--max-age-hours N`        | 720 (30d) | Bỏ qua activation cũ hơn N giờ |
| `--min-interval-hours N`   | 6         | Bỏ qua username vừa refresh trong N giờ |
| `--limit N`                | 25        | Tối đa N username/vòng (chỉ áp dụng `--all`) |
| `--rate-limit-seconds X`   | 1.5       | Sleep giữa request RevenueCat |
| `--username NAME`          | —         | Chỉ refresh 1 username (debug) |
| `--dry-run`                | off       | In kế hoạch, KHÔNG gọi RC |
| `--daemon`                 | off       | Chạy lặp vô hạn |
| `--interval N`             | 600       | Chu kỳ daemon, giây |
| `--verbose`                | off       | Log DEBUG level |

## Troubleshooting

- **`[ABORT] No Locket account in pool`** — chưa có account Locket trong DB.
  Thêm qua `/admin` trước.
- **`[ABORT] Cannot acquire fresh LocketAPI`** — credentials Locket sai hoặc
  Firebase login bị block. Kiểm tra `/admin → Accounts`, refresh token thử,
  hoặc thay account mới.
- **Tất cả targets bị skip** — chưa quá `--min-interval-hours` từ lần refresh
  trước. Bình thường. Để force, truyền `--min-interval-hours 0`.
- **Không có target nào (`source=gold_purchases`)** — chưa có user nào mua/kích
  hoạt Gold trong `--max-age-hours` ngày qua. Bình thường lúc mới deploy.
