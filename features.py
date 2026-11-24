"""
features.py - the dynamic workspace of the bot (handlers, DB logic, admin panel).
main.py is a static engine; everything that changes lives here.

Flow in one paragraph
---------------------
A customer pays outside Telegram. A helper forwards the customer's message (or
uses /grant @username), ticks the channels the customer paid for and enters the
amount. The customer then sees every channel as a button: paid ones are
unlocked, the rest are locked. Unlocked buttons give a join-request invite link
and the bot (admin in the channel) approves exactly that customer's request.
If a forward hides the customer's ID, the customer just sends the screenshot to
the bot and a helper verifies it and picks the channels (or rejects it).
"""
import html
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from telegram import BotCommand, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)
HTML = ParseMode.HTML

# ---------------------------------------------------------------------------
# Channels are now managed by the ADMIN from the bot (Admin panel -> Channels).
# SEED_CHANNELS is optional: on the very first start, any entry below with a real
# chat_id is copied into the database. After that, use the panel.
# The bot MUST be an admin in each channel with "Invite Users via Link" and
# "Ban Users" (needed for revoking) permissions.
# ---------------------------------------------------------------------------
CHANNELS = {}  # live cache, filled from the database (code -> name, chat_id, link)

SEED_CHANNELS = {
    "A": {"name": "Premium Assets", "chat_id": -1001111111111},
    "B": {"name": "VIP Signals", "chat_id": -1002222222222},
    "C": {"name": "Masterclass", "chat_id": -1003333333333},
}

# ---------------------------------------------------------------------------
# Database schema (created on start-up, safe to run every time)
# ---------------------------------------------------------------------------
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS transactions (
        id SERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL,
        username TEXT,
        channels TEXT[] NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS amount NUMERIC DEFAULT 0",
    "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS helper_id BIGINT",
    """CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS helpers (
        user_id BIGINT PRIMARY KEY,
        name TEXT,
        added_by BIGINT,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS entitlements (
        user_id BIGINT NOT NULL,
        channel_code TEXT NOT NULL,
        active BOOLEAN DEFAULT TRUE,
        granted_by BIGINT,
        granted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        revoked_at TIMESTAMP,
        revoked_by BIGINT,
        PRIMARY KEY (user_id, channel_code)
    )""",
    """CREATE TABLE IF NOT EXISTS pending_grants (
        id SERIAL PRIMARY KEY,
        username TEXT NOT NULL,
        channel_code TEXT NOT NULL,
        granted_by BIGINT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (username, channel_code)
    )""",
    """CREATE TABLE IF NOT EXISTS drafts (
        id SERIAL PRIMARY KEY,
        source TEXT NOT NULL,
        target_user_id BIGINT,
        target_username TEXT,
        target_name TEXT,
        photo_id TEXT,
        selected JSONB DEFAULT '[]',
        state TEXT DEFAULT 'selecting',
        created_by BIGINT,
        card_chat_id BIGINT,
        card_message_id BIGINT,
        card_is_photo BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS inputs (
        user_id BIGINT PRIMARY KEY,
        kind TEXT NOT NULL,
        payload JSONB DEFAULT '{}'
    )""",
    """CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS activity_log (
        id SERIAL PRIMARY KEY,
        kind TEXT NOT NULL,
        user_id BIGINT,
        username TEXT,
        channel_code TEXT,
        helper_id BIGINT,
        amount NUMERIC,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS channels (
        code TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        chat_id BIGINT UNIQUE NOT NULL,
        link TEXT,
        position SERIAL
    )""",
    "CREATE SEQUENCE IF NOT EXISTS channel_code_seq",
    """CREATE TABLE IF NOT EXISTS categories (
        id SERIAL PRIMARY KEY,
        name TEXT UNIQUE NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS category_channels (
        category_id INT NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
        channel_code TEXT NOT NULL,
        PRIMARY KEY (category_id, channel_code)
    )""",
    """CREATE TABLE IF NOT EXISTS shared_links (
        id SERIAL PRIMARY KEY,
        channel_code TEXT NOT NULL,
        url TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS link_issues (
        user_id BIGINT NOT NULL,
        channel_code TEXT NOT NULL,
        link_id INT NOT NULL REFERENCES shared_links(id) ON DELETE CASCADE,
        issued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, channel_code)
    )""",
    """CREATE TABLE IF NOT EXISTS known_chats (
        chat_id BIGINT PRIMARY KEY,
        title TEXT,
        type TEXT,
        is_admin BOOLEAN DEFAULT FALSE,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
]

OPEN_STATES = ("selecting", "awaiting_amount")

# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def owner_id() -> int:
    return int(os.getenv("OWNER_ID"))


def currency() -> str:
    return os.getenv("CURRENCY", "₹")


def local_tz():
    try:
        return ZoneInfo(os.getenv("TIMEZONE", "Asia/Kolkata"))
    except Exception:  # tzdata missing or bad name
        logger.warning("Timezone not found, falling back to UTC. Install 'tzdata'.")
        return timezone.utc


def esc(value) -> str:
    return html.escape(str(value), quote=False)


def money(amount) -> str:
    a = Decimal(amount or 0)
    s = f"{a:,.2f}"
    if s.endswith(".00"):
        s = s[:-3]
    return f"{currency()}{s}"


def utcnow() -> datetime:
    """Naive UTC datetime (matches the TIMESTAMP columns)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def fmt_time(dt=None) -> str:
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(local_tz()).strftime("%d %b %Y, %I:%M %p")


def person(uid, username, name) -> str:
    bits = []
    if name:
        bits.append(esc(name))
    if username and username != "No_Username":
        bits.append(f"@{esc(username)}")
    head = " ".join(bits) or "Unknown"
    if uid:
        return f"{head} (ID: <code>{uid}</code>)"
    return f"{head} (ID not known yet)"


def short_person(uid, username) -> str:
    if username and username != "No_Username":
        return f"@{esc(username)}"
    return f"<code>{uid}</code>" if uid else "unknown"


def channel_names(codes) -> str:
    return ", ".join(CHANNELS[c]["name"] for c in codes if c in CHANNELS) or "-"


def pool_of(context):
    return context.bot_data["db_pool"]


async def is_helper(pool, uid: int) -> bool:
    if uid == owner_id():
        return True
    async with pool.acquire() as conn:
        return bool(await conn.fetchval("SELECT 1 FROM helpers WHERE user_id=$1", uid))


async def role_of(pool, uid: int) -> str:
    """The three interfaces: 'admin' (OWNER_ID in .env), 'helper' (added by the admin), 'customer'."""
    if uid == owner_id():
        return "admin"
    return "helper" if await is_helper(pool, uid) else "customer"


async def show_home(context, chat_id, user):
    """Send the right interface for this person's role."""
    pool = pool_of(context)
    role = await role_of(pool, user.id)
    if role == "admin":
        await context.bot.send_message(chat_id=chat_id, text=ADMIN_TEXT, parse_mode=HTML,
                                       reply_markup=admin_home_markup())
    elif role == "helper":
        await context.bot.send_message(chat_id=chat_id, text=PANEL_TEXT, parse_mode=HTML,
                                       reply_markup=panel_markup(user.id))
    else:
        await customer_fallback_msg(context, chat_id, user)
    return role


async def support_contact(pool) -> str:
    async with pool.acquire() as conn:
        value = await conn.fetchval("SELECT value FROM settings WHERE key='support_contact'")
    return value or os.getenv("SUPPORT_CONTACT") or "our support team"


async def upsert_user(conn, user) -> bool:
    """Saves the user. Returns True if this is the first time we see them."""
    return bool(await conn.fetchval(
        "INSERT INTO users (user_id, username, first_name) VALUES ($1, $2, $3) "
        "ON CONFLICT (user_id) DO UPDATE SET username=$2, first_name=$3, updated_at=CURRENT_TIMESTAMP "
        "RETURNING (xmax = 0)",
        user.id, user.username, user.first_name,
    ))


async def active_codes(conn, uid: int):
    rows = await conn.fetch(
        "SELECT channel_code FROM entitlements WHERE user_id=$1 AND active ORDER BY channel_code", uid
    )
    return [r["channel_code"] for r in rows if r["channel_code"] in CHANNELS]


async def grant_codes(conn, uid: int, codes, helper_id: int):
    for code in codes:
        await conn.execute(
            "INSERT INTO entitlements (user_id, channel_code, active, granted_by, granted_at, revoked_at, revoked_by) "
            "VALUES ($1, $2, TRUE, $3, CURRENT_TIMESTAMP, NULL, NULL) "
            "ON CONFLICT (user_id, channel_code) DO UPDATE SET active=TRUE, granted_by=$3, "
            "granted_at=CURRENT_TIMESTAMP, revoked_at=NULL, revoked_by=NULL",
            uid, code, helper_id,
        )


async def log_activity(conn, kind, uid, username, codes, helper_id=None, amount=None):
    await conn.execute(
        "INSERT INTO activity_log (kind, user_id, username, channel_code, helper_id, amount) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        kind, uid, username, ",".join(codes) if isinstance(codes, (list, tuple)) else codes, helper_id, amount,
    )


async def claim_pending(conn, user):
    """If a helper granted access by @username before this person opened the bot, attach it now."""
    if not user.username:
        return []
    uname = user.username.lower()
    rows = await conn.fetch(
        "DELETE FROM pending_grants WHERE username=$1 RETURNING channel_code, granted_by", uname
    )
    for r in rows:
        await grant_codes(conn, user.id, [r["channel_code"]], r["granted_by"])
    if rows:
        await conn.execute(
            "UPDATE transactions SET user_id=$1 WHERE user_id=0 AND lower(username)=$2", user.id, uname
        )
    return [r["channel_code"] for r in rows]


async def masterlog(context, text, photo_id=None):
    chat_id = int(os.getenv("MASTERLOG_CHAT_ID"))
    try:
        if photo_id:
            await context.bot.send_photo(chat_id=chat_id, photo=photo_id, caption=text, parse_mode=HTML)
        else:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=HTML)
    except TelegramError as e:
        logger.error(f"Masterlog post failed: {e}")


async def log_event(context, kind, text, uid=None, username=None, codes="", helper_id=None, photo_id=None):
    """Record an event in the activity log AND post it to the master log chat."""
    try:
        async with pool_of(context).acquire() as conn:
            await log_activity(conn, kind, uid, username, codes, helper_id)
    except Exception as e:
        logger.error(f"Activity log insert failed: {e}")
    await masterlog(context, f"{text}\n🕒 {fmt_time()}", photo_id)


