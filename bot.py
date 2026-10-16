import os
import re
import asyncio
import logging
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

# Regex pattern for WhatsApp group invite links
WA_LINK_REGEX = re.compile(
    r"https?://chat\.whatsapp\.com/(?:invite/)?([a-zA-Z0-9]{20,24})",
    re.IGNORECASE
)

# User state constants
STATE_NONE = 0
STATE_AWAITING_WEBSITES = 1
STATE_AWAITING_LINKS = 2


# ==========================================
# HELPER & VALIDATION FUNCTIONS
# ==========================================

def extract_wa_links(text: str) -> list[str]:
    """Extracts unique, normalized WhatsApp invite links from text, handling HTML entities."""
    if not text:
        return []
    cleaned_text = html.unescape(text)
    matches = WA_LINK_REGEX.findall(cleaned_text)
    unique_links = []
    seen = set()
    for code in matches:
        if code not in seen:
            seen.add(code)
            unique_links.append(f"https://chat.whatsapp.com/{code}")
    return unique_links


async def validate_wa_link(client: httpx.AsyncClient, url: str) -> dict:
    """Bulletproof validation using WhatsApp preview crawler headers and comprehensive keywords."""
    headers = {
        "User-Agent": "WhatsApp/2.23.2.74 A",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    
    try:
        response = await client.get(url, headers=headers, follow_redirects=True, timeout=10.0)
        
        if response.status_code in [429, 403, 503, 500]:
            return {"url": url, "valid": False, "approval_required": False, "reason": "Rate Limited / Blocked"}
        
        if response.status_code != 200:
            return {"url": url, "valid": False, "approval_required": False, "reason": "HTTP Error"}

        html_text = response.text
        soup = BeautifulSoup(html_text, 'html.parser')
        
        og_desc = ""
        og_title = ""
        meta_desc = soup.find("meta", property="og:description")
        if meta_desc:
            og_desc = meta_desc.get("content", "")
        meta_title = soup.find("meta", property="og:title")
        if meta_title:
            og_title = meta_title.get("content", "")

        combined_text = f"{html_text} {og_desc} {og_title}".lower()
        
        invalid_keywords = [
            "invite link is invalid",
            "reset invite link",
            "couldn't find group",
            "this invite link was reset",
            "expired",
            "revoked",
            "doesn't exist",
            "link was reset"
        ]
        if any(kw in combined_text for kw in invalid_keywords):
            return {"url": url, "valid": False, "approval_required": False, "reason": "Revoked/Invalid"}

        approval_keywords = [
            "admin approval needed",
            "admin_approval_required",
            "an admin must approve",
            "request to join",
            "requires_approval",
            "must approve your request",
            "request to join this group"
        ]
        requires_approval = any(kw in combined_text for kw in approval_keywords)

        return {
            "url": url,
            "valid": True,
            "approval_required": requires_approval,
            "reason": "OK" if not requires_approval else "Admin Approval Required"
        }

    except Exception as e:
        logger.error(f"Error checking {url}: {e}")
        return {"url": url, "valid": False, "approval_required": False, "reason": "Connection Failed"}


async def fetch_and_extract_from_url(client: httpx.AsyncClient, website_url: str) -> list[str]:
    """Dual-method extraction: Scans <a> href tags AND raw HTML text to guarantee zero missed links."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    try:
        if not website_url.startswith(("http://", "https://")):
            website_url = "https://" + website_url
            
        res = await client.get(website_url, headers=headers, follow_redirects=True, timeout=15.0)
        if res.status_code == 200:
            html_content = res.text
            soup = BeautifulSoup(html_content, 'html.parser')
            
            found_links = set()
            
            # Method 1: Search all <a> tag href attributes
            for a_tag in soup.find_all('a', href=True):
                href = a_tag['href']
                if 'chat.whatsapp.com' in href:
                    found_links.update(extract_wa_links(href))
                    
            # Method 2: Search raw HTML content via regex
            found_links.update(extract_wa_links(html_content))
            
            return list(found_links)
    except Exception as e:
        logger.error(f"Failed to fetch site {website_url}: {e}")
    return []


# ==========================================
# BOT TELEGRAM HANDLERS
# ==========================================

def get_main_menu_keyboard():
    """Returns the main action button markup."""
    keyboard = [
        [InlineKeyboardButton("🌐 Scrape & Validate Websites", callback_data="btn_scrape")],
        [InlineKeyboardButton("📁 Validate Link List / File", callback_data="btn_validate_list")]
    ]
    return InlineKeyboardMarkup(keyboard)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the main menu with inline action buttons."""
    context.user_data["state"] = STATE_NONE
    welcome_text = (
        "⚡ **WhatsApp Group Validator & Scraper Bot** ⚡\n\n"
        "Choose an option below to start:"
    )
    
    if update.message:
        await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=get_main_menu_keyboard())
    elif update.callback_query:
        await update.callback_query.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=get_main_menu_keyboard())


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles button clicks and immediately prompts user with a fresh message."""
    query = update.callback_query
    await query.answer()

    if query.data == "btn_scrape":
        context.user_data["state"] = STATE_AWAITING_WEBSITES
        await query.message.reply_text(
            "🌐 **Scrape & Validate Websites Mode Active**\n\n"
            "Please send or paste your list of target website URLs below (one URL per line).",
            parse_mode="Markdown"
        )
    elif query.data == "btn_validate_list":
        context.user_data["state"] = STATE_AWAITING_LINKS
        await query.message.reply_text(
            "📁 **Validate Link List Mode Active**\n\n"
            "Please upload your `.txt` file containing WhatsApp group links or paste the raw list directly in chat.",
            parse_mode="Markdown"
        )


async def handle_message_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes incoming text messages depending on active state."""
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
                        f"🌐 Scraped **{idx}/{len(urls)}** websites...\n"
                        f"🔗 Extracted **{len(extracted_links)}** unique WhatsApp links so far.",
                        parse_mode="Markdown"
                    )

        if not extracted_links:
            await progress_msg.edit_text(
                "❌ No WhatsApp group links found on the provided websites.",
                reply_markup=get_main_menu_keyboard()
            )
            return

        await process_and_validate_links(progress_msg, list(extracted_links), context)

    elif state == STATE_AWAITING_LINKS or state == STATE_NONE:
        context.user_data["state"] = STATE_NONE
        extracted_links = extract_wa_links(update.message.text)
        
        if not extracted_links:
            await update.message.reply_text(
                "❌ No valid WhatsApp group links detected. Send /start to begin.",
                reply_markup=get_main_menu_keyboard()
            )
            return

        progress_msg = await update.message.reply_text(f"⚡ Detected **{len(extracted_links)}** unique links. Initializing validation...", parse_mode="Markdown")
        await process_and_validate_links(progress_msg, extracted_links, context)


