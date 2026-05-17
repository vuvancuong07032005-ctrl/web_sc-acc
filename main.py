"""
AccShop Bot — muathengay.vn Automation
Tự động tạo đơn thẻ cào trên muathengay.vn khi có đơn mới từ web_shop.
"""

import asyncio
import os
import json
import re
import logging
from datetime import datetime

import firebase_admin
from firebase_admin import credentials, db as firebase_db
from playwright.async_api import async_playwright
import httpx
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("accshop-bot")

# ── Config ──────────────────────────────────────────────────────────────────
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL", "")
TELEGRAM_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT   = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
ORDER_EMAIL     = os.getenv("ORDER_EMAIL", "bot@example.com")
ORDER_TIMEOUT   = int(os.getenv("ORDER_TIMEOUT_SECONDS", "1800"))
POLL_INTERVAL   = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
WATCH_INTERVAL  = int(os.getenv("WATCH_INTERVAL_SECONDS", "5"))

# ── Firebase ─────────────────────────────────────────────────────────────────
_sa = os.getenv("FIREBASE_SERVICE_ACCOUNT", "")
cred = credentials.Certificate(json.loads(_sa)) if _sa else credentials.Certificate("serviceAccount.json")
firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})

# ── Carrier map — theo đúng tên hiển thị trên muathengay.vn ─────────────────
CARRIER_LABEL = {
    "viettel":      "Viettel",
    "mobifone":     "Mobifone",
    "vinaphone":    "Vinaphone",
    "vietnamobile": "Vietnamobile",
    "gmobile":      "Gmobile",
    "garena":       "Garena",
    "zing":         "Zing",
    "vcoin":        "VCoin",
    "funcard":      "Funcard",
    "scoin":        "Scoin",
}

def fmt_vn(amount: int) -> str:
    """500000 → '500.000' (định dạng VN, không có đ)"""
    return f"{amount:,}".replace(",", ".")

def fmt_money(amount: int) -> str:
    return fmt_vn(amount) + "đ"

# ── Telegram ─────────────────────────────────────────────────────────────────
async def tg(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"},
            )
    except Exception as e:
        log.error(f"Telegram error: {e}")

# ── Firebase helpers ──────────────────────────────────────────────────────────
def db_update(order_id: str, data: dict):
    firebase_db.reference(f"orders/{order_id}").update(data)

def db_get_pending() -> dict:
    try:
        orders = firebase_db.reference("orders") \
            .order_by_child("status").equal_to("pending_bot").get()
        if not orders:
            return {}
        return {oid: o for oid, o in orders.items() if (o.get("type") or "") == "card"}
    except Exception as e:
        log.error(f"Firebase query error: {e}")
        return {}

# ── Trích xuất thông tin thanh toán ──────────────────────────────────────────
async def extract_payment_info(page, denomination: int) -> dict:
    """
    Sau khi click Thanh toán, muathengay.vn chuyển sang trang hiển thị
    thông tin chuyển khoản ngân hàng. Trích xuất các trường cần thiết.
    """
    info = {"amount": denomination}
    await page.wait_for_timeout(2000)

    # Các selector thường gặp trên trang thanh toán
    field_selectors = {
        "bankName": [
            ".bank-name", "[class*='bank-name']", "[class*='ten-ngan-hang']",
            "td:has-text('Ngân hàng') + td", ".payment-bank",
            "tr:has-text('Ngân hàng') td:last-child",
        ],
        "accountNumber": [
            ".account-number", "[class*='account-number']", ".stk",
            "td:has-text('Số tài khoản') + td", "[class*='so-tai-khoan']",
            "tr:has-text('Số tài khoản') td:last-child",
            "tr:has-text('STK') td:last-child",
        ],
        "accountHolder": [
            ".account-holder", "[class*='account-holder']",
            "td:has-text('Chủ tài khoản') + td",
            "tr:has-text('Chủ tài khoản') td:last-child",
            "tr:has-text('Tên tài khoản') td:last-child",
        ],
        "transferContent": [
            ".transfer-content", ".noi-dung", ".ma-giao-dich",
            "td:has-text('Nội dung') + td", "[class*='noi-dung']",
            "tr:has-text('Nội dung') td:last-child",
            "tr:has-text('Mã giao dịch') td:last-child",
        ],
    }

    for field, sels in field_selectors.items():
        for sel in sels:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    text = (await el.inner_text(timeout=3000)).strip()
                    if text and 2 < len(text) < 200:
                        info[field] = text
                        log.info(f"  {field}: {text!r}")
                        break
            except Exception:
                pass

    # Fallback: regex tìm số tài khoản trong HTML
    if not info.get("accountNumber"):
        try:
            content = await page.content()
            for m in re.findall(r"\b(\d{9,16})\b", content):
                if not m.startswith(("202", "201", "200", "199", "000")):
                    info["accountNumber"] = m
                    log.info(f"  accountNumber (regex): {m}")
                    break
        except Exception:
            pass

    return info

