#!/usr/bin/env python3
"""
Telegram Deal-Post Automation Bot
===================================
Reads messages from Channel-A, classifies them, and routes them through
@AzFkMathsbot (via a shared bridge group) before posting to Channel-B.

Prerequisites
-------------
1. Create your bot via @BotFather.
2. Enable **Bot-to-Bot Communication Mode** in @BotFather for your bot.
3. Add your bot as **admin** in Channel-A (read messages), Channel-B (post),
   and the bridge group (read + write).
4. @AzFkMathsbot must also be in the bridge group with send-message rights.
5. Disable "Group Privacy" for your bot in @BotFather so it can see all
   messages in the bridge group (including those from @AzFkMathsbot).
6. pip install python-telegram-bot==22.7   (or latest 22.x)

Environment Variables / config.py  – fill these in.
"""

import asyncio
import logging
import re
import time
import html
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional

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
from telegram.constants import ParseMode

# ──────────────────────────  CONFIGURATION  ──────────────────────────

BOT_TOKEN        = "YOUR_BOT_TOKEN_HERE"

CHANNEL_A_ID     = -1001234567890      # source channel (your bot is admin)
CHANNEL_B_ID     = -1009876543210      # destination channel (your bot is admin)
BRIDGE_GROUP_ID  = -1005555555555      # private group with your bot + @AzFkMathsbot

AZFK_BOT_USERNAME = "AzFkMathsbot"     # without @
AZFK_BOT_ID       = 0                  # fill in the numeric user-id of @AzFkMathsbot
                                        # (get via @userinfobot or getUpdates)

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

# Patterns to SKIP entirely (telegram links, youtube, promo bot mentions)
SKIP_PATTERNS = [
    re.compile(r"https?://t\.me/\S+", re.I),
    re.compile(r"https?://youtu\.?be\S*", re.I),
    re.compile(r"https?://(?:www\.)?youtube\.com/\S+", re.I),
    re.compile(r"@Thankukool_bot", re.I),
]

# Marketplace link patterns
AMAZON_LINK   = re.compile(r"https?://(?:amzn\.to|(?:www\.)?amazon\.\w{2,3})/\S+", re.I)
FLIPKART_LINK = re.compile(r"https?://(?:fkrt\.it|fkrt\.cc|(?:www\.)?flipkart\.com)/\S+", re.I)
MYNTRA_LINK   = re.compile(r"https?://(?:www\.)?myntra\.com/\S+", re.I)
AJIO_LINK     = re.compile(r"https?://(?:www\.)?ajio\.com/\S+", re.I)

# Generic URL pattern
URL_PATTERN   = re.compile(r"https?://\S+", re.I)

# Keywords signalling coupon / bank-offer deals  (case-insensitive search)
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

# Price extraction: @1119, ₹297, Rs.9999, Loot 499, etc.
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
    """Tracks a single Channel-A message being processed."""
    original_msg: Message                 # the Channel-A message object
    caption_text: str                     # extracted text / caption
    marketplace: Marketplace = Marketplace.OTHER
    price: Optional[str] = None
    mode_used: BotMode = BotMode.STANDARD
    sent_to_bridge_msg: Optional[Message] = None    # our copy sent to bridge
    azfk_response_msg: Optional[Message] = None     # latest @AzFkMathsbot reply
    azfk_done: bool = False
    retry_count: int = 0
    created_at: float = field(default_factory=time.time)


# Global state
current_bot_mode: BotMode = BotMode.STANDARD     # what @AzFkMathsbot is set to now
pending_jobs: dict[int, PendingJob] = {}          # bridge_msg_id -> PendingJob
mode_switch_lock = asyncio.Lock()
processing_lock  = asyncio.Lock()                 # serialize Channel-A processing


# ──────────────────────────  UTILITIES  ──────────────────────────────

def extract_text(msg: Message) -> str:
    """Return the best available text from a message."""
    return (msg.caption or msg.text or "").strip()


