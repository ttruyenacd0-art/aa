import os
import time
import uuid
import hashlib
import requests as _requests
from datetime import datetime, timezone

from flask import (
    current_app, jsonify, render_template, request, send_file, session,
)

from .. import db, site_settings
from ..notifications import send_payment_notification, send_reactivate_notification
from . import bp

PAYMENT_EXPIRE_SECONDS = 600  # 10 phút

def _get_payment_cfg():
    """Đọc cấu hình thanh toán từ DB (site_settings)."""
    from .. import site_settings as _ss
    return _ss.get_payment()


def _mobileconfig_path():
    static_dir = os.path.join(current_app.root_path, "static")
    return os.path.join(static_dir, "locket.mobileconfig")


def _mask_username(name):
    if not name:
        return "—"
    s = str(name)
    return s[0] + "*" * min(4, max(0, len(s) - 1))


def _no_accounts_response():
    return jsonify({
        "success": False,
        "msg": "Chưa có tài khoản Locket nào. Admin hãy thêm qua /admin.",
    }), 503


def _maintenance_active():
    m = site_settings.get_maintenance()
    if not m.get("enabled"):
        return None
    if m.get("allow_admin", True) and session.get("admin"):
        return None
    return m


def _maintenance_json_response():
    m = _maintenance_active()
    if m is None:
        return None
    return jsonify({
        "success": False,
        "maintenance": True,
        "msg": m.get("message") or "Hệ thống đang bảo trì.",
        "end_at": m.get("end_at") or None,
    }), 503


@bp.route("/")
def index():
    m = _maintenance_active()
    if m is not None:
        return render_template("maintenance.html", settings=m), 503
    theme = site_settings.get_theme().get("name", "gold")
    layout = site_settings.get_layout().get("name", "stacked")
    return render_template("index.html", theme=theme, layout=layout)


@bp.route("/api/mobileconfig", methods=["GET"])
def mobileconfig_download():
    """Serve the mobileconfig with the exact headers iOS needs to trigger the
    'Install Profile' system dialog (instead of saving as a regular download).

    - Content-Type: application/x-apple-aspen-config — required by iOS Safari.
    - Content-Disposition: inline — keeps Safari from offering "Save to Files".
    - No-cache — admins can re-upload and clients see the new version.
    """
    path = _mobileconfig_path()
    if not os.path.exists(path):
        return jsonify({"success": False, "msg": "Profile not configured"}), 404
    resp = send_file(
        path,
        mimetype="application/x-apple-aspen-config",
        as_attachment=False,
        download_name="locket.mobileconfig",
    )
    resp.headers["Content-Disposition"] = 'inline; filename="locket.mobileconfig"'
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@bp.route("/api/site-settings", methods=["GET"])
def site_settings_public():
    payload = site_settings.public_view()
    # Whether the current visitor is actually under maintenance (after admin
    # bypass). FE uses this to decide whether to redirect to the maintenance
    # page — `maintenance.enabled` alone would loop admins.
    payload["maintenance_active"] = _maintenance_active() is not None
    return jsonify({"success": True, **payload})


@bp.route("/api/get-user-info", methods=["POST"])
def get_user_info():
    blocked = _maintenance_json_response()
    if blocked is not None:
        return blocked
    rotator = current_app.rotator
    qm = current_app.queue_manager
    if rotator is None or rotator.size() == 0:
        return _no_accounts_response()

    data = request.json or {}
    username = data.get("username")
    if not username:
        return jsonify({"success": False, "msg": "Username is required"}), 400

    try:
        print(f"Looking up user: {username}")
        account_info = qm.call_round_robin("getUserByUsername", username)

        if not account_info or "result" not in account_info:
            return jsonify({"success": False, "msg": "User not found or API error"}), 404

        user_data = account_info.get("result", {}).get("data")
        if not user_data:
            return jsonify({"success": False, "msg": "User data not found"}), 404

        return jsonify({
            "success": True,
            "data": {
                "uid": user_data.get("uid"),
                "username": user_data.get("username"),
                "first_name": user_data.get("first_name", ""),
                "last_name": user_data.get("last_name", ""),
                "profile_picture_url": user_data.get("profile_picture_url", ""),
            },
        })

    except Exception as e:
        print(f"Error in get user info: {e}")
        return jsonify({"success": False, "msg": f"An error occurred: {str(e)}"}), 500


