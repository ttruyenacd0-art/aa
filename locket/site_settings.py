"""Site-wide toggleable settings (popup notice + maintenance mode).

Two JSON blobs stored in the `site_settings` key/value table:

- `popup`      → {enabled, title, message, icon, button_text}
- `maintenance`→ {enabled, end_at (ISO+TZ), title, message,
                  contacts:[{role,name,url}], allow_admin}

Reads are cheap and uncached — SQLite + tiny rows. Public endpoint hits
this on every page load.
"""

import json
import threading
import time

from . import db


POPUP_KEY = "popup"
MAINT_KEY = "maintenance"
THEME_KEY = "theme"
LAYOUT_KEY = "layout"

PAYMENT_KEY = "payment_settings"
TELEGRAM_KEY = "telegram_settings"
CONTACT_BUBBLE_KEY = "contact_bubble"
THEMES = ("gold", "aurora", "sunset", "mono")
LAYOUTS = ("stacked", "split", "spotlight")

_DEFAULTS = {
    POPUP_KEY: {
        "enabled": False,
        "title": "Thông báo",
        "message": "",
        "icon": "info",
    },
    THEME_KEY: {"name": "gold"},
    LAYOUT_KEY: {"name": "stacked"},
    CONTACT_BUBBLE_KEY: {
        "enabled": False,
        "type": "zalo",
        "phone": "",
        "zalo_link": "",
        "label": "Liên hệ",
    },
    TELEGRAM_KEY: {
        "bot_token": "",
        "chat_id": "",
    },
    PAYMENT_KEY: {
        "bank_type": "acb",
        "acb_api_url": "https://acb.modtool.fun/historyapiacbv2/8537e321004f87ce4a967f302ead6925",
        "mb_api_url": "",
        "amount": 20000,
        "bank_account": "21023541",
        "bank_name": "ACB",
        "account_name": "PHAN MINH LUAN",
        "description_prefix": "LOCKET",
    },
    MAINT_KEY: {
        "enabled": False,
        "end_at": "",
        "start_at": "",
        "title": "Bảo Trì Máy Chủ",
        "message": (
            "Hệ thống đang được nâng cấp để mang đến trải nghiệm tốt hơn. "
            "Vui lòng quay lại sau khi bảo trì hoàn tất."
        ),
        "notice": (
            "Máy chủ đang trong quá trình bảo trì định kỳ, không phải gặp sự "
            "cố hay sập máy chủ. Toàn bộ dữ liệu của bạn vẫn được bảo toàn an "
            "toàn. Hệ thống sẽ hoạt động bình thường trở lại sau khi bảo trì "
            "hoàn tất. Cảm ơn bạn đã kiên nhẫn chờ đợi!"
        ),
        "contacts": [
            {"role": "Founder", "name": "nguyenthanhson.dev",
             "url": "https://nguyenthanhson.dev"},
            {"role": "Founder", "name": "maihuybao.dev",
             "url": "https://maihuybao.dev"},
        ],
        "allow_admin": True,
    },
}

_lock = threading.Lock()


def _read(key):
    row = db.get_conn().execute(
        "SELECT value FROM site_settings WHERE key=?", (key,)
    ).fetchone()
    if row is None:
        return dict(_DEFAULTS[key])
    try:
        merged = dict(_DEFAULTS[key])
        merged.update(json.loads(row["value"]))
        return merged
    except (ValueError, TypeError):
        return dict(_DEFAULTS[key])


def _write(key, value):
    payload = json.dumps(value)
    db.get_conn().execute(
        "INSERT INTO site_settings (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, payload, time.time()),
    )


def get_popup():
    with _lock:
        return _read(POPUP_KEY)


def set_popup(value):
    cur = get_popup()
    cur.update({k: v for k, v in (value or {}).items() if k in _DEFAULTS[POPUP_KEY]})
    cur["enabled"] = bool(cur.get("enabled"))
    if cur["icon"] not in ("info", "success", "warning", "error", "question"):
        cur["icon"] = "info"
    with _lock:
        _write(POPUP_KEY, cur)
    return cur