async def register_customer(context, user):
    """Save the user, attach any grants made by @username, and log first contact / claims."""
    pool = pool_of(context)
    async with pool.acquire() as conn:
        is_new = await upsert_user(conn, user)
        claimed = await claim_pending(conn, user)
    if await is_helper(pool, user.id):
        return
    if is_new:
        await log_event(context, "new_customer",
                        f"🆕 <b>NEW CUSTOMER</b>\n👤 {person(user.id, user.username, user.first_name)}",
                        user.id, user.username)
    if claimed:
        await log_event(context, "claim",
                        f"🔓 <b>PENDING ACCESS CLAIMED</b>\n👤 {person(user.id, user.username, user.first_name)}\n"
                        f"📂 {esc(channel_names(claimed))}", user.id, user.username, claimed)


async def edit_card(bot, chat_id, message_id, is_photo, text, markup=None):
    try:
        if is_photo:
            await bot.edit_message_caption(
                chat_id=chat_id, message_id=message_id, caption=text, parse_mode=HTML, reply_markup=markup
            )
        else:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text, parse_mode=HTML, reply_markup=markup
            )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            logger.warning(f"Could not edit card: {e}")


# ---------------------------------------------------------------------------
# Forward helpers (work with older and newer python-telegram-bot)
# ---------------------------------------------------------------------------


def forwarded_user(msg):
    fu = getattr(msg, "forward_from", None)
    if fu:
        return fu
    origin = getattr(msg, "forward_origin", None)
    return getattr(origin, "sender_user", None) if origin is not None else None


# ---------------------------------------------------------------------------
# Customer side
# ---------------------------------------------------------------------------


def menu_markup(active) -> InlineKeyboardMarkup:
    rows = []
    for code, ch in CHANNELS.items():
        if code in active:
            rows.append([InlineKeyboardButton(f"✅ {ch['name']}", callback_data=f"open:{code}")])
        else:
            rows.append([InlineKeyboardButton(f"🔒 {ch['name']}", callback_data=f"locked:{code}")])
    return InlineKeyboardMarkup(rows)


async def send_menu(bot, pool, chat_id, uid, intro):
    async with pool.acquire() as conn:
        active = await active_codes(conn, uid)
    await bot.send_message(chat_id=chat_id, text=intro, parse_mode=HTML, reply_markup=menu_markup(active))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user = update.effective_user
    pool = pool_of(context)
    await register_customer(context, user)

    if await role_of(pool, user.id) != "customer":
        await show_home(context, update.effective_chat.id, user)
        return

    await send_menu(
        context.bot, pool, update.effective_chat.id, user.id,
        f"👋 Welcome, <b>{esc(user.first_name)}</b>!\n\n"
        "✅ = unlocked, tap it to join.\n"
        "🔒 = not purchased yet.\n\n"
        "To unlock a channel, send your <b>payment screenshot</b> here.",
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user = update.effective_user
    pool = pool_of(context)
    await register_customer(context, user)
    await send_menu(
        context.bot, pool, update.effective_chat.id, user.id,
        "📂 <b>Your channels</b>\n✅ unlocked · 🔒 locked\nSend a payment screenshot to unlock more.",
    )


def link_capacity() -> int:
    """How many customers share one invite link before the bot creates a new one."""
    try:
        return max(1, int(os.getenv("LINK_CAPACITY", "100")))
    except ValueError:
        return 100


async def get_join_url(context, code, uid):
    """Join link for a customer.

    1. The admin's own link for the channel, if one was set.
    2. Otherwise a bot-made join-request link that is SHARED by up to LINK_CAPACITY (default 100) customers;
       when it is full the bot creates the next one. A customer who asks again gets the same link.
    The bot approves only customers who paid, whichever link they use.
    """
    chan = CHANNELS[code]
    if chan.get("link"):
        return chan["link"]
    pool = pool_of(context)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", code)
            row = await conn.fetchrow(
                "SELECT l.url FROM link_issues i JOIN shared_links l ON l.id = i.link_id "
                "WHERE i.user_id=$1 AND i.channel_code=$2", uid, code)
            if row:
                return row["url"]
            cur = await conn.fetchrow(
                "SELECT l.id, l.url, (SELECT COUNT(*) FROM link_issues i WHERE i.link_id = l.id) AS n "
                "FROM shared_links l WHERE l.channel_code=$1 ORDER BY l.id DESC LIMIT 1", code)
            if cur is None or cur["n"] >= link_capacity():
                try:
                    made = await context.bot.create_chat_invite_link(
                        chat_id=chan["chat_id"], name=chan["name"][:32], creates_join_request=True)
                except TelegramError as e:
                    logger.error(f"Invite link failed for {code}: {e}")
                    return None
                link_id = await conn.fetchval(
                    "INSERT INTO shared_links (channel_code, url) VALUES ($1,$2) RETURNING id",
                    code, made.invite_link)
                url = made.invite_link
            else:
                link_id, url = cur["id"], cur["url"]
            await conn.execute(
                "INSERT INTO link_issues (user_id, channel_code, link_id) VALUES ($1,$2,$3) "
                "ON CONFLICT DO NOTHING", uid, code, link_id)
            return url


def link_block(name, url) -> str:
    """Bold channel name as the header, the link right below it."""
    return f"<b>{esc(name)}</b>\n{esc(url)}"


async def customer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    action, _, code = q.data.partition(":")
    if code not in CHANNELS:
        await q.answer()
        return
    pool = pool_of(context)
    uid = q.from_user.id
    async with pool.acquire() as conn:
        ok = await conn.fetchval(
            "SELECT 1 FROM entitlements WHERE user_id=$1 AND channel_code=$2 AND active", uid, code
        )
    if action == "locked" or not ok:
        await q.answer("🔒 Not purchased yet. Send your payment screenshot here to unlock it.", show_alert=True)
        return

    chan = CHANNELS[code]
    link_url = await get_join_url(context, code, uid)
    if not link_url:
        await q.answer("⚠️ Couldn't create the link. Please contact support.", show_alert=True)
        return

    await q.answer()
    await log_event(context, "link",
                    f"🔗 <b>INVITE LINK ISSUED</b>\n👤 {person(uid, q.from_user.username, q.from_user.first_name)}\n"
                    f"📂 {esc(chan['name'])}", uid, q.from_user.username, code)
    await context.bot.send_message(
        chat_id=q.message.chat_id,
        text=f"{link_block(chan['name'], link_url)}\n\nTap the link (or the button) and request to join. "
             "You will be approved automatically.",
        parse_mode=HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"🚪 Join {chan['name']}", url=link_url)]]),
    )


async def join_request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Approve a join request only if that exact user paid for that channel."""
    req = update.chat_join_request
    code = next((c for c, ch in CHANNELS.items() if ch["chat_id"] == req.chat.id), None)
    if code is None:
        return
    user = req.from_user
    pool = pool_of(context)
    async with pool.acquire() as conn:
        allowed = await conn.fetchval(
            "SELECT 1 FROM entitlements WHERE user_id=$1 AND channel_code=$2 AND active", user.id, code
        )
    try:
        if allowed:
            await context.bot.approve_chat_join_request(chat_id=req.chat.id, user_id=user.id)
        else:
            await context.bot.decline_chat_join_request(chat_id=req.chat.id, user_id=user.id)
    except TelegramError as e:
        logger.error(f"Join request handling failed: {e}")
        return

    async with pool.acquire() as conn:
        await log_activity(conn, "join" if allowed else "join_declined", user.id, user.username, code)
    if allowed:
        chan_name = CHANNELS[code]["name"]
        await masterlog(
            context,
            f"🚪 <b>USER JOINED: {esc(chan_name)}</b>\n👤 {person(user.id, user.username, user.first_name)}\n"
            f"📂 Channel: {esc(chan_name)}\n🕒 {fmt_time()}",
        )
        try:  # tell the customer they are in (a join request allows a DM for a few minutes)
            await context.bot.send_message(
                chat_id=req.user_chat_id, parse_mode=HTML,
                text=f"✅ You have joined <b>{esc(chan_name)}</b>. Welcome!")
        except TelegramError as e:
            logger.info(f"Could not DM {user.id} after join: {e}")
    else:
        await masterlog(
            context,
            f"⛔ <b>JOIN DECLINED (no access)</b>\n👤 {person(user.id, user.username, user.first_name)}\n"
            f"📂 {esc(CHANNELS[code]['name'])}\n🕒 {fmt_time()}",
        )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A photo in private chat: a customer's payment screenshot (or a helper forwarding one)."""
    msg = update.message
    user = update.effective_user
    pool = pool_of(context)

    if await is_helper(pool, user.id):
        if getattr(msg, "forward_from", None) or getattr(msg, "forward_sender_name", None) \
                or getattr(msg, "forward_origin", None):
            await handle_forward(update, context)
        else:
            await msg.reply_text("To grant access, forward the customer's message to me, or use the panel buttons.",
                                 reply_markup=panel_markup(user.id))
        return

    photo_id = msg.photo[-1].file_id
    await register_customer(context, user)
    async with pool.acquire() as conn:
        dup = await conn.fetchval(
            "SELECT id FROM drafts WHERE source='screenshot' AND target_user_id=$1 "
            "AND state IN ('selecting','awaiting_amount') AND created_at > $2",
            user.id, utcnow() - timedelta(hours=24),
        )
        if dup:
            await msg.reply_text("⏳ Your previous screenshot is still being verified. Please wait for our team.")
            return
        draft_id = await conn.fetchval(
            "INSERT INTO drafts (source, target_user_id, target_username, target_name, photo_id, created_by) "
            "VALUES ('screenshot', $1, $2, $3, $4, $1) RETURNING id",
            user.id, user.username, user.first_name, photo_id,
        )

    sent = await post_card(context.bot, pool, int(os.getenv("HELPER_CHAT_ID")), draft_id)
    await log_event(context, "screenshot",
                    f"📸 <b>SCREENSHOT RECEIVED</b>\n👤 {person(user.id, user.username, user.first_name)}\n"
                    f"{'Sent to the helper chat for verification.' if sent else '⚠️ Could not reach the helper chat.'}",
                    user.id, user.username, photo_id=photo_id)
    if sent:
        await msg.reply_text("✅ Screenshot received! Please wait while our team verifies your payment.")
    else:
        contact = await support_contact(pool)
        await msg.reply_text(f"⚠️ I couldn't reach our verification team right now. Please contact {esc(contact)}.",
                             parse_mode=HTML)


