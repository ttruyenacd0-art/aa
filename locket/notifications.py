"""Out-of-band notifications. Today: Telegram only."""

import json
import os
import time

import requests


def _get_all_telegram_targets():
    """Trả về list các (token, chat_id) cần gửi.
    Bot 1: từ .env (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)
    Bot 2: từ DB admin settings (nếu đã cấu hình)
    → Gửi về cả 2 nếu cả 2 đều có."""
    targets = []
    # Bot 1 — từ .env
    env_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    env_chat  = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if env_token and env_chat:
        targets.append((env_token, env_chat))
    # Bot 2 — từ admin DB
    try:
        from . import site_settings
        cfg = site_settings.get_telegram()
        db_token = cfg.get("bot_token", "").strip()
        db_chat  = cfg.get("chat_id", "").strip()
        if db_token and db_chat and (db_token, db_chat) not in targets:
            targets.append((db_token, db_chat))
    except Exception:
        pass
    return targets


def _send_to_all(message):
    """Gửi message đến tất cả bot đã cấu hình."""
    targets = _get_all_telegram_targets()
    if not targets:
        print("Telegram notification skipped: no bot configured.")
        return
    for token, chat_id in targets:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
                timeout=5,
            )
        except Exception as e:
            print(f"Failed to send Telegram notification: {e}")


def send_telegram_notification(username, uid, product_id, raw_json):
    subscription_info = json.dumps(
        raw_json.get("subscriber", {}).get("entitlements", {}).get("Gold", {}),
        indent=2,
    )
    message = (
        f"✅ <b>Locket Gold Unlocked!</b>\n\n"
        f"👤 <b>User:</b> {username} ({uid})\n"
        f"⏰ <b>Time:</b> {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"<b>Subscription Info:</b>\n<pre>{subscription_info}</pre>"
    )
    _send_to_all(message)


def send_payment_notification(locket_username, amount, payment_id, account_user=None):
    """Gửi thông báo Telegram khi user nạp tiền / kích hoạt Gold thành công."""
    acc_line = f"\n🔑 <b>Tài khoản:</b> {account_user}" if account_user else ""
    message = (
        f"💰 <b>Thanh toán thành công!</b>\n\n"
        f"🎮 <b>Locket user:</b> <code>{locket_username}</code>"
        f"{acc_line}\n"
        f"💵 <b>Số tiền:</b> {amount:,} VNĐ\n"
        f"🧾 <b>Mã GD:</b> <code>{payment_id}</code>\n"
        f"⏰ <b>Thời gian:</b> {time.strftime('%d/%m/%Y %H:%M:%S')}"
    )
    _send_to_all(message)


def send_reactivate_notification(locket_username, account_user=None):
    """Gửi thông báo khi user kích hoạt lại Gold từ lịch sử."""
    acc_line = f"\n🔑 <b>Tài khoản:</b> {account_user}" if account_user else ""
    message = (
        f"⚡ <b>Kích hoạt lại Gold!</b>\n\n"
        f"🎮 <b>Locket user:</b> <code>{locket_username}</code>"
        f"{acc_line}\n"
        f"⏰ <b>Thời gian:</b> {time.strftime('%d/%m/%Y %H:%M:%S')}"
    )
    _send_to_all(message)