# ── Poll mã thẻ ───────────────────────────────────────────────────────────────
async def poll_card_code(page, order_id: str) -> str | None:
    """
    Poll trang kết quả muathengay.vn để tìm tên thẻ / số thẻ / số serial.
    Trả về string dạng 'Tên: X | Số thẻ: X | Serial: X' hoặc chỉ mã thẻ.
    """
    deadline = asyncio.get_event_loop().time() + ORDER_TIMEOUT
    attempt  = 0

    # Selector cho thông tin thẻ trên trang kết quả
    card_selectors = [
        ".card-code", ".ma-the", ".serial", "[class*='card-code']",
        "[class*='ma-the']", "[class*='serial']",
        "td:has-text('Số thẻ') + td", "td:has-text('Mã thẻ') + td",
        "td:has-text('Serial') + td", "td:has-text('Pin') + td",
        ".result-code", ".topup-code", "[data-field='serial']",
        "tr:has-text('Số thẻ') td:last-child",
        "tr:has-text('Mã thẻ') td:last-child",
        "tr:has-text('Serial') td:last-child",
        "tr:has-text('Pin') td:last-child",
    ]

    card_code_pattern = re.compile(r"\b\d[\d\-\s]{8,25}\d\b")

    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(POLL_INTERVAL)
        attempt += 1
        remaining = int(deadline - asyncio.get_event_loop().time())
        log.info(f"[{order_id}] Poll #{attempt} — còn {remaining}s")

        try:
            await page.reload(wait_until="networkidle", timeout=20_000)
            await page.wait_for_timeout(2000)

            # Thử lấy nhiều trường: tên thẻ, số thẻ, serial
            card_info = {}
            content = await page.content()

            # Selector đặc thù
            for sel in card_selectors:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        val = (await el.inner_text(timeout=2000)).strip()
                        clean = re.sub(r"[\s\-]", "", val)
                        if re.fullmatch(r"\d{9,20}", clean):
                            log.info(f"[{order_id}] Mã thẻ (selector): {val}")
                            return val
                except Exception:
                    pass

            # Thử tìm bảng kết quả có các trường thẻ
            serial_match = re.search(
                r"(?:serial|số thẻ|mã thẻ|pin)[:\s]+([0-9\-\s]{9,25})",
                content, re.IGNORECASE
            )
            if serial_match:
                code = serial_match.group(1).strip()
                log.info(f"[{order_id}] Mã thẻ (regex): {code}")
                return code

            # Kiểm tra đơn thất bại / hủy
            fail_kw = ["thất bại", "failed", "không thành công", "đã hủy", "hủy đơn"]
            if any(k in content.lower() for k in fail_kw):
                log.warning(f"[{order_id}] Phát hiện đơn thất bại trên muathengay.vn")

        except Exception as e:
            log.error(f"[{order_id}] Poll error: {e}")

    return None