async def customer_fallback_msg(context, chat_id, user):
    pool = pool_of(context)
    contact = await support_contact(pool)
    await send_menu(
        context.bot, pool, chat_id, user.id,
        "ℹ️ Please send your <b>payment screenshot</b> to unlock channels.\n"
        f"If something is wrong, contact {esc(contact)}.",
    )


async def customer_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Any other private message from a customer."""
    pool = pool_of(context)
    user = update.effective_user
    contact = await support_contact(pool)
    await register_customer(context, user)
    await send_menu(
        context.bot, pool, update.effective_chat.id, user.id,
        "ℹ️ Please send your <b>payment screenshot</b> to unlock channels.\n"
        f"If something is wrong, contact {esc(contact)}.",
    )


# ---------------------------------------------------------------------------
# Helper side: verification cards
# ---------------------------------------------------------------------------

SOURCE_LABEL = {
    "screenshot": "Customer sent a screenshot",
    "forward": "Helper forwarded the customer's message",
    "username": "Helper entered the username",
}


def card_text(d, existing, selected) -> str:
    lines = [
        "💰 <b>Payment Verification</b>",
        f"👤 {person(d['target_user_id'], d['target_username'], d['target_name'])}",
        f"📥 {SOURCE_LABEL.get(d['source'], d['source'])}",
    ]
    if existing:
        lines.append(f"🔓 Already has: {esc(channel_names(existing))}")
    if d["target_user_id"] is None:
        lines.append("⏳ Access unlocks when this user first opens the bot.")
    lines.append(f"📂 Selected: {esc(channel_names(selected)) if selected else 'none yet'}")
    lines.append("\n👇 Is it genuine? Pick the channels to unlock, then Approve - or Reject.")
    return "\n".join(lines)


def closed_text(d, selected, status) -> str:
    lines = [
        "💰 <b>Payment Verification</b>",
        f"👤 {person(d['target_user_id'], d['target_username'], d['target_name'])}",
    ]
    if selected:
        lines.append(f"📂 Channels: {esc(channel_names(selected))}")
    lines.append(status)
    return "\n".join(lines)


def draft_keyboard(draft_id, selected, show_revoke, cats=()) -> InlineKeyboardMarkup:
    rows = []
    for code, ch in CHANNELS.items():
        mark = "✅" if code in selected else "📂"
        rows.append([InlineKeyboardButton(f"{mark} {ch['name']}", callback_data=f"t:{draft_id}:{code}")])
    for c in cats:
        codes = [x for x in c["codes"] if x in CHANNELS]
        if not codes:
            continue
        on = all(x in selected for x in codes)
        rows.append([InlineKeyboardButton(f"{'✅' if on else '📦'} Category: {c['name']}",
                                          callback_data=f"tc:{draft_id}:{c['id']}")])
    rows.append([InlineKeyboardButton("🌐 Approve ALL channels", callback_data=f"all:{draft_id}")])
    rows.append([
        InlineKeyboardButton("✅ Approve", callback_data=f"ok:{draft_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"no:{draft_id}"),
    ])
    if show_revoke:
        rows.append([InlineKeyboardButton("🗑 Revoke existing access", callback_data=f"rvm:{draft_id}")])
    return InlineKeyboardMarkup(rows)


async def post_card(bot, pool, chat_id, draft_id) -> bool:
    async with pool.acquire() as conn:
        d = await conn.fetchrow("SELECT * FROM drafts WHERE id=$1", draft_id)
        existing = await active_codes(conn, d["target_user_id"]) if d["target_user_id"] else []
    text = card_text(d, existing, [])
    markup = draft_keyboard(draft_id, [], bool(existing), await load_cats(pool))
    try:
        if d["photo_id"]:
            m = await bot.send_photo(chat_id=chat_id, photo=d["photo_id"], caption=text,
                                     parse_mode=HTML, reply_markup=markup)
        else:
            m = await bot.send_message(chat_id=chat_id, text=text, parse_mode=HTML, reply_markup=markup)
    except TelegramError as e:
        logger.error(f"Could not post card: {e}")
        return False
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE drafts SET card_chat_id=$1, card_message_id=$2, card_is_photo=$3 WHERE id=$4",
            chat_id, m.message_id, bool(d["photo_id"]), draft_id,
        )
    return True


async def handle_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user
    pool = pool_of(context)

    # Owner adding a helper by forwarding one of their messages
    async with pool.acquire() as conn:
        inp = await conn.fetchrow("SELECT kind FROM inputs WHERE user_id=$1", user.id)
    if inp and inp["kind"] == "add_helper" and user.id == owner_id():
        fu = forwarded_user(msg)
        if fu is None:
            await msg.reply_text("🔒 That person hides their account on forwards. Send their numeric ID instead.")
            return
        await add_helper(context, user, fu.id, fu.full_name)
        return

    if not await is_helper(pool, user.id):
        await customer_fallback(update, context)
        return

    fu = forwarded_user(msg)
    if fu is None:
        await msg.reply_text(
            "🔒 This person hides their account on forwards, so I can't read their ID.\n\n"
            "Ask the customer to open the bot and send the screenshot there - I'll ask you to verify it. "
            "Or tap ➕ Grant access in the panel and give their @username."
        )
        return
    if fu.is_bot:
        await msg.reply_text("That message is from a bot, not a customer.")
        return

    photo_id = msg.photo[-1].file_id if msg.photo else None
    async with pool.acquire() as conn:
        await upsert_user(conn, fu)
        draft_id = await conn.fetchval(
            "INSERT INTO drafts (source, target_user_id, target_username, target_name, photo_id, created_by) "
            "VALUES ('forward', $1, $2, $3, $4, $5) RETURNING id",
            fu.id, fu.username, fu.first_name, photo_id, user.id,
        )
    await post_card(context.bot, pool, update.effective_chat.id, draft_id)
    await log_event(context, "card_opened",
                    f"📝 <b>GRANT CARD OPENED</b> (forwarded message)\n👤 {person(fu.id, fu.username, fu.first_name)}\n"
                    f"🧑‍💼 Helper: {esc(user.full_name)}", fu.id, fu.username, "", user.id)


async def resolve_target(pool, arg: str):
    arg = arg.strip()
    async with pool.acquire() as conn:
        if arg.lstrip("-").isdigit():
            uid = int(arg)
            row = await conn.fetchrow("SELECT username, first_name FROM users WHERE user_id=$1", uid)
            return {"id": uid, "username": row["username"] if row else None,
                    "name": row["first_name"] if row else None}
        uname = arg.lstrip("@").lower()
        if not re.fullmatch(r"[a-z0-9_]{5,32}", uname):
            return None
        row = await conn.fetchrow("SELECT user_id, username, first_name FROM users WHERE lower(username)=$1", uname)
        if row:
            return {"id": row["user_id"], "username": row["username"], "name": row["first_name"]}
        return {"id": None, "username": uname, "name": None}


async def do_grant(context, user, chat_id, arg):
    """Opens a grant card for a customer given as @username or numeric ID. Returns an error text or None."""
    pool = pool_of(context)
    t = await resolve_target(pool, arg)
    if not t:
        return "⚠️ That doesn't look like a valid @username or numeric ID. Try again."
    async with pool.acquire() as conn:
        draft_id = await conn.fetchval(
            "INSERT INTO drafts (source, target_user_id, target_username, target_name, created_by) "
            "VALUES ('username', $1, $2, $3, $4) RETURNING id",
            t["id"], t["username"], t["name"], user.id,
        )
    await post_card(context.bot, pool, chat_id, draft_id)
    await log_event(context, "card_opened",
                    f"📝 <b>GRANT CARD OPENED</b> (username)\n👤 {person(t['id'], t['username'], t['name'])}\n"
                    f"🧑‍💼 Helper: {esc(user.full_name)}", t["id"], t["username"], "", user.id)
    return None


async def grant_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shortcut for the ➕ Grant access button."""
    pool = pool_of(context)
    if not await is_helper(pool, update.effective_user.id):
        await update.message.reply_text("⛔ Helpers only.")
        return
    if not context.args:
        await update.message.reply_text("Tap ➕ Grant access in the panel (send /start), or: /grant @username")
        return
    err = await do_grant(context, update.effective_user, update.effective_chat.id, context.args[0])
    if err:
        await update.message.reply_text(err)


