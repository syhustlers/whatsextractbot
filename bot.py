import os
import re
import asyncio
import logging
import httpx
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
        pass  # Suppress HTTP logs to keep output clean

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info(f"Render health check server bound to port {port}")
    server.serve_forever()

# Start health server in a background daemon thread
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
    """Extracts unique, normalized WhatsApp invite links from text."""
    matches = WA_LINK_REGEX.findall(text)
    unique_links = []
    seen = set()
    for code in matches:
        if code not in seen:
            seen.add(code)
            unique_links.append(f"https://chat.whatsapp.com/{code}")
    return unique_links


async def validate_wa_link(client: httpx.AsyncClient, url: str) -> dict:
    """Checks if a WhatsApp group link is valid and does not require admin approval."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    
    try:
        response = await client.get(url, headers=headers, follow_redirects=True, timeout=10.0)
        
        if response.status_code != 200:
            return {"url": url, "valid": False, "approval_required": False, "reason": "HTTP Error"}

        html_text = response.text
        
        # Check if the invite link is invalid or revoked
        if "Invite link is invalid" in html_text or "Reset invite link" in html_text or "Couldn't find group" in html_text:
            return {"url": url, "valid": False, "approval_required": False, "reason": "Revoked/Invalid"}

        # Check for Admin Approval flag indicators on the web preview
        approval_indicators = [
            "Admin approval needed",
            "admin_approval_required",
            "An admin must approve your request",
            "Request to Join",
            '"requires_approval":true'
        ]
        
        requires_approval = any(indicator.lower() in html_text.lower() for indicator in approval_indicators)

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
    """Fetches HTML from a website URL and extracts all embedded WhatsApp links."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
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

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the main menu with inline action buttons."""
    context.user_data["state"] = STATE_NONE
    
    keyboard = [
        [InlineKeyboardButton("🌐 Scrape & Validate Websites", callback_data="btn_scrape")],
        [InlineKeyboardButton("📁 Validate Link List / File", callback_data="btn_validate_list")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_text = (
        "⚡ **WhatsApp Group Validator & Scraper Bot** ⚡\n\n"
        "Choose an option below to start:\n"
        "1️⃣ **Scrape & Validate Websites:** Send multiple website URLs to extract and filter links automatically.\n"
        "2️⃣ **Validate Link List / File:** Send a raw list of links or upload a `.txt` file."
    )
    
    if update.message:
        await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.message.edit_text(welcome_text, parse_mode="Markdown", reply_markup=reply_markup)


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles button clicks from the main menu."""
    query = update.callback_query
    await query.answer()

    if query.data == "btn_scrape":
        context.user_data["state"] = STATE_AWAITING_WEBSITES
        await query.edit_text(
            "🌐 **Scrape & Validate Websites Mode**\n\n"
            "Please send or paste your list of target website URLs below (one URL per line).\n\n"
            "Example:\n"
            "```\n"
            "[https://example.com/groups](https://example.com/groups)\n"
            "[https://another-site.org/whatsapp-links](https://another-site.org/whatsapp-links)\n"
            "```",
            parse_mode="Markdown"
        )
    elif query.data == "btn_validate_list":
        context.user_data["state"] = STATE_AWAITING_LINKS
        await query.edit_text(
            "📁 **Validate Link List Mode**\n\n"
            "Please upload a `.txt` file containing WhatsApp group links or paste the raw list directly in chat.",
            parse_mode="Markdown"
        )


async def handle_message_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes incoming text messages depending on active state."""
    state = context.user_data.get("state", STATE_NONE)

    if state == STATE_AWAITING_WEBSITES:
        context.user_data["state"] = STATE_NONE
        urls = [line.strip() for line in update.message.text.splitlines() if line.strip()]
        
        if not urls:
            await update.message.reply_text("❌ No valid URLs provided. Try again by pressing /start.")
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
            await progress_msg.edit_text("❌ No WhatsApp group links found on the provided websites.")
            return

        await process_and_validate_links(progress_msg, list(extracted_links), context)

    elif state == STATE_AWAITING_LINKS or state == STATE_NONE:
        context.user_data["state"] = STATE_NONE
        extracted_links = extract_wa_links(update.message.text)
        
        if not extracted_links:
            await update.message.reply_text("❌ No valid WhatsApp group links detected in your message. Send /start to begin.")
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
        await progress_msg.edit_text("❌ No valid WhatsApp group links found inside the uploaded `.txt` file.")
        return

    await progress_msg.edit_text(f"⚡ Extracted **{len(extracted_links)}** unique links from file. Initializing validation...", parse_mode="Markdown")
    await process_and_validate_links(progress_msg, extracted_links, context)


async def process_and_validate_links(status_message, links: list[str], context: ContextTypes.DEFAULT_TYPE):
    """Validates links, displays live progress updates, and outputs raw lists + .txt file backup."""
    total_links = len(links)
    valid_instant_join_links = []
    
    async with httpx.AsyncClient(verify=False) as client:
        for idx, link in enumerate(links, start=1):
            res = await validate_wa_link(client, link)
            
            if res["valid"] and not res["approval_required"]:
                valid_instant_join_links.append(link)

            # Live progress message update every 5 links or on last link
            if idx % 5 == 0 or idx == total_links:
                progress_bar = f"[{'█' * int(idx / total_links * 10)}{'░' * (10 - int(idx / total_links * 10))}]"
                await status_message.edit_text(
                    f"⚡ **Validating Links...**\n\n"
                    f"Progress: `{progress_bar}` **{idx}/{total_links}** ({int(idx/total_links*100)}%)\n"
                    f"✅ **Instant-Join Active:** {len(valid_instant_join_links)}",
                    parse_mode="Markdown"
                )
            await asyncio.sleep(0.3)

    # Final completion update
    await status_message.edit_text(
        f"✅ **Validation Complete!**\n\n"
        f"📊 **Total Links Scanned:** {total_links}\n"
        f"🚀 **Instant-Join (No Admin Approval):** {len(valid_instant_join_links)}\n"
        f"❌ **Filtered Out (Invalid/Admin Needed):** {total_links - len(valid_instant_join_links)}",
        parse_mode="Markdown"
    )

    if not valid_instant_join_links:
        await status_message.reply_text("⚠️ No free-to-join active WhatsApp groups were found in this batch.")
        return

    # Deliver raw text block chunks
    raw_output_text = "\n".join(valid_instant_join_links)
    await status_message.reply_text("👇 **Here are your instant-join valid WhatsApp group links:**")
    
    max_chars = 3800
    for i in range(0, len(raw_output_text), max_chars):
        chunk = raw_output_text[i:i + max_chars]
        await status_message.reply_text(chunk, disable_web_page_preview=True)

    # Deliver output as attached .txt file backup
    file_bytes = BytesIO(raw_output_text.encode("utf-8"))
    file_bytes.name = "valid_whatsapp_groups.txt"
    await context.bot.send_document(
        chat_id=status_message.chat_id,
        document=file_bytes,
        filename="valid_whatsapp_groups.txt",
        caption="📄 **Downloadable `.txt` export of verified instant-join links.**",
        parse_mode="Markdown"
    )


# ==========================================
# MAIN BOT INITIALIZATION
# ==========================================

def main():
    """Starts the bot application with an explicit event loop for Python 3.14+."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    app = Application.builder().token(BOT_TOKEN).build()

    # Register handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document_upload))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message_input))

    logger.info("Bot started successfully and polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
        