# ── Xử lý đơn hàng chính ─────────────────────────────────────────────────────
async def process_order(order_id: str, order: dict):
    carrier_key   = (order.get("carrier") or "").lower()
    carrier_label = CARRIER_LABEL.get(carrier_key, carrier_key.capitalize())
    denomination  = int(order.get("denomination") or order.get("price") or 0)
    username      = order.get("username", "?")
    denom_vn      = fmt_vn(denomination)   # VD: "500.000"
    denom_str     = fmt_money(denomination) # VD: "500.000đ"

    log.info(f"[{order_id}] Xử lý: {carrier_label} {denom_str} | @{username}")
    db_update(order_id, {"status": "Đang xử lý", "botStartedAt": datetime.now().isoformat()})

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        ctx  = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()

        try:
            # ── 1. Mở trang ──────────────────────────────────────────
            log.info(f"[{order_id}] Mở muathengay.vn...")
            await page.goto("https://www.muathengay.vn/", timeout=40_000)
            await page.wait_for_load_state("networkidle", timeout=20_000)
            await page.wait_for_timeout(1500)

            # ── 2. Chọn nhà mạng ─────────────────────────────────────
            log.info(f"[{order_id}] Chọn nhà mạng: {carrier_label}")
            # muathengay.vn hiển thị carrier dưới dạng ô ảnh logo
            clicked_carrier = False
            for loc_str in [
                f"img[alt='{carrier_label}']",
                f"img[alt*='{carrier_label}']",
                f"[title='{carrier_label}']",
                f"text={carrier_label}",
                f"*:has-text('{carrier_label}')",
            ]:
                try:
                    loc = page.locator(loc_str).first
                    if await loc.count() > 0:
                        await loc.click(timeout=5000)
                        clicked_carrier = True
                        log.info(f"  → Carrier OK: {loc_str}")
                        break
                except Exception:
                    pass

            if not clicked_carrier:
                raise Exception(f"Không tìm thấy nhà mạng '{carrier_label}'")
            await page.wait_for_timeout(1200)

            # ── 3. Chọn mệnh giá ─────────────────────────────────────
            log.info(f"[{order_id}] Chọn mệnh giá: {denom_str}")
            # muathengay.vn hiển thị "500.000" (không có đ) trong ô mệnh giá
            clicked_denom = False
            for dv in [denom_vn, denom_str, str(denomination), f"{denomination // 1000}K"]:
                for loc_str in [
                    f"text={dv}",
                    f"*:has-text('{dv}')",
                    f"[data-value='{denomination}']",
                    f"button:has-text('{dv}')",
                ]:
                    try:
                        # Lọc đúng ô mệnh giá (tránh click vào "Giá bán")
                        loc = page.locator(loc_str).first
                        if await loc.count() > 0:
                            await loc.click(timeout=5000)
                            clicked_denom = True
                            log.info(f"  → Denomination OK: {loc_str} ({dv})")
                            break
                    except Exception:
                        pass
                if clicked_denom:
                    break

            if not clicked_denom:
                raise Exception(f"Không tìm thấy mệnh giá {denom_str}")
            await page.wait_for_timeout(1000)

            # ── 4. Nhập email ─────────────────────────────────────────
            log.info(f"[{order_id}] Nhập email: {ORDER_EMAIL}")
            for email_sel in [
                "input[type='email']",
                "input[name='email']",
                "input[placeholder*='email' i]",
                "input[placeholder*='Email']",
                "#email",
            ]:
                try:
                    el = page.locator(email_sel).first
                    if await el.count() > 0:
                        await el.triple_click(timeout=3000)  # xóa text cũ
                        await el.fill(ORDER_EMAIL, timeout=3000)
                        log.info(f"  → Email OK: {email_sel}")
                        break
                except Exception:
                    pass
            await page.wait_for_timeout(500)

            # ── 5. Click "Thanh toán" ─────────────────────────────────
            log.info(f"[{order_id}] Click Thanh toán...")
            for buy_sel in [
                "button:has-text('Thanh toán')",
                "a:has-text('Thanh toán')",
                "button:has-text('THANH TOÁN')",
                "button:has-text('Mua ngay')",
                "button:has-text('Đặt hàng')",
                "input[type='submit']",
                "button[type='submit']",
            ]:
                try:
                    loc = page.locator(buy_sel).first
                    if await loc.count() > 0:
                        await loc.click(timeout=8000)
                        log.info(f"  → Buy OK: {buy_sel}")
                        break
                except Exception:
                    pass

            await page.wait_for_load_state("networkidle", timeout=25_000)
            await page.wait_for_timeout(3000)

            # ── 6. Lấy thông tin thanh toán ──────────────────────────
            log.info(f"[{order_id}] Trích xuất thông tin thanh toán...")
            payment_info = await extract_payment_info(page, denomination)

            # Cập nhật Firebase → web_shop hiển thị ngay cho khách
            db_update(order_id, {
                "status": "Chờ thanh toán",
                "paymentInfo": payment_info,
                "paymentUpdatedAt": datetime.now().isoformat(),
            })

            await tg(
                f"💳 <b>Đơn {order_id}</b> — {carrier_label} {denom_str}\n"
                f"👤 Khách: <code>{username}</code>\n"
                f"🏦 Ngân hàng: {payment_info.get('bankName', '?')}\n"
                f"💰 Số TK: <code>{payment_info.get('accountNumber', '?')}</code>\n"
                f"👤 Chủ TK: {payment_info.get('accountHolder', '?')}\n"
                f"💵 Số tiền: <b>{denom_str}</b>\n"
                f"📝 Nội dung: <code>{payment_info.get('transferContent', '?')}</code>\n"
                f"⏳ Chờ khách chuyển khoản..."
            )
            log.info(f"[{order_id}] Payment info → Firebase OK. Poll mã thẻ...")

            # ── 7. Poll mã thẻ ────────────────────────────────────────
            card_result = await poll_card_code(page, order_id)

            if card_result:
                db_update(order_id, {
                    "status": "Hoàn thành",
                    "cardCode": card_result,
                    "accountDetails": card_result,
                    "completedAt": datetime.now().isoformat(),
                })
                await tg(
                    f"🎉 <b>MÃ THẺ — Đơn {order_id}</b>\n"
                    f"📱 {carrier_label} {denom_str}\n"
                    f"👤 Khách: <code>{username}</code>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🔑 <b>{card_result}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"✅ Đã cập nhật web!"
                )
                log.info(f"[{order_id}] ✅ Xong! {card_result}")
            else:
                db_update(order_id, {
                    "status": "Hết hạn",
                    "timedOutAt": datetime.now().isoformat(),
                })
                await tg(
                    f"⏰ <b>Hết hạn — Đơn {order_id}</b>\n"
                    f"Không thanh toán sau {ORDER_TIMEOUT // 60} phút.\n"
                    f"📱 {carrier_label} {denom_str} / @{username}"
                )
                log.warning(f"[{order_id}] Hết hạn.")

        except Exception as e:
            log.error(f"[{order_id}] Lỗi: {e}")
            db_update(order_id, {
                "status": "Lỗi",
                "errorMessage": str(e)[:500],
                "errorAt": datetime.now().isoformat(),
            })
            await tg(f"❌ <b>Lỗi đơn {order_id}</b>\n{str(e)[:300]}")
        finally:
            await browser.close()

