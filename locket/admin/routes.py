import json
import os
import time

from flask import current_app, jsonify, redirect, render_template, request, session

import requests

from .. import db
from .. import proxies as proxy_pool
from .. import site_settings
from ..rotator import AccountRotator
from ..tokens import tokens_store
from . import bp
from .auth import admin_required, check_credentials, is_admin_logged_in


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if is_admin_logged_in():
            return redirect("/admin/")
        return render_template("admin_login.html", error=None)

    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()
    if not check_credentials(username, password):
        return render_template("admin_login.html", error="Invalid username or password"), 401
    session.clear()
    session["admin"] = True
    return redirect("/admin/")


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/admin/login")


@bp.route("/")
@admin_required
def dashboard():
    return render_template("admin.html")


# ---- accounts ----


@bp.route("/api/accounts", methods=["GET"])
@admin_required
def accounts_list():
    rotator = current_app.rotator
    if rotator is None:
        return jsonify({"success": False, "error": "rotator not initialized"}), 500
    return jsonify({"success": True, "accounts": rotator.list_accounts()})


@bp.route("/api/accounts", methods=["POST"])
@admin_required
def accounts_add():
    rotator = current_app.rotator
    if rotator is None:
        return jsonify({"success": False, "error": "rotator not initialized"}), 500

    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip()
    password = body.get("password") or ""
    if not email or not password:
        return jsonify({"success": False, "error": "email and password are required"}), 400

    ok, err = rotator.test_login(email, password)
    if not ok:
        return jsonify({"success": False, "error": f"Login failed: {err}"}), 400

    slot_id = rotator.add(email, password)
    current_app.queue_manager.add_worker(slot_id)
    return jsonify({"success": True, "id": slot_id, "email": email})


@bp.route("/api/accounts/<slot_id>", methods=["DELETE"])
@admin_required
def accounts_remove(slot_id):
    rotator = current_app.rotator
    if rotator is None:
        return jsonify({"success": False, "error": "rotator not initialized"}), 500
    if not rotator.has(slot_id):
        return jsonify({"success": False, "error": "not found"}), 404
    if rotator.size() <= 1:
        return jsonify({"success": False, "error": "must keep at least 1 account"}), 400
    current_app.queue_manager.remove_worker(slot_id)
    rotator.remove(slot_id)
    return jsonify({"success": True})


@bp.route("/api/accounts/test", methods=["POST"])
@admin_required
def accounts_test():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip()
    password = body.get("password") or ""
    if not email or not password:
        return jsonify({"success": False, "error": "email and password are required"}), 400
    ok, err = AccountRotator.test_login(email, password)
    return jsonify({"success": ok, "error": err})


# ---- tokens ----


@bp.route("/api/tokens", methods=["GET"])
@admin_required
def tokens_list():
    return jsonify({"success": True, "tokens": tokens_store.list()})


@bp.route("/api/tokens", methods=["POST"])
@admin_required
def tokens_add():
    body = request.get_json(silent=True) or {}
    payload = body.get("payload")
    if payload is None:
        # Allow raw JSON in a "raw" string field (UI textarea convenience).
        raw = body.get("raw")
        if not raw:
            return jsonify({"success": False, "error": "missing payload"}), 400
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            return jsonify({"success": False, "error": f"Invalid JSON: {e}"}), 400
    try:
        tokens_store.add(payload)
    except (ValueError, OSError) as e:
        return jsonify({"success": False, "error": str(e)}), 400
    return jsonify({"success": True})


@bp.route("/api/tokens/<int:index>", methods=["DELETE"])
@admin_required
def tokens_remove(index):
    try:
        tokens_store.remove(index)
    except IndexError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    return jsonify({"success": True})


# ---- queue ----


# ---- popup + maintenance ----


@bp.route("/api/popup", methods=["GET"])
@admin_required
def popup_get():
    return jsonify({"success": True, "popup": site_settings.get_popup()})


@bp.route("/api/popup", methods=["PUT"])
@admin_required
def popup_set():
    body = request.get_json(silent=True) or {}
    saved = site_settings.set_popup(body)
    return jsonify({"success": True, "popup": saved})


@bp.route("/api/maintenance", methods=["GET"])
@admin_required
def maintenance_get():
    return jsonify({"success": True, "maintenance": site_settings.get_maintenance()})


