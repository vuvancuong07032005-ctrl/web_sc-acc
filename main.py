"""
AccShop Bot — muathengay.vn Automation
Chạy liên tục trên Railway, lắng nghe Firebase để xử lý đơn thẻ cào tự động.

Flow:
1. Phát hiện đơn mới status='pending_bot' & type='card' trên Firebase
2. Mở muathengay.vn bằng Playwright (headless)
3. Chọn nhà mạng → mệnh giá → nhập email → mua ngay
4. Lấy thông tin chuyển khoản → ghi lên Firebase
5. Poll trang đến khi thấy mã thẻ (hoặc timeout 30 phút)
6. Gửi mã thẻ lên Telegram + cập nhật Firebase
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

# ── Load environment ────────────────────────────────────────────────────────
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("accshop-bot")

FIREBASE_DB_URL   = os.getenv("FIREBASE_DB_URL", "")
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT     = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
ORDER_EMAIL       = os.getenv("ORDER_EMAIL", "bot@example.com")
ORDER_TIMEOUT     = int(os.getenv("ORDER_TIMEOUT_SECONDS", "1800"))  # 30 phút
POLL_INTERVAL     = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))    # Poll mã thẻ mỗi 30s
WATCH_INTERVAL    = int(os.getenv("WATCH_INTERVAL_SECONDS", "5"))    # Check Firebase mỗi 5s

# ── Firebase init ───────────────────────────────────────────────────────────
_sa_json = os.getenv("FIREBASE_SERVICE_ACCOUNT", "")
if _sa_json:
    # Railway: dán JSON của service account vào biến môi trường
    cred = credentials.Certificate(json.loads(_sa_json))
else:
    # Local: để file serviceAccount.json cùng thư mục
    cred = credentials.Certificate("serviceAccount.json")

firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})

# ── Carrier mapping ─────────────────────────────────────────────────────────
CARRIER_LABEL = {
    "viettel":      "Viettel",
    "mobifone":     "Mobifone",
    "vinaphone":    "Vinaphone",
    "vietnamobile": "Vietnamobile",
    "gmobile":      "Gmobile",
}

# ── Helpers ─────────────────────────────────────────────────────────────────
def fmt_money(amount: int) -> str:
    return f"{amount:,}".replace(",", ".") + "đ"

async def tg(msg: str):
    """Gửi tin nhắn Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        log.warning("Telegram chưa cấu hình, bỏ qua.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(url, json={
                "chat_id": TELEGRAM_CHAT,
                "text": msg,
                "parse_mode": "HTML",
            })
            if not r.json().get("ok"):
                log.warning(f"Telegram lỗi: {r.text}")
    except Exception as e:
        log.error(f"Telegram send error: {e}")

def db_update(order_id: str, data: dict):
    """Cập nhật đơn hàng trên Firebase."""
    firebase_db.reference(f"orders/{order_id}").update(data)

def db_get_pending() -> dict:
    """Lấy tất cả đơn có status='pending_bot' và type='card'."""
    try:
        orders = firebase_db.reference("orders") \
            .order_by_child("status").equal_to("pending_bot").get()
        if not orders:
            return {}
        return {oid: o for oid, o in orders.items() if (o.get("type") or "") == "card"}
    except Exception as e:
        log.error(f"Firebase query error: {e}")
        return {}

