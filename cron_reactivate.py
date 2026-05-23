"""Tự động re-activate Locket Gold cho user đã có trong database.

Tương đương file PHP `cron_re_activate.php` nhưng chạy thẳng trên codebase
Python này. Đọc lịch sử Gold từ SQLite (`gold_activations` nếu có — bảng
PR mới; ngược lại fallback sang `gold_purchases` đã tồn tại) và gọi lại
RevenueCat /v1/receipts thông qua `LocketAPI.restorePurchase(uid)` để giữ
Gold không bị Apple thu hồi.

═══════════════════════════════════════════════════════════════════════════
LOGIC GIỐNG PHP CRON
═══════════════════════════════════════════════════════════════════════════
- SINGLE RECEIPT (mặc định): codebase này có 1 fetch_token cứng trong
  `locket/locket_api.py::HARDCODED_PAYLOAD`. Một receipt chỉ có thể "thuộc về"
  1 UID trên RevenueCat tại 1 thời điểm. Re-activate nhiều UID liên tiếp sẽ
  gây ping-pong (UID sau cướp Gold của UID trước). Mode này CHỈ refresh UID
  được kích hoạt GẦN NHẤT — đảm bảo user mới nhất luôn có Gold.

- MULTI RECEIPT (--all): khi đã có nhiều receipt trên RevenueCat (mỗi UID
  một fetch_token riêng). Lúc đó scan toàn bộ và refresh từng UID là an toàn.
  Mode này dành cho người tự host nhiều account/receipt; mặc định KHÔNG nên
  bật khi codebase còn dùng 1 HARDCODED_PAYLOAD.

═══════════════════════════════════════════════════════════════════════════
CÁC TÙY CHỌN CHÍNH
═══════════════════════════════════════════════════════════════════════════
  --all                  Scan toàn bộ (multi-receipt mode)
  --max-age-hours N      Bỏ qua activation cũ hơn N giờ (mặc định 720h = 30 ngày)
  --min-interval-hours N Bỏ qua UID đã refresh trong N giờ qua (mặc định 6h)
  --limit N              Chỉ xử lý tối đa N username mỗi vòng
  --rate-limit-seconds X Sleep X giây giữa mỗi request (mặc định 1.5s)
  --username NAME        Chỉ refresh duy nhất username này (override DB)
  --dry-run              In kế hoạch, KHÔNG gọi RevenueCat
  --daemon               Chạy lặp vô hạn, mỗi `--interval` giây
  --interval N           Chu kỳ daemon (mặc định 600s = 10 phút)

═══════════════════════════════════════════════════════════════════════════
CRONTAB (chạy qua cron Linux)
═══════════════════════════════════════════════════════════════════════════
  # Mỗi 10 phút
  */10 * * * * cd /opt/locket && /usr/bin/python3 cron_reactivate.py \\
      >> /var/log/locket-reactivate.log 2>&1

  # Hoặc chạy daemon thay cho cron — interval 10 phút
  python3 cron_reactivate.py --daemon --interval 600

═══════════════════════════════════════════════════════════════════════════
"""

import argparse
import logging
import os
import random
import signal
import sys
import time
from pathlib import Path

# Cho phép import package `locket.*` khi chạy trực tiếp từ repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from locket import db  # noqa: E402

logger = logging.getLogger("reactivate")


# ─── Schema helpers ──────────────────────────────────────────────────────────

def _ensure_reactivation_log(conn):
    """Bảng riêng theo dõi từng lần refresh — KHÔNG đụng schema có sẵn nên
    chạy được trên cả nhánh main (chỉ có gold_purchases) và PR
    (có gold_activations). Idempotent."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS reactivation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            locket_username TEXT NOT NULL,
            uid TEXT,
            success INTEGER NOT NULL,
            message TEXT,
            reactivated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_reactivation_log_username
            ON reactivation_log(locket_username);
        CREATE INDEX IF NOT EXISTS idx_reactivation_log_at
            ON reactivation_log(reactivated_at DESC);
        """
    )