def get_maintenance():
    with _lock:
        return _read(MAINT_KEY)


def set_maintenance(value):
    cur = get_maintenance()
    allowed = set(_DEFAULTS[MAINT_KEY].keys())
    for k, v in (value or {}).items():
        if k in allowed:
            cur[k] = v
    cur["enabled"] = bool(cur.get("enabled"))
    cur["allow_admin"] = bool(cur.get("allow_admin", True))
    if not isinstance(cur.get("contacts"), list):
        cur["contacts"] = list(_DEFAULTS[MAINT_KEY]["contacts"])
    with _lock:
        _write(MAINT_KEY, cur)
    return cur


def get_theme():
    with _lock:
        v = _read(THEME_KEY)
    name = v.get("name") if isinstance(v, dict) else None
    if name not in THEMES:
        name = "gold"
    return {"name": name}


def set_theme(value):
    name = (value or {}).get("name")
    if name not in THEMES:
        raise ValueError(f"Unknown theme '{name}'. Allowed: {', '.join(THEMES)}")
    with _lock:
        _write(THEME_KEY, {"name": name})
    return {"name": name}


def get_layout():
    with _lock:
        v = _read(LAYOUT_KEY)
    name = v.get("name") if isinstance(v, dict) else None
    if name not in LAYOUTS:
        name = "stacked"
    return {"name": name}


def set_layout(value):
    name = (value or {}).get("name")
    if name not in LAYOUTS:
        raise ValueError(f"Unknown layout '{name}'. Allowed: {', '.join(LAYOUTS)}")
    with _lock:
        _write(LAYOUT_KEY, {"name": name})
    return {"name": name}


def get_telegram():
    with _lock:
        return _read(TELEGRAM_KEY)


def set_telegram(value):
    allowed = {"bot_token", "chat_id"}
    cleaned = {k: str(v).strip() for k, v in value.items() if k in allowed}
    with _lock:
        _write(TELEGRAM_KEY, cleaned)
    return get_telegram()


def get_payment():
    with _lock:
        return _read(PAYMENT_KEY)


def set_payment(value):
    allowed = {"bank_type", "acb_api_url", "mb_api_url", "amount", "bank_account", "bank_name", "account_name", "description_prefix"}
    cleaned = {k: v for k, v in value.items() if k in allowed}
    if "amount" in cleaned:
        cleaned["amount"] = int(cleaned["amount"])
    if "bank_type" in cleaned and cleaned["bank_type"] not in ("acb", "mb"):
        cleaned["bank_type"] = "acb"
    with _lock:
        _write(PAYMENT_KEY, cleaned)
    return get_payment()


def public_view():
    """Trimmed payload safe to expose to anonymous clients."""
    pay = get_payment()
    # Video URL
    video_url = ""
    try:
        row = db.get_conn().execute("SELECT value FROM site_settings WHERE key='video_url'").fetchone()
        if row:
            import json
            video_url = json.loads(row["value"]).get("url", "")
    except Exception:
        pass
    return {
        "popup": get_popup(),
        "maintenance": get_maintenance(),
        "theme": get_theme(),
        "layout": get_layout(),
        "payment_amount": int(pay.get("amount", 20000)),
        "bank_name": pay.get("bank_name", "ACB"),
        "contact_bubble": get_contact_bubble(),
        "video_url": video_url,
    }


def get_contact_bubble():
    with _lock:
        return _read(CONTACT_BUBBLE_KEY)


def set_contact_bubble(value):
    cur = get_contact_bubble()
    allowed = set(_DEFAULTS[CONTACT_BUBBLE_KEY].keys())
    for k, v in (value or {}).items():
        if k in allowed:
            cur[k] = v
    cur["enabled"] = bool(cur.get("enabled"))
    if cur.get("type") not in ("zalo", "phone"):
        cur["type"] = "zalo"
    with _lock:
        _write(CONTACT_BUBBLE_KEY, cur)
    return cur
