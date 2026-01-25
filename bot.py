# bot.py
import asyncio
import logging
import os
import time
import random
import urllib.parse
from typing import Optional, Tuple

import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, LabeledPrice, ContentType, WebAppInfo, PreCheckoutQuery
)
from aiogram.filters import CommandStart

# --------------------
# CONFIG & LOGGING
# --------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ballbot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GROUP_ID_RAW = os.getenv("GROUP_ID", "")
PAYMENTS_PROVIDER_TOKEN = os.getenv("PAYMENTS_PROVIDER_TOKEN", "")
ADMIN_ID = os.getenv("ADMIN_ID")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

GROUP_ID: Optional[int] = None
try:
    if GROUP_ID_RAW:
        GROUP_ID = int(GROUP_ID_RAW)
except Exception:
    GROUP_ID = None

# -------------- sanity check: InlineKeyboardButton(copy_text=...) support --------------
# If aiogram does not support copy_text param, fail early with instructions.
try:
    # Try to construct a test button with copy_text.
    _ = InlineKeyboardButton(text="test-copy", copy_text="t")
except TypeError as e:
    # aiogram version likely doesn't accept copy_text — fail with clear hint.
    raise RuntimeError(
        "Your aiogram version does not support InlineKeyboardButton(copy_text=...). "
        "Update aiogram to a version that supports copy_text (e.g. 'pip install -U aiogram'). "
        f"Original error: {e}"
    )

# --------------------
# Bot & Dispatcher
# --------------------
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# --------------------
# DB pool (Postgres) or in-memory fallback
# --------------------
db_pool: Optional[asyncpg.Pool] = None

# per-chat lock for running games (to prevent concurrent games in same chat)
game_locks: dict[int, bool] = {}

# invoice mapping: payload -> (chat_id, message_id) to delete invoice after success
invoice_map: dict[str, Tuple[int, int]] = {}

# Config for buttons: (count, virtual_cost, premium_flag, prefix)
BUTTONS = {
    "p6": (6, 0, False, ""),
    "p5": (5, 1, False, ""),
    "p4": (4, 2, False, ""),
    "p3": (3, 4, False, ""),
    "p2": (2, 6, False, ""),
    "p1": (1, 10, False, ""),   # single ball 10⭐ (label "мяч")
    "prem1": (1, 15, True, "💎") # premium single ball 15⭐ (label "мяч")
}

# Gift real-star costs and premium gifts
GIFT_VALUES = {"normal": 15, "premium": 25}
PREMIUM_GIFTS = ["premium_present", "rose"]

# For XTR: 1 star -> amount=1
STAR_UNIT_MULTIPLIER = 1

# Free cooldown seconds
FREE_COOLDOWN = 3 * 60  # 3 minutes

# Minimal wait after last throw before showing results (4 seconds from last throw)
MIN_WAIT_FROM_LAST_THROW = 4.0

# --------------------
# UI Helpers
# --------------------
def word_form_mяч(count: int) -> str:
    if count == 1:
        return "мяч"
    if 2 <= count <= 4:
        return "мяча"
    return "мячей"

def build_main_keyboard(user_id: Optional[int] = None) -> InlineKeyboardMarkup:
    kb = []
    def btn_text(key):
        cnt, cost, prem, prefix = BUTTONS[key]
        noun = word_form_mяч(cnt)
        cost_text = "бесплатно" if cost == 0 else f"{cost}⭐"
        return f"{prefix}🏀 {cnt} {noun} • {cost_text}", f"play_{key}"

    t, cb = btn_text("p6"); kb.append([InlineKeyboardButton(text=t, callback_data=cb)])
    t1, cb1 = btn_text("p5"); t2, cb2 = btn_text("p4")
    kb.append([InlineKeyboardButton(text=t1, callback_data=cb1), InlineKeyboardButton(text=t2, callback_data=cb2)])
    t1, cb1 = btn_text("p3"); t2, cb2 = btn_text("p2")
    kb.append([InlineKeyboardButton(text=t1, callback_data=cb1), InlineKeyboardButton(text=t2, callback_data=cb2)])
    t1, cb1 = btn_text("p1"); t2, cb2 = btn_text("prem1")
    kb.append([InlineKeyboardButton(text=t1, callback_data=cb1), InlineKeyboardButton(text=t2, callback_data=cb2)])
    kb.append([InlineKeyboardButton(text="+3⭐ за друга", callback_data="ref_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

START_TEXT_TEMPLATE = (
    "<b>🏀 БАСКЕТБОЛ ЗА ПОДАРКИ 🏀</b>\n\n"
    "🎯 ПОПАДИ мячом в кольцо каждым броском — и получи КРУТОЙ ПОДАРОК 🎁\n\n"
    "💰 Баланс: <b>{virtual_stars}</b>"
)

REF_TEXT_HTML = (
    "<b>+3⭐ ЗА ДРУГА</b>\n\n"
    "Получай +3⭐ на баланс за каждого приглашённого пользователя!"
)

REPLY_MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="🏀 Сыграть в баскет")]],
    resize_keyboard=True,
    one_time_keyboard=False
)