def _table_exists(conn, name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _select_targets(conn, args):
    """Trả về danh sách dict {locket_username, activated_at, source}.

    Ưu tiên `gold_activations` (PR branch); fallback `gold_purchases` (main).
    Loại bỏ duplicate username — chỉ giữ bản ghi mới nhất cho mỗi username,
    vì RevenueCat chỉ cần biết UID, không cần biết bao nhiêu lần đã activate.

    `--max-age-hours` chặn activation quá cũ (mặc định 30 ngày — match PHP
    cron). `--min-interval-hours` bỏ qua username đã được cron này refresh
    trong N giờ qua, dựa trên reactivation_log.
    """
    cutoff = time.time() - args.max_age_hours * 3600
    skip_cutoff = time.time() - args.min_interval_hours * 3600

    rows = []
    if _table_exists(conn, "gold_activations"):
        # Status 'active' / 'reactivated' đều coi là user đang dùng Gold.
        # 'expired' là user mà admin chủ động thu hồi → bỏ qua.
        rows = list(conn.execute(
            """
            SELECT locket_username, MAX(activated_at) AS last_at
              FROM gold_activations
             WHERE status IN ('active','reactivated')
               AND activated_at > ?
             GROUP BY locket_username
             ORDER BY last_at DESC
            """,
            (cutoff,),
        ))
        source = "gold_activations"
    elif _table_exists(conn, "gold_purchases"):
        rows = list(conn.execute(
            """
            SELECT locket_username, MAX(purchased_at) AS last_at
              FROM gold_purchases
             WHERE purchased_at > ?
             GROUP BY locket_username
             ORDER BY last_at DESC
            """,
            (cutoff,),
        ))
        source = "gold_purchases"
    else:
        return [], "none"

    # Lọc theo reactivation_log — bỏ qua username vừa refresh thành công gần đây.
    last_ok_map = {
        r["locket_username"]: r["last_at"]
        for r in conn.execute(
            "SELECT locket_username, MAX(reactivated_at) AS last_at "
            "FROM reactivation_log WHERE success=1 GROUP BY locket_username"
        )
    }

    targets = []
    for r in rows:
        username = r["locket_username"]
        if not username:
            continue
        last_ok = last_ok_map.get(username, 0.0)
        if last_ok and last_ok > skip_cutoff:
            continue  # đã refresh trong N giờ qua, để dành lượt cho UID khác
        targets.append({
            "username": username,
            "activated_at": r["last_at"] or 0.0,
            "last_reactivated_at": last_ok,
        })
    return targets, source


# ─── Locket API helpers ──────────────────────────────────────────────────────

def _build_rotator():
    """Khởi tạo AccountRotator độc lập (không cần Flask app). Trả về None
    nếu chưa có account nào trong DB."""
    from locket.rotator import AccountRotator
    rotator = AccountRotator()
    if rotator.size() == 0:
        return None
    return rotator


def _get_fresh_api(rotator):
    """Lấy 1 LocketAPI từ pool với token đã refresh. Random slot để phân tán
    tải qua nhiều account khi pool có >1 slot."""
    slot_ids = rotator.list_ids()
    if not slot_ids:
        return None
    slot_id = random.choice(slot_ids)
    api = rotator.ensure_fresh(slot_id)
    return api


def _reactivate_one(api, username, dry_run=False):
    """Gọi getUserByUsername → restorePurchase. Trả về (success, message, uid)."""
    # Bước 1: username → UID
    try:
        info = api.getUserByUsername(username)
    except Exception as e:
        return False, f"getUserByUsername failed: {e}", None

    user_data = (info or {}).get("result", {}).get("data") or {}
    uid = user_data.get("uid")
    if not uid:
        return False, "UID not found in Locket response", None

    if dry_run:
        return True, f"[DRY] would call restorePurchase(uid={uid})", uid

    # Bước 2: restore purchase qua RevenueCat
    try:
        result = api.restorePurchase(uid)
    except Exception as e:
        return False, f"restorePurchase failed: {e}", uid

    ents = (result or {}).get("subscriber", {}).get("entitlements", {}) or {}
    gold = ents.get("Gold", {}) or {}
    prod = gold.get("product_identifier")
    if prod:
        return True, f"Gold restored ({prod})", uid
    # Một số response không trả product_identifier nhưng vẫn có entitlement
    # đang active. Dù vậy, an toàn nhất là báo lỗi để admin xem log.
    return False, f"No Gold entitlement returned (raw keys={list(ents.keys())})", uid


# ─── Cron loops ──────────────────────────────────────────────────────────────

def _log_attempt(conn, username, uid, success, message):
    conn.execute(
        "INSERT INTO reactivation_log (locket_username, uid, success, message, reactivated_at) "
        "VALUES (?,?,?,?,?)",
        (username, uid, 1 if success else 0, message, time.time()),
    )


def run_once(args):
    """Chạy 1 vòng. Trả về dict thống kê {ok, fail, skipped, total}."""
    db.init()
    conn = db.get_conn()
    _ensure_reactivation_log(conn)

    # Override: chỉ xử lý 1 username cụ thể (debug)
    if args.username:
        targets = [{"username": args.username, "activated_at": time.time(), "last_reactivated_at": 0.0}]
        source = "cli-override"
    else:
        targets, source = _select_targets(conn, args)

    if not targets:
        logger.info("[DONE] No targets to re-activate (source=%s)", source)
        return {"ok": 0, "fail": 0, "skipped": 0, "total": 0}

    pool_size = 1  # codebase hiện tại = 1 hardcoded receipt
    is_single_receipt = pool_size <= 1 and not args.all
    mode_label = "SINGLE RECEIPT (latest only)" if is_single_receipt else "MULTI RECEIPT (full scan)"
    if is_single_receipt:
        # Match PHP cron logic: chỉ refresh username mới nhất → tránh ping-pong.
        targets = targets[:1]
    elif args.limit:
        targets = targets[: args.limit]

    logger.info("=== Cron Re-Activate ===")
    logger.info("Source : %s", source)
    logger.info("Mode   : %s", mode_label)
    logger.info("Targets: %d username(s)", len(targets))

    # Khởi tạo rotator để có Locket API
    rotator = _build_rotator()
    if rotator is None:
        logger.error("[ABORT] No Locket account in pool. Add via /admin first.")
        return {"ok": 0, "fail": 0, "skipped": len(targets), "total": len(targets)}

    api = _get_fresh_api(rotator)
    if api is None:
        logger.error("[ABORT] Cannot acquire fresh LocketAPI from rotator.")
        return {"ok": 0, "fail": 0, "skipped": len(targets), "total": len(targets)}

    ok = fail = skipped = 0
    for idx, t in enumerate(targets):
        username = t["username"]
        prefix = f"[{idx + 1}/{len(targets)}] {username}"

        success, message, uid = _reactivate_one(api, username, dry_run=args.dry_run)
        if success:
            ok += 1
            logger.info("OK    %s — uid=%s — %s", prefix, uid, message)
        else:
            fail += 1
            logger.warning("FAIL  %s — %s", prefix, message)

        if not args.dry_run:
            try:
                _log_attempt(conn, username, uid, success, message)
            except Exception as e:
                logger.warning("Could not write reactivation_log: %s", e)

        # Rate-limit giữa các request — RevenueCat 429 nếu burst
        if idx + 1 < len(targets) and args.rate_limit_seconds > 0:
            time.sleep(args.rate_limit_seconds)

    logger.info("=== Summary === OK: %d | Fail: %d | Skip: %d | Total: %d",
                ok, fail, skipped, len(targets))
    return {"ok": ok, "fail": fail, "skipped": skipped, "total": len(targets)}


def run_daemon(args):
    """Lặp vô hạn cho đến khi nhận SIGINT/SIGTERM."""
    stop = {"flag": False}

    def _handler(signum, _frame):
        logger.info("Got signal %d → exiting after current cycle", signum)
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)

    cycle = 0
    while not stop["flag"]:
        cycle += 1
        logger.info("─── Cycle #%d ───", cycle)
        try:
            run_once(args)
        except Exception as e:
            logger.exception("Cycle #%d crashed: %s", cycle, e)

        if stop["flag"]:
            break
        # Sleep theo từng giây để phản hồi tín hiệu nhanh
        for _ in range(args.interval):
            if stop["flag"]:
                break
            time.sleep(1)
    logger.info("Daemon exited cleanly after %d cycle(s)", cycle)