def extract_all_urls(text: str) -> list[str]:
    return URL_PATTERN.findall(text)


def should_skip(text: str) -> bool:
    """Return True if the message should be completely ignored."""
    for pat in SKIP_PATTERNS:
        if pat.search(text):
            return True
    return False


def count_marketplace_links(text: str):
    """Return (total_links, list_of_Marketplace)."""
    urls = extract_all_urls(text)
    marketplaces = []
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


def has_coupon_bank_keywords(text: str) -> bool:
    return bool(COUPON_BANK_KEYWORDS.search(text))


def dominant_marketplace(mp_list: list[Marketplace]) -> Marketplace:
    """If all links are same marketplace return that, else MIXED."""
    s = set(mp_list)
    s.discard(Marketplace.OTHER)
    if len(s) == 1:
        return s.pop()
    if len(s) == 0:
        return Marketplace.OTHER
    return Marketplace.MIXED


def is_azfk_response_final(text: str) -> bool:
    """
    @AzFkMathsbot streams: ⏳ Processing... → 🔍 Scraping... → final card.
    We consider it "final" when it no longer starts with ⏳ or 🔍
    and has substantial content (>80 chars) OR is an error message.
    """
    if not text:
        return False
    if text.startswith("⏳") or text.startswith("🔍"):
        return False
    if "❌" in text:
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


def is_mode_confirm(text: str, mode: BotMode) -> bool:
    """Check if text is a mode-switch confirmation from @AzFkMathsbot."""
    if mode == BotMode.OPTIMIZED:
        return "Default mode set to Optimized" in (text or "")
    return "Default mode set to Standard" in (text or "")


# ──────────────────────────  MODE MANAGEMENT  ────────────────────────

async def ensure_mode(bot: Bot, desired: BotMode) -> None:
    """
    Toggle @AzFkMathsbot to the desired mode via the bridge group.
    Waits for the confirmation message.
    """
    global current_bot_mode
    async with mode_switch_lock:
        if current_bot_mode == desired:
            return

        logger.info(f"Switching @AzFkMathsbot to {desired.value} mode")

        # Send /Optimized command (it toggles between standard ↔ optimized)
        await bot.send_message(
            chat_id=BRIDGE_GROUP_ID,
            text="/Optimized",
        )

        # Wait for confirmation (up to 15 s)
        deadline = time.time() + 15
        while time.time() < deadline:
            await asyncio.sleep(1.5)
            # The confirmation will be picked up by our bridge-group handler
            # which sets current_bot_mode.  Check here:
            if current_bot_mode == desired:
                logger.info(f"Mode confirmed: {desired.value}")
                return

        # If we didn't see a confirmation, try one more toggle
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
    """
    Copy a message (with image + caption, or text) to dest_chat_id
    preserving formatting.  Returns the sent Message.
    """
    try:
        sent = await bot.copy_message(
            chat_id=dest_chat_id,
            from_chat_id=msg.chat_id,
            message_id=msg.message_id,
        )
        return sent
    except Exception as e:
        logger.error(f"copy_message_to failed: {e}")
        # Fallback: try forward
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
    """Forward the original Channel-A message to Channel-B as-is."""
    logger.info(f"Forwarding original C-A msg {job.original_msg.message_id} → C-B")
    await copy_message_to(job.original_msg, CHANNEL_B_ID, bot)


async def forward_azfk_to_channel_b(job: PendingJob, bot: Bot) -> None:
    """Forward the @AzFkMathsbot result to Channel-B."""
    if job.azfk_response_msg:
        logger.info(f"Forwarding @AzFkMathsbot response → C-B")
        await copy_message_to(job.azfk_response_msg, CHANNEL_B_ID, bot)
    else:
        await forward_original_to_channel_b(job, bot)


# ──────────────────────  SEND TO @AzFkMathsbot  ─────────────────────