# ── Payment info extractor ───────────────────────────────────────────────────
async def extract_payment_info(page, expected_amount: int) -> dict:
    """
    Trích xuất thông tin ngân hàng từ trang thanh toán của muathengay.vn.
    Dùng nhiều chiến lược để tăng độ tin cậy.
    """
    info = {"amount": expected_amount}

    # Danh sách selector theo trường
    selectors = {
        "bankName": [
            ".bank-name", "[class*='bank-name']", "[class*='ten-ngan-hang']",
            "td:has-text('Ngân hàng') + td", ".payment-bank-name",
        ],
        "accountNumber": [
            ".account-number", "[class*='account-number']", ".stk",
            "[class*='so-tai-khoan']", "td:has-text('Số tài khoản') + td",
            "[class*='bank-account']", ".bank-stk",
        ],
        "accountHolder": [
            ".account-holder", "[class*='account-holder']", ".owner-name",
            "td:has-text('Chủ tài khoản') + td", "[class*='chu-tai-khoan']",
        ],
        "transferContent": [
            ".transfer-content", "[class*='transfer-content']", ".noi-dung",
            ".ma-giao-dich", "td:has-text('Nội dung') + td",
            "[class*='noi-dung']", ".payment-content",
        ],
    }

    for field, sels in selectors.items():
        for sel in sels:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    text = (await el.inner_text(timeout=3000)).strip()
                    if text and 2 < len(text) < 200:
                        info[field] = text
                        log.info(f"  {field} = {text!r}")
                        break
            except Exception:
                pass

    # Fallback regex nếu selectors không bắt được số tài khoản
    if not info.get("accountNumber"):
        try:
            content = await page.content()
            # Số tài khoản ngân hàng VN: 9–16 chữ số
            for m in re.findall(r"\b(\d{9,16})\b", content):
                # Loại timestamp và năm
                if not m.startswith(("202", "201", "200", "199")):
                    info["accountNumber"] = m
                    log.info(f"  accountNumber (regex) = {m}")
                    break
        except Exception as e:
            log.warning(f"Regex fallback error: {e}")

    return info

# ── Card code poller ─────────────────────────────────────────────────────────
async def poll_card_code(page, order_id: str) -> str | None:
    """
    Poll trang muathengay.vn mỗi POLL_INTERVAL giây để tìm mã thẻ.
    Trả về mã thẻ hoặc None nếu hết ORDER_TIMEOUT.
    """
    deadline = asyncio.get_event_loop().time() + ORDER_TIMEOUT
    attempt = 0

    # Selectors cho mã thẻ/serial
    code_selectors = [
        ".card-code", ".the-code", ".ma-the", ".serial-number",
        "[class*='card-code']", "[class*='ma-the']",
        ".result-code", ".pin-code", ".topup-code",
        "td:has-text('Mã thẻ') + td", "td:has-text('Serial') + td",
        ".topup-result .code", "[data-field='serial']", "[data-field='code']",
    ]

    # Regex patterns cho mã thẻ cào VN (thường 9–15 chữ số, có thể có dấu gạch)
    code_patterns = [
        r"\b(\d{4}[-\s]\d{4}[-\s]\d{4})\b",           # XXXX-XXXX-XXXX
        r"\b(\d{4}[-\s]\d{4}[-\s]\d{4}[-\s]\d{4})\b", # XXXX-XXXX-XXXX-XXXX
        r"\b(\d{9,15})\b",                              # Liền (không dấu)
    ]

    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(POLL_INTERVAL)
        attempt += 1
        remaining = int(deadline - asyncio.get_event_loop().time())
        log.info(f"[{order_id}] Poll #{attempt} — còn {remaining}s")

        try:
            await page.reload(wait_until="networkidle", timeout=20_000)
            await page.wait_for_timeout(2000)

            # 1. Thử selector
            for sel in code_selectors:
                try:
                    el = page.locator(sel).first
                    if await el.count() > 0:
                        code = (await el.inner_text(timeout=3000)).strip()
                        # Validate: chỉ gồm số và dấu gạch/khoảng trắng
                        clean = re.sub(r"[\s\-]", "", code)
                        if re.fullmatch(r"\d{9,20}", clean):
                            log.info(f"[{order_id}] Mã thẻ tìm thấy (selector): {code}")
                            return code
                except Exception:
                    pass

            # 2. Regex trong toàn bộ HTML
            content = await page.content()
            for pat in code_patterns:
                matches = re.findall(pat, content)
                for m in matches:
                    clean = re.sub(r"[\s\-]", "", m)
                    if re.fullmatch(r"\d{9,20}", clean):
                        log.info(f"[{order_id}] Mã thẻ tìm thấy (regex): {m}")
                        return m.strip()

            # Kiểm tra dấu hiệu thất bại
            fail_kw = ["hủy đơn", "thất bại", "failed", "không thành công", "đã hết hạn"]
            if any(kw in content.lower() for kw in fail_kw):
                log.warning(f"[{order_id}] Phát hiện dấu hiệu đơn thất bại/hủy.")

        except Exception as e:
            log.error(f"[{order_id}] Poll error: {e}")

    return None  # Timeout