# ─── CLI entrypoint ──────────────────────────────────────────────────────────

def _build_parser():
    p = argparse.ArgumentParser(
        description="Tự động re-activate Locket Gold cho user đã có trong DB.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Ví dụ:\n"
            "  python cron_reactivate.py                       # 1 lần, single-receipt\n"
            "  python cron_reactivate.py --dry-run             # in kế hoạch, không gọi RC\n"
            "  python cron_reactivate.py --daemon              # chạy nền, mỗi 10 phút\n"
            "  python cron_reactivate.py --all --limit 25      # multi-receipt mode\n"
            "  python cron_reactivate.py --username phan_tino  # refresh 1 user cụ thể\n"
        ),
    )
    p.add_argument("--all", action="store_true",
                   help="Multi-receipt mode: scan toàn bộ DB (mặc định: chỉ refresh username mới nhất)")
    p.add_argument("--max-age-hours", type=int, default=24 * 30,
                   help="Bỏ qua activation cũ hơn N giờ (mặc định 720h = 30 ngày)")
    p.add_argument("--min-interval-hours", type=int, default=6,
                   help="Bỏ qua username đã refresh trong N giờ qua (mặc định 6h)")
    p.add_argument("--limit", type=int, default=25,
                   help="Tối đa N username/vòng khi --all (mặc định 25, match PHP cron)")
    p.add_argument("--rate-limit-seconds", type=float, default=1.5,
                   help="Sleep giữa các request RevenueCat (mặc định 1.5s)")
    p.add_argument("--username", default=None,
                   help="Chỉ refresh username này (override DB scan)")
    p.add_argument("--dry-run", action="store_true",
                   help="In kế hoạch, KHÔNG gọi RevenueCat")
    p.add_argument("--daemon", action="store_true",
                   help="Chạy lặp vô hạn — không cần crontab nữa")
    p.add_argument("--interval", type=int, default=600,
                   help="Chu kỳ daemon, giây (mặc định 600s = 10 phút)")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Log chi tiết hơn (DEBUG level)")
    return p


def main():
    args = _build_parser().parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Hạn chế noise từ requests/urllib3 trừ khi -v
    if not args.verbose:
        logging.getLogger("urllib3").setLevel(logging.WARNING)

    # .env (nếu có) — để có FLASK_SECRET_KEY / EMAIL / PASSWORD seed cho rotator
    try:
        import dotenv
        dotenv.load_dotenv()
    except Exception:
        pass

    if args.daemon:
        run_daemon(args)
        return 0

    stats = run_once(args)
    return 0 if stats["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