async def draft_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    pool = pool_of(context)
    user = q.from_user
    if not await is_helper(pool, user.id):
        await q.answer("⛔ Only helpers can use this.", show_alert=True)
        return

    action, _, rest = q.data.partition(":")
    parts = rest.split(":")
    draft_id = int(parts[0])
    is_photo = bool(q.message.photo)

    # ---- toggle a channel -------------------------------------------------
    if action == "t":
        code = parts[1]
        if code not in CHANNELS:
            await q.answer()
            return
        async with pool.acquire() as conn:
            async with conn.transaction():
                d = await conn.fetchrow("SELECT * FROM drafts WHERE id=$1 FOR UPDATE", draft_id)
                if not d or d["state"] not in OPEN_STATES:
                    await q.answer("This request is already closed.", show_alert=True)
                    return
                sel = json.loads(d["selected"])
                if code in sel:
                    sel.remove(code)
                else:
                    sel.append(code)
                await conn.execute("UPDATE drafts SET selected=$1, state='selecting' WHERE id=$2",
                                   json.dumps(sel), draft_id)
                await conn.execute(
                    "DELETE FROM inputs WHERE kind='amount' AND (payload->>'draft')::int=$1", draft_id)
            existing = await active_codes(conn, d["target_user_id"]) if d["target_user_id"] else []
        await edit_card(context.bot, q.message.chat_id, q.message.message_id, is_photo,
                        card_text(d, existing, sel), draft_keyboard(draft_id, sel, bool(existing), await load_cats(pool)))
        await q.answer()
        return

    # ---- toggle a whole category ---------------------------------------------
    if action == "tc":
        cat_id = int(parts[1])
        async with pool.acquire() as conn:
            codes = [r["channel_code"] for r in await conn.fetch(
                "SELECT channel_code FROM category_channels WHERE category_id=$1", cat_id)]
            codes = [c for c in codes if c in CHANNELS]
            if not codes:
                await q.answer("This category has no channels yet.", show_alert=True)
                return
            async with conn.transaction():
                d = await conn.fetchrow("SELECT * FROM drafts WHERE id=$1 FOR UPDATE", draft_id)
                if not d or d["state"] not in OPEN_STATES:
                    await q.answer("This request is already closed.", show_alert=True)
                    return
                sel = json.loads(d["selected"])
                if all(c in sel for c in codes):
                    sel = [c for c in sel if c not in codes]
                else:
                    sel += [c for c in codes if c not in sel]
                await conn.execute("UPDATE drafts SET selected=$1, state='selecting' WHERE id=$2",
                                   json.dumps(sel), draft_id)
                await conn.execute(
                    "DELETE FROM inputs WHERE kind='amount' AND (payload->>'draft')::int=$1", draft_id)
            existing = await active_codes(conn, d["target_user_id"]) if d["target_user_id"] else []
        await edit_card(context.bot, q.message.chat_id, q.message.message_id, is_photo,
                        card_text(d, existing, sel), draft_keyboard(draft_id, sel, bool(existing), await load_cats(pool)))
        await q.answer()
        return

    # ---- approve: ask for the amount ------------------------------------
    if action in ("ok", "all"):
        async with pool.acquire() as conn:
            d = await conn.fetchrow("SELECT * FROM drafts WHERE id=$1", draft_id)
            if not d or d["state"] not in OPEN_STATES:
                await q.answer("This request is already closed.", show_alert=True)
                return
            if action == "all":
                sel = list(CHANNELS)
                if not sel:
                    await q.answer("⚠️ No channels are set up yet. The admin must add them first.", show_alert=True)
                    return
                await conn.execute("UPDATE drafts SET selected=$1 WHERE id=$2", json.dumps(sel), draft_id)
            else:
                sel = json.loads(d["selected"])
                if not sel:
                    await q.answer("⚠️ Select at least one channel before approving.", show_alert=True)
                    return
            existing_codes = await active_codes(conn, d["target_user_id"]) if d["target_user_id"] else []
            await conn.execute("UPDATE drafts SET state='awaiting_amount' WHERE id=$1", draft_id)
            await conn.execute(
                "INSERT INTO inputs (user_id, kind, payload) VALUES ($1, 'amount', $2) "
                "ON CONFLICT (user_id) DO UPDATE SET kind='amount', payload=$2",
                user.id, json.dumps({"draft": draft_id}),
            )
        mention = f'<a href="tg://user?id={user.id}">{esc(user.first_name)}</a>'
        await context.bot.send_message(
            chat_id=q.message.chat_id,
            text=f"💵 {mention}, reply to this message with the <b>amount received</b> "
                 f"from {esc(d['target_name'] or d['target_username'] or d['target_user_id'])} (numbers only).",
            parse_mode=HTML,
            reply_markup=ForceReply(selective=True, input_field_placeholder="e.g. 499"),
        )
        if action == "all":
            await edit_card(context.bot, q.message.chat_id, q.message.message_id, is_photo,
                            card_text(d, existing_codes, sel),
                            draft_keyboard(draft_id, sel, bool(existing_codes), await load_cats(pool)))
        await q.answer("Now send the amount.")
        return

    # ---- reject --------------------------------------------------------------
    if action == "no":
        async with pool.acquire() as conn:
            d = await conn.fetchrow(
                "UPDATE drafts SET state='rejected' WHERE id=$1 AND state IN ('selecting','awaiting_amount') "
                "RETURNING *", draft_id)
            if not d:
                await q.answer("This request is already closed.", show_alert=True)
                return
            await conn.execute("DELETE FROM inputs WHERE kind='amount' AND (payload->>'draft')::int=$1", draft_id)
            await log_activity(conn, "reject", d["target_user_id"], d["target_username"], "", user.id)
        contact = await support_contact(pool)
        if d["target_user_id"]:
            try:
                await context.bot.send_message(
                    chat_id=d["target_user_id"], parse_mode=HTML,
                    text="❌ We couldn't verify your payment.\n"
                         f"If you think this is a mistake, please contact {esc(contact)}.",
                )
            except TelegramError:
                pass
        await edit_card(context.bot, q.message.chat_id, q.message.message_id, is_photo,
                        closed_text(d, [], f"❌ <b>REJECTED</b> by {esc(user.full_name)}"), None)
        await masterlog(
            context,
            f"❌ <b>PAYMENT REJECTED</b>\n👤 {person(d['target_user_id'], d['target_username'], d['target_name'])}\n"
            f"🧑‍💼 Helper: {esc(user.full_name)}\n🕒 {fmt_time()}",
            d["photo_id"],
        )
        await q.answer("Rejected.")
        return

    # ---- revoke menu from a card -------------------------------------------
    if action == "rvm":
        async with pool.acquire() as conn:
            d = await conn.fetchrow("SELECT * FROM drafts WHERE id=$1", draft_id)
        if not d:
            await q.answer()
            return
        rows = await revoke_rows(pool, d["target_user_id"], d["target_username"])
        await q.answer()
        if not rows:
            await context.bot.send_message(chat_id=q.message.chat_id, text="No active access to revoke.")
            return
        await context.bot.send_message(
            chat_id=q.message.chat_id, parse_mode=HTML,
            text=f"🗑 <b>Revoke access</b> for {person(d['target_user_id'], d['target_username'], d['target_name'])}",
            reply_markup=InlineKeyboardMarkup(rows),
        )


async def finalize_draft(context, draft_id: int, amount: Decimal, helper):
    """Called once the helper has typed the amount. Returns a result dict or None if already handled."""
    pool = pool_of(context)
    async with pool.acquire() as conn:
        async with conn.transaction():
            d = await conn.fetchrow(
                "UPDATE drafts SET state='done' WHERE id=$1 AND state='awaiting_amount' RETURNING *", draft_id)
            if not d:
                return None
            selected = [c for c in json.loads(d["selected"]) if c in CHANNELS]
            names = [CHANNELS[c]["name"] for c in selected]
            uid = d["target_user_id"]
            uname = d["target_username"]
            if uid:
                await grant_codes(conn, uid, selected, helper.id)
            else:
                for c in selected:
                    await conn.execute(
                        "INSERT INTO pending_grants (username, channel_code, granted_by) VALUES ($1, $2, $3) "
                        "ON CONFLICT DO NOTHING", (uname or "").lower(), c, helper.id)
            await conn.execute(
                "INSERT INTO transactions (user_id, username, channels, amount, helper_id) VALUES ($1,$2,$3,$4,$5)",
                uid or 0, uname or "No_Username", names, amount, helper.id,
            )
            await log_activity(conn, "grant", uid, uname, selected, helper.id, amount)
            await conn.execute("DELETE FROM inputs WHERE user_id=$1", helper.id)

    notified = False
    if uid:
        try:
            blocks = []
            for c in selected:
                url = await get_join_url(context, c, uid)
                if url:
                    blocks.append(link_block(CHANNELS[c]["name"], url))
            text = "✅ <b>Payment approved!</b>\n\n"
            if blocks:
                text += "\n\n".join(blocks) + "\n\nRequest to join with the links above - you will be approved automatically."
                text += "\nYour channels are also in the buttons below."
            else:
                text += "Tap an unlocked channel below to join."
            await send_menu(context.bot, pool, uid, uid, text)
            notified = True
        except TelegramError as e:
            logger.info(f"Could not notify customer {uid}: {e}")

    if d["card_chat_id"] and d["card_message_id"]:
        await edit_card(
            context.bot, d["card_chat_id"], d["card_message_id"], d["card_is_photo"],
            closed_text(d, selected, f"✅ <b>APPROVED</b> by {esc(helper.full_name)} - {esc(money(amount))}"), None)

    await masterlog(
        context,
        "🚨 <b>ACCESS GRANTED</b>\n"
        f"👤 {person(uid, uname, d['target_name'])}\n"
        f"📂 Channels: {esc(', '.join(names))}\n"
        f"💵 Amount: {esc(money(amount))}\n"
        f"🧑‍💼 Helper: {esc(helper.full_name)}\n"
        f"🕒 {fmt_time()}",
        d["photo_id"],
    )
    return {"uid": uid, "username": uname, "notified": notified}


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------


async def revoke_rows(pool, uid, username):
    rows = []
    async with pool.acquire() as conn:
        codes = await active_codes(conn, uid) if uid else []
        pend = []
        if username and username != "No_Username":
            pend = await conn.fetch("SELECT channel_code FROM pending_grants WHERE username=$1", username.lower())
    for c in codes:
        rows.append([InlineKeyboardButton(f"🗑 Revoke {CHANNELS[c]['name']}", callback_data=f"rv:{uid}:{c}")])
    for r in pend:
        c = r["channel_code"]
        if c in CHANNELS:
            rows.append([InlineKeyboardButton(f"🗑 Cancel pending {CHANNELS[c]['name']}",
                                              callback_data=f"rvp:{username.lower()}:{c}")])
    return rows


