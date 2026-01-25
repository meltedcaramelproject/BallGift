# Полный файл: bot (1).py
# Изменения:
# - Убрано уведомление "Откройте оплату в личных сообщениях."
# - Добавлены бан/разбан (таблица banned_users, команды "бан <юз>", "разбан <юз>")
# - Добавлена команда "пополнить <число>" (создаёт инвойс admin_topup:<amount>)
# - Везде проверка бана перед взаимодействиями (callback/message handlers)
# - Остальная логика сохранена (подарки, очередь pending_gifts, реальный баланс через get_my_star_balance)

import asyncio
import asyncpg
import logging
import os
import time
import random
import urllib.parse
from typing import Optional, Tuple, Any, List

from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ContentType
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    PreCheckoutQuery, LabeledPrice
)
from aiogram import F

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# --------------------
# Config / env
# --------------------
DATABASE_URL = os.getenv("DATABASE_URL")  # if not set, in-memory fallback
GROUP_ID = int(os.getenv("GROUP_ID")) if os.getenv("GROUP_ID") else None
PAYMENTS_PROVIDER_TOKEN = os.getenv("PAYMENTS_PROVIDER_TOKEN")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in environment")
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# --------------------
# DB pool (Postgres) or in-memory fallback
# --------------------
db_pool: Optional[asyncpg.Pool] = None

# per-chat lock for running games (to prevent concurrent games in same chat)
game_locks: dict[int, bool] = {}

# invoice mapping: payload -> (origin_chat_id, invoice_chat_id, message_id)
invoice_map: dict[str, Tuple[int, int, int]] = {}

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

# Gift real-star costs
GIFT_VALUES = {"normal": 15, "premium": 25}