async def handle_document_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles `.txt` document file uploads containing WhatsApp links."""
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
        await progress_msg.edit_text(
            "❌ No valid WhatsApp group links found inside the uploaded `.txt` file.",
            reply_markup=get_main_menu_keyboard()
        )
        return

    await progress_msg.edit_text(f"⚡ Extracted **{len(extracted_links)}** unique links from file. Initializing validation...", parse_mode="Markdown")
    await process_and_validate_links(progress_msg, extracted_links, context)


async def process_and_validate_links(status_message, links: list[str], context: ContextTypes.DEFAULT_TYPE):
    """Validates links, displays live progress updates, outputs strict numbered lists + .txt file backup, and loops buttons."""
    total_links = len(links)
    valid_instant_join_links = []
    
    async with httpx.AsyncClient(verify=False) as client:
        for idx, link in enumerate(links, start=1):
            res = await validate_wa_link(client, link)
            
            if res["valid"] and not res["approval_required"]:
                valid_instant_join_links.append(link)

            if idx % 5 == 0 or idx == total_links:
                progress_bar = f"[{'█' * int(idx / total_links * 10)}{'░' * (10 - int(idx / total_links * 10))}]"
                await status_message.edit_text(
                    f"⚡ **Validating Links...**\n\n"
                    f"Progress: `{progress_bar}` **{idx}/{total_links}** ({int(idx/total_links*100)}%)\n"
                    f"✅ **Instant-Join Active:** {len(valid_instant_join_links)}",
                    parse_mode="Markdown"
                )
            await asyncio.sleep(0.3)

    await status_message.edit_text(
        f"✅ **Validation Complete!**\n\n"
        f"📊 **Total Links Scanned:** {total_links}\n"
        f"🚀 **Instant-Join (No Admin Approval):** {len(valid_instant_join_links)}\n"
        f"❌ **Filtered Out (Invalid/Admin Needed):** {total_links - len(valid_instant_join_links)}",
        parse_mode="Markdown"
    )

    if not valid_instant_join_links:
        await status_message.reply_text(
            "⚠️ No free-to-join active WhatsApp groups were found in this batch.\n\n"
            "Would you like to validate another batch or check more websites?",
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


# ==========================================
# MAIN BOT INITIALIZATION
# ==========================================

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
            