async def do_revoke_menu(context, chat_id, arg):
    """Shows revoke buttons for a customer given as @username or ID. Returns an error text or None."""
    pool = pool_of(context)
    t = await resolve_target(pool, arg)
    if not t:
        return "⚠️ That doesn't look like a valid @username or numeric ID. Try again."
    rows = await revoke_rows(pool, t["id"], t["username"])
    if not rows:
        return "No active access found for that customer."
    await context.bot.send_message(
        chat_id=chat_id, parse_mode=HTML,
        text=f"🗑 <b>Revoke access</b> for {person(t['id'], t['username'], t['name'])}",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return None


async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shortcut for the 🗑 Revoke access button."""
    pool = pool_of(context)
    if not await is_helper(pool, update.effective_user.id):
        await update.message.reply_text("⛔ Helpers only.")
        return
    if not context.args:
        await update.message.reply_text("Tap 🗑 Revoke access in the panel (send /start), or: /revoke @username")
        return
    err = await do_revoke_menu(context, update.effective_chat.id, context.args[0])
    if err:
        await update.message.reply_text(err)


async def revoke_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    pool = pool_of(context)
    user = q.from_user
    if not await is_helper(pool, user.id):
        await q.answer("⛔ Only helpers can use this.", show_alert=True)
        return
    kind, a, code = q.data.split(":")
    if code not in CHANNELS:
        await q.answer()
        return
    chan = CHANNELS[code]
    uid = None
    username = None
    name = None

    if kind == "rv":
        uid = int(a)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE entitlements SET active=FALSE, revoked_at=CURRENT_TIMESTAMP, revoked_by=$3 "
                "WHERE user_id=$1 AND channel_code=$2 AND active RETURNING user_id", uid, code, user.id)
            urow = await conn.fetchrow("SELECT username, first_name FROM users WHERE user_id=$1", uid)
            username = urow["username"] if urow else None
            name = urow["first_name"] if urow else None
            if not row:
                await q.answer("Already revoked.", show_alert=True)
            else:
                await log_activity(conn, "revoke", uid, username, code, user.id)
        if row:
            try:  # remove from the channel without a permanent ban
                await context.bot.ban_chat_member(chan["chat_id"], uid)
                await context.bot.unban_chat_member(chan["chat_id"], uid, only_if_banned=True)
            except TelegramError as e:
                logger.warning(f"Could not remove {uid} from {code}: {e}")
            contact = await support_contact(pool)
            try:
                await context.bot.send_message(
                    chat_id=uid, parse_mode=HTML,
                    text=f"⚠️ Your access to <b>{esc(chan['name'])}</b> has been removed.\n"
                         f"Contact {esc(contact)} if this is a mistake.")
            except TelegramError:
                pass
            await masterlog(
                context,
                f"🔴 <b>ACCESS REVOKED</b>\n👤 {person(uid, username, name)}\n📂 {esc(chan['name'])}\n"
                f"🧑‍💼 Helper: {esc(user.full_name)}\n🕒 {fmt_time()}")
            await q.answer("Revoked.")
    else:  # "rvp" - cancel a pending username grant
        username = a
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM pending_grants WHERE username=$1 AND channel_code=$2", username, code)
            await log_activity(conn, "revoke", None, username, code, user.id)
        await q.answer("Cancelled.")

    rows = await revoke_rows(pool, uid, username)
    try:
        if rows:
            await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(rows))
        else:
            await q.edit_message_text("✅ No more active access for this customer.")
    except BadRequest:
        pass


# ---------------------------------------------------------------------------
# Text router: amount prompts, admin inputs, customer fallback
# ---------------------------------------------------------------------------


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user
    if msg is None or user is None:  # e.g. channel posts have no user
        return
    pool = pool_of(context)
    async with pool.acquire() as conn:
        inp = await conn.fetchrow("SELECT kind, payload FROM inputs WHERE user_id=$1", user.id)

    if inp:
        payload = json.loads(inp["payload"]) if inp["payload"] else {}
        if inp["kind"] == "amount":
            await handle_amount(update, context, payload)
        elif inp["kind"] == "add_helper" and user.id == owner_id():
            await handle_add_helper_text(update, context)
        elif inp["kind"] == "support" and user.id == owner_id():
            await handle_support_text(update, context)
        elif inp["kind"] == "chan_id" and user.id == owner_id():
            await handle_chan_id_text(update, context)
        elif inp["kind"] == "chan_link" and user.id == owner_id():
            await handle_chan_link_text(update, context, payload)
        elif inp["kind"] == "chan_name" and user.id == owner_id():
            await handle_chan_name_text(update, context, payload)
        elif inp["kind"] == "cat_name" and user.id == owner_id():
            await handle_cat_name_text(update, context)
        elif inp["kind"] in ("grant_user", "revoke_user"):
            await handle_target_text(update, context, inp["kind"])
        return

    if update.effective_chat.type != "private":
        return
    if await role_of(pool, user.id) != "customer":
        await show_home(context, update.effective_chat.id, user)
        return
    await customer_fallback(update, context)


async def handle_amount(update, context, payload):
    msg = update.message
    user = update.effective_user
    raw = (msg.text or "").replace(",", "").replace(currency(), "").strip()
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        amount = None
    if amount is None or not amount.is_finite() or amount < 0 or amount > Decimal("99999999"):
        await msg.reply_text("⚠️ Please send just the amount as a number, e.g. 499 (or /cancel).")
        return
    amount = amount.quantize(Decimal("0.01"))

    result = await finalize_draft(context, int(payload["draft"]), amount, user)
    if result is None:
        async with pool_of(context).acquire() as conn:
            await conn.execute("DELETE FROM inputs WHERE user_id=$1", user.id)
        await msg.reply_text("This request was already handled or changed. Use the card buttons again.")
        return

    if result["uid"] is None:
        note = f"⏳ Saved. Access unlocks as soon as @{esc(result['username'])} opens the bot."
    elif result["notified"]:
        note = "✅ Done. The customer has been notified and their channels are unlocked."
    else:
        note = ("✅ Done, channels are unlocked. ⚠️ I couldn't message the customer - "
                "ask them to open the bot and tap /start.")
    await msg.reply_text(f"{note}\n💵 Recorded {esc(money(amount))}.", parse_mode=HTML)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with pool_of(context).acquire() as conn:
        await conn.execute("DELETE FROM inputs WHERE user_id=$1", update.effective_user.id)
    await update.message.reply_text("Cancelled.")


# ---------------------------------------------------------------------------
# Helper commands and stats
# ---------------------------------------------------------------------------


PANEL_TEXT = (
    "🧑‍💼 <b>Helper panel</b>\n\n"
    "• Forward a customer's message or screenshot here → I open a grant card\n"
    "• Or use the buttons below"
)


def panel_markup(uid) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("➕ Grant access", callback_data="hp:grant"),
         InlineKeyboardButton("🗑 Revoke access", callback_data="hp:revoke")],
        [InlineKeyboardButton("📈 My stats", callback_data="hp:stats"),
         InlineKeyboardButton("👀 Customer view", callback_data="hp:menu")],
    ]
    return InlineKeyboardMarkup(rows)


async def panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    pool = pool_of(context)
    user = q.from_user
    chat = q.message.chat
    if not await is_helper(pool, user.id):
        await q.answer("⛔ Only helpers can use this.", show_alert=True)
        return
    action = q.data.split(":")[1]

    if action in ("admin", "report") and user.id != owner_id():
        await q.answer("⛔ Owner only.", show_alert=True)
        return

    if action in ("grant", "revoke"):
        kind = "grant_user" if action == "grant" else "revoke_user"
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO inputs (user_id, kind, payload) VALUES ($1, $2, '{}') "
                "ON CONFLICT (user_id) DO UPDATE SET kind=$2, payload='{}'", user.id, kind)
        what = "grant access to" if action == "grant" else "revoke access from"
        mention = f'<a href="tg://user?id={user.id}">{esc(user.first_name)}</a>'
        text = f"{mention}, send the <b>@username or numeric ID</b> of the customer to {what}."
        if chat.type == "private":
            markup = InlineKeyboardMarkup([[InlineKeyboardButton("✖ Cancel", callback_data="hp:cancel")]])
            text += "\nYou can also forward one of their messages."
        else:
            markup = ForceReply(selective=True, input_field_placeholder="@username or ID")
        await context.bot.send_message(chat_id=chat.id, text=text, parse_mode=HTML, reply_markup=markup)
        await q.answer()
    elif action == "cancel":
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM inputs WHERE user_id=$1", user.id)
        try:
            await q.message.delete()
        except TelegramError:
            pass
        await q.answer("Cancelled.")
    elif action == "stats":
        await context.bot.send_message(chat_id=chat.id, text=await my_stats_text(pool, user.id), parse_mode=HTML)
        await q.answer()
    elif action == "menu":
        await send_menu(context.bot, pool, chat.id, user.id,
                        "📂 <b>Customer view</b>\n✅ unlocked · 🔒 locked")
        await q.answer()
    elif action == "admin":
        await context.bot.send_message(chat_id=chat.id, text="🛠 <b>Admin Panel</b>", parse_mode=HTML,
                                       reply_markup=admin_home_markup())
        await q.answer()
    elif action == "report":
        await context.bot.send_message(chat_id=chat.id, text=await build_report(pool), parse_mode=HTML)
        await q.answer()
    else:
        await q.answer()


async def handle_target_text(update, context, kind):
    """The helper typed the @username / ID after tapping Grant or Revoke."""
    user = update.effective_user
    pool = pool_of(context)
    if not await is_helper(pool, user.id):
        return
    words = (update.message.text or "").split()
    if not words:
        return
    if kind == "grant_user":
        err = await do_grant(context, user, update.effective_chat.id, words[0])
    else:
        err = await do_revoke_menu(context, update.effective_chat.id, words[0])
    if err:
        await update.message.reply_text(err)
        return
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inputs WHERE user_id=$1", user.id)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pool = pool_of(context)
    if await role_of(pool, update.effective_user.id) != "customer":
        await show_home(context, update.effective_chat.id, update.effective_user)
    else:
        await customer_fallback(update, context)


RANGE_LABEL = {"today": "Today", "7d": "Last 7 days", "30d": "Last 30 days", "all": "All time"}


def range_start(rng: str) -> datetime:
    now = datetime.now(local_tz())
    if rng == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif rng == "7d":
        start = now - timedelta(days=7)
    elif rng == "30d":
        start = now - timedelta(days=30)
    else:
        return datetime(1970, 1, 1)
    return start.astimezone(timezone.utc).replace(tzinfo=None)


async def name_map(conn):
    m = {}
    for r in await conn.fetch("SELECT user_id, first_name, username FROM users"):
        m[r["user_id"]] = r["first_name"] or (f"@{r['username']}" if r["username"] else None)
    for r in await conn.fetch("SELECT user_id, name FROM helpers"):
        if r["name"]:
            m[r["user_id"]] = r["name"]
    return m


async def stats_text(pool, rng: str) -> str:
    since = range_start(rng)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT helper_id, COUNT(*) AS sales, COALESCE(SUM(amount),0) AS total "
            "FROM transactions WHERE created_at >= $1 GROUP BY helper_id ORDER BY total DESC", since)
        names = await name_map(conn)
    total_sales = sum(r["sales"] for r in rows)
    total_amount = sum((r["total"] for r in rows), Decimal(0))
    lines = [f"📈 <b>Stats - {RANGE_LABEL[rng]}</b>",
             f"Total: <b>{total_sales}</b> sales · <b>{esc(money(total_amount))}</b>", ""]
    if not rows:
        lines.append("No sales in this period.")
    for r in rows:
        hid = r["helper_id"]
        if hid is None:
            label = "Earlier sales (no helper recorded)"
        elif hid == owner_id():
            label = f"Owner ({esc(names.get(hid) or 'you')})"
        else:
            label = f"{esc(names.get(hid) or 'Helper')} (<code>{hid}</code>)"
        lines.append(f"🧑‍💼 {label}: {r['sales']} sales · {esc(money(r['total']))}")
    return "\n".join(lines)


async def my_stats_text(pool, uid) -> str:
    lines = ["📈 <b>Your sales</b>"]
    async with pool.acquire() as conn:
        for rng in ("today", "7d", "30d", "all"):
            r = await conn.fetchrow(
                "SELECT COUNT(*) AS c, COALESCE(SUM(amount),0) AS s FROM transactions "
                "WHERE helper_id=$1 AND created_at >= $2", uid, range_start(rng))
            lines.append(f"• {RANGE_LABEL[rng]}: {r['c']} sales · {esc(money(r['s']))}")
    return "\n".join(lines)


async def mystats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pool = pool_of(context)
    uid = update.effective_user.id
    if not await is_helper(pool, uid):
        await update.message.reply_text("⛔ Helpers only.")
        return
    await update.message.reply_text(await my_stats_text(pool, uid), parse_mode=HTML)


ACT_ICON = {
    "grant": "🟢", "join": "🚪", "revoke": "🔴", "reject": "❌", "join_declined": "⛔",
    "new_customer": "🆕", "screenshot": "📸", "card_opened": "📝", "link": "🔗", "claim": "🔓",
    "helper_added": "➕", "channel_added": "📡", "channel_removed": "📡", "channel_link": "🔗",
    "category_added": "🗂", "helper_removed": "➖", "support_changed": "📞",
}


def act_line(r) -> str:
    kind = r["kind"].replace("_", " ")
    parts = [f"{ACT_ICON.get(r['kind'], '•')} {kind}", short_person(r["user_id"], r["username"])]
    if r["channel_code"]:
        parts.append(esc(channel_names(r["channel_code"].split(","))))
    if r["amount"] is not None:
        parts.append(esc(money(r["amount"])))
    parts.append(fmt_time(r["created_at"]))
    return " • ".join(parts)


async def recent_activity_text(pool, limit=10) -> str:
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM activity_log ORDER BY id DESC LIMIT $1", limit)
    if not rows:
        return "🧾 <b>Recent activity</b>\n\nNothing yet."
    return "🧾 <b>Recent activity</b>\n\n" + "\n".join(act_line(r) for r in rows)


async def build_report(pool) -> str:
    async with pool.acquire() as conn:
        tx = await conn.fetch("SELECT channels, amount FROM transactions")
        joins = await conn.fetchval("SELECT COUNT(*) FROM activity_log WHERE kind='join'")
        revokes = await conn.fetchval("SELECT COUNT(*) FROM activity_log WHERE kind='revoke'")
        period = {}
        for rng in ("today", "7d", "30d"):
            period[rng] = await conn.fetchrow(
                "SELECT COUNT(*) AS c, COALESCE(SUM(amount),0) AS s FROM transactions WHERE created_at >= $1",
                range_start(rng))
    counts = {}
    for r in tx:
        for chan in r["channels"]:
            counts[chan] = counts.get(chan, 0) + 1
    revenue = sum((r["amount"] or Decimal(0) for r in tx), Decimal(0))
    lines = [
        "📊 <b>Sales Report</b>",
        f"Total grants: <b>{len(tx)}</b> · Revenue: <b>{esc(money(revenue))}</b>",
        "",
        f"Today: {period['today']['c']} · {esc(money(period['today']['s']))}",
        f"7 days: {period['7d']['c']} · {esc(money(period['7d']['s']))}",
        f"30 days: {period['30d']['c']} · {esc(money(period['30d']['s']))}",
        "",
        "<b>By channel (times sold):</b>",
    ]
    if counts:
        lines += [f"• {esc(c)}: {n}" for c, n in counts.items()]
    else:
        lines.append("• none yet")
    lines += ["", f"Joins: {joins} · Revokes: {revokes}", "", (await recent_activity_text(pool, 10))]
    return "\n".join(lines)


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != owner_id():
        await update.message.reply_text("⛔ Access Denied.")
        return
    await update.message.reply_text(await build_report(pool_of(context)), parse_mode=HTML)


# ---------------------------------------------------------------------------
# Owner admin panel
# ---------------------------------------------------------------------------


ADMIN_TEXT = ("🛠 <b>Admin Panel</b>\n\nManage helpers, channels and categories, see stats and activity, "
              "and set the support contact.")


def admin_home_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Helpers (add / remove)", callback_data="adm:helpers")],
        [InlineKeyboardButton("📡 Channels", callback_data="chn:list"),
         InlineKeyboardButton("🗂 Categories", callback_data="cat:list")],
        [InlineKeyboardButton("📈 Helper stats", callback_data="adm:stats"),
         InlineKeyboardButton("📊 Sales report", callback_data="adm:report")],
        [InlineKeyboardButton("🧾 Recent activity", callback_data="adm:activity"),
         InlineKeyboardButton("📞 Support contact", callback_data="adm:support")],
        [InlineKeyboardButton("👀 Customer view", callback_data="hp:menu")],
    ])


BACK = [InlineKeyboardButton("⬅ Back", callback_data="adm:home")]


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != owner_id():
        await update.message.reply_text("⛔ Access Denied.")
        return
    await update.message.reply_text(ADMIN_TEXT, parse_mode=HTML, reply_markup=admin_home_markup())


async def helpers_view(pool):
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, name FROM helpers ORDER BY added_at")
    lines = ["👥 <b>Helpers</b>", ""]
    buttons = []
    if not rows:
        lines.append("No helpers yet. You (the owner) can always verify payments.")
    for r in rows:
        lines.append(f"• {esc(r['name'] or 'Helper')} (<code>{r['user_id']}</code>)")
        buttons.append([InlineKeyboardButton(f"❌ Remove {r['name'] or r['user_id']}",
                                             callback_data=f"hrm:{r['user_id']}")])
    buttons.append([InlineKeyboardButton("➕ Add helper", callback_data="adm:addhelper")])
    buttons.append(BACK)
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def stats_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Today", callback_data="st:today"), InlineKeyboardButton("7 days", callback_data="st:7d"),
         InlineKeyboardButton("30 days", callback_data="st:30d"), InlineKeyboardButton("All", callback_data="st:all")],
        BACK,
    ])


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    pool = pool_of(context)
    if q.from_user.id != owner_id():
        await q.answer("⛔ Owner only.", show_alert=True)
        return
    action, _, arg = q.data.partition(":")

    async def show(text, markup):
        try:
            await q.edit_message_text(text, parse_mode=HTML, reply_markup=markup)
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                raise

    if action == "adm":
        if arg == "home":
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM inputs WHERE user_id=$1", q.from_user.id)
            await show(ADMIN_TEXT, admin_home_markup())
        elif arg == "helpers":
            text, markup = await helpers_view(pool)
            await show(text, markup)
        elif arg == "addhelper":
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO inputs (user_id, kind, payload) VALUES ($1,'add_helper','{}') "
                    "ON CONFLICT (user_id) DO UPDATE SET kind='add_helper', payload='{}'", q.from_user.id)
            await show("➕ <b>Add helper</b>\n\nSend the helper's <b>numeric Telegram ID</b>, their @username "
                       "(they must have opened this bot once), or forward me a message from them.\n\n/cancel to stop.",
                       InlineKeyboardMarkup([BACK]))
        elif arg == "stats":
            await show(await stats_text(pool, "7d"), stats_markup())
        elif arg == "activity":
            await show(await recent_activity_text(pool, 10), InlineKeyboardMarkup([BACK]))
        elif arg == "support":
            current = await support_contact(pool)
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO inputs (user_id, kind, payload) VALUES ($1,'support','{}') "
                    "ON CONFLICT (user_id) DO UPDATE SET kind='support', payload='{}'", q.from_user.id)
            await show(f"📞 <b>Support contact</b>\nCurrently: {esc(current)}\n\nSend the @username (or text) customers "
                       "should contact when a payment is rejected or the bot doesn't recognise them. "
                       "Send <code>clear</code> to use the SUPPORT_CONTACT env value.",
                       InlineKeyboardMarkup([BACK]))
        elif arg == "report":
            await show(await build_report(pool), InlineKeyboardMarkup([BACK]))
        await q.answer()
        return

    if action == "st" and arg in RANGE_LABEL:
        await show(await stats_text(pool, arg), stats_markup())
        await q.answer()
        return

    if action == "hrm":
        async with pool.acquire() as conn:
            removed = await conn.fetchrow("DELETE FROM helpers WHERE user_id=$1 RETURNING user_id, name", int(arg))
        if removed:
            await log_event(context, "helper_removed",
                            f"➖ <b>HELPER REMOVED</b>\n🧑‍💼 {esc(removed['name'] or removed['user_id'])} "
                            f"(<code>{removed['user_id']}</code>)\nBy: {esc(q.from_user.full_name)}",
                            removed["user_id"], None, "", q.from_user.id)
        text, markup = await helpers_view(pool)
        await show(text, markup)
        await q.answer("Removed.")


async def add_helper(context, owner_user, helper_uid: int, name):
    pool = pool_of(context)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO helpers (user_id, name, added_by) VALUES ($1,$2,$3) "
            "ON CONFLICT (user_id) DO UPDATE SET name=$2", helper_uid, name, owner_user.id)
        await conn.execute("DELETE FROM inputs WHERE user_id=$1", owner_user.id)
    await log_event(context, "helper_added",
                    f"➕ <b>HELPER ADDED</b>\n🧑‍💼 {esc(name or helper_uid)} (<code>{helper_uid}</code>)\n"
                    f"By: {esc(owner_user.full_name)}", helper_uid, None, "", owner_user.id)
    await context.bot.send_message(
        chat_id=owner_user.id, parse_mode=HTML,
        text=f"✅ Added helper {esc(name or helper_uid)} (<code>{helper_uid}</code>).\nUse /admin → Helpers to manage.")
    try:
        await context.bot.send_message(chat_id=helper_uid, text="🧑‍💼 You are now a helper. Send /help to see what you can do.")
    except TelegramError:
        pass


async def handle_add_helper_text(update, context):
    msg = update.message
    pool = pool_of(context)
    raw = (msg.text or "").strip()
    if raw.lstrip("-").isdigit():
        uid = int(raw)
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT first_name FROM users WHERE user_id=$1", uid)
        await add_helper(context, update.effective_user, uid, row["first_name"] if row else None)
        return
    uname = raw.lstrip("@").lower()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id, first_name FROM users WHERE lower(username)=$1", uname)
    if not row:
        await msg.reply_text("I don't know that username yet. Ask them to open this bot once and send /start, "
                             "or send their numeric ID.")
        return
    await add_helper(context, update.effective_user, row["user_id"], row["first_name"])


async def handle_support_text(update, context):
    pool = pool_of(context)
    value = (update.message.text or "").strip()
    old = await support_contact(pool)
    async with pool.acquire() as conn:
        if value.lower() == "clear":
            await conn.execute("DELETE FROM settings WHERE key='support_contact'")
        else:
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES ('support_contact', $1) "
                "ON CONFLICT (key) DO UPDATE SET value=$1", value[:100])
        await conn.execute("DELETE FROM inputs WHERE user_id=$1", update.effective_user.id)
    new = await support_contact(pool)
    await log_event(context, "support_changed",
                    f"📞 <b>SUPPORT CONTACT CHANGED</b>\n{esc(old)} → {esc(new)}", None, None, "", update.effective_user.id)
    await update.message.reply_text(f"✅ Support contact is now: {new}")


# ---------------------------------------------------------------------------
# Setup / wiring (called from main.py)
# ---------------------------------------------------------------------------


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled error", exc_info=context.error)


# ---------------------------------------------------------------------------
# Channels and categories (managed by the admin from the panel)
# ---------------------------------------------------------------------------

PLACEHOLDER_IDS = {-1001111111111, -1002222222222, -1003333333333}
LINK_RE = re.compile(r"^https?://(t\.me|telegram\.me)/\S+$", re.I)
LINK_WARNING = ("⚠️ The link must be a <b>join-request link</b> (\"Request admin approval\" turned ON). "
                "Otherwise people could join without paying.")


async def reload_channels(pool):
    """Refresh the in-memory CHANNELS cache from the database."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT code, name, chat_id, link FROM channels ORDER BY position")
    CHANNELS.clear()
    for r in rows:
        CHANNELS[r["code"]] = {"name": r["name"], "chat_id": r["chat_id"], "link": r["link"]}