@bp.route("/api/restore", methods=["POST"])
def restore_purchase():
    """Add a request to the queue. Returns client_id for polling."""
    blocked = _maintenance_json_response()
    if blocked is not None:
        return blocked
    rotator = current_app.rotator
    qm = current_app.queue_manager
    if rotator is None or rotator.size() == 0:
        return _no_accounts_response()

    data = request.json or {}
    username = data.get("username")
    if not username:
        return jsonify({"success": False, "msg": "Username is required"}), 400

    try:
        client_id = qm.add_to_queue(username)
        if client_id is None:
            return jsonify({"success": False, "msg": "Queue is full, please try again later."}), 503

        status = qm.get_status(client_id)
        return jsonify({
            "success": True,
            "client_id": client_id,
            "position": status["position"],
            "total_queue": status["total_queue"],
            "estimated_time": status["estimated_time"],
        })
    except Exception as e:
        print(f"Error adding to queue: {e}")
        return jsonify({"success": False, "msg": f"An error occurred: {str(e)}"}), 500


@bp.route("/api/recent-history", methods=["GET"])
def recent_history():
    """Public-safe recent history. Username is masked (a**** style); slot_id
    and error details are stripped. Returns up to 30 newest entries."""
    cutoff = __import__("time").time() - 24 * 3600
    rows = db.get_conn().execute(
        "SELECT username, status, duration, completed_at "
        "FROM recent_log WHERE completed_at >= ? "
        "ORDER BY id DESC LIMIT 30",
        (cutoff,),
    ).fetchall()
    items = []
    for r in rows:
        completed_at = None
        if r["completed_at"] is not None:
            completed_at = datetime.fromtimestamp(
                r["completed_at"], tz=timezone.utc
            ).isoformat()
        items.append({
            "username": _mask_username(r["username"]),
            "status": r["status"],
            "duration": r["duration"],
            "completed_at": completed_at,
        })
    return jsonify({"success": True, "items": items})


