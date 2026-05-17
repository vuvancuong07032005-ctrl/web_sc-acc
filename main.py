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
from playwright_stealth import stealth_async
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
    "vcoin":        "Vtc",        # muathengay.vn dùng alt="Mua thẻ Vtc"
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
    Dùng JavaScript DOM traversal để tìm chính xác từng label → value.
    Tránh lỗi đọc nhầm do layout 2 cột của trang /thanh-toan-don-hang.
    """
    info = {"amount": denomination}
    await page.wait_for_timeout(2000)

    try:
        data = await page.evaluate("""() => {
            // DOM thật: label là <p> bên trong DIV.p-4
            // value = p.nextElementSibling.textContent
            function findVal(label) {
                const pTags = [...document.querySelectorAll('p')];
                for (const p of pTags) {
                    if (p.textContent.trim() === label) {
                        const sib = p.nextElementSibling;
                        if (sib) {
                            const t = sib.textContent.trim();
                            if (t) return t;
                        }
                    }
                }
                return null;
            }
            return {
                accountHolder:   findVal('Chủ tài khoản'),
                bankName:        findVal('Ngân hàng'),
                accountNumber:   findVal('Số tài khoản'),
                transferContent: findVal('Nội dung chuyển khoản'),
            };
        }""")

        for k, v in (data or {}).items():
            if v and v.strip() and v.strip() not in ('-', '—'):
                info[k] = v.strip()
                log.info(f"  {k}: {v.strip()!r}")

    except Exception as e:
        log.error(f"  extract JS error: {e}")

    # Fallback: regex tìm số TK trong HTML
    if not info.get("accountNumber"):
        try:
            html = await page.content()
            for m in re.findall(r"\b(\d{6,16})\b", html):
                if not re.match(r"^(202[0-6]|000|999)", m):
                    info["accountNumber"] = m
                    log.info(f"  accountNumber (fallback): {m}")
                    break
        except Exception:
            pass

    log.info(f"  Payment info final: {info}")
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
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-zygote",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-translate",
                "--hide-scrollbars",
                "--metrics-recording-only",
                "--mute-audio",
                "--no-first-run",
                "--safebrowsing-disable-auto-update",
            ],
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
        await stealth_async(page)   # ← ẩn dấu hiệu headless, qua Cloudflare

        try:
            # ── 1. Mở trang ──────────────────────────────────────────
            log.info(f"[{order_id}] Mở muathengay.vn...")
            await page.goto("https://www.muathengay.vn/", timeout=60_000)

            # Chờ Cloudflare "Just a moment..." tự qua (tối đa 20s)
            try:
                await page.wait_for_function(
                    "document.title !== 'Just a moment...'",
                    timeout=20_000
                )
                log.info(f"[{order_id}] Đã qua Cloudflare.")
            except Exception:
                log.warning(f"[{order_id}] Cloudflare timeout, tiếp tục...")

            await page.wait_for_load_state("networkidle", timeout=20_000)
            await page.wait_for_timeout(2000)

            # ── 2. Chọn nhà mạng ─────────────────────────────────────
            log.info(f"[{order_id}] Chọn nhà mạng: {carrier_label}")
            # ── DEBUG: xem trang bot đang thấy ─────────────────────────
            debug = await page.evaluate("""() => ({
                url:     window.location.href,
                title:   document.title,
                imgs:    [...document.querySelectorAll('img')].map(i=>i.alt).filter(a=>a).slice(0,20),
                btns:    [...document.querySelectorAll('button')].map(b=>b.textContent.trim().slice(0,40)).slice(0,15),
                body200: document.body?.innerText?.slice(0,200) || ''
            })""")
            log.info(f"  [DEBUG] URL: {debug.get('url')}")
            log.info(f"  [DEBUG] Title: {debug.get('title')}")
            log.info(f"  [DEBUG] Imgs: {debug.get('imgs')}")
            log.info(f"  [DEBUG] Btns: {debug.get('btns')}")
            log.info(f"  [DEBUG] Body: {debug.get('body200')}")

            # Chờ img carrier xuất hiện
            try:
                await page.wait_for_selector("img[alt*='Mua thẻ']", timeout=12_000)
                log.info("  Carrier images đã load.")
            except Exception:
                log.warning("  Timeout chờ carrier images, thử tiếp...")
            await page.wait_for_timeout(500)

            # Dùng JS để click trực tiếp — tránh lỗi selector :has()
            clicked_carrier = await page.evaluate(f"""() => {{
                const imgs = [...document.querySelectorAll('img')];
                const target = imgs.find(img =>
                    img.alt && img.alt.toLowerCase().includes('{carrier_label.lower()}')
                );
                if (target) {{
                    const btn = target.closest('button') || target.parentElement;
                    if (btn) {{ btn.click(); return true; }}
                    target.click();
                    return true;
                }}
                return false;
            }}""")

            if not clicked_carrier:
                # Fallback: thử Playwright locator
                for loc_str in [
                    f"img[alt*='{carrier_label}']",
                    f"img[alt='Mua thẻ {carrier_label}']",
                    f"text={carrier_label}",
                ]:
                    try:
                        loc = page.locator(loc_str).first
                        if await loc.count() > 0:
                            await loc.click(timeout=5000)
                            clicked_carrier = True
                            log.info(f"  → Carrier OK (fallback): {loc_str}")
                            break
                    except Exception:
                        pass

            if not clicked_carrier:
                raise Exception(f"Không tìm thấy nhà mạng '{carrier_label}'")
            log.info(f"  → Carrier clicked: {carrier_label}")
            await page.wait_for_timeout(1200)

            # ── 3. Chọn mệnh giá ─────────────────────────────────────
            log.info(f"[{order_id}] Chọn mệnh giá: {denom_str}")
            # muathengay.vn hiển thị "500.000" (không có đ) trong ô mệnh giá
            # Chờ denomination buttons load
            try:
                await page.wait_for_selector("button[type='submit'] h6", timeout=8_000)
            except Exception:
                log.warning("  Timeout chờ denomination buttons...")
            await page.wait_for_timeout(300)

            # Dùng JS click denomination
            clicked_denom = await page.evaluate(f"""() => {{
                const h6s = [...document.querySelectorAll('button h6, button .text-lg')];
                const target = h6s.find(h => h.textContent.trim() === '{denom_vn}');
                if (target) {{
                    const btn = target.closest('button');
                    if (btn) {{ btn.click(); return true; }}
                }}
                // Fallback: tìm button[type=submit] chứa text mệnh giá
                const btns = [...document.querySelectorAll('button[type="submit"]')];
                const b = btns.find(b => b.textContent.includes('{denom_vn}'));
                if (b) {{ b.click(); return true; }}
                return false;
            }}""")

            if not clicked_denom:
                log.warning(f"  JS click denom failed, thử Playwright...")
                for loc_str in [
                    f"button[type='submit']:has-text('{denom_vn}')",
                    f"h6:has-text('{denom_vn}')",
                    f"text={denom_vn}",
                ]:
                    try:
                        loc = page.locator(loc_str).first
                        if await loc.count() > 0:
                            await loc.click(timeout=5000)
                            clicked_denom = True
                            log.info(f"  → Denom OK (fallback): {denom_vn}")
                            break
                    except Exception:
                        pass

            if not clicked_denom:
                raise Exception(f"Không tìm thấy mệnh giá {denom_str}")
            log.info(f"  → Denomination clicked: {denom_vn}")

            if not clicked_denom:
                raise Exception(f"Không tìm thấy mệnh giá {denom_str}")
            await page.wait_for_timeout(1000)

            # ── 4. Nhập email ─────────────────────────────────────────
            log.info(f"[{order_id}] Nhập email: {ORDER_EMAIL}")
            for email_sel in [
                "input[placeholder='Nhập email']",    # chính xác theo HTML thật
                "input[placeholder*='email' i]",       # chứa "email" (case-insensitive)
                "input[type='text'][placeholder*='mail']",
                "input[name='email']",
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

            # ── 5. Submit bằng JS click (bypass visibility/overlay) ──────
            log.info(f"[{order_id}] JS click Thanh toán lần 1...")
            await page.evaluate("""() => {
                const btns = [...document.querySelectorAll('button[type="button"]')];
                const btn = btns.find(b => b.textContent.trim() === 'Thanh toán');
                if (btn) { btn.click(); return true; }
                // fallback: bất kỳ button nào có text Thanh toán
                const any = [...document.querySelectorAll('button')]
                    .find(b => b.textContent.trim() === 'Thanh toán');
                if (any) any.click();
            }""")
            log.info(f"[{order_id}] Chờ modal xác nhận...")
            await page.wait_for_timeout(2500)

            # Click lần 2 trong modal (nếu chưa redirect)
            if "thanh-toan-don-hang" not in page.url:
                log.info(f"[{order_id}] JS click Thanh toán lần 2 (modal)...")
                await page.evaluate("""() => {
                    const btns = [...document.querySelectorAll('button')]
                        .filter(b => b.textContent.trim() === 'Thanh toán');
                    if (btns.length > 0) btns[btns.length - 1].click();
                }""")
                await page.wait_for_timeout(2000)

            # Chờ redirect sang trang thanh toán (tối đa 40s)
            try:
                await page.wait_for_url("**/thanh-toan**", timeout=40_000)
                log.info(f"[{order_id}] ✅ Redirect OK → {page.url[:80]}")
            except Exception:
                log.warning(f"[{order_id}] Không redirect được. URL hiện: {page.url[:80]}")

            await page.wait_for_load_state("networkidle", timeout=20_000)
            await page.wait_for_timeout(2000)
            log.info(f"[{order_id}] URL khi extract: {page.url[:100]}")

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

# ── Semaphore: chỉ 1 trình duyệt chạy 1 lúc (tránh hết RAM) ─────────────────
_browser_sem = asyncio.Semaphore(1)

# ── Task wrapper ──────────────────────────────────────────────────────────────
async def handle_order(order_id: str, order: dict, processing: set):
    async with _browser_sem:
        try:
            await process_order(order_id, order)
        except Exception as e:
            log.error(f"[{order_id}] Unhandled: {e}")
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