async def seed_channels(pool):
    """First start only: copy the optional SEED_CHANNELS above into the database."""
    async with pool.acquire() as conn:
        if await conn.fetchval("SELECT COUNT(*) FROM channels"):
            return
        for code, ch in SEED_CHANNELS.items():
            if ch["chat_id"] in PLACEHOLDER_IDS:
                continue
            await conn.execute(
                "INSERT INTO channels (code, name, chat_id) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
                code, ch["name"], ch["chat_id"])


async def load_cats(pool):
    async with pool.acquire() as conn:
        cats = await conn.fetch("SELECT id, name FROM categories ORDER BY id")
        links = await conn.fetch("SELECT category_id, channel_code FROM category_channels")
    by = {}
    for r in links:
        by.setdefault(r["category_id"], []).append(r["channel_code"])
    return [{"id": c["id"], "name": c["name"], "codes": by.get(c["id"], [])} for c in cats]


async def safe_edit(q, text, markup):
    try:
        await q.edit_message_text(text, parse_mode=HTML, reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def set_input(pool, uid, kind, payload=None):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO inputs (user_id, kind, payload) VALUES ($1,$2,$3) "
            "ON CONFLICT (user_id) DO UPDATE SET kind=$2, payload=$3", uid, kind, json.dumps(payload or {}))