@bp.route("/api/mobileconfig/history", methods=["GET"])
def mobileconfig_history_public():
    """Public-safe profile update history. Filenames are stripped (admins only
    see those); we expose action + size + signed flag + timestamp so users
    know when the profile was last refreshed."""
    rows = db.get_conn().execute(
        "SELECT action, size, signed, created_at "
        "FROM mobileconfig_history ORDER BY id DESC LIMIT 10"
    ).fetchall()
    items = [
        {
            "action": r["action"],
            "size": r["size"],
            "signed": bool(r["signed"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    return jsonify({"success": True, "items": items})


@bp.route("/api/queue/global-status", methods=["GET"])
def global_queue_status():
    """Aggregate queue stats — no client_id required."""
    return jsonify({"success": True, **current_app.queue_manager.get_global_status()})


@bp.route("/api/queue/status", methods=["POST"])
def queue_status():
    """Per-client polling endpoint. Returns success even on `not_found` so
    the frontend can recover instead of treating it as fatal."""
    data = request.json or {}
    client_id = data.get("client_id")
    if not client_id:
        return jsonify({"success": False, "msg": "client_id is required"}), 400
    return jsonify({"success": True, **current_app.queue_manager.get_status(client_id)})


# ─── PAYMENT ROUTES ───────────────────────────────────────────────────────────

@bp.route("/api/payment/create", methods=["POST"])
def payment_create():
    """Tạo payment mới, trả về payment_id và thông tin chuyển khoản."""
    data = request.json or {}
    username = (data.get("username") or "").strip()
    if not username:
        return jsonify({"success": False, "msg": "Username is required"}), 400

    now = time.time()
    conn = db.get_conn()

    # Huỷ các payment pending cũ của username này
    conn.execute(
        "UPDATE payments SET status='expired' WHERE username=? AND status='pending'",
        (username,)
    )

    cfg = _get_payment_cfg()
    PAYMENT_AMOUNT = int(cfg.get("amount", 20000))
    bank_type = cfg.get("bank_type", "acb").lower()
    bank_api_url = cfg.get("mb_api_url", "") if bank_type == "mb" else cfg.get("acb_api_url", "")

    # Lấy max transactionID hiện tại để làm mốc — chỉ chấp nhận tx mới hơn
    # ACB: max(int ID), MB: hash của ID đầu tiên trong list (mới nhất)
    min_tx_id = 0
    try:
        snap_resp = _requests.get(bank_api_url, timeout=8)
        snap_resp.raise_for_status()
        existing_txs = snap_resp.json().get("transactions", [])
        if existing_txs:
            if bank_type == "mb":
                # MB trả danh sách theo thứ tự mới → cũ; snapshot hash của tx mới nhất
                min_tx_id = _normalize_tx_id(existing_txs[0].get("transactionID"))
            else:
                min_tx_id = max(_normalize_tx_id(t.get("transactionID")) for t in existing_txs)
    except Exception:
        min_tx_id = 0  # fallback: không lọc được, nhưng vẫn tạo payment

    payment_id = str(uuid.uuid4())[:8].upper()
    conn.execute(
        "INSERT INTO payments (payment_id, username, amount, status, created_at, min_tx_id) VALUES (?,?,?,?,?,?)",
        (payment_id, username, PAYMENT_AMOUNT, "pending", now, min_tx_id)
    )

    return jsonify({
        "success": True,
        "payment_id": payment_id,
        "amount": PAYMENT_AMOUNT,
        "bank_account": cfg.get("bank_account", ""),
        "bank_name": cfg.get("bank_name", "ACB"),
        "account_name": cfg.get("account_name", ""),
        "description": f"{cfg.get('description_prefix', 'LOCKET')} {username}",
        "expire_at": int(now + PAYMENT_EXPIRE_SECONDS),
    })



# ─── PAYMENT HELPERS ──────────────────────────────────────────────────────────

def _normalize_tx_id(raw_id):
    """Chuẩn hóa transactionID về dạng có thể so sánh.
    - ACB: số nguyên (2500, 2501…) → int
    - MB:  string dạng FT25062999583597 → hash thành int để so sánh thứ tự
           nhưng thực ra MB trả theo thứ tự mới nhất đầu tiên, nên ta dùng
           index vị trí để làm mốc thay vì parse.
    """
    if raw_id is None:
        return 0
    try:
        return int(raw_id)
    except (ValueError, TypeError):
        # MB string ID — hash để tạo số nguyên duy nhất (dùng cho dedup)
        return abs(hash(str(raw_id))) % (10 ** 15)


def _get_bank_api_url(cfg):
    """Trả về URL API ngân hàng đang được chọn."""
    bank_type = cfg.get("bank_type", "acb").lower()
    if bank_type == "mb":
        return cfg.get("mb_api_url", ""), "mb"
    return cfg.get("acb_api_url", ""), "acb"


def _fetch_transactions(cfg):
    """Fetch transactions từ ngân hàng đang dùng.
    Trả về (list_transactions, bank_type) hoặc raise Exception.
    """
    url, bank_type = _get_bank_api_url(cfg)
    if not url:
        raise ValueError(f"Chưa cấu hình URL API ngân hàng ({bank_type.upper()})")
    resp = _requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    return data.get("transactions", []), bank_type


def _row_get(row, key, default=None):
    """sqlite3.Row không có .get() — helper an toàn."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _find_matching_tx(transactions, username_lower, min_tx_id, conn, min_amount=20000, bank_type="acb"):
    """Tìm giao dịch mới nhất khớp với payment.
    Hỗ trợ cả ACB (transactionID số nguyên) và MB (transactionID string như FT...).

    - ACB: transactionID là int, so sánh > min_tx_id
    - MB:  transactionID là string (FT...), min_tx_id là hash của tx mới nhất lúc snapshot.
           Bỏ qua tx có hash == min_tx_id (đã tồn tại), hoặc hash < min_tx_id không hợp lệ.
           Thực tế MB list theo thứ tự mới → cũ nên các tx mới sẽ ở đầu list.
    """
    is_mb = bank_type == "mb"

    for tx in transactions:
        raw_tx_id = tx.get("transactionID")
        norm_tx_id = _normalize_tx_id(raw_tx_id)
        dedup_key = str(raw_tx_id) if raw_tx_id is not None else str(norm_tx_id)

        # --- Lọc giao dịch đã tồn tại trước khi tạo payment ---
        if is_mb:
            # MB: min_tx_id = hash của tx MỚI NHẤT lúc snapshot.
            # Tx nào có hash == min_tx_id là tx cuối cùng đã biết → bỏ qua nó và mọi tx sau.
            # Tx mới hơn (chưa có lúc snapshot) sẽ có hash khác.
            if norm_tx_id == min_tx_id and min_tx_id != 0:
                break  # đến tx đã biết → dừng (phần còn lại cũ hơn)
        else:
            # ACB: int so sánh thẳng
            if norm_tx_id <= min_tx_id:
                continue

        if tx.get("type") != "IN":
            continue

        # amount: ACB là số nguyên, MB trả về string hoặc số
        try:
            amount = int(str(tx.get("amount", 0)).replace(",", "").strip())
        except (ValueError, TypeError):
            amount = 0
        if amount < min_amount:
            continue

        desc = (tx.get("description") or "").lower()
        if username_lower not in desc:
            continue

        # Dedup: kiểm tra tx này chưa được dùng cho payment nào khác
        used = conn.execute(
            "SELECT 1 FROM payments WHERE transaction_id=? AND status='confirmed'",
            (dedup_key,)
        ).fetchone()
        if used:
            continue

        return dedup_key  # string ID (ACB: "2501", MB: "FT25062999583597")
    return None


def _confirm_payment_and_save(conn, payment_id, username, tx_id, now):
    """Xác nhận payment và ghi gold_purchases nếu user đang login."""
    conn.execute(
        "UPDATE payments SET status='confirmed', confirmed_at=?, transaction_id=? WHERE payment_id=?",
        (now, tx_id, payment_id)
    )
    user_id = session.get("user_id")
    account_user = session.get("user_name")
    if user_id:
        dup = conn.execute(
            "SELECT 1 FROM gold_purchases WHERE user_id=? AND locket_username=? AND payment_id=?",
            (user_id, username, payment_id)
        ).fetchone()
        if not dup:
            conn.execute(
                "INSERT INTO gold_purchases (user_id, locket_username, payment_id, purchased_at) VALUES (?,?,?,?)",
                (user_id, username, payment_id, now)
            )
    # Gửi thông báo Telegram
    cfg = _get_payment_cfg()
    import threading
    threading.Thread(
        target=send_payment_notification,
        args=(username, int(cfg.get("amount", 20000)), payment_id, account_user),
        daemon=True
    ).start()


@bp.route("/api/payment/check", methods=["POST"])
def payment_check():
    """Polling: kiểm tra API ACB xem đã nhận tiền chưa."""
    data = request.json or {}
    payment_id = (data.get("payment_id") or "").strip()
    if not payment_id:
        return jsonify({"success": False, "msg": "payment_id is required"}), 400

    conn = db.get_conn()
    row = conn.execute(
        "SELECT * FROM payments WHERE payment_id=?", (payment_id,)
    ).fetchone()
    if not row:
        return jsonify({"success": False, "msg": "Payment not found"}), 404
    if row["status"] == "confirmed":
        return jsonify({"success": True, "status": "confirmed", "username": row["username"]})

    now = time.time()
    if now - row["created_at"] > PAYMENT_EXPIRE_SECONDS:
        conn.execute("UPDATE payments SET status='expired' WHERE payment_id=?", (payment_id,))
        return jsonify({"success": True, "status": "expired"})

    _cfg = _get_payment_cfg()
    try:
        transactions, bank_type = _fetch_transactions(_cfg)
    except Exception as e:
        return jsonify({"success": True, "status": "pending", "msg": f"Bank API error: {e}"})

    tx_id = _find_matching_tx(transactions, row["username"].lower(), _row_get(row, "min_tx_id", 0), conn, int(_cfg.get("amount", 20000)), bank_type)
    if tx_id:
        _confirm_payment_and_save(conn, payment_id, row["username"], tx_id, now)
        return jsonify({"success": True, "status": "confirmed", "username": row["username"]})

    return jsonify({"success": True, "status": "pending"})


# ─── USER AUTH ROUTES ─────────────────────────────────────────────────────────

def _hash_password(password: str) -> str:
    """SHA-256 hash with a static salt prefix. Use bcrypt in production."""
    salted = "LOCKET_SALT_2025:" + password
    return hashlib.sha256(salted.encode()).hexdigest()


@bp.route("/api/auth/register", methods=["POST"])
def auth_register():
    """Đăng ký tài khoản người dùng."""
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not username or not password:
        return jsonify({"success": False, "msg": "Username và password là bắt buộc"}), 400
    if len(username) < 3:
        return jsonify({"success": False, "msg": "Username phải có ít nhất 3 ký tự"}), 400
    if len(password) < 6:
        return jsonify({"success": False, "msg": "Password phải có ít nhất 6 ký tự"}), 400

    conn = db.get_conn()
    existing = conn.execute(
        "SELECT id FROM user_accounts WHERE username=?", (username,)
    ).fetchone()
    if existing:
        return jsonify({"success": False, "msg": "Username đã tồn tại"}), 409

    now = time.time()
    conn.execute(
        "INSERT INTO user_accounts (username, password_hash, created_at) VALUES (?,?,?)",
        (username, _hash_password(password), now)
    )
    row = conn.execute("SELECT id FROM user_accounts WHERE username=?", (username,)).fetchone()
    session["user_id"] = row["id"]
    session["user_name"] = username
    return jsonify({"success": True, "username": username})


@bp.route("/api/auth/login", methods=["POST"])
def auth_login():
    """Đăng nhập tài khoản người dùng."""
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not username or not password:
        return jsonify({"success": False, "msg": "Username và password là bắt buộc"}), 400

    conn = db.get_conn()
    row = conn.execute(
        "SELECT id, username, password_hash FROM user_accounts WHERE username=?", (username,)
    ).fetchone()
    if not row or row["password_hash"] != _hash_password(password):
        return jsonify({"success": False, "msg": "Username hoặc password không đúng"}), 401

    session["user_id"] = row["id"]
    session["user_name"] = row["username"]
    return jsonify({"success": True, "username": row["username"]})


@bp.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.pop("user_id", None)
    session.pop("user_name", None)
    return jsonify({"success": True})


@bp.route("/api/auth/me", methods=["GET"])
def auth_me():
    """Trả về thông tin user đang đăng nhập và danh sách locket_username đã lên Gold."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"success": False, "logged_in": False})
    conn = db.get_conn()
    user = conn.execute(
        "SELECT username FROM user_accounts WHERE id=?", (user_id,)
    ).fetchone()
    if not user:
        session.clear()
        return jsonify({"success": False, "logged_in": False})
    purchases = conn.execute(
        "SELECT locket_username, purchased_at FROM gold_purchases WHERE user_id=? ORDER BY purchased_at DESC",
        (user_id,)
    ).fetchall()
    items = [
        {"locket_username": r["locket_username"],
         "purchased_at": r["purchased_at"]}
        for r in purchases
    ]
    return jsonify({
        "success": True,
        "logged_in": True,
        "username": user["username"],
        "gold_history": items,
    })


# ─── PAYMENT CONFIRM (manual trigger) ─────────────────────────────────────────

@bp.route("/api/payment/confirm-now", methods=["POST"])
def payment_confirm_now():
    """Người dùng bấm 'Tôi đã chuyển khoản' → kiểm tra ngay lập tức 1 lần."""
    data = request.json or {}
    payment_id = (data.get("payment_id") or "").strip()
    if not payment_id:
        return jsonify({"success": False, "msg": "payment_id is required"}), 400

    conn = db.get_conn()
    row = conn.execute(
        "SELECT * FROM payments WHERE payment_id=?", (payment_id,)
    ).fetchone()
    if not row:
        return jsonify({"success": False, "msg": "Không tìm thấy giao dịch"}), 404
    if row["status"] == "confirmed":
        return jsonify({"success": True, "status": "confirmed", "username": row["username"]})

    now = time.time()
    if now - row["created_at"] > PAYMENT_EXPIRE_SECONDS:
        conn.execute("UPDATE payments SET status='expired' WHERE payment_id=?", (payment_id,))
        return jsonify({"success": True, "status": "expired",
                        "msg": "Giao dịch đã hết hạn. Vui lòng tạo lại."})

    _cfg2 = _get_payment_cfg()
    try:
        transactions, bank_type2 = _fetch_transactions(_cfg2)
    except Exception as e:
        return jsonify({"success": False, "msg": f"Không thể kết nối ngân hàng: {e}"}), 502

    tx_id = _find_matching_tx(transactions, row["username"].lower(), _row_get(row, "min_tx_id", 0), conn, int(_cfg2.get("amount", 20000)), bank_type2)
    if tx_id:
        _confirm_payment_and_save(conn, payment_id, row["username"], tx_id, now)
        return jsonify({"success": True, "status": "confirmed", "username": row["username"]})

    return jsonify({
        "success": True,
        "status": "pending",
        "msg": "Chưa tìm thấy giao dịch khớp. Vui lòng chờ thêm hoặc kiểm tra lại nội dung chuyển khoản.",
    })


# ─── GOLD REACTIVATE (kích hoạt lại từ lịch sử) ──────────────────────────────

@bp.route("/api/gold/reactivate", methods=["POST"])
def gold_reactivate():
    """Kích hoạt lại Gold cho locket_username đã có trong lịch sử mua của user.
    Không cần thanh toán lại — chỉ cần xác nhận đã mua trước đó."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"success": False, "msg": "Bạn chưa đăng nhập"}), 401

    data = request.json or {}
    locket_username = (data.get("locket_username") or "").strip()
    if not locket_username:
        return jsonify({"success": False, "msg": "locket_username is required"}), 400

    conn = db.get_conn()
    # Kiểm tra user đã từng mua locket_username này chưa
    purchase = conn.execute(
        "SELECT id FROM gold_purchases WHERE user_id=? AND locket_username=? LIMIT 1",
        (user_id, locket_username)
    ).fetchone()
    if not purchase:
        return jsonify({"success": False, "msg": "Không tìm thấy lịch sử mua Gold cho username này"}), 403

    # Gửi thông báo Telegram
    import threading
    threading.Thread(
        target=send_reactivate_notification,
        args=(locket_username, session.get("user_name")),
        daemon=True
    ).start()

    # Đủ điều kiện → thêm vào queue như bình thường
    rotator = current_app.rotator
    qm = current_app.queue_manager
    if rotator is None or rotator.size() == 0:
        return jsonify({"success": False, "msg": "Chưa có tài khoản Locket nào. Admin hãy thêm qua /admin."}), 503

    client_id = qm.add_to_queue(locket_username)
    if client_id is None:
        return jsonify({"success": False, "msg": "Queue đang đầy, vui lòng thử lại sau."}), 503

    status = qm.get_status(client_id)
    return jsonify({
        "success": True,
        "client_id": client_id,
        "position": status["position"],
        "total_queue": status["total_queue"],
        "estimated_time": status["estimated_time"],
    })




# ─── PAYMENT RECOVER (mất QR, tìm lại bằng payment_id hoặc username) ──────────

@bp.route("/api/payment/recover", methods=["POST"])
def payment_recover():
    """Khách mất QR, nhập payment_id hoặc locket_username để tìm lại giao dịch."""
    data = request.json or {}
    query = (data.get("query") or "").strip().upper()
    if not query:
        return jsonify({"success": False, "msg": "Vui lòng nhập mã hoặc username"}), 400

    conn = db.get_conn()

    # Thử tìm theo payment_id trước
    row = conn.execute(
        "SELECT * FROM payments WHERE UPPER(payment_id)=? ORDER BY created_at DESC LIMIT 1",
        (query,)
    ).fetchone()

    # Nếu không thấy, tìm theo username (lấy giao dịch mới nhất)
    if not row:
        row = conn.execute(
            "SELECT * FROM payments WHERE UPPER(username)=? AND status IN ('pending','confirmed') ORDER BY created_at DESC LIMIT 1",
            (query,)
        ).fetchone()

    if not row:
        return jsonify({"success": False, "msg": "Không tìm thấy giao dịch nào. Kiểm tra lại mã hoặc username."}), 404

    if row["status"] == "confirmed":
        return jsonify({"success": True, "status": "confirmed", "payment_id": row["payment_id"], "username": row["username"]})

    if row["status"] == "pending":
        now = time.time()
        if now - row["created_at"] > PAYMENT_EXPIRE_SECONDS:
            conn.execute("UPDATE payments SET status='expired' WHERE payment_id=?", (row["payment_id"],))
            return jsonify({"success": False, "msg": "Giao dịch đã hết hạn. Vui lòng tạo mới."}), 410
        return jsonify({"success": True, "status": "pending", "payment_id": row["payment_id"], "username": row["username"]})

    return jsonify({"success": False, "msg": "Giao dịch đã hết hạn."}), 410

# ─── LINK PAYMENT TO ACCOUNT ──────────────────────────────────────────────────

def _link_pending_payments_to_user(conn, user_id: int, payment_ids: list):
    """Link các payment_id đã confirmed vào tài khoản user (dùng sau login/register)."""
    now = time.time()
    linked = 0
    for pid in payment_ids:
        row = conn.execute(
            "SELECT * FROM payments WHERE payment_id=? AND status='confirmed'",
            (pid,)
        ).fetchone()
        if not row:
            continue
        dup = conn.execute(
            "SELECT 1 FROM gold_purchases WHERE user_id=? AND payment_id=?",
            (user_id, pid)
        ).fetchone()
        if not dup:
            conn.execute(
                "INSERT INTO gold_purchases (user_id, locket_username, payment_id, purchased_at) VALUES (?,?,?,?)",
                (user_id, row["username"], pid, now)
            )
            linked += 1
    return linked


@bp.route("/api/payment/link-to-account", methods=["POST"])
def payment_link_to_account():
    """Sau khi đăng nhập/đăng ký, user gửi danh sách payment_id để link vào tài khoản."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"success": False, "msg": "Bạn chưa đăng nhập"}), 401

    data = request.json or {}
    payment_ids = data.get("payment_ids") or []
    if not isinstance(payment_ids, list):
        payment_ids = [payment_ids]

    conn = db.get_conn()
    linked = _link_pending_payments_to_user(conn, user_id, payment_ids)
    return jsonify({"success": True, "linked": linked})



# ─── PRICING PAGE & PACKAGES API ──────────────────────────────────────────────

@bp.route("/pricing")
def pricing_page():
    return render_template("pricing.html")


@bp.route("/activate")
def activate_page():
    return render_template("activate.html")


@bp.route("/login")
def login_page():
    """Trang đăng nhập người dùng."""
    if session.get("user_id"):
        from flask import redirect
        return redirect("/activate")
    return render_template("login.html")


@bp.route("/register")
def register_page():
    """Trang đăng ký người dùng."""
    if session.get("user_id"):
        from flask import redirect
        return redirect("/activate")
    return render_template("register.html")


@bp.route("/api/packages", methods=["GET"])
def packages_list():
    """Public API: list enabled pricing packages."""
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT * FROM pricing_packages WHERE enabled=1 ORDER BY sort_order ASC, id ASC"
    ).fetchall()
    packages = []
    for r in rows:
        packages.append({
            "id": r["id"],
            "name": r["name"],
            "price": r["price"],
            "duration": r["duration"],
            "description": r["description"],
            "features": r["features"],
            "purchase_count": r["purchase_count"],
            "max_activations": r["max_activations"],
            "is_featured": bool(r["is_featured"]),
        })
    return jsonify({"success": True, "packages": packages})


@bp.route("/api/user-package", methods=["GET"])
def user_package():
    """Return the package info for current logged-in user (based on their purchases)."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"success": False, "msg": "Chua dang nhap"}), 401

    conn = db.get_conn()

    # Find user's latest gold_purchase that has a package_id
    purchase = conn.execute(
        "SELECT package_id FROM gold_purchases WHERE user_id=? AND package_id IS NOT NULL ORDER BY purchased_at DESC LIMIT 1",
        (user_id,)
    ).fetchone()

    # Also check gold_activations
    activation = conn.execute(
        "SELECT package_id FROM gold_activations WHERE user_id=? ORDER BY activated_at DESC LIMIT 1",
        (user_id,)
    ).fetchone()

    pkg_id = None
    if purchase:
        pkg_id = purchase["package_id"]
    elif activation:
        pkg_id = activation["package_id"]

    if not pkg_id:
        # Check if user has any confirmed payment -> assign first available package
        confirmed = conn.execute(
            "SELECT payment_id FROM payments WHERE username IN (SELECT username FROM user_accounts WHERE id=?) AND status='confirmed' ORDER BY confirmed_at DESC LIMIT 1",
            (user_id,)
        ).fetchone()
        if not confirmed:
            return jsonify({"success": True, "package": None, "activations_used": 0})
        # Assign the first enabled package
        first_pkg = conn.execute(
            "SELECT id FROM pricing_packages WHERE enabled=1 ORDER BY sort_order ASC LIMIT 1"
        ).fetchone()
        if first_pkg:
            pkg_id = first_pkg["id"]
        else:
            return jsonify({"success": True, "package": None, "activations_used": 0})

    pkg = conn.execute(
        "SELECT * FROM pricing_packages WHERE id=?", (pkg_id,)
    ).fetchone()
    if not pkg:
        return jsonify({"success": True, "package": None, "activations_used": 0})

    # Count activations used
    activations_used = conn.execute(
        "SELECT COUNT(*) as c FROM gold_activations WHERE user_id=? AND package_id=?",
        (user_id, pkg_id)
    ).fetchone()["c"]

    return jsonify({
        "success": True,
        "package": {
            "id": pkg["id"],
            "name": pkg["name"],
            "price": pkg["price"],
            "duration": pkg["duration"],
            "description": pkg["description"],
            "max_activations": pkg["max_activations"],
        },
        "activations_used": activations_used,
    })


@bp.route("/api/gold/activate", methods=["POST"])
def gold_activate():
    """Activate Gold for a locket_username - only works if user has purchased a package."""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"success": False, "msg": "Chua dang nhap"}), 401

    data = request.json or {}
    locket_username = (data.get("locket_username") or "").strip()
    if not locket_username:
        return jsonify({"success": False, "msg": "Locket username la bat buoc"}), 400

    conn = db.get_conn()

    # Get user's package
    purchase = conn.execute(
        "SELECT package_id FROM gold_purchases WHERE user_id=? AND package_id IS NOT NULL ORDER BY purchased_at DESC LIMIT 1",
        (user_id,)
    ).fetchone()
    activation_rec = conn.execute(
        "SELECT package_id FROM gold_activations WHERE user_id=? ORDER BY activated_at DESC LIMIT 1",
        (user_id,)
    ).fetchone()

    pkg_id = None
    if purchase:
        pkg_id = purchase["package_id"]
    elif activation_rec:
        pkg_id = activation_rec["package_id"]

    if not pkg_id:
        # Try to find from confirmed payments
        user_row = conn.execute("SELECT username FROM user_accounts WHERE id=?", (user_id,)).fetchone()
        if user_row:
            confirmed = conn.execute(
                "SELECT payment_id FROM payments WHERE username=? AND status='confirmed' ORDER BY confirmed_at DESC LIMIT 1",
                (user_row["username"],)
            ).fetchone()
            if confirmed:
                first_pkg = conn.execute(
                    "SELECT id FROM pricing_packages WHERE enabled=1 ORDER BY sort_order ASC LIMIT 1"
                ).fetchone()
                if first_pkg:
                    pkg_id = first_pkg["id"]

    if not pkg_id:
        return jsonify({"success": False, "msg": "Ban chua mua goi nao. Vui long mua goi truoc."}), 403

    pkg = conn.execute("SELECT * FROM pricing_packages WHERE id=?", (pkg_id,)).fetchone()
    if not pkg:
        return jsonify({"success": False, "msg": "Goi khong ton tai"}), 404

    # Check activation limit
    used = conn.execute(
        "SELECT COUNT(*) as c FROM gold_activations WHERE user_id=? AND package_id=?",
        (user_id, pkg_id)
    ).fetchone()["c"]

    if used >= pkg["max_activations"]:
        return jsonify({"success": False, "msg": f"Da het luot kich hoat ({used}/{pkg['max_activations']})"}), 403

    # Add to queue
    rotator = current_app.rotator
    qm = current_app.queue_manager
    if rotator is None or rotator.size() == 0:
        return jsonify({"success": False, "msg": "Chua co tai khoan Locket. Admin hay them qua /admin."}), 503

    client_id = qm.add_to_queue(locket_username)
    if client_id is None:
        return jsonify({"success": False, "msg": "Queue dang day, vui long thu lai sau."}), 503

    # Record activation
    now = time.time()
    conn.execute(
        "INSERT INTO gold_activations (user_id, package_id, locket_username, activated_at, status) VALUES (?,?,?,?,?)",
        (user_id, pkg_id, locket_username, now, "active")
    )
    # Also record in gold_purchases for history
    conn.execute(
        "INSERT INTO gold_purchases (user_id, locket_username, package_id, purchased_at) VALUES (?,?,?,?)",
        (user_id, locket_username, pkg_id, now)
    )

    status = qm.get_status(client_id)
    return jsonify({
        "success": True,
        "client_id": client_id,
        "position": status["position"],
        "total_queue": status["total_queue"],
        "estimated_time": status["estimated_time"],
    })