async def send_to_azfk(job: PendingJob, bot: Bot) -> Optional[Message]:
    """
    Copy the Channel-A message into the bridge group so @AzFkMathsbot
    picks it up.  Returns the Message we sent.
    """
    sent = await copy_message_to(job.original_msg, BRIDGE_GROUP_ID, bot)
    if sent:
        # Store mapping so we can match @AzFkMathsbot's reply
        if isinstance(sent, Message):
            job.sent_to_bridge_msg = sent
            pending_jobs[sent.message_id] = job
        else:
            # copy_message returns MessageId, not Message – handle gracefully
            # We'll track by time-window instead
            job.sent_to_bridge_msg = None
            # Store with a synthetic key
            pending_jobs[int(time.time() * 1000)] = job
    return sent


async def wait_for_azfk_response(job: PendingJob, timeout: float = AZFK_FINAL_TIMEOUT) -> bool:
    """
    Poll until job.azfk_done is True (set by bridge group handler)
    or timeout.  Returns True if we got a final response.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if job.azfk_done:
            return True
        await asyncio.sleep(2)
    return job.azfk_done


# ──────────────────  AMAZON DECISION LOGIC  ──────────────────────────

def should_use_optimized_for_amazon(caption: str) -> bool:
    """
    First line of defense: if caption has coupon/bank keywords → Standard.
    Otherwise → Optimized.
    """
    return not has_coupon_bank_keywords(caption)


def cross_verify_amazon_response(
    caption_from_a: str,
    response_text: str,
    mode_used: BotMode,
) -> Optional[BotMode]:
    """
    Second line of defense.
    Returns the mode we SHOULD have used, or None if current is fine.
    """
    caption_has_coupon = has_coupon_bank_keywords(caption_from_a)
    response_has_coupon = has_coupon_bank_keywords(response_text)

    if mode_used == BotMode.OPTIMIZED and response_has_coupon:
        # Bot found coupon/bank in its scrape but we used Optimized
        # → should redo with Standard
        logger.info("Cross-verify: response has coupon keywords but Optimized was used → retry Standard")
        return BotMode.STANDARD

    if mode_used == BotMode.STANDARD and not response_has_coupon and not caption_has_coupon:
        # We used Standard (perhaps caption hinted coupon) but neither
        # original nor response actually has coupon → redo Optimized
        logger.info("Cross-verify: no coupon found in response, Standard was used → retry Optimized")
        return BotMode.OPTIMIZED

    if mode_used == BotMode.STANDARD and caption_has_coupon and not response_has_coupon:
        # Caption says coupon but bot didn't find it → possible scrape failure
        # Retry Standard again to see if bot catches it
        logger.info("Cross-verify: caption has coupon but response doesn't → retry Standard (scrape might have failed)")
        return BotMode.STANDARD

    return None  # all good


# ──────────────────────  MAIN PROCESSING PIPELINE  ───────────────────

async def process_channel_a_message(msg: Message, bot: Bot) -> None:
    """
    Core orchestrator called for every new Channel-A post.
    Runs under processing_lock to serialize.
    """
    async with processing_lock:
        text = extract_text(msg)
        if not text:
            # No text at all – might be pure image.  Forward as-is.
            logger.info("No text/caption – forwarding to C-B")
            await copy_message_to(msg, CHANNEL_B_ID, bot)
            return

        # ── Check skip patterns ──
        if should_skip(text):
            logger.info(f"Skipping message (matched skip pattern): {text[:80]}")
            return

        # ── Count links ──
        n_links, mp_list = count_marketplace_links(text)

        # ═══════════  WAY-1:  Multiple links  ═══════════
        if n_links > 1:
            logger.info(f"WAY-1: {n_links} links detected – copying to C-B as-is")
            await copy_message_to(msg, CHANNEL_B_ID, bot)
            return

        # ═══════════  WAY-2:  0 or 1 link  ═══════════
        if n_links == 0:
            # No link at all – just text or image+text.
            # Treat same as "other" – forward to C-B
            logger.info("No links found – forwarding to C-B")
            await copy_message_to(msg, CHANNEL_B_ID, bot)
            return

        # Exactly 1 link
        marketplace = mp_list[0]
        price = extract_price(text)
        logger.info(f"WAY-2: 1 link, marketplace={marketplace.name}, price={price}")

        # ── Myntra / Ajio / unsupported others: send to C-B directly ──
        if marketplace in (Marketplace.MYNTRA, Marketplace.AJIO):
            logger.info(f"Unsupported marketplace ({marketplace.name}) – forwarding to C-B")
            await copy_message_to(msg, CHANNEL_B_ID, bot)
            return

        # ── "Other" marketplace (not Amazon/Flipkart/Myntra/Ajio) ──
        if marketplace == Marketplace.OTHER:
            # Check if it's a telegram/youtube link that slipped through
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
            # Forward other marketplace links to C-B directly
            logger.info("Other marketplace link – forwarding to C-B")
            await copy_message_to(msg, CHANNEL_B_ID, bot)
            return

        # ── Amazon or Flipkart → use @AzFkMathsbot ──
        job = PendingJob(
            original_msg=msg,
            caption_text=text,
            marketplace=marketplace,
            price=price,
        )

        await _process_via_azfk(job, bot)


async def _process_via_azfk(job: PendingJob, bot: Bot) -> None:
    """Send to @AzFkMathsbot, handle mode switching, wait, cross-verify."""

    # ── Determine initial mode ──
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

    # ── Switch mode if needed ──
    await ensure_mode(bot, desired_mode)

    # ── Send to bridge group ──
    logger.info(f"Sending to @AzFkMathsbot via bridge group (mode={desired_mode.value})")
    await send_to_azfk(job, bot)

    # ── Wait for @AzFkMathsbot response ──
    got_response = await wait_for_azfk_response(job, AZFK_FINAL_TIMEOUT)

    if not got_response or job.azfk_response_msg is None:
        # ── CASE 3: No response at all ──
        logger.warning("No response from @AzFkMathsbot – forwarding original to C-B")
        await forward_original_to_channel_b(job, bot)
        _cleanup_job(job)
        return

    response_text = extract_text(job.azfk_response_msg)

    # ── CASE 1: ❌ Couldn't detect product ──
    if is_azfk_not_detected(response_text):
        logger.info("@AzFkMathsbot: couldn't detect product – forwarding original to C-B")
        await forward_original_to_channel_b(job, bot)
        _cleanup_job(job)
        return

    # ── CASE 2: ❌ Error: Timed out ──
    if is_azfk_timeout_error(response_text):
        logger.info("@AzFkMathsbot: timed out – waiting a bit for delayed result")
        # Wait extra time for a delayed result
        await asyncio.sleep(15)
        if job.azfk_done and job.azfk_response_msg and not is_azfk_error(extract_text(job.azfk_response_msg)):
            # Bot eventually gave a result after timeout message
            logger.info("Got delayed result after timeout – proceeding")
            response_text = extract_text(job.azfk_response_msg)
        else:
            # Truly timed out – resend
            logger.info("No delayed result – resending to @AzFkMathsbot")
            job.azfk_done = False
            job.azfk_response_msg = None
            job.retry_count += 1
            await send_to_azfk(job, bot)
            got_response2 = await wait_for_azfk_response(job, AZFK_FINAL_TIMEOUT)
            if not got_response2 or job.azfk_response_msg is None:
                logger.warning("Second attempt also failed – forwarding original to C-B")
                await forward_original_to_channel_b(job, bot)
                _cleanup_job(job)
                return
            response_text = extract_text(job.azfk_response_msg)
            if is_azfk_error(response_text):
                logger.warning("Second attempt error – forwarding original to C-B")
                await forward_original_to_channel_b(job, bot)
                _cleanup_job(job)
                return

    # ── If we reach here, we have a successful response ──

    # ── CASE ALPHA: Cross-verify for Amazon ──
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
            job.mode_used = correction
            job.azfk_done = False
            job.azfk_response_msg = None

            await ensure_mode(bot, correction)
            await send_to_azfk(job, bot)
            got = await wait_for_azfk_response(job, AZFK_FINAL_TIMEOUT)

            if not got or job.azfk_response_msg is None or is_azfk_error(
                extract_text(job.azfk_response_msg)
            ):
                # Retry failed – use whatever we have
                logger.warning("Cross-verify retry failed")
                break

            response_text = extract_text(job.azfk_response_msg)
            correction = cross_verify_amazon_response(
                job.caption_text, response_text, job.mode_used
            )

        # After max retries, if still error → forward original
        if job.azfk_response_msg is None or is_azfk_error(
            extract_text(job.azfk_response_msg)
        ):
            logger.warning("All retries exhausted – forwarding original to C-B")
            await forward_original_to_channel_b(job, bot)
            _cleanup_job(job)
            return

    # ── Forward the good result to Channel-B ──
    await forward_azfk_to_channel_b(job, bot)
    _cleanup_job(job)

    # ── Restore mode to Standard as a safe default ──
    if current_bot_mode != BotMode.STANDARD:
        await ensure_mode(bot, BotMode.STANDARD)


def _cleanup_job(job: PendingJob) -> None:
    """Remove job from pending map."""
    keys_to_remove = [k for k, v in pending_jobs.items() if v is job]
    for k in keys_to_remove:
        pending_jobs.pop(k, None)


# ──────────────────────  TELEGRAM HANDLERS  ──────────────────────────

async def on_channel_a_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handler for every new message in Channel-A.
    """
    msg = update.channel_post or update.message
    if msg is None:
        return
    if msg.chat_id != CHANNEL_A_ID:
        return

    logger.info(f"📩 New Channel-A message #{msg.message_id}")

    try:
        await process_channel_a_message(msg, context.bot)
    except Exception:
        logger.exception(f"Error processing Channel-A msg #{msg.message_id}")
        # Safety net: forward original
        try:
            await copy_message_to(msg, CHANNEL_B_ID, context.bot)
        except Exception:
            logger.exception("Safety-net forward also failed")