@bp.route("/api/maintenance", methods=["PUT"])
@admin_required
def maintenance_set():
    body = request.get_json(silent=True) or {}
    saved = site_settings.set_maintenance(body)
    return jsonify({"success": True, "maintenance": saved})


@bp.route("/api/theme", methods=["GET"])
@admin_required
def theme_get():
    return jsonify({
        "success": True,
        "theme": site_settings.get_theme(),
        "available": list(site_settings.THEMES),
    })


@bp.route("/api/theme", methods=["PUT"])
@admin_required
def theme_set():
    body = request.get_json(silent=True) or {}
    try:
        saved = site_settings.set_theme(body)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    return jsonify({"success": True, "theme": saved})


@bp.route("/api/layout", methods=["GET"])
@admin_required
def layout_get():
    return jsonify({
        "success": True,
        "layout": site_settings.get_layout(),
        "available": list(site_settings.LAYOUTS),
    })


@bp.route("/api/layout", methods=["PUT"])
@admin_required
def layout_set():
    body = request.get_json(silent=True) or {}
    try:
        saved = site_settings.set_layout(body)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    return jsonify({"success": True, "layout": saved})


# ---- proxies ----


def _redact(url):
    # Hide password in user:pass@host
    try:
        if "://" not in url:
            return url
        scheme, rest = url.split("://", 1)
        if "@" not in rest:
            return url
        creds, host = rest.rsplit("@", 1)
        if ":" in creds:
            user, _ = creds.split(":", 1)
            return f"{scheme}://{user}:***@{host}"
        return f"{scheme}://{creds}@{host}"
    except Exception:
        return url


@bp.route("/api/proxies", methods=["GET"])
@admin_required
def proxies_list():
    items = proxy_pool.list_all()
    for it in items:
        it["url_redacted"] = _redact(it["url"])
    return jsonify({
        "success": True,
        "master_enabled": proxy_pool.is_master_on(),
        "items": items,
    })


@bp.route("/api/proxies", methods=["POST"])
@admin_required
def proxies_add():
    body = request.get_json(silent=True) or {}
    raw = body.get("raw") or body.get("url") or ""
    try:
        added = proxy_pool.add_many(raw)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    return jsonify({"success": True, "added": added})


@bp.route("/api/proxies/<int:proxy_id>", methods=["PATCH"])
@admin_required
def proxies_patch(proxy_id):
    body = request.get_json(silent=True) or {}
    if "enabled" in body:
        proxy_pool.set_enabled(proxy_id, bool(body["enabled"]))
    return jsonify({"success": True})


@bp.route("/api/proxies/<int:proxy_id>", methods=["DELETE"])
@admin_required
def proxies_remove(proxy_id):
    proxy_pool.remove(proxy_id)
    return jsonify({"success": True})


@bp.route("/api/proxies/<int:proxy_id>/test", methods=["POST"])
@admin_required
def proxies_test_one(proxy_id):
    rows = [r for r in proxy_pool.list_all() if r["id"] == proxy_id]
    if not rows:
        return jsonify({"success": False, "error": "not found"}), 404
    url = rows[0]["url"]
    try:
        resp = requests.post(
            "https://api.locketcamera.com/getUserByUsername",
            json={"data": {"username": "locket"}},
            proxies={"http": url, "https": url},
            timeout=15,
        )
        ok = resp.status_code < 500
        if ok:
            proxy_pool.mark_ok(proxy_id)
        else:
            proxy_pool.mark_err(proxy_id, f"HTTP {resp.status_code}")
        return jsonify({"success": ok, "status": resp.status_code})
    except Exception as e:
        proxy_pool.mark_err(proxy_id, str(e)[:200])
        return jsonify({"success": False, "error": str(e)}), 502


@bp.route("/api/proxies/master", methods=["PUT"])
@admin_required
def proxies_master():
    body = request.get_json(silent=True) or {}
    proxy_pool.set_master(bool(body.get("enabled")))
    return jsonify({"success": True, "master_enabled": proxy_pool.is_master_on()})


# ---- mobileconfig upload ----


MAX_MOBILECONFIG_BYTES = 5 * 1024 * 1024  # 5 MB ceiling
MOBILECONFIG_HISTORY_LIMIT = 20