# For payments: STAR_UNIT_MULTIPLIER = how many smallest currency units equal 1 star.
# Default 1 means provider returns amount in stars directly.
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
    if bot_username:
        link = f"https://t.me/{bot_username}?start={user_id}"
    else:
        link = f"/start {user_id}"
    share_text = f"🏀 Приглашаю тебя сыграть в баскет за подарки!\n{link}"
    share_url = f"https://t.me/share/url?text={urllib.parse.quote(share_text)}"
    kb_rows = []
    kb_rows.append([InlineKeyboardButton(text="➡️ Отправить другу", url=share_url)])
    try:
        btn_copy = InlineKeyboardButton(text="Скопировать ссылку", copy_text=types.CopyTextButton(text=link))
        kb_rows.append([btn_copy])
    except Exception:
        kb_rows.append([InlineKeyboardButton(text="Скопировать ссылку", url=share_url)])
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
            # Keep bot_state for compatibility but real balance will be fetched from Telegram
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
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_gifts (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    amount_stars BIGINT NOT NULL,
                    premium BOOLEAN NOT NULL DEFAULT FALSE,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
                )
            """)
            # New table: banned users
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS banned_users (
                    user_id BIGINT PRIMARY KEY,
                    banned_at TIMESTAMP WITH TIME ZONE DEFAULT now()
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

# --------------------
# pending_gifts helper to insert tasks (for worker)
# --------------------
async def add_pending_gift(user_id: int, amount_stars: int, premium: bool = False):
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO pending_gifts (user_id, amount_stars, premium, status) VALUES ($1, $2, $3, 'pending')",
                    user_id, amount_stars, premium
                )
                return
        except Exception:
            log.exception("add_pending_gift DB failed")
    if not hasattr(bot, "_mem_pending_gifts"):
        bot._mem_pending_gifts = []
    bot._mem_pending_gifts.append({"user_id": user_id, "amount_stars": amount_stars, "premium": premium, "status": "pending", "created_at": int(time.time())})

# --------------------
# banned users helpers
# --------------------
async def ban_user(user_id: int):
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("INSERT INTO banned_users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", user_id)
                return
        except Exception:
            log.exception("ban_user DB failed")
    if not hasattr(bot, "_mem_banned"):
        bot._mem_banned = set()
    bot._mem_banned.add(user_id)

async def unban_user(user_id: int):
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("DELETE FROM banned_users WHERE user_id=$1", user_id)
                return
        except Exception:
            log.exception("unban_user DB failed")
    if hasattr(bot, "_mem_banned"):
        bot._mem_banned.discard(user_id)

async def is_banned(user_id: int) -> bool:
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("SELECT 1 FROM banned_users WHERE user_id=$1", user_id)
                return bool(row)
        except Exception:
            log.exception("is_banned DB failed")
            return False
    return user_id in getattr(bot, "_mem_banned", set())

# --------------------
# Helper to get real bot balance via Telegram API
# --------------------
async def get_real_bot_stars() -> int:
    """
    Use Bot API to fetch real Telegram Stars balance for the bot (getMyStarBalance).
    Return integer amount or 0 on failure.
    """
    try:
        bal = await bot.get_my_star_balance()
        amount = 0
        if hasattr(bal, "amount"):
            amount = getattr(bal, "amount") or 0
        elif hasattr(bal, "total_amount"):
            amount = getattr(bal, "total_amount") or 0
        else:
            try:
                amount = int(bal.get("amount", 0))
            except Exception:
                amount = 0
        return int(amount or 0)
    except Exception:
        log.exception("get_my_star_balance failed")
        return int(getattr(bot, "_mem_bot_stars", 0) or 0)

# --------------------
# inc_user_plays helper
# --------------------
async def inc_user_plays(user_id: int, cnt: int = 1):
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("INSERT INTO users (user_id, plays_total) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET plays_total = users.plays_total + $2", user_id, cnt)
                return
        except Exception:
            log.exception("inc_user_plays DB failed")
    if not hasattr(bot, "_mem_users"):
        bot._mem_users = {}
    bot._mem_users.setdefault(user_id, {"virtual_stars": 0, "free_next_at": 0, "spent_real": 0, "earned_real": 0, "plays_total": 0})
    bot._mem_users[user_id]["plays_total"] = bot._mem_users[user_id].get("plays_total", 0) + cnt

# --------------------
# Helpers to get nice actor display (username first, else clickable name)
# --------------------
async def get_user_display_short(user_id: int) -> str:
    try:
        u = await bot.get_chat(user_id)
        if getattr(u, "username", None):
            return f"@{u.username}"
        name = getattr(u, "first_name", None) or str(user_id)
        return f'<a href="tg://user?id={user_id}">{name}</a>'
    except Exception:
        return f'<a href="tg://user?id={user_id}">{user_id}</a>'

async def get_user_mention_link(user_id: int) -> str:
    try:
        u = await bot.get_chat(user_id)
        display = getattr(u, "username", None) or getattr(u, "first_name", None) or str(user_id)
        return f'<a href="tg://user?id={user_id}">{display}</a>'
    except Exception:
        return f'<a href="tg://user?id={user_id}">{user_id}</a>'

# --------------------
# Try to send a real Telegram gift now (fallback to pending_gifts if fail)
# --------------------
async def try_send_real_gift(user_id: int, chat_id: int, amount_stars: int, premium: bool = False) -> bool:
    try:
        gifts_obj = await bot.get_available_gifts()
        gifts_list = getattr(gifts_obj, "gifts", []) or []
        candidates = [g for g in gifts_list if getattr(g, "star_count", None) == int(amount_stars)]
        if not candidates:
            log.info("No available gifts matching %s stars (user=%s)", amount_stars, user_id)
            return False
        chosen = random.choice(candidates)
        gift_id = getattr(chosen, "id", None)
        if not gift_id:
            log.warning("Chosen gift has no id, fallback to queue (user=%s)", user_id)
            return False
        try:
            success = await bot.send_gift(gift_id=gift_id, user_id=user_id)
        except Exception as e:
            log.exception("send_gift API error for gift_id=%s user=%s: %s", gift_id, user_id, e)
            success = False
        if success:
            log.info("send_gift succeeded gift_id=%s user=%s", gift_id, user_id)
            try:
                await bot.send_message(chat_id, f"🎁 Подарок отправлен — проверьте ваш профиль.")
            except Exception:
                pass
            return True
        else:
            log.info("send_gift returned False for gift_id=%s user=%s", gift_id, user_id)
            return False
    except Exception:
        log.exception("try_send_real_gift failed unexpectedly")
        return False

# --------------------
# Referrals & stats & game flow
# --------------------
async def register_ref_visit(referred_user: int, inviter: int) -> bool:
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                res = await conn.execute("INSERT INTO referrals (referred_user, inviter, plays, rewarded) VALUES ($1, $2, 0, FALSE) ON CONFLICT (referred_user) DO NOTHING", referred_user, inviter)
                if res and res.endswith(" 1"):
                    mention = await get_user_mention_link(referred_user)
                    try:
                        await bot.send_message(inviter, f"🔗 По вашей ссылке перешёл {mention}. Вы получите +3⭐ после того, как он сыграет 5 игр", parse_mode=ParseMode.HTML)
                    except Exception:
                        pass
                    if GROUP_ID:
                        actor = await get_user_display_short(inviter)
                        try:
                            await bot.send_message(GROUP_ID, f"{actor}: по ссылке перешёл {mention}", parse_mode=ParseMode.HTML)
                        except Exception:
                            pass
                    return True
                return False
        except Exception:
            log.exception("register_ref_visit DB failed")
            return False
    if not hasattr(bot, "_mem_referrals"):
        bot._mem_referrals = {}
    if referred_user in bot._mem_referrals:
        return False
    bot._mem_referrals[referred_user] = {"inviter": inviter, "plays": 0, "rewarded": False}
    mention = await get_user_mention_link(referred_user)
    try:
        await bot.send_message(inviter, f"🔗 По вашей ссылке перешёл {mention}. Вы получите +3⭐ после того, как он сыграет 5 игр", parse_mode=ParseMode.HTML)
    except Exception:
        pass
    if GROUP_ID:
        actor = await get_user_display_short(inviter)
        try:
            await bot.send_message(GROUP_ID, f"{actor}: по ссылке перешёл {mention}", parse_mode=ParseMode.HTML)
        except Exception:
            pass
    return True

async def increment_referred_play(referred_user: int):
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("SELECT inviter, plays, rewarded FROM referrals WHERE referred_user=$1", referred_user)
                if not row:
                    return
                inviter, plays, rewarded = row["inviter"], row["plays"], row["rewarded"]
                if rewarded:
                    return
                plays += 1
                if plays >= 5:
                    await conn.execute("UPDATE referrals SET plays=$1, rewarded=TRUE WHERE referred_user=$2", plays, referred_user)
                    await change_user_virtual(inviter, 3)
                    try:
                        await bot.send_message(inviter, "🔥 Вам начислено +3⭐ — приглашённый сыграл 5 раз!")
                    except Exception:
                        pass
                    cnt = await conn.fetchval("SELECT COUNT(*) FROM referrals WHERE inviter=$1 AND rewarded=TRUE", inviter)
                    if GROUP_ID:
                        actor = await get_user_display_short(inviter)
                        try:
                            await bot.send_message(GROUP_ID, f"{actor}: приглашённый {await get_user_display_short(referred_user)} сыграл пять игр. Теперь у {actor} — {int(cnt or 0)} верифицированных рефералов", parse_mode=ParseMode.HTML)
                        except Exception:
                            pass
                else:
                    await conn.execute("UPDATE referrals SET plays=$1 WHERE referred_user=$2", plays, referred_user)
        except Exception:
            log.exception("increment_referred_play DB failed")
    else:
        mem = getattr(bot, "_mem_referrals", {})
        rec = mem.get(referred_user)
        if not rec or rec.get("rewarded"):
            return
        rec["plays"] = rec.get("plays", 0) + 1
        if rec["plays"] >= 5:
            rec["rewarded"] = True
            inviter = rec["inviter"]
            await change_user_virtual(inviter, 3)
            try:
                await bot.send_message(inviter, "🔥 Вам начислено +3⭐ — приглашённый сыграл 5 раз!")
            except Exception:
                pass
            if GROUP_ID:
                actor = await get_user_display_short(inviter)
                try:
                    await bot.send_message(GROUP_ID, f"{actor}: приглашённый {await get_user_display_short(referred_user)} сыграл пять игр. Теперь у {actor} — 1 верифицированный реферал", parse_mode=ParseMode.HTML)
                except Exception:
                    pass

async def inc_stats(count: int, premium: bool, win: bool):
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                if win:
                    await conn.execute("UPDATE stats SET wins = wins + 1 WHERE count=$1 AND premium=$2", count, premium)
                else:
                    await conn.execute("UPDATE stats SET losses = losses + 1 WHERE count=$1 AND premium=$2", count, premium)
        except Exception:
            log.exception("inc_stats DB failed")
    else:
        if not hasattr(bot, "_mem_stats"):
            bot._mem_stats = {}
            for c,p in [(1,False),(2,False),(3,False),(4,False),(5,False),(6,False),(1,True)]:
                bot._mem_stats[(c,p)] = {"wins":0,"losses":0}
        rec = bot._mem_stats.setdefault((count,premium), {"wins":0,"losses":0})
        if win:
            rec["wins"] += 1
        else:
            rec["losses"] += 1

# --------------------
# Game flow
# --------------------
async def start_game_flow(chat_id: int, count: int, premium: bool, user_id: int, paid_real_amount: int = 0):
    if await is_banned(user_id):
        try:
            await bot.send_message(user_id, "⚠️ Вы забанены и не можете участвовать в играх.")
        except Exception:
            pass
        return False, "banned"

    if game_locks.get(chat_id):
        return False, "busy"
    game_locks[chat_id] = True
    try:
        messages = []
        last_send_time = None
        for i in range(count):
            try:
                msg = await bot.send_dice(chat_id, emoji="🏀")
            except Exception:
                log.exception("send_dice failed")
                continue
            last_send_time = time.monotonic()
            messages.append(msg)
            await asyncio.sleep(0.5)
        if last_send_time is None:
            last_send_time = time.monotonic()
        elapsed = time.monotonic() - last_send_time
        wait_for = MIN_WAIT_FROM_LAST_THROW - elapsed
        if wait_for > 0:
            await asyncio.sleep(wait_for)

        hits = 0
        results = []
        for msg in messages:
            v = getattr(getattr(msg, "dice", None), "value", 0)
            try:
                results.append(int(v))
            except Exception:
                results.append(0)
            if int(v) >= 4:
                hits += 1

        await inc_user_plays(user_id, len(results))
        await increment_referred_play(user_id)
        await inc_stats(count, premium, hits == len(results) and len(results) > 0)

        win = len(results) > 0 and hits == len(results)
        won_amount = 0
        spent_real = int(paid_real_amount or 0)

        if win:
            # 1) send hits summary
            try:
                await bot.send_message(chat_id, f"🎯 Вы попали: {hits}/{len(results)}")
            except Exception:
                log.exception("Failed to send hits summary to chat %s", chat_id)

            # 2) Try to send gift
            gift_cost = GIFT_VALUES["premium"] if premium else GIFT_VALUES["normal"]
            bot_balance_before = await get_real_bot_stars()
            if bot_balance_before >= gift_cost:
                sent = await try_send_real_gift(user_id, chat_id, gift_cost, premium=premium)
                if sent:
                    won_amount = gift_cost
                else:
                    await add_pending_gift(user_id, gift_cost, premium=premium)
                    won_amount = gift_cost
                    try:
                        await bot.send_message(chat_id, "🎁 Вы выиграли подарок — его покупка/отправка поставлена в очередь.")
                    except Exception:
                        log.exception("Failed to send queued gift message to chat %s", chat_id)
            else:
                await add_pending_gift(user_id, gift_cost, premium=premium)
                won_amount = gift_cost
                try:
                    await bot.send_message(chat_id, "🎁 Вы выиграли подарок — его покупка/отправка поставлена в очередь.")
                except Exception:
                    log.exception("Failed to send queued gift message to chat %s", chat_id)

            bot_balance_after = await get_real_bot_stars()

            # 3) send main menu immediately
            try:
                vnow = await get_user_virtual(user_id)
                await bot.send_message(chat_id, START_TEXT_TEMPLATE.format(virtual_stars=vnow), reply_markup=build_main_keyboard(user_id))
            except Exception:
                log.exception("Failed to send main menu to chat %s", chat_id)

        else:
            # Non-win: existing summary
            text_lines = ["🎯 <b>Результаты бросков:</b>\n"]
            if results:
                for v in results:
                    text_lines.append("✅ Попал" if v >= 4 else "❌ Промах")
            else:
                text_lines.append("⚠️ Не удалось отправить ни одного мяча.")
            try:
                await bot.send_message(chat_id, "\n".join(text_lines))
            except Exception:
                log.exception("Failed to send results summary to chat %s", chat_id)

            await asyncio.sleep(1)
            try:
                await bot.send_message(chat_id, "✅ ПОПАДАНИЕ!" if len(results)>0 and hits==len(results) else "🟡 Не все попали. Попробуем ещё?")
            except Exception:
                log.exception("Failed to send follow-up to chat %s", chat_id)

            won_amount = 0
            bot_balance_after = await get_real_bot_stars()

            await asyncio.sleep(1)
            try:
                vnow = await get_user_virtual(user_id)
                await bot.send_message(chat_id, START_TEXT_TEMPLATE.format(virtual_stars=vnow), reply_markup=build_main_keyboard(user_id))
            except Exception:
                log.exception("Failed to send main menu to chat %s", chat_id)

        # Send group summary to GROUP_ID
        if GROUP_ID:
            try:
                actor = await get_user_display_short(user_id)
                emoji = "🎁" if win else "🥺"
                verb = "выиграл" if win else "проиграл"
                spent_display = spent_real
                won_display = int(won_amount or 0)
                msg = (
                    f"{emoji} Пользователь {actor} {verb}\n\n"
                    f"Бросков: {len(results)}🏀\n"
                    f"Он потратил: {spent_display}⭐\n"
                    f"Он выиграл: {won_display}⭐\n"
                    f"Остаток баланса бота: {int(bot_balance_after)}⭐"
                )
                await bot.send_message(GROUP_ID, msg, parse_mode=ParseMode.HTML)
            except Exception:
                log.exception("Failed to send group summary to GROUP_ID")

        return True, "win" if win else "ok"
    finally:
        game_locks.pop(chat_id, None)

# --------------------
# Handlers: start, menu, ref
# --------------------
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    uid = message.from_user.id
    if await is_banned(uid):
        try:
            await message.answer("⚠️ Вы забанены и не можете взаимодействовать с ботом.")
        except Exception:
            pass
        return

    await ensure_user(uid)
    try:
        actor = await get_user_display_short(uid)
        if GROUP_ID:
            try:
                await bot.send_message(GROUP_ID, f"{actor} перешёл в бота", parse_mode=ParseMode.HTML)
            except Exception:
                pass
    except Exception:
        pass
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
        await message.answer(start_text, reply_markup=build_main_keyboard(uid))
    except Exception:
        pass

@dp.message(F.text == "🏀 Сыграть в баскет")
async def open_main_menu(message: types.Message):
    uid = message.from_user.id
    if await is_banned(uid):
        try:
            await message.answer("⚠️ Вы забанены и не можете взаимодействовать с ботом.")
        except Exception:
            pass
        return
    v = await get_user_virtual(uid)
    await message.answer(START_TEXT_TEMPLATE.format(virtual_stars=v), reply_markup=build_main_keyboard(uid))

@dp.callback_query(F.data == "ref_menu")
async def ref_menu(call: types.CallbackQuery):
    uid = call.from_user.id
    if await is_banned(uid):
        await call.answer("Вы забанены.", show_alert=True)
        return
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
    if await is_banned(uid):
        await call.answer("Вы забанены.", show_alert=True)
        return
    await call.answer()
    v = await get_user_virtual(uid)
    try:
        await call.message.edit_text(START_TEXT_TEMPLATE.format(virtual_stars=v), reply_markup=build_main_keyboard(uid), parse_mode=ParseMode.HTML)
    except Exception:
        await call.message.answer(START_TEXT_TEMPLATE.format(virtual_stars=v), reply_markup=build_main_keyboard(uid), parse_mode=ParseMode.HTML)

# --------------------
# Play/payment/stat/balance handlers
# --------------------
@dp.callback_query(F.data and F.data.startswith("play_"))
async def play_callback(call: types.CallbackQuery):
    chat_id = call.message.chat.id
    user_id = call.from_user.id

    if await is_banned(user_id):
        await call.answer("Вы забанены.", show_alert=True)
        return

    await ensure_user(user_id)

    if game_locks.get(chat_id):
        await call.answer("Сначала дождитесь окончания текущей игры!", show_alert=True)
        return

    key = call.data.split("_", 1)[1]
    if key not in BUTTONS:
        await call.answer("Неизвестная кнопка.", show_alert=True)
        return

    cnt, cost, premium, prefix = BUTTONS[key]

    if cost == 0:
        now = int(time.time())
        free_next = await get_user_free_next(user_id)
        if now < free_next:
            rem = free_next - now
            mins = rem // 60
            secs = rem % 60
            min_word = "минут" if mins != 1 else "минуту"
            sec_word = "секунд" if secs != 1 else "секунду"
            try:
                await call.answer(f"🏀 Вы сможете повторно сделать бесплатный бросок через {mins} {min_word} и {secs} {sec_word}", show_alert=True)
            except Exception:
                try:
                    await bot.send_message(user_id, f"🏀 Вы сможете повторно сделать бесплатный бросок через {mins} {min_word} и {secs} {sec_word}")
                    await call.answer()
                except Exception:
                    await call.answer("Подождите.", show_alert=False)
            return
        await set_user_free_next(user_id, now + FREE_COOLDOWN)
        await call.answer()
        await start_game_flow(chat_id, cnt, premium, user_id, paid_real_amount=0)
        return

    vstars = await get_user_virtual(user_id)
    if vstars >= cost:
        await change_user_virtual(user_id, -cost)
        await call.answer()
        await start_game_flow(chat_id, cnt, premium, user_id, paid_real_amount=0)
        return

    missing = cost - vstars
    noun = word_form_mяч(cnt)
    title = f"{cnt} {noun}"
    description = "🎁 Получай крутой подарок за попадание 🏀 в кольцо"
    label = f"Играть за {missing}⭐"
    amount = int(missing * STAR_UNIT_MULTIPLIER)
    prices = [LabeledPrice(label=label, amount=amount)]
    ts = int(time.time())
    payload = f"buy_and_play:{user_id}:{cnt}:{1 if premium else 0}:{ts}"
    try:
        invoice_msg = await bot.send_invoice(
            chat_id=user_id,
            title=title,
            description=description,
            provider_token=PAYMENTS_PROVIDER_TOKEN,
            currency="XTR",
            prices=prices,
            payload=payload,
            start_parameter="buyandplay"
        )
        invoice_map[payload] = (chat_id, invoice_msg.chat.id, invoice_msg.message_id)
        # Note: per request, DO NOT show text "Откройте оплату в личных сообщениях."
        await call.answer()
    except Exception:
        log.exception("send_invoice failed")
        await call.answer("Не удалось создать платёж. Проверьте Payments в BotFather.", show_alert=True)

@dp.pre_checkout_query()
async def precheckout_handler(pre_q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_q.id, ok=True)

@dp.message(F.content_type == ContentType.SUCCESSFUL_PAYMENT)
async def on_successful_payment(message: types.Message):
    sp = message.successful_payment
    payload = getattr(sp, "invoice_payload", "") or ""
    payer_id = message.from_user.id
    if payload.startswith("buy_and_play:"):
        try:
            parts = payload.split(":")
            if len(parts) >= 4:
                _payload_payer = int(parts[1]) if parts[1].isdigit() else payer_id
                cnt = int(parts[2]) if parts[2].isdigit() else 1
                prem_flag = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 0
            else:
                cnt = 1
                prem_flag = 0
        except Exception:
            cnt = 1
            prem_flag = 0
        try:
            mapping = invoice_map.pop(payload, None)
            if mapping:
                origin_chat_id, inv_chat_id, inv_msg_id = mapping
                try:
                    await bot.delete_message(inv_chat_id, inv_msg_id)
                except Exception:
                    log.exception("Failed to delete invoice message")
            else:
                origin_chat_id = payer_id
        except Exception:
            log.exception("invoice_map delete error")
            origin_chat_id = payer_id
        try:
            paid_amount_raw = int(sp.total_amount or 0)
        except Exception:
            paid_amount_raw = 0
        try:
            paid_stars = int(paid_amount_raw // STAR_UNIT_MULTIPLIER)
        except Exception:
            paid_stars = paid_amount_raw
        log.info("Payment accepted: raw=%s -> stars=%s (mult=%s) payer=%s", paid_amount_raw, paid_stars, STAR_UNIT_MULTIPLIER, payer_id)
        if paid_stars > 0:
            await add_user_spent_real(payer_id, paid_stars)
        await set_user_virtual(payer_id, 0)
        if game_locks.get(origin_chat_id):
            try:
                await bot.send_message(payer_id, "Сейчас идёт другая игра в этом чате — ваша оплата зачислена, игра начнётся позже.")
            except Exception:
                pass
            return
        await start_game_flow(origin_chat_id, cnt, bool(prem_flag), payer_id, paid_real_amount=int(paid_stars))
        return

    if payload.startswith("admin_topup:"):
        # Admin top-up invoice paid
        try:
            parts = payload.split(":")
            if len(parts) >= 2:
                amount = int(parts[1])
            else:
                amount = 0
        except Exception:
            amount = 0
        # Telegram will credit the bot automatically. Just confirm.
        try:
            await message.answer(f"✅ Пополнение на {amount}⭐ принято. Спасибо!")
        except Exception:
            pass
        # Notify group
        if GROUP_ID:
            try:
                actor = await get_user_display_short(payer_id)
                await bot.send_message(GROUP_ID, f"🔔 {actor} пополнил баланс бота на {amount}⭐", parse_mode=ParseMode.HTML)
            except Exception:
                pass
        return

    if payload.startswith("buy_virtual_"):
        try:
            parts = payload.split("_")
            target_user = int(parts[2])
            missing = int(parts[3])
            await change_user_virtual(target_user, missing)
            await add_user_spent_real(target_user, missing)
            try:
                await bot.send_message(target_user, f"Оплата принята — вам начислено {missing}⭐.")
            except Exception:
                pass
        except Exception:
            pass
        return

    # other payments
    try:
        await message.answer("Платёж принят. Спасибо!")
    except Exception:
        pass

@dp.callback_query(F.data and F.data.startswith("pay_virtual_"))
async def pay_virtual_cb(call: types.CallbackQuery):
    uid = call.from_user.id
    if await is_banned(uid):
        await call.answer("Вы забанены.", show_alert=True)
        return
    await call.answer()
    try:
        missing = int(call.data.split("_", 2)[2])
    except Exception:
        await call.message.answer("Ошибка данных покупки.")
        return
    user = call.from_user
    amount = int(missing * STAR_UNIT_MULTIPLIER)
    prices = [LabeledPrice(label=f"Играть за {missing}⭐", amount=amount)]
    payload = f"buy_virtual_{user.id}_{missing}_{int(time.time())}"
    try:
        invoice_msg = await bot.send_invoice(
            chat_id=user.id,
            title=f"{missing} мячей",
            description="Пополнение виртуальных звёзд",
            provider_token=PAYMENTS_PROVIDER_TOKEN,
            currency="XTR",
            prices=prices,
            payload=payload,
            start_parameter="buyvirtual"
        )
        invoice_map[payload] = (user.id, invoice_msg.chat.id, invoice_msg.message_id)
    except Exception:
        log.exception("send_invoice failed")
        await call.message.answer("Не удалось создать платёж.")

# --------------------
# Admin commands: бан, разбан, пополнить
# These are group-only and require sending from GROUP_ID chat (admins)
# --------------------
@dp.message(F.text)
async def admin_commands_router(message: types.Message):
    text = (message.text or "").strip()
    if not text:
        return
    lowered = text.split()[0].lower()

    # Only allow admin commands in GROUP_ID
    if message.chat.id != GROUP_ID:
        return

    # BAN: "бан <user>" or "/бан <user>"
    if lowered in ("/бан", "бан"):
        parts = text.split(maxsplit=1)
        if len(parts) == 1 and message.reply_to_message:
            # use reply target
            target = message.reply_to_message.from_user.id
        elif len(parts) >= 2:
            token = parts[1].strip()
            if token.startswith("@"):
                try:
                    chat = await bot.get_chat(token)
                    target = chat.id
                except Exception:
                    await message.reply("Не удалось найти пользователя по username.")
                    return
            elif token.isdigit():
                target = int(token)
            else:
                await message.reply("Неверный формат. Используйте: бан <@username|id> или ответьте командой на сообщение пользователя.")
                return
        else:
            await message.reply("Укажите пользователя для бана: бан <@username|id> или ответьте на сообщение.")
            return
        await ban_user(target)
        actor = await get_user_display_short(target)
        await message.reply(f"✅ Пользователь {actor} забанен.", parse_mode=ParseMode.HTML)
        return

    # UNBAN: "разбан <user>" or "/разбан <user>"
    if lowered in ("/разбан", "разбан"):
        parts = text.split(maxsplit=1)
        if len(parts) == 1 and message.reply_to_message:
            target = message.reply_to_message.from_user.id
        elif len(parts) >= 2:
            token = parts[1].strip()
            if token.startswith("@"):
                try:
                    chat = await bot.get_chat(token)
                    target = chat.id
                except Exception:
                    await message.reply("Не удалось найти пользователя по username.")
                    return
            elif token.isdigit():
                target = int(token)
            else:
                await message.reply("Неверный формат. Используйте: разбан <@username|id> или ответьте командой на сообщение пользователя.")
                return
        else:
            await message.reply("Укажите пользователя для разбанa: разбан <@username|id> или ответьте на сообщение.")
            return
        await unban_user(target)
        actor = await get_user_display_short(target)
        await message.reply(f"✅ Пользователь {actor} разбанен.", parse_mode=ParseMode.HTML)
        return

    # TOPUP / пополнить: "пополнить <число>" -> create invoice for admin to pay.
    if lowered in ("/пополнить", "пополнить"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply("Использование: пополнить <число_звёзд>")
            return
        try:
            amount = int(parts[1].strip())
            if amount <= 0:
                await message.reply("Сумма должна быть положительным числом.")
                return
        except Exception:
            await message.reply("Неверный формат числа.")
            return
        # Create invoice to admin (message.from_user.id)
        admin_id = message.from_user.id
        prices = [LabeledPrice(label=f"Пополнение бота на {amount}⭐", amount=int(amount * STAR_UNIT_MULTIPLIER))]
        payload = f"admin_topup:{amount}:{int(time.time())}"
        try:
            invoice_msg = await bot.send_invoice(
                chat_id=admin_id,
                title=f"Пополнение бота — {amount}⭐",
                description="Пополнение реального баланса бота звёздами Telegram",
                provider_token=PAYMENTS_PROVIDER_TOKEN,
                currency="XTR",
                prices=prices,
                payload=payload,
                start_parameter="admintopup"
            )
            invoice_map[payload] = (message.chat.id, invoice_msg.chat.id, invoice_msg.message_id)
            await message.reply("Инвойс создан и отправлен администратору в личку.")
        except Exception:
            log.exception("send_invoice failed")
            await message.reply("Не удалось создать инвойс. Проверьте Payments в BotFather.")
        return

    # If it's /стат or /баланс etc, let existing handler handle them (no conflict)
    # Otherwise ignore here.

# --------------------
# Commands: /стат and /баланс (group only)
# --------------------
@dp.message(F.text)
async def stat_and_balans_router(message: types.Message):
    text = (message.text or "").strip()
    if not text:
        return
    lowered = text.lower().split()[0]
    # /стат
    if lowered == "/стат" or lowered == "стат":
        if GROUP_ID is None or message.chat.id != GROUP_ID:
            return
        try:
            if db_pool:
                async with db_pool.acquire() as conn:
                    users_count = int(await conn.fetchval("SELECT COUNT(*) FROM users") or 0)
            else:
                users_count = len(getattr(bot, "_mem_users", {}))
            botstars = await get_real_bot_stars()
            await message.answer(f"👤 Пользователей: {users_count}\n⭐ Звёзд на счету бота: {botstars}")
        except Exception:
            log.exception("Failed to produce /стат")
        return

    # /баланс or баланс (group only)
    if lowered.startswith("/баланс") or lowered == "баланс":
        if GROUP_ID is None or message.chat.id != GROUP_ID:
            return
        parts = text.split()
        if len(parts) == 1:
            b = await get_real_bot_stars()
            await message.answer(f"💰 Баланс бота (реальные звёзд): <b>{b}</b>")
            return
        # disallow manual DB set
        await message.answer("Управление реальным балансом бота запрещено вручную. Баланс формирует Telegram через платежи.")
        return

# --------------------
# Web server health
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