async def my_chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remember the groups/channels the bot is in, so the admin can pick them from a list."""
    u = update.my_chat_member
    chat = u.chat
    if chat.type == "private":
        return
    status = u.new_chat_member.status
    async with pool_of(context).acquire() as conn:
        if status in ("left", "kicked"):
            await conn.execute("DELETE FROM known_chats WHERE chat_id=$1", chat.id)
        else:
            await conn.execute(
                "INSERT INTO known_chats (chat_id, title, type, is_admin) VALUES ($1,$2,$3,$4) "
                "ON CONFLICT (chat_id) DO UPDATE SET title=$2, type=$3, is_admin=$4, updated_at=CURRENT_TIMESTAMP",
                chat.id, chat.title, chat.type, status == "administrator")


# ---- channel screens ------------------------------------------------------


async def channels_list_view():
    lines = ["📡 <b>Channels &amp; groups</b>", ""]
    rows = []
    if not CHANNELS:
        lines.append("None yet. Add the bot as an <b>admin</b> to your channel/group, then tap ➕ Add.")
    for code, ch in CHANNELS.items():
        lines.append(f"• {esc(ch['name'])}{' 🔗' if ch.get('link') else ''}")
        rows.append([InlineKeyboardButton(f"📂 {ch['name']}", callback_data=f"chn:v:{code}")])
    lines.append("\n🔗 = has your own join link")
    rows.append([InlineKeyboardButton("➕ Add channel / group", callback_data="chn:add")])
    rows.append(BACK)
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def channel_view(code):
    ch = CHANNELS.get(code)
    if not ch:
        return "That channel no longer exists.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data="chn:list")]])
    link = ch.get("link")
    text = (f"📂 <b>{esc(ch['name'])}</b>\nChat ID: <code>{ch['chat_id']}</code>\n"
            f"Join link: {esc(link) if link else 'none - the bot makes join-request links and shares each one with up to 100 customers'}\n\n"
            "When a paid customer requests to join, the bot approves them automatically.")
    rows = [[InlineKeyboardButton("🔗 Set / change link", callback_data=f"chn:lk:{code}"),
             InlineKeyboardButton("✏️ Rename", callback_data=f"chn:rn:{code}")]]
    if link:
        rows.append([InlineKeyboardButton("🚫 Remove my link", callback_data=f"chn:rl:{code}")])
    if not link:
        rows.append([InlineKeyboardButton("♻️ Start a new shared link", callback_data=f"chn:nl:{code}")])
    rows.append([InlineKeyboardButton("🗑 Remove channel", callback_data=f"chn:dl:{code}")])
    rows.append([InlineKeyboardButton("⬅ Back", callback_data="chn:list")])
    return text, InlineKeyboardMarkup(rows)


async def add_channel_row(pool, chat_id, name):
    async with pool.acquire() as conn:
        n = await conn.fetchval("SELECT nextval('channel_code_seq')")
        code = f"c{n}"
        await conn.execute("INSERT INTO channels (code, name, chat_id) VALUES ($1,$2,$3)", code, name[:60], chat_id)
    await reload_channels(pool)
    return code


async def prompt_link(context, owner, code):
    pool = pool_of(context)
    await set_input(pool, owner, "chan_link", {"code": code})
    await context.bot.send_message(
        chat_id=owner, parse_mode=HTML,
        text=f"🔗 Send the <b>join-request link</b> for <b>{esc(CHANNELS[code]['name'])}</b>.\n"
             f"Customers who unlocked it will get this link, and the bot approves them automatically when they request.\n\n"
             f"{LINK_WARNING}\n\nOr send <code>skip</code> and the bot will make the links itself (one link shared by up to 100 customers).",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Cancel", callback_data="chn:list")]]))


async def send_channel_view(context, chat_id, code):
    text, markup = await channel_view(code)
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=HTML, reply_markup=markup)


async def handle_chan_id_text(update, context):
    pool = pool_of(context)
    owner = update.effective_user.id
    raw = (update.message.text or "").strip()
    try:
        cid = int(raw)
    except ValueError:
        await update.message.reply_text("⚠️ Send the numeric chat ID, like -1001234567890 (or /cancel).")
        return
    if any(c["chat_id"] == cid for c in CHANNELS.values()):
        await update.message.reply_text("That chat is already added.")
        return
    try:
        chat = await context.bot.get_chat(cid)
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(cid, me.id)
    except TelegramError:
        await update.message.reply_text("⚠️ I can't see that chat. Add me as admin there first and check the ID.")
        return
    if member.status != "administrator":
        await update.message.reply_text("⚠️ I'm in that chat but not an admin. Make me admin, then send the ID again.")
        return
    code = await add_channel_row(pool, cid, chat.title or str(cid))
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM inputs WHERE user_id=$1", owner)
    await log_event(context, "channel_added", f"📡 <b>CHANNEL ADDED</b>\n{esc(CHANNELS[code]['name'])}", None, None, code, owner)
    await prompt_link(context, owner, code)


async def handle_chan_link_text(update, context, payload):
    pool = pool_of(context)
    owner = update.effective_user.id
    code = payload.get("code")
    raw = (update.message.text or "").strip()
    if code not in CHANNELS:
        await update.message.reply_text("That channel no longer exists.")
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM inputs WHERE user_id=$1", owner)
        return
    link = None
    if raw.lower() not in ("skip", "none", "clear"):
        if not LINK_RE.match(raw):
            await update.message.reply_text("⚠️ That doesn't look like a Telegram link (https://t.me/...). Try again, or send skip.")
            return
        link = raw
    async with pool.acquire() as conn:
        await conn.execute("UPDATE channels SET link=$1 WHERE code=$2", link, code)
        await conn.execute("DELETE FROM inputs WHERE user_id=$1", owner)
    await reload_channels(pool)
    await log_event(context, "channel_link", f"🔗 <b>CHANNEL LINK {'SET' if link else 'CLEARED'}</b>\n{esc(CHANNELS[code]['name'])}",
                    None, None, code, owner)
    await update.message.reply_text("✅ Saved." + (f"\n{LINK_WARNING}" if link else ""), parse_mode=HTML)
    await send_channel_view(context, owner, code)


async def handle_chan_name_text(update, context, payload):
    pool = pool_of(context)
    owner = update.effective_user.id
    code = payload.get("code")
    name = (update.message.text or "").strip()[:60]
    if not name:
        return
    async with pool.acquire() as conn:
        await conn.execute("UPDATE channels SET name=$1 WHERE code=$2", name, code)
        await conn.execute("DELETE FROM inputs WHERE user_id=$1", owner)
    await reload_channels(pool)
    if code in CHANNELS:
        await send_channel_view(context, owner, code)


# ---- category screens -----------------------------------------------------


async def categories_list_view(pool):
    cats = await load_cats(pool)
    lines = ["🗂 <b>Categories</b>", "",
             "A category is a bundle of channels. Helpers can unlock the whole bundle with one tap."]
    rows = []
    for c in cats:
        n = len([x for x in c["codes"] if x in CHANNELS])
        rows.append([InlineKeyboardButton(f"📦 {c['name']} ({n})", callback_data=f"cat:v:{c['id']}")])
    if not cats:
        lines.append("\nNo categories yet.")
    rows.append([InlineKeyboardButton("➕ New category", callback_data="cat:new")])
    rows.append(BACK)
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def category_view(pool, cat_id):
    cats = await load_cats(pool)
    c = next((x for x in cats if x["id"] == cat_id), None)
    if not c:
        return "That category no longer exists.", InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data="cat:list")]])
    rows = []
    for code, ch in CHANNELS.items():
        mark = "✅" if code in c["codes"] else "☐"
        rows.append([InlineKeyboardButton(f"{mark} {ch['name']}", callback_data=f"cat:t:{cat_id}:{code}")])
    rows.append([InlineKeyboardButton("🗑 Delete category", callback_data=f"cat:dl:{cat_id}")])
    rows.append([InlineKeyboardButton("⬅ Back", callback_data="cat:list")])
    text = (f"📦 <b>{esc(c['name'])}</b>\n\nTick the channels/groups that belong to this category. "
            "Helpers will see a button for it on every payment card.")
    if not CHANNELS:
        text += "\n\n(Add channels first from 📡 Channels.)"
    return text, InlineKeyboardMarkup(rows)


async def handle_cat_name_text(update, context):
    pool = pool_of(context)
    owner = update.effective_user.id
    name = (update.message.text or "").strip()[:40]
    if not name:
        return
    async with pool.acquire() as conn:
        cat_id = await conn.fetchval(
            "INSERT INTO categories (name) VALUES ($1) ON CONFLICT (name) DO UPDATE SET name=EXCLUDED.name RETURNING id", name)
        await conn.execute("DELETE FROM inputs WHERE user_id=$1", owner)
    await log_event(context, "category_added", f"🗂 <b>CATEGORY SAVED</b>\n{esc(name)}", None, None, "", owner)
    text, markup = await category_view(pool, cat_id)
    await context.bot.send_message(chat_id=owner, text=text, parse_mode=HTML, reply_markup=markup)


async def catalog_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin panel: channels and categories."""
    q = update.callback_query
    pool = pool_of(context)
    user = q.from_user
    if user.id != owner_id():
        await q.answer("⛔ Admin only.", show_alert=True)
        return
    parts = q.data.split(":")
    kind, action = parts[0], parts[1]
    arg = parts[2] if len(parts) > 2 else None

    async def clear_input():
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM inputs WHERE user_id=$1", user.id)

    # ------------------------------ channels ------------------------------
    if kind == "chn":
        if action == "list":
            await clear_input()
            await safe_edit(q, *await channels_list_view())
        elif action == "v":
            await safe_edit(q, *await channel_view(arg))
        elif action == "add":
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT chat_id, title FROM known_chats WHERE is_admin ORDER BY updated_at DESC")
            rows = [r for r in rows if not any(c["chat_id"] == r["chat_id"] for c in CHANNELS.values())]
            buttons = [[InlineKeyboardButton(f"➕ {r['title'] or r['chat_id']}", callback_data=f"chn:p:{r['chat_id']}")]
                       for r in rows[:20]]
            buttons.append([InlineKeyboardButton("🔢 Enter chat ID manually", callback_data="chn:man")])
            buttons.append([InlineKeyboardButton("⬅ Back", callback_data="chn:list")])
            text = ("➕ <b>Add a channel / group</b>\n\nFirst make this bot an <b>admin</b> there "
                    "(Invite Users + Ban Users). It will then appear below.")
            if not rows:
                text += "\n\nNo new chats found yet."
            await safe_edit(q, text, InlineKeyboardMarkup(buttons))
        elif action == "p":
            cid = int(arg)
            async with pool.acquire() as conn:
                row = await conn.fetchrow("SELECT title FROM known_chats WHERE chat_id=$1", cid)
            if any(c["chat_id"] == cid for c in CHANNELS.values()):
                await q.answer("Already added.", show_alert=True)
                return
            code = await add_channel_row(pool, cid, (row["title"] if row and row["title"] else str(cid)))
            await log_event(context, "channel_added", f"📡 <b>CHANNEL ADDED</b>\n{esc(CHANNELS[code]['name'])}",
                            None, None, code, user.id)
            await q.answer("Added.")
            await prompt_link(context, user.id, code)
            return
        elif action == "man":
            await set_input(pool, user.id, "chan_id")
            await safe_edit(q, "🔢 Send the numeric chat ID (like <code>-1001234567890</code>). "
                               "I must already be an admin there.\n\n/cancel to stop.",
                            InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data="chn:add")]]))
        elif action in ("lk", "rn") and arg in CHANNELS:
            if action == "lk":
                await q.answer()
                await prompt_link(context, user.id, arg)
                return
            await set_input(pool, user.id, "chan_name", {"code": arg})
            await safe_edit(q, f"✏️ Send the new name for <b>{esc(CHANNELS[arg]['name'])}</b>.",
                            InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data=f"chn:v:{arg}")]]))
        elif action == "rl" and arg in CHANNELS:
            async with pool.acquire() as conn:
                await conn.execute("UPDATE channels SET link=NULL WHERE code=$1", arg)
            await reload_channels(pool)
            await safe_edit(q, *await channel_view(arg))
        elif action == "nl" and arg in CHANNELS:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM shared_links WHERE channel_code=$1", arg)
            await q.answer("Done. The next customer gets a brand-new link.", show_alert=True)
            return
        elif action == "dl" and arg in CHANNELS:
            await safe_edit(
                q, f"🗑 Remove <b>{esc(CHANNELS[arg]['name'])}</b>?\nCustomers will no longer see it and join requests "
                   "to it will be ignored. Existing paid records stay in the database.",
                InlineKeyboardMarkup([[InlineKeyboardButton("✅ Yes, remove", callback_data=f"chn:dy:{arg}")],
                                      [InlineKeyboardButton("⬅ No", callback_data=f"chn:v:{arg}")]]))
        elif action == "dy" and arg in CHANNELS:
            name = CHANNELS[arg]["name"]
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM channels WHERE code=$1", arg)
                await conn.execute("DELETE FROM category_channels WHERE channel_code=$1", arg)
            await reload_channels(pool)
            await log_event(context, "channel_removed", f"📡 <b>CHANNEL REMOVED</b>\n{esc(name)}", None, None, arg, user.id)
            await safe_edit(q, *await channels_list_view())
        await q.answer()
        return

    # ----------------------------- categories -----------------------------
    if action == "list":
        await clear_input()
        await safe_edit(q, *await categories_list_view(pool))
    elif action == "new":
        await set_input(pool, user.id, "cat_name")
        await safe_edit(q, "🗂 Send the <b>name</b> of the new category (e.g. Gold Plan).",
                        InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back", callback_data="cat:list")]]))
    elif action == "v":
        await safe_edit(q, *await category_view(pool, int(arg)))
    elif action == "t":
        cat_id, code = int(arg), parts[3]
        if code in CHANNELS:
            async with pool.acquire() as conn:
                removed = await conn.fetchval(
                    "DELETE FROM category_channels WHERE category_id=$1 AND channel_code=$2 RETURNING 1", cat_id, code)
                if not removed:
                    await conn.execute("INSERT INTO category_channels (category_id, channel_code) VALUES ($1,$2) "
                                       "ON CONFLICT DO NOTHING", cat_id, code)
        await safe_edit(q, *await category_view(pool, cat_id))
    elif action == "dl":
        await safe_edit(q, "🗑 Delete this category? The channels themselves are not affected.",
                        InlineKeyboardMarkup([[InlineKeyboardButton("✅ Yes, delete", callback_data=f"cat:dy:{arg}")],
                                              [InlineKeyboardButton("⬅ No", callback_data=f"cat:v:{arg}")]]))
    elif action == "dy":
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM categories WHERE id=$1", int(arg))
        await safe_edit(q, *await categories_list_view(pool))
    await q.answer()



async def setup(application, pool):
    """Create tables and warn early if the bot is not set up correctly in the channels."""
    async with pool.acquire() as conn:
        for stmt in SCHEMA:
            await conn.execute(stmt)
    logger.info("✅ Database tables verified/created.")

    # Never print the bot token: httpx logs full request URLs at INFO level.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    await seed_channels(pool)
    await reload_channels(pool)
    logger.info(f"Loaded {len(CHANNELS)} channel(s) from the database.")

    try:
        await application.bot.set_my_commands([BotCommand("start", "Open the menu")])
    except TelegramError as e:
        logger.warning(f"Could not set the bot menu: {e}")

    me = await application.bot.get_me()
    for code, ch in CHANNELS.items():
        try:
            member = await application.bot.get_chat_member(ch["chat_id"], me.id)
            if member.status != "administrator":
                logger.warning(f"[{code}] {ch['name']}: bot is not an admin.")
                continue
            if not getattr(member, "can_invite_users", False):
                logger.warning(f"[{code}] {ch['name']}: bot lacks 'Invite Users via Link' permission.")
            if not getattr(member, "can_restrict_members", False):
                logger.warning(f"[{code}] {ch['name']}: bot lacks 'Ban Users' permission (needed for /revoke).")
        except TelegramError as e:
            logger.warning(f"[{code}] {ch['name']}: cannot check channel ({e}). Is the chat_id correct?")


def register_handlers(application):
    private = filters.ChatType.PRIVATE
    add = application.add_handler

    add(CommandHandler("start", start))
    add(CommandHandler("help", help_command))
    add(CommandHandler("menu", menu_command))
    add(CommandHandler("grant", grant_command))
    add(CommandHandler("revoke", revoke_command))
    add(CommandHandler("mystats", mystats_command))
    add(CommandHandler("report", report_command))
    add(CommandHandler("admin", admin_command))
    add(CommandHandler("cancel", cancel_command))

    add(MessageHandler(private & filters.PHOTO, handle_photo))
    add(MessageHandler(private & filters.FORWARDED & ~filters.PHOTO, handle_forward))
    add(MessageHandler(filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND & ~filters.FORWARDED, text_router))

    add(ChatJoinRequestHandler(join_request_handler))
    add(ChatMemberHandler(my_chat_member_handler, ChatMemberHandler.MY_CHAT_MEMBER))

    add(CallbackQueryHandler(draft_callback, pattern=r"^(t|tc|ok|all|no|rvm):"))
    add(CallbackQueryHandler(revoke_callback, pattern=r"^(rv|rvp):"))
    add(CallbackQueryHandler(customer_callback, pattern=r"^(open|locked):"))
    add(CallbackQueryHandler(admin_callback, pattern=r"^(adm|st|hrm):"))
    add(CallbackQueryHandler(catalog_callback, pattern=r"^(chn|cat):"))
    add(CallbackQueryHandler(panel_callback, pattern=r"^hp:"))

    application.add_error_handler(error_handler)
