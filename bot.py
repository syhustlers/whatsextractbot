import os
import re
import asyncio
import logging
import random
import httpx
import html
from bs4 import BeautifulSoup
from io import BytesIO
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==========================================
# RENDER HEALTH CHECK SERVER (Prevents Port Crash)
# ==========================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Telegram Bot is active and running!")
    def log_message(self, format, *args):
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info(f"Render health check server bound to port {port}")
    server.serve_forever()

threading.Thread(target=run_health_server, daemon=True).start()

# Bot Token
BOT_TOKEN = "8962511038:AAFvS1LZYYHwjIqVAh3Kx48irYntMcb9Y-Q"

# User state constants
STATE_NONE = 0
STATE_AWAITING_WEBSITES = 1
STATE_AWAITING_LINKS = 2


# ==========================================
# FAST & SECURE WA VALIDATOR ENGINE
# ==========================================
CODE_RE = re.compile(r"chat\.whatsapp\.com/(?:invite/)?([A-Za-z0-9_-]{20,24})", re.I)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

GENERIC_TITLES = {"", "whatsapp", "whatsapp group invite", "whatsapp web", "whatsapp messenger"}

DEAD_PHRASES = (
    "invite link was reset", "invite link is invalid", "invite link has been revoked",
    "invite link is no longer valid", "couldn't find group", "could not find group",
    "link has expired", "group doesn't exist", "no longer available",
)
APPROVAL_PHRASES = (
    "request to join", "admin must approve", "admins must approve",
    "approve your request", "approval required", "pending approval",
)


def _result(url, valid, approval, reason, status):
    return {"url": url, "valid": valid, "approval_required": approval,
            "reason": reason, "status": status}


def _meta(soup: BeautifulSoup, prop: str) -> str:
    tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    return (tag.get("content") or "").strip() if tag else ""


async def validate_wa_link(client: httpx.AsyncClient, url: str) -> dict:
    m = CODE_RE.search(url or "")
    if not m:
        return _result(url, False, False, "malformed_link", "dead")
    canonical = f"https://chat.whatsapp.com/{m.group(1)}"

    try:
        # Strict 6-second timeout so it never hangs
        r = await client.get(canonical, headers=HEADERS, follow_redirects=True, timeout=6.0)
        
        if r.status_code in [403, 429, 500, 502, 503, 504]:
            return _result(canonical, False, False, f"rate_limited_{r.status_code}", "unknown")
        if r.status_code == 404:
            return _result(canonical, False, False, "http_404", "dead")
        if r.status_code != 200:
            return _result(canonical, False, False, f"unexpected_http_{r.status_code}", "unknown")

        if not str(r.url.host).endswith(("whatsapp.com", "whatsapp.net")):
            return _result(canonical, False, False, "redirected_off_whatsapp", "unknown")

        html_text = r.text or ""
        low = html_text.lower()
        soup = BeautifulSoup(html_text, "html.parser")

        title = _meta(soup, "og:title")
        image = _meta(soup, "og:image")
        has_og = bool(title or image or _meta(soup, "og:description"))

        if not has_og and len(html_text) < 300:
            return _result(canonical, False, False, "masked_response", "unknown")

        visible = soup.get_text(" ", strip=True).lower()

        has_name = title.lower() not in GENERIC_TITLES
        img_host = re.sub(r"^https?://([^/]+).*$", r"\1", image).lower() if image else ""
        has_avatar = img_host.endswith("whatsapp.net")

        if not has_name:
            for p in DEAD_PHRASES:
                if p in visible:
                    return _result(canonical, False, False, f"dead_link:'{p}'", "dead")
            if has_og:
                return _result(canonical, False, False, "dead_link:generic_fallback_page", "dead")
            return _result(canonical, False, False, "unrecognised_page", "unknown")

        for p in DEAD_PHRASES:
            if p in visible:
                return _result(canonical, False, False, f"dead_link:'{p}'", "dead")

        approval = any(p in visible or p in low for p in APPROVAL_PHRASES)
        if approval:
            return _result(canonical, True, True, f"active_group:'{title}' (admin approval wording found)", "active")

        note = "" if has_avatar else ", no_avatar"
        return _result(canonical, True, False, f"active_group:'{title}'{note}", "active")

    except Exception as e:
        return _result(canonical, False, False, f"error:{type(e).__name__}", "unknown")