def _mobileconfig_path():
    static_dir = os.path.join(current_app.root_path, "static")
    return os.path.join(static_dir, "locket.mobileconfig")


def _record_mobileconfig_history(action, filename=None, size=None, signed=None):
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO mobileconfig_history (action, filename, size, signed, created_at) "
        "VALUES (?,?,?,?,?)",
        (
            action,
            filename,
            int(size) if size is not None else None,
            1 if signed else 0,
            time.time(),
        ),
    )
    # Trim to most-recent N rows so the log doesn't grow unbounded.
    conn.execute(
        "DELETE FROM mobileconfig_history WHERE id NOT IN ("
        "SELECT id FROM mobileconfig_history ORDER BY id DESC LIMIT ?)",
        (MOBILECONFIG_HISTORY_LIMIT,),
    )


def _looks_like_mobileconfig(blob):
    """Accept either a plain XML plist or a CMS/PKCS7-signed .mobileconfig.
    Plain plists start with "<?xml". Signed ones are DER bags whose payload
    contains '<plist' somewhere in the first 4KB."""
    if not blob:
        return False
    head = blob[:4096]
    if head.lstrip().startswith(b"<?xml") or b"<plist" in head:
        return True
    # PKCS7 / signed mobileconfig: DER seq starts with 0x30 0x82 (or 0x30 0x80)
    if blob[:1] == b"\x30":
        return b"<plist" in blob[:8192] or b"-//Apple//DTD PLIST" in blob[:8192]
    return False


@bp.route("/api/mobileconfig", methods=["GET"])
@admin_required
def mobileconfig_info():
    path = _mobileconfig_path()
    if not os.path.exists(path):
        return jsonify({"success": True, "exists": False})
    st = os.stat(path)
    with open(path, "rb") as f:
        blob = f.read(8192)
    return jsonify({
        "success": True,
        "exists": True,
        "size": st.st_size,
        "modified_at": st.st_mtime,
        "signed": blob[:1] == b"\x30",
    })


@bp.route("/api/mobileconfig", methods=["POST"])
@admin_required
def mobileconfig_upload():
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify({"success": False, "error": "Missing file"}), 400

    blob = f.read(MAX_MOBILECONFIG_BYTES + 1)
    if len(blob) == 0:
        return jsonify({"success": False, "error": "Empty file"}), 400
    if len(blob) > MAX_MOBILECONFIG_BYTES:
        return jsonify({"success": False, "error": "File too large (max 5 MB)"}), 400
    if not _looks_like_mobileconfig(blob):
        return jsonify({
            "success": False,
            "error": "File doesn't look like a .mobileconfig (no <plist> found)",
        }), 400

    target = _mobileconfig_path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "wb") as out:
        out.write(blob)
    os.replace(tmp, target)
    st = os.stat(target)
    signed = blob[:1] == b"\x30"
    _record_mobileconfig_history(
        "upload",
        filename=f.filename,
        size=st.st_size,
        signed=signed,
    )
    return jsonify({
        "success": True,
        "size": st.st_size,
        "modified_at": st.st_mtime,
    })


@bp.route("/api/mobileconfig", methods=["DELETE"])
@admin_required
def mobileconfig_remove():
    path = _mobileconfig_path()
    existed = os.path.exists(path)
    if existed:
        os.remove(path)
        _record_mobileconfig_history("delete")
    return jsonify({"success": True})