# ── Task wrapper ──────────────────────────────────────────────────────────────
async def handle_order(order_id: str, order: dict, processing: set):
    try:
        await process_order(order_id, order)
    finally:
        processing.discard(order_id)

# ── Vòng lặp chính ───────────────────────────────────────────────────────────
async def watch():
    log.info("=" * 50)
    log.info("AccShop Bot khởi động")
    log.info(f"Email: {ORDER_EMAIL}")
    log.info(f"Timeout: {ORDER_TIMEOUT}s | Poll: {POLL_INTERVAL}s")
    log.info("=" * 50)

    processing: set = set()

    while True:
        try:
            pending = db_get_pending()
            for order_id, order in pending.items():
                if order_id not in processing:
                    processing.add(order_id)
                    log.info(f"📦 Đơn mới: {order_id}")
                    asyncio.create_task(handle_order(order_id, order, processing))
        except Exception as e:
            log.error(f"Watch error: {e}")

        await asyncio.sleep(WATCH_INTERVAL)

async def startup_test():
    """Kiểm tra kết nối Firebase khi bot khởi động."""
    log.info("🔍 Kiểm tra kết nối Firebase...")
    try:
        test = firebase_db.reference("orders").limit_to_last(1).get()
        log.info(f"✅ Firebase OK — {len(test) if test else 0} đơn sample")
    except Exception as e:
        log.error(f"❌ Firebase lỗi: {e}")

    log.info(f"🤖 Telegram token: {'OK' if TELEGRAM_TOKEN else '⚠ TRỐNG!'}")
    log.info(f"📧 Email đặt hàng: {ORDER_EMAIL}")
    log.info(f"🌐 Firebase URL: {FIREBASE_DB_URL}")

    # Gửi Telegram ping
    await tg("🟢 <b>AccShop Bot đã khởi động!</b>\nĐang theo dõi đơn hàng...")


if __name__ == "__main__":
    async def main():
        await startup_test()
        await watch()
    asyncio.run(main())