async def on_bridge_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handler for messages in the bridge group.
    We watch for @AzFkMathsbot replies and mode confirmations.
    """
    global current_bot_mode

    msg = update.message or update.edited_message
    if msg is None:
        return
    if msg.chat_id != BRIDGE_GROUP_ID:
        return

    # Identify sender
    sender_id = msg.from_user.id if msg.from_user else None
    sender_username = (msg.from_user.username or "").lower() if msg.from_user else ""

    is_from_azfk = (
        (AZFK_BOT_ID and sender_id == AZFK_BOT_ID)
        or sender_username == AZFK_BOT_USERNAME.lower()
    )

    if not is_from_azfk:
        return

    text = extract_text(msg)

    # ── Check for mode-switch confirmation ──
    if "Default mode set to Optimized" in text:
        current_bot_mode = BotMode.OPTIMIZED
        logger.info("🔄 Mode confirmed → OPTIMIZED")
        return
    if "Default mode set to Standard" in text:
        current_bot_mode = BotMode.STANDARD
        logger.info("🔄 Mode confirmed → STANDARD")
        return

    # ── Check if this is a response to one of our pending jobs ──
    # Match by reply_to_message
    matched_job: Optional[PendingJob] = None

    if msg.reply_to_message:
        reply_id = msg.reply_to_message.message_id
        if reply_id in pending_jobs:
            matched_job = pending_jobs[reply_id]

    # If no reply match, try matching by most recent pending job (fallback)
    if matched_job is None and pending_jobs:
        # Get the most recent job
        most_recent_key = max(pending_jobs.keys())
        matched_job = pending_jobs[most_recent_key]

    if matched_job is None:
        return

    # ── Process the response ──
    if is_azfk_response_final(text):
        matched_job.azfk_response_msg = msg
        matched_job.azfk_done = True
        logger.info(f"✅ @AzFkMathsbot final response received (msg #{msg.message_id})")
    else:
        # Intermediate update (⏳, 🔍, etc.) – just note it
        logger.info(f"⏳ @AzFkMathsbot intermediate update: {text[:60]}")


async def on_bridge_group_edited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle edited messages in bridge group.
    @AzFkMathsbot progressively edits its message from
    ⏳ Processing... → 🔍 Scraping... → final deal card.
    """
    global current_bot_mode

    msg = update.edited_message
    if msg is None:
        return
    if msg.chat_id != BRIDGE_GROUP_ID:
        return

    sender_username = (msg.from_user.username or "").lower() if msg.from_user else ""
    sender_id = msg.from_user.id if msg.from_user else None

    is_from_azfk = (
        (AZFK_BOT_ID and sender_id == AZFK_BOT_ID)
        or sender_username == AZFK_BOT_USERNAME.lower()
    )

    if not is_from_azfk:
        return

    text = extract_text(msg)

    # Mode confirmation can also come as an edit
    if "Default mode set to Optimized" in text:
        current_bot_mode = BotMode.OPTIMIZED
        logger.info("🔄 Mode confirmed (via edit) → OPTIMIZED")
        return
    if "Default mode set to Standard" in text:
        current_bot_mode = BotMode.STANDARD
        logger.info("🔄 Mode confirmed (via edit) → STANDARD")
        return

    # Find the matching job
    matched_job: Optional[PendingJob] = None

    if msg.reply_to_message:
        reply_id = msg.reply_to_message.message_id
        if reply_id in pending_jobs:
            matched_job = pending_jobs[reply_id]

    if matched_job is None and pending_jobs:
        most_recent_key = max(pending_jobs.keys())
        matched_job = pending_jobs[most_recent_key]

    if matched_job is None:
        return

    if is_azfk_response_final(text):
        matched_job.azfk_response_msg = msg
        matched_job.azfk_done = True
        logger.info(f"✅ @AzFkMathsbot final response (edited msg #{msg.message_id})")
    else:
        logger.info(f"⏳ @AzFkMathsbot edit (intermediate): {text[:60]}")