@bp.route("/api/mobileconfig/history", methods=["GET"])
@admin_required
def mobileconfig_history():
    rows = db.get_conn().execute(
        "SELECT id, action, filename, size, signed, created_at "
        "FROM mobileconfig_history ORDER BY id DESC LIMIT ?",
        (MOBILECONFIG_HISTORY_LIMIT,),
    ).fetchall()
    items = [
        {
            "id": r["id"],
            "action": r["action"],
            "filename": r["filename"],
            "size": r["size"],
            "signed": bool(r["signed"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    return jsonify({"success": True, "items": items})


@bp.route("/api/queue", methods=["GET"])
@admin_required
def queue_snapshot():
    qm = current_app.queue_manager
    rotator = current_app.rotator
    snap = qm.admin_snapshot()
    worker_emails = {}
    for slot_id in list(qm.workers.keys()):
        try:
            worker_emails[slot_id] = rotator.email(slot_id) if rotator else "<no rotator>"
        except KeyError:
            worker_emails[slot_id] = "<removed>"
    return jsonify({"success": True, "workers": worker_emails, **snap})


# ─── TELEGRAM SETTINGS ────────────────────────────────────────────────────────

@bp.route("/api/telegram-settings", methods=["GET"])
@admin_required
def get_telegram_settings():
    return jsonify({"success": True, "telegram": site_settings.get_telegram()})


@bp.route("/api/telegram-settings", methods=["PUT"])
@admin_required
def set_telegram_settings():
    body = request.json or {}
    try:
        saved = site_settings.set_telegram(body)
        return jsonify({"success": True, "telegram": saved})
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)}), 400


@bp.route("/api/telegram-settings/test", methods=["POST"])
@admin_required
def test_telegram():
    """Gửi tin nhắn test để kiểm tra bot hoạt động."""
    from ..notifications import _get_all_telegram_targets, _send_to_all
    import time
    targets = _get_all_telegram_targets()
    if not targets:
        return jsonify({"success": False, "msg": "Chưa cấu hình bot nào (kiểm tra .env hoặc tab Telegram)"}), 400
    try:
        _send_to_all("✅ Test thành công từ Admin!\n⏰ " + time.strftime('%d/%m/%Y %H:%M:%S'))
        return jsonify({"success": True, "msg": f"Đã gửi đến {len(targets)} bot!"})
    except Exception as e:
        return jsonify({"success": False, "msg": str(e)}), 502


# ─── PAYMENT SETTINGS ─────────────────────────────────────────────────────────

@bp.route("/api/payment-settings", methods=["GET"])
@admin_required
def get_payment_settings():
    return jsonify({"success": True, "payment": site_settings.get_payment()})


@bp.route("/api/payment-settings", methods=["PUT"])
@admin_required
def set_payment_settings():
    body = request.json or {}
    try:
        saved = site_settings.set_payment(body)
        return jsonify({"success": True, "payment": saved})
    except (ValueError, TypeError) as e:
        return jsonify({"success": False, "msg": str(e)}), 400


# ─── BANK HISTORY VIEWER ──────────────────────────────────────────────────────

@bp.route("/api/bank-history", methods=["GET"])
@admin_required
def bank_history():
    """Lấy lịch sử giao dịch từ API ngân hàng đang cấu hình (ACB hoặc MB)."""
    cfg = site_settings.get_payment()
    bank_type = cfg.get("bank_type", "acb").lower()
    if bank_type == "mb":
        url = cfg.get("mb_api_url", "")
    else:
        url = cfg.get("acb_api_url", "")

    if not url:
        return jsonify({"success": False, "error": f"Chưa cấu hình URL API {bank_type.upper()}"}), 400

    try:
        resp = requests.get(url, timeout=12)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.Timeout:
        return jsonify({"success": False, "error": "API ngân hàng timeout (>12s)"}), 504
    except requests.exceptions.HTTPError as e:
        return jsonify({"success": False, "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}), 502
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 502

    if data.get("status") != "success":
        return jsonify({
            "success": False,
            "error": data.get("message") or "API trả về lỗi",
            "raw": data,
        }), 502

    transactions = data.get("transactions", [])
    # Chuẩn hóa transactions về format thống nhất
    normalized = []
    for tx in transactions:
        try:
            amount = int(str(tx.get("amount", 0)).replace(",", "").strip())
        except (ValueError, TypeError):
            amount = 0
        normalized.append({
            "transactionID": str(tx.get("transactionID", "")),
            "amount": amount,
            "description": tx.get("description", ""),
            "transactionDate": tx.get("transactionDate", ""),
            "type": tx.get("type", ""),
        })

    return jsonify({
        "success": True,
        "bank_type": bank_type.upper(),
        "count": len(normalized),
        "transactions": normalized,
    })



# ─── PRICING PACKAGES MANAGEMENT ──────────────────────────────────────────────

MAX_PACKAGES = 10


@bp.route("/api/packages", methods=["GET"])
@admin_required
def admin_packages_list():
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT * FROM pricing_packages ORDER BY sort_order ASC, id ASC"
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
            "sort_order": r["sort_order"],
            "enabled": bool(r["enabled"]),
            "created_at": r["created_at"],
        })
    return jsonify({"success": True, "packages": packages})


@bp.route("/api/packages", methods=["POST"])
@admin_required
def admin_packages_add():
    conn = db.get_conn()
    count = conn.execute("SELECT COUNT(*) as c FROM pricing_packages").fetchone()["c"]
    if count >= MAX_PACKAGES:
        return jsonify({"success": False, "error": f"Toi da {MAX_PACKAGES} goi"}), 400

    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    price = body.get("price", 0)
    duration = (body.get("duration") or "Vinh vien").strip()
    description = (body.get("description") or "").strip()
    features = body.get("features", [])
    max_activations = int(body.get("max_activations", 1))
    is_featured = bool(body.get("is_featured", False))
    purchase_count = int(body.get("purchase_count", 0))

    if not name:
        return jsonify({"success": False, "error": "Ten goi la bat buoc"}), 400
    if not isinstance(features, list):
        features = []

    import json as _json
    now = time.time()
    conn.execute(
        "INSERT INTO pricing_packages (name, price, duration, description, features, purchase_count, max_activations, is_featured, sort_order, enabled, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (name, int(price), duration, description, _json.dumps(features), purchase_count, max_activations, 1 if is_featured else 0, count, 1, now)
    )
    return jsonify({"success": True})


@bp.route("/api/packages/<int:pkg_id>", methods=["PUT"])
@admin_required
def admin_packages_update(pkg_id):
    conn = db.get_conn()
    row = conn.execute("SELECT id FROM pricing_packages WHERE id=?", (pkg_id,)).fetchone()
    if not row:
        return jsonify({"success": False, "error": "Khong tim thay goi"}), 404

    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    price = body.get("price", 0)
    duration = (body.get("duration") or "Vinh vien").strip()
    description = (body.get("description") or "").strip()
    features = body.get("features", [])
    max_activations = int(body.get("max_activations", 1))
    is_featured = bool(body.get("is_featured", False))
    purchase_count = int(body.get("purchase_count", 0))
    enabled = bool(body.get("enabled", True))

    if not name:
        return jsonify({"success": False, "error": "Ten goi la bat buoc"}), 400
    if not isinstance(features, list):
        features = []

    import json as _json
    conn.execute(
        "UPDATE pricing_packages SET name=?, price=?, duration=?, description=?, features=?, purchase_count=?, max_activations=?, is_featured=?, enabled=? WHERE id=?",
        (name, int(price), duration, description, _json.dumps(features), purchase_count, max_activations, 1 if is_featured else 0, 1 if enabled else 0, pkg_id)
    )
    return jsonify({"success": True})


@bp.route("/api/packages/<int:pkg_id>", methods=["DELETE"])
@admin_required
def admin_packages_delete(pkg_id):
    conn = db.get_conn()
    # Remove related activations first (foreign key constraint)
    conn.execute("DELETE FROM gold_activations WHERE package_id=?", (pkg_id,))
    conn.execute("DELETE FROM pricing_packages WHERE id=?", (pkg_id,))
    return jsonify({"success": True})


# ─── CONTACT BUBBLE SETTINGS ──────────────────────────────────────────────────

@bp.route("/api/contact-bubble", methods=["GET"])
@admin_required
def contact_bubble_get():
    return jsonify({"success": True, "contact_bubble": site_settings.get_contact_bubble()})


@bp.route("/api/contact-bubble", methods=["PUT"])
@admin_required
def contact_bubble_set():
    body = request.get_json(silent=True) or {}
    saved = site_settings.set_contact_bubble(body)
    return jsonify({"success": True, "contact_bubble": saved})


# ─── COUPONS (Mã giảm giá) ───────────────────────────────────────────────────

@bp.route("/api/coupons", methods=["GET"])
@admin_required
def coupons_list():
    conn = db.get_conn()
    rows = conn.execute("SELECT * FROM coupons ORDER BY created_at DESC").fetchall()
    items = [
        {
            "id": r["id"],
            "code": r["code"],
            "discount_percent": r["discount_percent"],
            "max_uses": r["max_uses"],
            "used_count": r["used_count"],
            "enabled": bool(r["enabled"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    return jsonify({"success": True, "coupons": items})


@bp.route("/api/coupons", methods=["POST"])
@admin_required
def coupons_add():
    body = request.get_json(silent=True) or {}
    code = (body.get("code") or "").strip().upper().replace(" ", "")
    discount_percent = body.get("discount_percent", 0)
    max_uses = body.get("max_uses")

    if not code:
        return jsonify({"success": False, "error": "Mã giảm giá là bắt buộc"}), 400
    try:
        discount_percent = int(discount_percent)
    except (ValueError, TypeError):
        return jsonify({"success": False, "error": "Phần trăm giảm giá không hợp lệ"}), 400
    if discount_percent < 1 or discount_percent > 100:
        return jsonify({"success": False, "error": "Phần trăm phải từ 1-100"}), 400

    if max_uses is not None and max_uses != "":
        try:
            max_uses = int(max_uses)
        except (ValueError, TypeError):
            max_uses = None
    else:
        max_uses = None

    conn = db.get_conn()
    existing = conn.execute("SELECT 1 FROM coupons WHERE code=?", (code,)).fetchone()
    if existing:
        return jsonify({"success": False, "error": f"Mã '{code}' đã tồn tại"}), 409

    conn.execute(
        "INSERT INTO coupons (code, discount_percent, max_uses, used_count, enabled, created_at) VALUES (?,?,?,0,1,?)",
        (code, discount_percent, max_uses, time.time())
    )
    return jsonify({"success": True})


@bp.route("/api/coupons/<int:coupon_id>", methods=["PUT"])
@admin_required
def coupons_update(coupon_id):
    body = request.get_json(silent=True) or {}
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM coupons WHERE id=?", (coupon_id,)).fetchone()
    if not row:
        return jsonify({"success": False, "error": "Không tìm thấy mã"}), 404

    enabled = body.get("enabled", row["enabled"])
    conn.execute(
        "UPDATE coupons SET enabled=? WHERE id=?",
        (1 if enabled else 0, coupon_id)
    )
    return jsonify({"success": True})


@bp.route("/api/coupons/<int:coupon_id>", methods=["DELETE"])
@admin_required
def coupons_delete(coupon_id):
    conn = db.get_conn()
    conn.execute("DELETE FROM coupons WHERE id=?", (coupon_id,))
    return jsonify({"success": True})


# ─── USER MANAGEMENT ──────────────────────────────────────────────────────────

@bp.route("/api/users", methods=["GET"])
@admin_required
def users_list():
    q = (request.args.get("q") or "").strip()
    conn = db.get_conn()
    if q:
        rows = conn.execute(
            "SELECT id, username, created_at FROM user_accounts WHERE username LIKE ? ORDER BY created_at DESC LIMIT 50",
            (f"%{q}%",)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, username, created_at FROM user_accounts ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
    users = [{"id": r["id"], "username": r["username"], "created_at": r["created_at"]} for r in rows]
    return jsonify({"success": True, "users": users})


@bp.route("/api/users/<int:user_id>/password", methods=["PUT"])
@admin_required
def users_change_password(user_id):
    import hashlib
    body = request.get_json(silent=True) or {}
    password = (body.get("password") or "").strip()
    if len(password) < 6:
        return jsonify({"success": False, "error": "Mật khẩu phải có ít nhất 6 ký tự"}), 400

    salted = "LOCKET_SALT_2025:" + password
    password_hash = hashlib.sha256(salted.encode()).hexdigest()

    conn = db.get_conn()
    row = conn.execute("SELECT id FROM user_accounts WHERE id=?", (user_id,)).fetchone()
    if not row:
        return jsonify({"success": False, "error": "User không tồn tại"}), 404

    conn.execute("UPDATE user_accounts SET password_hash=? WHERE id=?", (password_hash, user_id))
    return jsonify({"success": True})


# ─── VIDEO SETTINGS ───────────────────────────────────────────────────────────

@bp.route("/api/video-settings", methods=["GET"])
@admin_required
def video_settings_get():
    conn = db.get_conn()
    row = conn.execute("SELECT value FROM site_settings WHERE key='video_url'").fetchone()
    video_url = ""
    if row:
        try:
            import json as _json
            video_url = _json.loads(row["value"]).get("url", "")
        except Exception:
            video_url = row["value"] if isinstance(row["value"], str) else ""
    return jsonify({"success": True, "video_url": video_url})


@bp.route("/api/video-settings", methods=["PUT"])
@admin_required
def video_settings_set():
    body = request.get_json(silent=True) or {}
    video_url = (body.get("video_url") or "").strip()
    import json as _json
    payload = _json.dumps({"url": video_url})
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO site_settings (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        ("video_url", payload, time.time()),
    )
    return jsonify({"success": True})


# ─── LOCKET 15s SETTINGS ──────────────────────────────────────────────────────

@bp.route("/api/locket15s", methods=["GET"])
@admin_required
def locket15s_get():
    """Lấy cài đặt trang Locket 15s (guide HTML, video URL, DNS info)."""
    import json as _json
    conn = db.get_conn()

    # Guide HTML
    row = conn.execute("SELECT value FROM site_settings WHERE key='locket15s_guide'").fetchone()
    guide_html = ""
    if row:
        try:
            guide_html = _json.loads(row["value"]).get("html", "")
        except Exception:
            guide_html = row["value"] if isinstance(row["value"], str) else ""

    # Video URL
    row2 = conn.execute("SELECT value FROM site_settings WHERE key='locket15s_video'").fetchone()
    video_url = ""
    if row2:
        try:
            video_url = _json.loads(row2["value"]).get("url", "")
        except Exception:
            video_url = ""

    # DNS file info
    dns_path = os.path.join(current_app.root_path, "static", "locket15s_dns.mobileconfig")
    dns_exists = os.path.exists(dns_path)
    dns_size = os.path.getsize(dns_path) if dns_exists else 0

    return jsonify({
        "success": True,
        "guide_html": guide_html,
        "video_url": video_url,
        "dns_exists": dns_exists,
        "dns_size": dns_size,
    })


@bp.route("/api/locket15s/guide", methods=["PUT"])
@admin_required
def locket15s_guide_set():
    """Cập nhật nội dung hướng dẫn (HTML) cho trang Locket 15s."""
    import json as _json
    body = request.get_json(silent=True) or {}
    guide_html = (body.get("guide_html") or "").strip()
    payload = _json.dumps({"html": guide_html})
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO site_settings (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        ("locket15s_guide", payload, time.time()),
    )
    return jsonify({"success": True})


@bp.route("/api/locket15s/video", methods=["PUT"])
@admin_required
def locket15s_video_set():
    """Cập nhật video URL cho trang Locket 15s."""
    import json as _json
    body = request.get_json(silent=True) or {}
    video_url = (body.get("video_url") or "").strip()
    payload = _json.dumps({"url": video_url})
    conn = db.get_conn()
    conn.execute(
        "INSERT INTO site_settings (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        ("locket15s_video", payload, time.time()),
    )
    return jsonify({"success": True})


MAX_DNS_FILE_BYTES = 5 * 1024 * 1024  # 5 MB


@bp.route("/api/locket15s/dns", methods=["POST"])
@admin_required
def locket15s_dns_upload():
    """Upload DNS config file (.mobileconfig) cho trang Locket 15s."""
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify({"success": False, "error": "Missing file"}), 400

    blob = f.read(MAX_DNS_FILE_BYTES + 1)
    if len(blob) == 0:
        return jsonify({"success": False, "error": "Empty file"}), 400
    if len(blob) > MAX_DNS_FILE_BYTES:
        return jsonify({"success": False, "error": "File too large (max 5 MB)"}), 400

    target = os.path.join(current_app.root_path, "static", "locket15s_dns.mobileconfig")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "wb") as out:
        out.write(blob)
    os.replace(tmp, target)
    st = os.stat(target)
    return jsonify({"success": True, "size": st.st_size})


@bp.route("/api/locket15s/dns", methods=["DELETE"])
@admin_required
def locket15s_dns_delete():
    """Xóa DNS config file."""
    dns_path = os.path.join(current_app.root_path, "static", "locket15s_dns.mobileconfig")
    if os.path.exists(dns_path):
        os.remove(dns_path)
    return jsonify({"success": True})