# ==========================================
# EXTRACTION HELPERS
# ==========================================
def extract_wa_links(text: str) -> list[str]:
    if not text:
        return []
    cleaned_text = html.unescape(text)
    matches = CODE_RE.findall(cleaned_text)
    unique_links = []
    seen = set()
    for code in matches:
        if code not in seen:
            seen.add(code)
            unique_links.append(f"https://chat.whatsapp.com/{code}")
    return unique_links


async def fetch_and_extract_from_url(client: httpx.AsyncClient, website_url: str) -> list[str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        if not website_url.startswith(("http://", "https://")):
            website_url = "https://" + website_url
            
        res = await client.get(website_url, headers=headers, follow_redirects=True, timeout=12.0)
        if res.status_code == 200:
            return extract_wa_links(res.text)
    except Exception as e:
        logger.error(f"Failed to fetch site {website_url}: {e}")
    return []


# ==========================================
# BOT TELEGRAM HANDLERS
# ==========================================

def get_main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("🌐 Scrape & Validate Websites", callback_data="btn_scrape")],
        [InlineKeyboardButton("📁 Validate Link List / File", callback_data="btn_validate_list")]
    ]
    return InlineKeyboardMarkup(keyboard)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = STATE_NONE
    welcome_text = "⚡ **WhatsApp Group Validator & Scraper Bot** ⚡\n\nChoose an option below to start:"
    
    if update.message:
        await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=get_main_menu_keyboard())
    elif update.callback_query:
        await update.callback_query.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=get_main_menu_keyboard())


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "btn_scrape":
        context.user_data["state"] = STATE_AWAITING_WEBSITES
        await query.message.reply_text(
            "🌐 **Scrape & Validate Websites Mode Active**\n\nPlease send or paste your list of target website URLs below (one URL per line).",
            parse_mode="Markdown"
        )
    elif query.data == "btn_validate_list":
        context.user_data["state"] = STATE_AWAITING_LINKS
        await query.message.reply_text(
            "📁 **Validate Link List Mode Active**\n\nPlease upload your `.txt` file containing WhatsApp group links or paste the raw list directly in chat.",
            parse_mode="Markdown"
        )


async def handle_message_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state", STATE_NONE)

    if state == STATE_AWAITING_WEBSITES:
        context.user_data["state"] = STATE_NONE
        urls = [line.strip() for line in update.message.text.splitlines() if line.strip()]
        
        if not urls:
            await update.message.reply_text("❌ No valid URLs provided. Press /start to try again.")
            return
            
        progress_msg = await update.message.reply_text(f"🔍 **Scraping {len(urls)} website(s)...**", parse_mode="Markdown")
        
        extracted_links = set()
        async with httpx.AsyncClient(verify=False) as client:
            for idx, site in enumerate(urls, start=1):
                links = await fetch_and_extract_from_url(client, site)
                extracted_links.update(links)
                if idx % 2 == 0 or idx == len(urls):
                    await progress_msg.edit_text(
                        f"🌐 Scraped **{idx}/{len(urls)}** websites...\n🔗 Extracted **{len(extracted_links)}** unique WhatsApp links so far.",
                        parse_mode="Markdown"
                    )

        if not extracted_links:
            await progress_msg.edit_text("❌ No WhatsApp group links found on the provided websites.", reply_markup=get_main_menu_keyboard())
            return

        await process_and_validate_links(progress_msg, list(extracted_links), context)

    elif state == STATE_AWAITING_LINKS or state == STATE_NONE:
        context.user_data["state"] = STATE_NONE
        extracted_links = extract_wa_links(update.message.text)
        
        if not extracted_links:
            await update.message.reply_text("❌ No valid WhatsApp group links detected. Send /start to begin.", reply_markup=get_main_menu_keyboard())
            return

        progress_msg = await update.message.reply_text(f"⚡ Detected **{len(extracted_links)}** unique links. Initializing validation...", parse_mode="Markdown")
        await process_and_validate_links(progress_msg, extracted_links, context)