# ──────────────────────  STARTUP / SHUTDOWN  ─────────────────────────

async def post_init(application) -> None:
    """Called after the bot starts. Sync the current mode."""
    logger.info("Bot started. Verifying @AzFkMathsbot mode...")
    # We assume Standard on startup. If you want to verify, you can
    # send /Optimized twice to toggle and read confirmations, but that's
    # fragile. Better to just assume Standard is default.
    global current_bot_mode
    current_bot_mode = BotMode.STANDARD
    logger.info("Assumed starting mode: STANDARD")


def main() -> None:
    """Entry point."""
    logger.info("🚀 Starting Deal Automation Bot")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # ── Handler for Channel-A posts ──
    # channel_post filter catches messages posted to channels
    app.add_handler(
        MessageHandler(
            filters.Chat(CHANNEL_A_ID) & (filters.ALL),
            on_channel_a_message,
        ),
        group=0,  # priority group
    )

    # ── Handler for bridge group messages (new messages from @AzFkMathsbot) ──
    app.add_handler(
        MessageHandler(
            filters.Chat(BRIDGE_GROUP_ID)
            & filters.UpdateType.MESSAGE
            & (~filters.COMMAND),
            on_bridge_group_message,
        ),
        group=1,
    )

    # ── Handler for bridge group edited messages ──
    app.add_handler(
        MessageHandler(
            filters.Chat(BRIDGE_GROUP_ID)
            & filters.UpdateType.EDITED_MESSAGE,
            on_bridge_group_edited,
        ),
        group=2,
    )

    # Also handle commands in bridge group (for mode confirmations that
    # come as regular messages after /Optimized)
    app.add_handler(
        MessageHandler(
            filters.Chat(BRIDGE_GROUP_ID)
            & filters.UpdateType.MESSAGE,
            on_bridge_group_message,
        ),
        group=3,
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