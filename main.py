#!/usr/bin/env python3
"""
Telegram Deal-Post Automation Bot
===================================
Reads messages from Channel-A, classifies them, and routes them through
@AzFkMathsbot (via a shared bridge group) before posting to Channel-B.
"""

import asyncio
import logging
import re
import time
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional

# Requires: pip install python-dotenv
from dotenv import load_dotenv

from telegram import (
    Update,
    Message,
    Bot,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

# ──────────────────────────  CONFIGURATION  ──────────────────────────

# Load variables from .env file automatically
load_dotenv()

BOT_TOKEN        = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# Make sure IDs are converted to integers to prevent silent matching failures!
CHANNEL_A_ID     = int(os.getenv("CHANNEL_A_ID", "-1001234567890"))
CHANNEL_B_ID     = int(os.getenv("CHANNEL_B_ID", "-1009876543210"))
BRIDGE_GROUP_ID  = int(os.getenv("BRIDGE_GROUP_ID", "-1005555555555"))

AZFK_BOT_USERNAME = os.getenv("AZFK_BOT_USERNAME", "AzFkMathsbot")

# Timeouts (seconds)
AZFK_INITIAL_TIMEOUT   = 70            # max wait for first usable response
AZFK_FINAL_TIMEOUT     = 90            # max total wait including edits
AZFK_EDIT_SETTLE_TIME  = 8             # wait after last edit to assume "done"

MAX_RETRY_STANDARD     = 3             # max retries for coupon/bank cross-verify
MAX_RETRY_OPTIMIZED    = 3

# ──────────────────────────  LOGGING  ────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("DealBot")

# ──────────────────────────  LINK HELPERS  ───────────────────────────

SKIP_PATTERNS = [
    re.compile(r"https?://t\.me/\S+", re.I),
    re.compile(r"https?://youtu\.?be\S*", re.I),
    re.compile(r"https?://(?:www\.)?youtube\.com/\S+", re.I),
    re.compile(r"@Thankukool_bot", re.I),
    re.compile(r"\b(?:adidas|puma|tommy\s+hilfiger|h&m)\b", re.I),
]

AMAZON_LINK   = re.compile(r"https?://(?:amzn\.to|(?:www\.)?amazon\.\w{2,3})/\S+", re.I)
FLIPKART_LINK = re.compile(r"https?://(?:fkrt\.it|fkrt\.cc|(?:www\.)?flipkart\.com)/\S+", re.I)
MYNTRA_LINK   = re.compile(r"https?://(?:www\.)?myntra\.com/\S+", re.I)
AJIO_LINK     = re.compile(r"https?://(?:www\.)?ajio\.com/\S+", re.I)

URL_PATTERN   = re.compile(r"https?://\S+", re.I)

COUPON_BANK_KEYWORDS = re.compile(
    r"\b("
    r"coupon|coup|coupons|"
    r"bank\s*offer|bank\s*discount|bank\s*deal|"
    r"card\s*offer|card\s*discount|"
    r"cc\b|credit\s*card|debit\s*card|"
    r"hdfc|icici|sbi|axis|kotak|bob|canara|"
    r"cashback|cash\s*back|"
    r"no[\s\-]?cost\s*emi"
    r")\b",
    re.I,
)

PRICE_PATTERN = re.compile(
    r"(?:@|₹|rs\.?\s*|inr\.?\s*|loot\s*|price\s*|mrp\s*|deal\s*@?\s*)"
    r"(\d[\d,]*\.?\d*)",
    re.I,
)

# ──────────────────────────  ENUMS / STATE  ──────────────────────────

class Marketplace(Enum):
    AMAZON   = auto()
    FLIPKART = auto()
    MYNTRA   = auto()
    AJIO     = auto()
    OTHER    = auto()
    MIXED    = auto()


class BotMode(Enum):
    STANDARD  = "standard"
    OPTIMIZED = "optimized"


@dataclass
class PendingJob:
    original_msg: Message
    caption_text: str
    marketplace: Marketplace = Marketplace.OTHER
    price: Optional[str] = None
    mode_used: BotMode = BotMode.STANDARD
    sent_to_bridge_msg: Optional[Message] = None
    azfk_response_msg: Optional[Message] = None
    azfk_done: bool = False
    retry_count: int = 0
    created_at: float = field(default_factory=time.time)

# Global state
current_bot_mode: BotMode = BotMode.STANDARD
pending_jobs: dict[int, PendingJob] = {}
mode_switch_lock = asyncio.Lock()
processing_lock  = asyncio.Lock()


# ──────────────────────────  UTILITIES  ──────────────────────────────

def extract_text(msg: Message) -> str:
    return (msg.caption or msg.text or "").strip()


def extract_all_urls(text: str) -> list[str]:
    return URL_PATTERN.findall(text)


def should_skip(text: str) -> bool:
    for pat in SKIP_PATTERNS:
        if pat.search(text):
            return True
    return False


def count_marketplace_links(text: str):
    urls = extract_all_urls(text)
    marketplaces =[]
    for u in urls:
        if AMAZON_LINK.match(u):
            marketplaces.append(Marketplace.AMAZON)
        elif FLIPKART_LINK.match(u):
            marketplaces.append(Marketplace.FLIPKART)
        elif MYNTRA_LINK.match(u):
            marketplaces.append(Marketplace.MYNTRA)
        elif AJIO_LINK.match(u):
            marketplaces.append(Marketplace.AJIO)
        else:
            marketplaces.append(Marketplace.OTHER)
    return len(urls), marketplaces


def extract_price(text: str) -> Optional[str]:
    m = PRICE_PATTERN.search(text)
    return m.group(1).replace(",", "") if m else None


def parse_price(price_str: Optional[str]) -> Optional[float]:
    if not price_str:
        return None
    try:
        return float(price_str)
    except ValueError:
        return None


def has_coupon_bank_keywords(text: str) -> bool:
    # Remove URLs from the text first so domains like fkrt.cc don't trigger "cc" (credit card)
    text_without_urls = URL_PATTERN.sub("", text)
    return bool(COUPON_BANK_KEYWORDS.search(text_without_urls))


def is_azfk_response_final(text: str) -> bool:
    if not text:
        return False

    if any(text.startswith(icon) for icon in ["⏳", "🔍", "🎨", "📦", "⚙️"]):
        return False

    if "❌" in text:
        return True

    if URL_PATTERN.search(text):
        return True

    if len(text) > 80:
        return True

    return False


def is_azfk_error(text: str) -> bool:
    if not text:
        return True
    return "❌" in text


def is_azfk_not_detected(text: str) -> bool:
    return "Couldn't detect product" in (text or "")


def is_azfk_timeout_error(text: str) -> bool:
    return "Timed out" in (text or "") or "Error: Timed" in (text or "")


# ──────────────────────────  MODE MANAGEMENT  ────────────────────────

async def ensure_mode(bot: Bot, desired: BotMode) -> None:
    global current_bot_mode
    async with mode_switch_lock:
        if current_bot_mode == desired:
            return

        logger.info(f"Switching @AzFkMathsbot to {desired.value} mode")

        await bot.send_message(
            chat_id=BRIDGE_GROUP_ID,
            text="/Optimized",
        )

        deadline = time.time() + 15
        while time.time() < deadline:
            await asyncio.sleep(1.5)
            if current_bot_mode == desired:
                logger.info(f"Mode confirmed: {desired.value}")
                return

        logger.warning("Mode switch confirmation not received; retrying once")
        await bot.send_message(
            chat_id=BRIDGE_GROUP_ID,
            text="/Optimized",
        )
        deadline2 = time.time() + 15
        while time.time() < deadline2:
            await asyncio.sleep(1.5)
            if current_bot_mode == desired:
                logger.info(f"Mode confirmed on retry: {desired.value}")
                return

        logger.error(f"Could not confirm mode switch to {desired.value}")


# ──────────────────────────  COPY MESSAGE HELPER  ────────────────────

async def copy_message_to(msg: Message, dest_chat_id: int, bot: Bot) -> Optional[Message]:
    try:
        sent = await bot.copy_message(
            chat_id=dest_chat_id,
            from_chat_id=msg.chat_id,
            message_id=msg.message_id,
        )
        return sent
    except Exception as e:
        logger.error(f"copy_message_to failed: {e}")
        try:
            sent = await bot.forward_message(
                chat_id=dest_chat_id,
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
            )
            return sent
        except Exception as e2:
            logger.error(f"forward_message also failed: {e2}")
            return None


async def forward_original_to_channel_b(job: PendingJob, bot: Bot) -> None:
    logger.info(f"Forwarding original C-A msg {job.original_msg.message_id} → C-B")
    await copy_message_to(job.original_msg, CHANNEL_B_ID, bot)


async def forward_azfk_to_channel_b(job: PendingJob, bot: Bot) -> None:
    if job.azfk_response_msg:
        logger.info(f"Forwarding @AzFkMathsbot response → C-B")
        await copy_message_to(job.azfk_response_msg, CHANNEL_B_ID, bot)
    else:
        await forward_original_to_channel_b(job, bot)


# ──────────────────────  SEND TO @AzFkMathsbot  ─────────────────────

async def send_to_azfk(job: PendingJob, bot: Bot) -> Optional[Message]:
    sent = await copy_message_to(job.original_msg, BRIDGE_GROUP_ID, bot)
    if sent:
        msg_id = getattr(sent, 'message_id', None)

        if msg_id:
            job.sent_to_bridge_msg = sent
            pending_jobs[msg_id] = job
        else:
            job.sent_to_bridge_msg = None
            pending_jobs[int(time.time() * 1000)] = job
    return sent


async def wait_for_azfk_response(job: PendingJob, timeout: float = AZFK_FINAL_TIMEOUT) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if job.azfk_done:
            return True
        await asyncio.sleep(2)
    return job.azfk_done


# ──────────────────  AMAZON DECISION LOGIC  ──────────────────────────

def should_use_optimized_for_amazon(caption: str) -> bool:
    return not has_coupon_bank_keywords(caption)


def cross_verify_amazon_response(
    caption_from_a: str,
    response_text: str,
    mode_used: BotMode,
) -> Optional[BotMode]:

    caption_has_coupon = has_coupon_bank_keywords(caption_from_a)
    response_has_coupon = has_coupon_bank_keywords(response_text)

    if mode_used == BotMode.OPTIMIZED and response_has_coupon:
        logger.info("Cross-verify: response has coupon keywords but Optimized was used → retry Standard")
        return BotMode.STANDARD

    if mode_used == BotMode.STANDARD and not response_has_coupon and not caption_has_coupon:
        logger.info("Cross-verify: no coupon found in response, Standard was used → retry Optimized")
        return BotMode.OPTIMIZED

    if mode_used == BotMode.STANDARD and caption_has_coupon and not response_has_coupon:
        logger.info("Cross-verify: caption has coupon but response doesn't → retry Standard (scrape might have failed)")
        return BotMode.STANDARD

    return None


# ──────────────────────  MAIN PROCESSING PIPELINE  ───────────────────

async def process_channel_a_message(msg: Message, bot: Bot) -> None:
    async with processing_lock:
        text = extract_text(msg)
        if not text:
            logger.info("No text/caption – forwarding to C-B")
            await copy_message_to(msg, CHANNEL_B_ID, bot)
            return

        if should_skip(text):
            logger.info(f"Skipping message (matched skip pattern): {text[:80]}")
            return

        n_links, mp_list = count_marketplace_links(text)

        if n_links > 1:
            logger.info(f"WAY-1: {n_links} links detected – copying to C-B as-is")
            await copy_message_to(msg, CHANNEL_B_ID, bot)
            return

        if n_links == 0:
            logger.info("No links found – forwarding to C-B")
            await copy_message_to(msg, CHANNEL_B_ID, bot)
            return

        marketplace = mp_list[0]
        price = extract_price(text)
        logger.info(f"WAY-2: 1 link, marketplace={marketplace.name}, price={price}")

        if marketplace in (Marketplace.MYNTRA, Marketplace.AJIO):
            logger.info(f"Unsupported marketplace ({marketplace.name}) – forwarding to C-B")
            await copy_message_to(msg, CHANNEL_B_ID, bot)
            return

        if marketplace == Marketplace.OTHER:
            urls = extract_all_urls(text)
            skip_this = False
            for u in urls:
                for pat in SKIP_PATTERNS:
                    if pat.search(u):
                        skip_this = True
                        break
            if skip_this:
                logger.info("Other link is telegram/youtube – skipping")
                return
            logger.info("Other marketplace link – forwarding to C-B")
            await copy_message_to(msg, CHANNEL_B_ID, bot)
            return

        job = PendingJob(
            original_msg=msg,
            caption_text=text,
            marketplace=marketplace,
            price=price,
        )

        await _process_via_azfk(job, bot)


async def _process_via_azfk(job: PendingJob, bot: Bot) -> None:
    if job.marketplace == Marketplace.FLIPKART:
        desired_mode = BotMode.STANDARD
    elif job.marketplace == Marketplace.AMAZON:
        if should_use_optimized_for_amazon(job.caption_text):
            desired_mode = BotMode.OPTIMIZED
        else:
            desired_mode = BotMode.STANDARD
    else:
        desired_mode = BotMode.STANDARD

    job.mode_used = desired_mode
    await ensure_mode(bot, desired_mode)

    logger.info(f"Sending to @AzFkMathsbot via bridge group (mode={desired_mode.value})")
    await send_to_azfk(job, bot)

    got_response = await wait_for_azfk_response(job, AZFK_FINAL_TIMEOUT)

    if not got_response or job.azfk_response_msg is None:
        if has_coupon_bank_keywords(job.caption_text):
            logger.warning("No response from @AzFkMathsbot & had coupon – dropping post.")
        else:
            logger.warning("No response from @AzFkMathsbot - forwarding original to C-B")
            await forward_original_to_channel_b(job, bot)
        _cleanup_job(job)
        return

    response_text = extract_text(job.azfk_response_msg)

    if is_azfk_not_detected(response_text):
        if has_coupon_bank_keywords(job.caption_text):
            logger.warning("@AzFkMathsbot: couldn't detect product & had coupon – dropping post.")
        else:
            logger.info("@AzFkMathsbot: couldn't detect product – forwarding original to C-B")
            await forward_original_to_channel_b(job, bot)
        _cleanup_job(job)
        return

    if is_azfk_timeout_error(response_text):
        logger.info("@AzFkMathsbot: timed out – waiting a bit for delayed result")
        await asyncio.sleep(15)
        if job.azfk_done and job.azfk_response_msg and not is_azfk_error(extract_text(job.azfk_response_msg)):
            logger.info("Got delayed result after timeout – proceeding")
            response_text = extract_text(job.azfk_response_msg)
        else:
            logger.info("No delayed result – resending to @AzFkMathsbot")
            job.azfk_done = False
            job.azfk_response_msg = None
            job.retry_count += 1
            await send_to_azfk(job, bot)
            got_response2 = await wait_for_azfk_response(job, AZFK_FINAL_TIMEOUT)
            
            if not got_response2 or job.azfk_response_msg is None:
                if has_coupon_bank_keywords(job.caption_text):
                    logger.warning("Second attempt also failed & had coupon – dropping post.")
                else:
                    logger.warning("Second attempt also failed – forwarding original to C-B")
                    await forward_original_to_channel_b(job, bot)
                _cleanup_job(job)
                return
            
            response_text = extract_text(job.azfk_response_msg)
            if is_azfk_error(response_text):
                if has_coupon_bank_keywords(job.caption_text):
                    logger.warning("Second attempt error & had coupon – dropping post.")
                else:
                    logger.warning("Second attempt error – forwarding original to C-B")
                    await forward_original_to_channel_b(job, bot)
                _cleanup_job(job)
                return

    if job.marketplace == Marketplace.AMAZON:
        correction = cross_verify_amazon_response(
            job.caption_text, response_text, job.mode_used
        )
        retries_done = 0
        while correction is not None and retries_done < MAX_RETRY_STANDARD:
            retries_done += 1
            logger.info(
                f"Amazon cross-verify retry #{retries_done}: switching to {correction.value}"
            )
            
            # Anti-spam delay so bot doesn't reply instantly from cache
            await asyncio.sleep(2) 

            job.mode_used = correction
            job.azfk_done = False
            job.azfk_response_msg = None

            await ensure_mode(bot, correction)
            await send_to_azfk(job, bot)
            got = await wait_for_azfk_response(job, AZFK_FINAL_TIMEOUT)

            if not got or job.azfk_response_msg is None:
                logger.warning("Cross-verify retry failed (no response)")
                break
                
            response_text = extract_text(job.azfk_response_msg)

            # --- THE FIX: Handle timeout during cross-verify ---
            if is_azfk_timeout_error(response_text):
                logger.info("Cross-verify: @AzFkMathsbot timed out – waiting 15s for delayed result")
                await asyncio.sleep(15)
                if job.azfk_response_msg:
                    response_text = extract_text(job.azfk_response_msg)

            if is_azfk_error(response_text):
                logger.warning("Cross-verify retry failed with error. Retrying if attempts left.")
                continue # Allows loop to actually use its 3 retries instead of breaking!
            # ---------------------------------------------------

            correction = cross_verify_amazon_response(
                job.caption_text, response_text, job.mode_used
            )

        if job.azfk_response_msg is None or is_azfk_error(
            extract_text(job.azfk_response_msg)
        ):
            if has_coupon_bank_keywords(job.caption_text):
                logger.warning("All retries exhausted (error) and had coupon – dropping post.")
            else:
                logger.warning("All retries exhausted – forwarding original to C-B")
                await forward_original_to_channel_b(job, bot)
            
            _cleanup_job(job)
            if current_bot_mode != BotMode.STANDARD:
                await ensure_mode(bot, BotMode.STANDARD)
            return

    final_response_text = extract_text(job.azfk_response_msg)

    # 1. Coupon Failure Drop
    if has_coupon_bank_keywords(job.caption_text) and not has_coupon_bank_keywords(final_response_text):
        logger.warning("Coupon missing from final bot response after all retries. Deal likely over. Dropping post.")
        _cleanup_job(job)
        if current_bot_mode != BotMode.STANDARD:
            await ensure_mode(bot, BotMode.STANDARD)
        return

    # 2. Price Test Check Drop
    if job.price is not None:
        ca_price = parse_price(job.price)
        cb_price_str = extract_price(final_response_text)
        cb_price = parse_price(cb_price_str)
        
        if ca_price is not None and cb_price is not None:
            if cb_price > (ca_price + 10):  # Added ₹10 buffer here
                logger.warning(f"Generated deal card price (₹{cb_price}) is > C-A price (₹{ca_price}). Dropping post.")
                _cleanup_job(job)
                if current_bot_mode != BotMode.STANDARD:
                    await ensure_mode(bot, BotMode.STANDARD)
                return

    await forward_azfk_to_channel_b(job, bot)
    _cleanup_job(job)

    if current_bot_mode != BotMode.STANDARD:
        await ensure_mode(bot, BotMode.STANDARD)


def _cleanup_job(job: PendingJob) -> None:
    keys_to_remove = [k for k, v in pending_jobs.items() if v is job]
    for k in keys_to_remove:
        pending_jobs.pop(k, None)


# ──────────────────────  TELEGRAM HANDLERS  ──────────────────────────

async def process_channel_a_message_safe(msg: Message, bot: Bot) -> None:
    """Safe wrapper that ensures crashes don't break the background task."""
    try:
        await process_channel_a_message(msg, bot)
    except Exception:
        logger.exception(f"Error processing Channel-A msg #{msg.message_id}")
        try:
            await copy_message_to(msg, CHANNEL_B_ID, bot)
        except Exception:
            logger.exception("Safety-net forward also failed")


async def on_channel_a_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.channel_post or update.message
    if msg is None:
        return
    if msg.chat_id != CHANNEL_A_ID:
        return

    logger.info(f"📩 New Channel-A message #{msg.message_id}")

    # THE FIX: We push the 90-second waiting process into a background task!
    # This instantly frees up the bot so it NEVER stops listening to the bridge group.
    asyncio.create_task(process_channel_a_message_safe(msg, context.bot))


async def on_bridge_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global current_bot_mode

    msg = update.message or update.edited_message
    if msg is None:
        return

    if msg.from_user and msg.from_user.id == context.bot.id:
        return

    text = extract_text(msg)
    if not text:
        return

    clean_text = text[:60].replace('\n', ' ')
    logger.info(f"📥 Bridge Group Update: {clean_text}")

    if "Default mode set to Optimized" in text:
        current_bot_mode = BotMode.OPTIMIZED
        logger.info("🔄 Mode confirmed → OPTIMIZED")
        return
    if "Default mode set to Standard" in text:
        current_bot_mode = BotMode.STANDARD
        logger.info("🔄 Mode confirmed → STANDARD")
        return

    matched_job: Optional[PendingJob] = None

    if msg.reply_to_message:
        reply_id = msg.reply_to_message.message_id
        if reply_id in pending_jobs:
            matched_job = pending_jobs[reply_id]
            
            # RACE CONDITION FIX: Ignore late edits from @AzFkMathsbot that belong to older retries
            if matched_job.sent_to_bridge_msg and reply_id != matched_job.sent_to_bridge_msg.message_id:
                logger.info("Ignoring ghost edit/reply belonging to an older retry attempt.")
                return

    if matched_job is None and pending_jobs:
        most_recent_key = max(pending_jobs.keys())
        matched_job = pending_jobs[most_recent_key]

    if matched_job is None:
        return

    if is_azfk_response_final(text):
        matched_job.azfk_response_msg = msg
        matched_job.azfk_done = True
        logger.info(f"✅ Deal card matched & saved! (msg #{msg.message_id})")
    else:
        logger.info(f"⏳ Recognized as intermediate state. Waiting for final...")


# ──────────────────────  STARTUP / SHUTDOWN  ─────────────────────────

# ──────────────────────  STARTUP / SHUTDOWN & KEEP-ALIVE  ────────────

class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is alive and running!")
        
    # Suppress HTTP logging so UptimeRobot doesn't spam your terminal
    def log_message(self, format, *args):
        pass

def keep_alive():
    # Koyeb/Render assigns a dynamic PORT via env variables, defaults to 8080
    port = int(os.environ.get("PORT", 8080)) 
    server = HTTPServer(('0.0.0.0', port), DummyHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info(f"🌐 Dummy web server started on port {port} for keep-alive")


async def post_init(application) -> None:
    logger.info("Bot started. Verifying starting mode...")
    global current_bot_mode
    current_bot_mode = BotMode.STANDARD
    logger.info("Assumed starting mode: STANDARD")


def main() -> None:
    logger.info("🚀 Starting Deal Automation Bot")

    # Start the dummy web server for Koyeb/Render/HuggingFace + UptimeRobot
    keep_alive()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(
        MessageHandler(
            filters.Chat(CHANNEL_A_ID) & (filters.ALL),
            on_channel_a_message,
            block=False   # Double protection to never block the listener
        ),
        group=0,
    )

    app.add_handler(
        MessageHandler(
            filters.Chat(BRIDGE_GROUP_ID),
            on_bridge_group_message,
            block=False
        ),
        group=1,
    )

    logger.info("Polling for updates...")
    app.run_polling(
        allowed_updates=[
            "message",
            "edited_message",
            "channel_post",
            "edited_channel_post",
        ],
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