# ── Main order processor ────────────────────────────────────────────────────
async def process_order(order_id: str, order: dict):
    """
    Xử lý một đơn thẻ cào:
    muathengay.vn → payment info → Firebase → poll mã thẻ → Telegram
    """
    carrier_key   = (order.get("carrier") or "").lower()
    carrier_label = CARRIER_LABEL.get(carrier_key, carrier_key.capitalize())
    denomination  = int(order.get("denomination") or order.get("price") or 0)
    username      = order.get("username", "?")
    denom_str     = fmt_money(denomination)

    log.info(f"[{order_id}] Bắt đầu xử lý: {carrier_label} {denom_str} cho @{username}")
    db_update(order_id, {"status": "Đang xử lý", "botStartedAt": datetime.now().isoformat()})

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await ctx.new_page()

        try:
            # ── 1. Điều hướng ─────────────────────────────────────────
            log.info(f"[{order_id}] Mở muathengay.vn...")
            await page.goto("https://www.muathengay.vn/", timeout=40_000)
            await page.wait_for_load_state("networkidle", timeout=20_000)

            # ── 2. Chọn nhà mạng ──────────────────────────────────────
            log.info(f"[{order_id}] Chọn nhà mạng: {carrier_label}")
            # Thử nhiều cách click carrier
            carrier_found = False
            for loc_str in [
                f"[data-carrier='{carrier_key}']",
                f"button:has-text('{carrier_label}')",
                f".carrier-item:has-text('{carrier_label}')",
                f"img[alt*='{carrier_label}']",
                f"text={carrier_label}",
            ]:
                try:
                    loc = page.locator(loc_str).first
                    if await loc.count() > 0:
                        await loc.click(timeout=5000)
                        carrier_found = True
                        log.info(f"  → Click carrier OK: {loc_str}")
                        break
                except Exception:
                    pass

            if not carrier_found:
                raise Exception(f"Không tìm thấy nhà mạng '{carrier_label}' trên trang")
            await page.wait_for_timeout(1200)

            # ── 3. Chọn mệnh giá ──────────────────────────────────────
            log.info(f"[{order_id}] Chọn mệnh giá: {denom_str}")
            denom_variants = [
                denom_str,
                f"{denomination:,}".replace(",", "."),
                f"{denomination // 1000}K",
                f"{denomination // 1000}k",
                str(denomination),
            ]
            denom_found = False
            for dv in denom_variants:
                for loc_str in [
                    f"button:has-text('{dv}')",
                    f".price-item:has-text('{dv}')",
                    f"[data-value='{denomination}']",
                    f".denomination:has-text('{dv}')",
                    f"text={dv}",
                ]:
                    try:
                        loc = page.locator(loc_str).first
                        if await loc.count() > 0:
                            await loc.click(timeout=5000)
                            denom_found = True
                            log.info(f"  → Click denomination OK: {loc_str}")
                            break
                    except Exception:
                        pass
                if denom_found:
                    break

            if not denom_found:
                raise Exception(f"Không tìm thấy mệnh giá {denom_str} trên trang")
            await page.wait_for_timeout(1200)

            # ── 4. Nhập email ──────────────────────────────────────────
            log.info(f"[{order_id}] Nhập email: {ORDER_EMAIL}")
            for email_sel in [
                "input[type='email']",
                "input[name='email']",
                "input[placeholder*='email' i]",
                "input[placeholder*='Email' i]",
            ]:
                try:
                    el = page.locator(email_sel).first
                    if await el.count() > 0:
                        await el.fill(ORDER_EMAIL, timeout=5000)
                        log.info(f"  → Email nhập OK: {email_sel}")
                        break
                except Exception:
                    pass
            await page.wait_for_timeout(500)

            # ── 5. Click mua ngay ──────────────────────────────────────
            log.info(f"[{order_id}] Click mua ngay...")
            for buy_sel in [
                "button:has-text('Mua ngay')",
                "button:has-text('MUA NGAY')",
                "button:has-text('Đặt hàng')",
                "button:has-text('Thanh toán')",
                "input[type='submit']",
                "button[type='submit']",
            ]:
                try:
                    loc = page.locator(buy_sel).first
                    if await loc.count() > 0:
                        await loc.click(timeout=8000)
                        log.info(f"  → Click buy OK: {buy_sel}")
                        break
                except Exception:
                    pass

            await page.wait_for_load_state("networkidle", timeout=20_000)
            await page.wait_for_timeout(2500)

            # ── 6. Lấy thông tin thanh toán ───────────────────────────
            log.info(f"[{order_id}] Trích xuất thông tin thanh toán...")
            payment_info = await extract_payment_info(page, denomination)

            if not payment_info.get("accountNumber"):
                # Debug: chụp ảnh màn hình
                screenshot_path = f"/tmp/debug_{order_id}.png"
                await page.screenshot(path=screenshot_path)
                log.warning(f"  → Không tìm thấy STK. Screenshot: {screenshot_path}")
                # Vẫn tiếp tục với thông tin không đầy đủ
                # raise Exception("Không tìm thấy thông tin chuyển khoản")

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
                f"💵 Số tiền: <b>{fmt_money(denomination)}</b>\n"
                f"📝 Nội dung: <code>{payment_info.get('transferContent', '?')}</code>\n"
                f"⏳ Đang chờ khách chuyển khoản ({ORDER_TIMEOUT // 60} phút)..."
            )
            log.info(f"[{order_id}] Payment info cập nhật Firebase OK. Bắt đầu poll mã thẻ...")

            # ── 7. Poll mã thẻ ────────────────────────────────────────
            card_code = await poll_card_code(page, order_id)

            if card_code:
                db_update(order_id, {
                    "status": "Hoàn thành",
                    "cardCode": card_code,
                    "accountDetails": f"Mã thẻ: {card_code}",
                    "completedAt": datetime.now().isoformat(),
                })
                await tg(
                    f"🎉 <b>MÃ THẺ — Đơn {order_id}</b>\n"
                    f"📱 {carrier_label} {denom_str}\n"
                    f"👤 Khách: <code>{username}</code>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🔑 <b>Mã thẻ:</b> <code>{card_code}</code>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"✅ Đã cập nhật lên web!"
                )
                log.info(f"[{order_id}] ✅ Hoàn thành! Mã thẻ: {card_code}")
            else:
                db_update(order_id, {
                    "status": "Hết hạn",
                    "timedOutAt": datetime.now().isoformat(),
                })
                await tg(
                    f"⏰ <b>Hết hạn — Đơn {order_id}</b>\n"
                    f"Khách không thanh toán sau {ORDER_TIMEOUT // 60} phút.\n"
                    f"📱 {carrier_label} {denom_str} / @{username}"
                )
                log.warning(f"[{order_id}] ⏰ Hết hạn.")

        except Exception as e:
            log.error(f"[{order_id}] ❌ Lỗi: {e}")
            db_update(order_id, {
                "status": "Lỗi",
                "errorMessage": str(e)[:500],
                "errorAt": datetime.now().isoformat(),
            })
            await tg(
                f"❌ <b>Lỗi đơn {order_id}</b>\n"
                f"📱 {carrier_label} {denom_str} / @{username}\n"
                f"⚠️ {str(e)[:300]}"
            )
        finally:
            await browser.close()
            log.info(f"[{order_id}] Browser đóng.")

# ── Task wrapper ─────────────────────────────────────────────────────────────
async def handle_order(order_id: str, order: dict, processing: set):
    try:
        await process_order(order_id, order)
    finally:
        processing.discard(order_id)

# ── Firebase watcher ─────────────────────────────────────────────────────────
async def watch():
    """Vòng lặp chính: poll Firebase và xử lý đơn mới."""
    log.info("=" * 50)
    log.info("AccShop Bot khởi động.")
    log.info(f"Email đặt hàng: {ORDER_EMAIL}")
    log.info(f"Timeout: {ORDER_TIMEOUT}s ({ORDER_TIMEOUT // 60} phút)")
    log.info(f"Poll mã thẻ: mỗi {POLL_INTERVAL}s")
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
            log.error(f"Watch loop error: {e}")

        await asyncio.sleep(WATCH_INTERVAL)

# ── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(watch())