async def handle_document_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc.file_name.endswith(".txt"):
        await update.message.reply_text("❌ Please upload a valid `.txt` text file.")
        return

    progress_msg = await update.message.reply_text("📥 Reading uploaded file...", parse_mode="Markdown")
    
    file_obj = await context.bot.get_file(doc.file_id)
    file_bytes = await file_obj.download_as_bytearray()
    text_content = file_bytes.decode("utf-8", errors="ignore")

    extracted_links = extract_wa_links(text_content)
    if not extracted_links:
        await progress_msg.edit_text("❌ No valid WhatsApp group links found inside the uploaded `.txt` file.", reply_markup=get_main_menu_keyboard())
        return

    await progress_msg.edit_text(f"⚡ Extracted **{len(extracted_links)}** unique links from file. Initializing validation...", parse_mode="Markdown")
    await process_and_validate_links(progress_msg, extracted_links, context)


async def process_and_validate_links(status_message, links: list[str], context: ContextTypes.DEFAULT_TYPE):
    total_links = len(links)
    valid_instant_join_links = []
    
    concurrency = 5  # Increased concurrency for faster processing
    sem = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    
    completed_count = 0

    async with httpx.AsyncClient(limits=limits, http2=False, verify=False) as client:
        async def validate_single(link):
            nonlocal completed_count
            async with sem:
                res = await validate_wa_link(client, link)
                completed_count += 1
                
                is_instant = (res["status"] == "active" and res["valid"] and not res["approval_required"])
                if is_instant:
                    valid_instant_join_links.append(link)

                if completed_count % 3 == 0 or completed_count == total_links:
                    progress_bar = f"[{'█' * int(completed_count / total_links * 10)}{'░' * (10 - int(completed_count / total_links * 10))}]"
                    try:
                        await status_message.edit_text(
                            f"⚡ **Validating Links...**\n\n"
                            f"Progress: `{progress_bar}` **{completed_count}/{total_links}** ({int(completed_count/total_links*100)}%)\n"
                            f"✅ **Instant-Join Active:** {len(valid_instant_join_links)}",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass

        await asyncio.gather(*(validate_single(link) for link in links))

    await status_message.edit_text(
        f"✅ **Validation Complete!**\n\n"
        f"📊 **Total Links Scanned:** {total_links}\n"
        f"🚀 **Instant-Join (No Admin Approval):** {len(valid_instant_join_links)}\n"
        f"❌ **Filtered Out (Invalid/Admin Needed/Unknown):** {total_links - len(valid_instant_join_links)}",
        parse_mode="Markdown"
    )

    if not valid_instant_join_links:
        await status_message.reply_text(
            "⚠️ No free-to-join active WhatsApp groups were found in this batch.\n\nWould you like to validate another batch or check more websites?",
            reply_markup=get_main_menu_keyboard()
        )
        return

    numbered_links = [f"{i}. {link}" for i, link in enumerate(valid_instant_join_links, 1)]
    raw_output_text = "\n".join(numbered_links)
    
    await status_message.reply_text("👇 **Here are your numbered instant-join valid WhatsApp group links:**")
    
    max_chars = 3800
    for i in range(0, len(raw_output_text), max_chars):
        chunk = raw_output_text[i:i + max_chars]
        await status_message.reply_text(chunk, disable_web_page_preview=True)

    file_bytes = BytesIO(raw_output_text.encode("utf-8"))
    file_bytes.name = "valid_whatsapp_groups.txt"
    
    await context.bot.send_document(
        chat_id=status_message.chat_id,
        document=file_bytes,
        filename="valid_whatsapp_groups.txt",
        caption="📄 **Downloadable `.txt` export of verified instant-join links.**\n\n🔄 **Want to validate more? Choose an option below:**",
        reply_markup=get_main_menu_keyboard(),
        parse_mode="Markdown"
    )


def main():
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document_upload))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message_input))

    logger.info("Bot started successfully and polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
            