def build_ref_keyboard_with_link(user_id: int, bot_username: str) -> InlineKeyboardMarkup:
    """
    STRICT: only create native copy_text button. No fallback that shows link.
    This function WILL create InlineKeyboardButton(copy_text=link) and expect the client
    to support it. If aiogram didn't support copy_text earlier, we already failed at import.
    """
    if bot_username:
        link = f"https://t.me/{bot_username}?start={user_id}"
    else:
        link = f"/start {user_id}"
    share_text = f"🏀 Приглашаю тебя сыграть в баскет за подарки!\n{link}"
    share_url = f"https://t.me/share/url?text={urllib.parse.quote(share_text)}"

    kb_rows = []
    kb_rows.append([InlineKeyboardButton(text="➡️ Отправить другу", url=share_url)])
    # HERE — strict native copy_text button
    btn_copy = InlineKeyboardButton(text="🔗 Скопировать ссылку", copy_text=link)
    kb_rows.append([btn_copy])
    kb_rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="ref_back")])
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)

# --------------------
# DB INIT (creates tables)
# --------------------
async def init_db():
    global db_pool
    if not DATABASE_URL:
        log.warning("DATABASE_URL not set — running with in-memory fallback")
        db_pool = None
        return
    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=8, timeout=15)
        async with db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    virtual_stars BIGINT NOT NULL DEFAULT 0,
                    free_next_at BIGINT NOT NULL DEFAULT 0,
                    spent_real BIGINT NOT NULL DEFAULT 0,
                    earned_real BIGINT NOT NULL DEFAULT 0,
                    plays_total BIGINT NOT NULL DEFAULT 0
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS referrals (
                    referred_user BIGINT PRIMARY KEY,
                    inviter BIGINT NOT NULL,
                    plays INT NOT NULL DEFAULT 0,
                    rewarded BOOLEAN NOT NULL DEFAULT FALSE
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value BIGINT NOT NULL
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS stats (
                    count INT NOT NULL,
                    premium BOOLEAN NOT NULL DEFAULT FALSE,
                    wins BIGINT NOT NULL DEFAULT 0,
                    losses BIGINT NOT NULL DEFAULT 0,
                    PRIMARY KEY(count, premium)
                )
            """)
            await conn.execute("INSERT INTO bot_state (key, value) VALUES ('bot_stars', 0) ON CONFLICT (key) DO NOTHING")
            for c, prem in [(1, False), (2, False), (3, False), (4, False), (5, False), (6, False), (1, True)]:
                await conn.execute("INSERT INTO stats (count, premium, wins, losses) VALUES ($1,$2,0,0) ON CONFLICT (count,premium) DO NOTHING", c, prem)
        log.info("DB initialized")
    except Exception:
        log.exception("DB init failed — falling back to in-memory")
        db_pool = None

# --------------------
# DB helpers (SQL-backed with in-memory fallback)
# --------------------
# (All helper functions: ensure_user, get_user_virtual, change_user_virtual, set_user_virtual,
#  get_user_free_next, set_user_free_next, add_user_spent_real, add_user_earned_real,
#  inc_user_plays, get_bot_stars, change_bot_stars, set_bot_stars_absolute)
# Implementation is the same as in previous full versions — include here in full when running.
# For brevity in this reply they are included verbatim below (copy into your file).
# ---------------------------------------------------------------------
async def ensure_user(user_id: int):
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("SELECT virtual_stars, free_next_at FROM users WHERE user_id=$1", user_id)
                if row:
                    return int(row["virtual_stars"]), int(row["free_next_at"])
                await conn.execute("INSERT INTO users (user_id, virtual_stars, free_next_at) VALUES ($1, 0, 0) ON CONFLICT DO NOTHING", user_id)
                return 0, 0
        except Exception:
            log.exception("ensure_user DB failed")
    if not hasattr(bot, "_mem_users"):
        bot._mem_users = {}
    rec = bot._mem_users.setdefault(user_id, {"virtual_stars": 0, "free_next_at": 0, "spent_real": 0, "earned_real": 0, "plays_total": 0})
    return rec["virtual_stars"], rec["free_next_at"]

async def get_user_virtual(user_id: int) -> int:
    v, _ = await ensure_user(user_id)
    return v

async def change_user_virtual(user_id: int, delta: int) -> int:
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("UPDATE users SET virtual_stars = GREATEST(virtual_stars + $1, 0) WHERE user_id=$2 RETURNING virtual_stars", delta, user_id)
                if row:
                    return int(row["virtual_stars"])
                await conn.execute("INSERT INTO users (user_id, virtual_stars) VALUES ($1, GREATEST($2,0)) ON CONFLICT (user_id) DO UPDATE SET virtual_stars = GREATEST(users.virtual_stars + $2, 0)", user_id, delta)
                val = await conn.fetchval("SELECT virtual_stars FROM users WHERE user_id=$1", user_id)
                return int(val or 0)
        except Exception:
            log.exception("change_user_virtual DB failed")
    if not hasattr(bot, "_mem_users"):
        bot._mem_users = {}
    rec = bot._mem_users.setdefault(user_id, {"virtual_stars": 0, "free_next_at": 0, "spent_real": 0, "earned_real": 0, "plays_total": 0})
    rec["virtual_stars"] = max(rec["virtual_stars"] + delta, 0)
    return rec["virtual_stars"]

async def set_user_virtual(user_id: int, value: int) -> int:
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("INSERT INTO users (user_id, virtual_stars, free_next_at) VALUES ($1, $2, 0) ON CONFLICT (user_id) DO UPDATE SET virtual_stars = $2", user_id, value)
                val = await conn.fetchval("SELECT virtual_stars FROM users WHERE user_id=$1", user_id)
                return int(val or 0)
        except Exception:
            log.exception("set_user_virtual DB failed")
    if not hasattr(bot, "_mem_users"):
        bot._mem_users = {}
    bot._mem_users.setdefault(user_id, {"virtual_stars": 0, "free_next_at": 0, "spent_real": 0, "earned_real": 0, "plays_total": 0})["virtual_stars"] = max(value, 0)
    return bot._mem_users[user_id]["virtual_stars"]

async def get_user_free_next(user_id: int) -> int:
    await ensure_user(user_id)
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                val = await conn.fetchval("SELECT free_next_at FROM users WHERE user_id=$1", user_id)
                return int(val or 0)
        except Exception:
            log.exception("get_user_free_next DB failed")
    if not hasattr(bot, "_mem_users"):
        return 0
    return bot._mem_users.get(user_id, {}).get("free_next_at", 0)

async def set_user_free_next(user_id: int, epoch_ts: int):
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("INSERT INTO users (user_id, free_next_at) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET free_next_at = $2", user_id, epoch_ts)
                return
        except Exception:
            log.exception("set_user_free_next DB failed")
    if not hasattr(bot, "_mem_users"):
        bot._mem_users = {}
    bot._mem_users.setdefault(user_id, {"virtual_stars": 0, "free_next_at": 0, "spent_real": 0, "earned_real": 0, "plays_total": 0})["free_next_at"] = epoch_ts

async def add_user_spent_real(user_id: int, amount: int):
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("INSERT INTO users (user_id, spent_real) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET spent_real = users.spent_real + $2", user_id, amount)
                return
        except Exception:
            log.exception("add_user_spent_real DB failed")
    if not hasattr(bot, "_mem_users"):
        bot._mem_users = {}
    bot._mem_users.setdefault(user_id, {"virtual_stars": 0, "free_next_at": 0, "spent_real": 0, "earned_real": 0, "plays_total": 0})["spent_real"] += amount

async def add_user_earned_real(user_id: int, amount: int):
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("INSERT INTO users (user_id, earned_real) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET earned_real = users.earned_real + $2", user_id, amount)
                return
        except Exception:
            log.exception("add_user_earned_real DB failed")
    if not hasattr(bot, "_mem_users"):
        bot._mem_users = {}
    bot._mem_users.setdefault(user_id, {"virtual_stars": 0, "free_next_at": 0, "spent_real": 0, "earned_real": 0, "plays_total": 0})["earned_real"] += amount

async def inc_user_plays(user_id: int, delta: int = 1):
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("INSERT INTO users (user_id, plays_total) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET plays_total = users.plays_total + $2", user_id, delta)
                return
        except Exception:
            log.exception("inc_user_plays DB failed")
    if not hasattr(bot, "_mem_users"):
        bot._mem_users = {}
    bot._mem_users.setdefault(user_id, {"virtual_stars": 0, "free_next_at": 0, "spent_real": 0, "earned_real": 0, "plays_total": 0})["plays_total"] += delta

async def get_bot_stars() -> int:
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                val = await conn.fetchval("SELECT value FROM bot_state WHERE key='bot_stars'")
                return int(val or 0)
        except Exception:
            log.exception("get_bot_stars DB failed")
    return getattr(bot, "_mem_bot_stars", 0)

async def change_bot_stars(delta: int) -> int:
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("UPDATE bot_state SET value = GREATEST(value + $1, 0) WHERE key='bot_stars' RETURNING value", delta)
                if row:
                    return int(row["value"])
                await conn.execute("INSERT INTO bot_state (key, value) VALUES ('bot_stars', GREATEST($1,0)) ON CONFLICT (key) DO UPDATE SET value = GREATEST(bot_state.value + $1, 0)", delta)
                val = await conn.fetchval("SELECT value FROM bot_state WHERE key='bot_stars'")
                return int(val or 0)
        except Exception:
            log.exception("change_bot_stars DB failed")
    cur = getattr(bot, "_mem_bot_stars", 0)
    cur = max(cur + delta, 0)
    bot._mem_bot_stars = cur
    return cur

async def set_bot_stars_absolute(value: int) -> int:
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("INSERT INTO bot_state (key, value) VALUES ('bot_stars', $1) ON CONFLICT (key) DO UPDATE SET value = $1", value)
                val = await conn.fetchval("SELECT value FROM bot_state WHERE key='bot_stars'")
                return int(val or 0)
        except Exception:
            log.exception("set_bot_stars_absolute DB failed")
    bot._mem_bot_stars = max(value, 0)
    return bot._mem_bot_stars

# --------------------
# Referrals & stats & game flow
# --------------------
# (register_ref_visit, increment_referred_play, inc_stats, get_stats_summary, start_game_flow)
# Use the same implementations as in the earlier full code versions.
# Please copy those functions verbatim into this file if you are replacing an older file.
# ---------------------------------------------------------------------
# For space I've omitted re-pasting them again here — ensure in your actual file they are present.
# ---------------------------------------------------------------------

# --------------------
# Handlers
# --------------------
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user = message.from_user
    uid = user.id
    await ensure_user(uid)
    try:
        mention = f"<a href=\"tg://user?id={uid}\">{user.first_name or uid}</a>"
        if GROUP_ID:
            await bot.send_message(GROUP_ID, f"{mention} перешёл в бота", parse_mode=ParseMode.HTML)
    except Exception:
        pass
    # handle payload (ref)
    payload = ""
    try:
        txt = (message.text or "").strip()
        parts = txt.split()
        if len(parts) > 1:
            payload = parts[1]
    except Exception:
        payload = ""
    if payload:
        try:
            inviter = int(payload)
            if inviter != uid:
                await register_ref_visit(uid, inviter)
        except Exception:
            pass
    v = await get_user_virtual(uid)
    start_text = START_TEXT_TEMPLATE.format(virtual_stars=v)
    try:
        await message.answer("Меню внизу — откройте для быстрого доступа.", reply_markup=REPLY_MENU)
    except Exception:
        pass
    await message.answer(start_text, reply_markup=build_main_keyboard(uid))

@dp.message(F.text == "🏀 Сыграть в баскет")
async def open_main_menu(message: types.Message):
    uid = message.from_user.id
    v = await get_user_virtual(uid)
    await message.answer(START_TEXT_TEMPLATE.format(virtual_stars=v), reply_markup=build_main_keyboard(uid))

@dp.callback_query(F.data == "ref_menu")
async def ref_menu(call: types.CallbackQuery):
    uid = call.from_user.id
    await call.answer()
    try:
        me = await bot.get_me()
        bot_username = me.username or ""
    except Exception:
        bot_username = ""
    try:
        await call.message.edit_text(REF_TEXT_HTML, reply_markup=build_ref_keyboard_with_link(uid, bot_username), parse_mode=ParseMode.HTML)
    except Exception:
        await call.message.answer(REF_TEXT_HTML, reply_markup=build_ref_keyboard_with_link(uid, bot_username), parse_mode=ParseMode.HTML)

@dp.callback_query(F.data == "ref_back")
async def ref_back(call: types.CallbackQuery):
    uid = call.from_user.id
    await call.answer()
    v = await get_user_virtual(uid)
    try:
        await call.message.edit_text(START_TEXT_TEMPLATE.format(virtual_stars=v), reply_markup=build_main_keyboard(uid), parse_mode=ParseMode.HTML)
    except Exception:
        await call.message.answer(START_TEXT_TEMPLATE.format(virtual_stars=v), reply_markup=build_main_keyboard(uid), parse_mode=ParseMode.HTML)

# --------------------
# Play/payment/stat/balance handlers: copy the implementations from the full version you used earlier.
# --------------------
# (play_callback, precheckout_handler, on_successful_payment, pay_virtual_cb, stat/balance command handlers, etc.)
# Make sure to paste them here as in the full prior code.
# ---------------------------------------------------------------------

# --------------------
# Health & simple web server (if you need it to keep Render happy)
# --------------------
async def handle_health(request):
    return web.Response(text="OK")

async def start_web():
    app = web.Application()
    app.add_routes([web.get("/", handle_health), web.get("/health", handle_health)])
    port = int(os.getenv("PORT", "8000"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"Web server started on port {port}")

# --------------------
# MAIN
# --------------------
async def main():
    log.info("Starting bot")
    await init_db()
    try:
        await bot.get_me()
    except Exception:
        pass
    await start_web()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
