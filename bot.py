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
PUBLIC_URL = os.getenv("PUBLIC_URL", "")  # for WebApp copy feature (https required)
PAYMENTS_PROVIDER_TOKEN = os.getenv("PAYMENTS_PROVIDER_TOKEN", "")  # leave empty for Telegram Stars (XTR)
ADMIN_ID = os.getenv("ADMIN_ID")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

GROUP_ID: Optional[int] = None
try:
    if GROUP_ID_RAW:
        GROUP_ID = int(GROUP_ID_RAW)
except Exception:
    GROUP_ID = None

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

# Config for buttons: (key, count, virtual_cost, label_prefix, premium_flag)
BUTTONS = {
    "p6": (6, 0, False, ""),
    "p5": (5, 1, False, ""),
    "p4": (4, 2, False, ""),
    "p3": (3, 4, False, ""),
    "p2": (2, 6, False, ""),
    "p1": (1, 10, False, ""),
    "prem1": (1, 15, True, "💎")
}

# Gift real-star costs and premium gifts
GIFT_VALUES = {"normal": 15, "premium": 25}
PREMIUM_GIFTS = ["premium_present", "rose"]

# For XTR: 1 star -> amount=1
STAR_UNIT_MULTIPLIER = 1

# Free cooldown seconds
FREE_COOLDOWN = 3 * 60  # 3 minutes

# --------------------
# UI Helpers
# --------------------
def word_form_mяч(count: int) -> str:
    return "мяча" if 1 <= count <= 4 else "мячей"

def build_main_keyboard(user_id: Optional[int] = None) -> InlineKeyboardMarkup:
    """
    Layout requested:
    Row1: 6
    Row2: 5,4
    Row3: 3,2
    Row4: 1, prem1
    Row5: stars for friend
    """
    kb = []
    def btn_text(key):
        cnt, cost, prem, prefix = (BUTTONS[key][0], BUTTONS[key][1], BUTTONS[key][2], BUTTONS[key][3])
        noun = word_form_mяч(cnt)
        cost_text = "бесплатно" if cost == 0 else f"{cost}⭐"
        prefix = BUTTONS[key][3]
        return f"{prefix}🏀 {cnt} {noun} • {cost_text}", f"play_{key}"

    # row1
    t, cb = btn_text("p6"); kb.append([InlineKeyboardButton(text=t, callback_data=cb)])
    # row2
    t1, cb1 = btn_text("p5"); t2, cb2 = btn_text("p4")
    kb.append([InlineKeyboardButton(text=t1, callback_data=cb1), InlineKeyboardButton(text=t2, callback_data=cb2)])
    # row3
    t1, cb1 = btn_text("p3"); t2, cb2 = btn_text("p2")
    kb.append([InlineKeyboardButton(text=t1, callback_data=cb1), InlineKeyboardButton(text=t2, callback_data=cb2)])
    # row4
    t1, cb1 = btn_text("p1"); t2, cb2 = btn_text("prem1")
    kb.append([InlineKeyboardButton(text=t1, callback_data=cb1), InlineKeyboardButton(text=t2, callback_data=cb2)])
    # row5
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

def build_ref_keyboard(user_id: int) -> InlineKeyboardMarkup:
    # share link button (opens share chooser)
    me_username = ""
    try:
        # best-effort get bot username
        me = asyncio.get_event_loop().run_until_complete(bot.get_me())
        me_username = me.username or ""
    except Exception:
        me_username = ""
    link = f"https://t.me/{me_username}?start={user_id}" if me_username else f"/start {user_id}"
    # message text for sharing
    share_text = f"🏀 Приглашаю тебя сыграть в баскет за подарки!\n{link}"
    share_url = f"https://t.me/share/url?text={urllib.parse.quote(share_text)}"
    buttons = [
        [InlineKeyboardButton(text="➡️ Отправить другу", url=share_url)],
    ]
    # copy button: if PUBLIC_URL set, open WebApp which auto copies link then closes; else fallback to alert
    if PUBLIC_URL:
        copy_url = f"{PUBLIC_URL.rstrip('/')}/webapp/copy?link={urllib.parse.quote(link, safe='')}"
        buttons.append([InlineKeyboardButton(text="🔗 Скопировать ссылку", web_app=WebAppInfo(url=copy_url))])
    else:
        buttons.append([InlineKeyboardButton(text="🔗 Скопировать ссылку", callback_data=f"copy_ref_{user_id}")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="ref_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

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
            # users: per-user virtual stars, cooldown, spent/earned real stars, plays_total
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
            # referrals
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS referrals (
                    referred_user BIGINT PRIMARY KEY,
                    inviter BIGINT NOT NULL,
                    plays INT NOT NULL DEFAULT 0,
                    rewarded BOOLEAN NOT NULL DEFAULT FALSE
                )
            """)
            # bot_state: stores bot_stars
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value BIGINT NOT NULL
                )
            """)
            # stats
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS stats (
                    count INT NOT NULL,
                    premium BOOLEAN NOT NULL DEFAULT FALSE,
                    wins BIGINT NOT NULL DEFAULT 0,
                    losses BIGINT NOT NULL DEFAULT 0,
                    PRIMARY KEY(count, premium)
                )
            """)
            # ensure bot_stars row exists
            await conn.execute("INSERT INTO bot_state (key, value) VALUES ('bot_stars', 0) ON CONFLICT (key) DO NOTHING")
            # ensure stats rows exist
            for c, prem in [(1, False), (2, False), (3, False), (4, False), (5, False), (6, False), (1, True)]:
                await conn.execute("INSERT INTO stats (count, premium, wins, losses) VALUES ($1, $2, 0, 0) ON CONFLICT (count, premium) DO NOTHING", c, prem)
        log.info("DB initialized")
    except Exception:
        log.exception("DB init failed — falling back to in-memory")
        db_pool = None

# --------------------
# DB helpers (SQL-backed with in-memory fallback)
# --------------------
async def ensure_user(user_id: int) -> Tuple[int, int]:
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
    # fallback
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
                row = await conn.fetchrow(
                    "UPDATE users SET virtual_stars = GREATEST(virtual_stars + $1, 0) WHERE user_id=$2 RETURNING virtual_stars",
                    delta, user_id
                )
                if row:
                    return int(row["virtual_stars"])
                # insert if missing
                await conn.execute("INSERT INTO users (user_id, virtual_stars) VALUES ($1, GREATEST($2,0)) ON CONFLICT (user_id) DO UPDATE SET virtual_stars = GREATEST(users.virtual_stars + $2, 0)", user_id, delta)
                val = await conn.fetchval("SELECT virtual_stars FROM users WHERE user_id=$1", user_id)
                return int(val or 0)
        except Exception:
            log.exception("change_user_virtual DB failed")
    # fallback in-memory
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
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                val = await conn.fetchval("SELECT free_next_at FROM users WHERE user_id=$1", user_id)
                if val is None:
                    await conn.execute("INSERT INTO users (user_id, free_next_at) VALUES ($1, 0) ON CONFLICT DO NOTHING", user_id)
                    return 0
                return int(val)
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

# --------------------
# Referrals (SQL)
# --------------------
async def register_ref_visit(referred_user: int, inviter: int) -> bool:
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                res = await conn.execute("INSERT INTO referrals (referred_user, inviter, plays, rewarded) VALUES ($1, $2, 0, FALSE) ON CONFLICT (referred_user) DO NOTHING", referred_user, inviter)
                if res and res.endswith(" 1"):
                    # notify inviter
                    try:
                        u = await bot.get_chat(referred_user)
                        mention = f"@{u.username}" if getattr(u, "username", None) else (u.first_name or str(referred_user))
                    except Exception:
                        mention = str(referred_user)
                    try:
                        await bot.send_message(inviter, f"🔗 По вашей ссылке перешёл {mention}\nВы получите <b>+3⭐</b> на баланс после того, как он сыграет 5 раз в баскетбол", parse_mode=ParseMode.HTML)
                    except Exception:
                        pass
                    if GROUP_ID:
                        try:
                            await bot.send_message(GROUP_ID, f"{mention} перешёл по реферальной ссылке к {inviter}")
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
    try:
        await bot.send_message(inviter, f"🔗 По вашей ссылке перешёл пользователь. Вы получите +3⭐ после 5 игр")
    except Exception:
        pass
    if GROUP_ID:
        try:
            await bot.send_message(GROUP_ID, f"{referred_user} перешёл по реферальной ссылке к {inviter}")
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
                        try:
                            await bot.send_message(GROUP_ID, f"{referred_user} сыграл пять игр. Теперь у {inviter} — {int(cnt or 0)} верифицированных рефералов")
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
                try:
                    await bot.send_message(GROUP_ID, f"{referred_user} сыграл пять игр. Теперь у {inviter} — 1 верифицированный реферал")
                except Exception:
                    pass

# --------------------
# Stats
# --------------------
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

async def get_stats_summary() -> str:
    lines = []
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                users_count = int(await conn.fetchval("SELECT COUNT(*) FROM users") or 0)
                botstars = int(await conn.fetchval("SELECT value FROM bot_state WHERE key='bot_stars'") or 0)
                rows = await conn.fetch("SELECT count, premium, wins, losses FROM stats ORDER BY count DESC, premium ASC")
                lines.append(f"Пользователей в боте: {users_count}")
                lines.append(f"Реальных звёзд у бота: {botstars}")
                for r in rows:
                    cnt = r["count"]
                    prem = r["premium"]
                    wins = r["wins"]
                    losses = r["losses"]
                    label = f"{cnt}{' (premium)' if prem else ''}"
                    lines.append(f"{label}: выиграли {wins} | проиграли {losses}")
                return "\n".join(lines)
        except Exception:
            log.exception("get_stats DB failed")
    # fallback
    users_count = len(getattr(bot, "_mem_users", {}))
    botstars = getattr(bot, "_mem_bot_stars", 0)
    lines.append(f"Пользователей в боте: {users_count}")
    lines.append(f"Реальных звёзд у бота: {botstars}")
    for (cnt,prem), rec in getattr(bot, "_mem_stats", {}).items():
        lines.append(f"{cnt}{' (premium)' if prem else ''}: выиграли {rec['wins']} | проиграли {rec['losses']}")
    return "\n".join(lines)

# --------------------
# Game flow
# --------------------
async def start_game_flow(chat_id: int, count: int, premium: bool, user_id: int):
    if game_locks.get(chat_id):
        return False, "busy"
    game_locks[chat_id] = True
    try:
        messages = []
        first_send_time = None
        for i in range(count):
            try:
                msg = await bot.send_dice(chat_id, emoji="🏀")
            except Exception:
                log.exception("send_dice failed")
                continue
            if first_send_time is None:
                first_send_time = time.monotonic()
            messages.append(msg)
            await asyncio.sleep(0.5)
        if first_send_time is None:
            first_send_time = time.monotonic()
        elapsed = time.monotonic() - first_send_time
        if elapsed < 5.0:
            await asyncio.sleep(5.0 - elapsed)
        hits = 0
        results = []
        for msg in messages:
            v = getattr(getattr(msg, "dice", None), "value", 0)
            results.append(int(v))
            if int(v) >= 4:
                hits += 1
        await inc_user_plays(user_id, len(results))
        await increment_referred_play(user_id)
        await inc_stats(count, premium, hits == len(results) and len(results) > 0)
        # award gift if win (all hits)
        if len(results) > 0 and hits == len(results):
            if premium:
                gift_choice = random.choice(PREMIUM_GIFTS)
                gift_cost = GIFT_VALUES["premium"]
                bot_stars_now = await get_bot_stars()
                if bot_stars_now < gift_cost:
                    await bot.send_message(chat_id, "⚠️ У бота недостаточно звёзд для покупки премиального подарка.")
                else:
                    await change_bot_stars(-gift_cost)
                    await add_user_earned_real(user_id, gift_cost)
                    gift_text = "🎁 Вы выиграли: " + ("🎁 Премиум-подарок" if gift_choice == "premium_present" else "🌹 Роза")
                    await bot.send_message(chat_id, gift_text)
                    if GROUP_ID:
                        try:
                            spent = int(await (db_pool.fetchval("SELECT spent_real FROM users WHERE user_id=$1", user_id) if db_pool else 0) or 0)
                        except Exception:
                            spent = 0
                        try:
                            await bot.send_message(GROUP_ID, f"<a href=\"tg://user?id={user_id}\">{user_id}</a> выиграл премиальный подарок.\nПотрачено им: {spent}⭐\nЗаработано им: {gift_cost}⭐", parse_mode=ParseMode.HTML)
                        except Exception:
                            pass
            else:
                gift_cost = GIFT_VALUES["normal"]
                bot_stars_now = await get_bot_stars()
                if bot_stars_now < gift_cost:
                    await bot.send_message(chat_id, "⚠️ У бота недостаточно звёзд для покупки подарка.")
                else:
                    await change_bot_stars(-gift_cost)
                    await add_user_earned_real(user_id, gift_cost)
                    await bot.send_message(chat_id, "🎁 Вы выиграли: 🐻 Мишка или 💖 Сердечко")
                    if GROUP_ID:
                        try:
                            spent = int(await (db_pool.fetchval("SELECT spent_real FROM users WHERE user_id=$1", user_id) if db_pool else 0) or 0)
                        except Exception:
                            spent = 0
                        try:
                            await bot.send_message(GROUP_ID, f"<a href=\"tg://user?id={user_id}\">{user_id}</a> выиграл подарок.\nПотрачено им: {spent}⭐\nЗаработано им: {gift_cost}⭐", parse_mode=ParseMode.HTML)
                        except Exception:
                            pass
        # results summary
        text_lines = ["🎯 <b>Результаты бросков:</b>\n"]
        if results:
            for v in results:
                text_lines.append("✅ Попал" if v >= 4 else "❌ Промах")
        else:
            text_lines.append("⚠️ Не удалось отправить ни одного мяча.")
        await bot.send_message(chat_id, "\n".join(text_lines))
        await asyncio.sleep(1)
        await bot.send_message(chat_id, "✅ ПОПАДАНИЕ!" if len(results)>0 and hits==len(results) else "🟡 Не все попали. Попробуем ещё?")
        await asyncio.sleep(1)
        vnow = await get_user_virtual(user_id)
        await bot.send_message(chat_id, START_TEXT_TEMPLATE.format(virtual_stars=vnow), reply_markup=build_main_keyboard(user_id))
        return True, "ok"
    finally:
        game_locks.pop(chat_id, None)

# --------------------
# Handlers: start, menu, ref
# --------------------
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user = message.from_user
    uid = user.id
    await ensure_user(uid)
    # notify group user joined
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

@dp.callback_query(lambda c: c.data == "ref_menu")
async def ref_menu(call: types.CallbackQuery):
    uid = call.from_user.id
    try:
        await call.message.edit_text(REF_TEXT_HTML, reply_markup=build_ref_keyboard(uid), parse_mode=ParseMode.HTML)
    except Exception:
        await call.message.answer(REF_TEXT_HTML, reply_markup=build_ref_keyboard(uid), parse_mode=ParseMode.HTML)

@dp.callback_query(lambda c: c.data and c.data.startswith("copy_ref_"))
async def copy_ref_alert(call: types.CallbackQuery):
    try:
        uid = int(call.data.split("_", 2)[2])
    except Exception:
        uid = call.from_user.id
    try:
        me = await bot.get_me()
        bot_username = me.username or ""
    except Exception:
        bot_username = ""
    link = f"https://t.me/{bot_username}?start={uid}" if bot_username else f"/start {uid}"
    await call.answer(text=f"Скопируйте ссылку: {link}", show_alert=True)

@dp.callback_query(lambda c: c.data == "ref_back")
async def ref_back(call: types.CallbackQuery):
    uid = call.from_user.id
    v = await get_user_virtual(uid)
    try:
        await call.message.edit_text(START_TEXT_TEMPLATE.format(virtual_stars=v), reply_markup=build_main_keyboard(uid), parse_mode=ParseMode.HTML)
    except Exception:
        await call.message.answer(START_TEXT_TEMPLATE.format(virtual_stars=v), reply_markup=build_main_keyboard(uid), parse_mode=ParseMode.HTML)

# --------------------
# Play handling
# --------------------
@dp.callback_query(lambda c: c.data and c.data.startswith("play_"))
async def play_callback(call: types.CallbackQuery):
    chat_id = call.message.chat.id
    if game_locks.get(chat_id):
        await call.answer("Сначала дождитесь окончания текущей игры!", show_alert=False)
        return
    await call.answer()
    user_id = call.from_user.id
    key = call.data.split("_", 1)[1]
    if key not in BUTTONS:
        await call.message.answer("Неизвестная кнопка.")
        return
    cnt, cost, premium, prefix = BUTTONS[key]
    # free button logic
    if cost == 0:
        now = int(time.time())
        free_next = await get_user_free_next(user_id)
        if now < free_next:
            rem = free_next - now
            mins = rem // 60
            secs = rem % 60
            min_word = "минут" if mins != 1 else "минуту"
            sec_word = "секунд" if secs != 1 else "секунду"
            await call.answer(f"🏀 Вы сможете повторно сделать бесплатный бросок через {mins} {min_word} и {secs} {sec_word}", show_alert=False)
            return
        await set_user_free_next(user_id, now + FREE_COOLDOWN)
        await start_game_flow(chat_id, cnt, premium, user_id)
        return
    # paid flow
    vstars = await get_user_virtual(user_id)
    if vstars >= cost:
        await change_user_virtual(user_id, -cost)
        await change_bot_stars(cost)
        await start_game_flow(chat_id, cnt, premium, user_id)
        return
    # insufficient -> immediately create invoice and send to user's private chat
    missing = cost - vstars
    noun = word_form_mяч(cnt)
    title = f"{cnt} {noun}"
    description = "🎁 Получай крутой подарок за попадание 🏀 в кольцо"
    label = f"Играть за {missing}⭐"
    amount = int(missing * STAR_UNIT_MULTIPLIER)
    prices = [LabeledPrice(label=label, amount=amount)]
    # payload includes count and premium flag
    payload = f"buy_and_play:{cnt}:{1 if premium else 0}"
    try:
        await bot.send_invoice(
            chat_id=user_id,
            title=title,
            description=description,
            provider_token=PAYMENTS_PROVIDER_TOKEN,
            currency="XTR",
            prices=prices,
            payload=payload,
            start_parameter="buyandplay"
        )
    except Exception:
        log.exception("send_invoice failed")
        await call.message.answer("Не удалось создать платёж. Проверьте Payments в BotFather.")

# --------------------
# Payment handlers
# --------------------
@dp.pre_checkout_query()
async def precheckout_handler(pre_q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_q.id, ok=True)

@dp.message(F.content_type == ContentType.SUCCESSFUL_PAYMENT)
async def on_successful_payment(message: types.Message):
    sp = message.successful_payment
    payload = getattr(sp, "invoice_payload", "") or ""
    payer_id = message.from_user.id
    # payload pattern: buy_and_play:{count}:{premium_flag} or buy_virtual...
    if payload.startswith("buy_and_play:") or payload.startswith("buy_and_play"):
        try:
            # support both colon and underscore variants
            parts = payload.replace("buy_and_play_", "buy_and_play:").split(":")
            # parts = ['buy_and_play', '{count}', '{premium_flag}', ...]
            if len(parts) >= 3:
                cnt = int(parts[1])
                prem_flag = int(parts[2]) if len(parts) >= 3 else 0
            else:
                # fallback: if payload included missing stars as amount, try parse differently
                cnt = 1
                prem_flag = 0
        except Exception:
            cnt = 1
            prem_flag = 0
        # amount paid in successful_payment: use total_amount (in smallest units) if present
        # For currency XTR we used amount equal to number of stars; get it from successful_payment.total_amount if available
        try:
            paid_amount = int(sp.total_amount or 0)
        except Exception:
            paid_amount = 0
        # credit bot by paid_amount (we assume STAR_UNIT_MULTIPLIER=1)
        if paid_amount > 0:
            await change_bot_stars(paid_amount)
            await add_user_spent_real(payer_id, paid_amount)
        # reset payer virtual to 0 (per requirement)
        await set_user_virtual(payer_id, 0)
        # start the game in payer's chat, with premium if requested
        await start_game_flow(payer_id, cnt, bool(prem_flag), payer_id)
        return
    # handle buy_virtual_... fallback (not main path)
    if payload.startswith("buy_virtual_"):
        try:
            parts = payload.split("_")
            # buy_virtual_{user}_{missing}_{ts}
            target_user = int(parts[2])
            missing = int(parts[3])
            await change_user_virtual(target_user, missing)
            await change_bot_stars(missing)
            await add_user_spent_real(target_user, missing)
            try:
                await bot.send_message(target_user, f"Оплата принята — вам начислено {missing}⭐.")
            except Exception:
                pass
        except Exception:
            pass
    else:
        await message.answer("Платёж принят. Спасибо!")

@dp.callback_query(lambda c: c.data and c.data.startswith("pay_virtual_"))
async def pay_virtual_cb(call: types.CallbackQuery):
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
        await bot.send_invoice(
            chat_id=user.id,
            title=f"{missing} мячей",
            description="Пополнение виртуальных звёзд",
            provider_token=PAYMENTS_PROVIDER_TOKEN,
            currency="XTR",
            prices=prices,
            payload=payload,
            start_parameter="buyvirtual"
        )
    except Exception:
        log.exception("send_invoice failed")
        await call.message.answer("Не удалось создать платёж.")

# --------------------
# Commands: /стат and /баланс (group only)
# --------------------
@dp.message()
async def stat_cmd(message: types.Message):
    text = (message.text or "").strip()
    if not text:
        return
    lowered = text.lower()
    if not (lowered == "/стат" or lowered.split()[0] == "стат"):
        return
    if GROUP_ID is None or message.chat.id != GROUP_ID:
        return
    summary = await get_stats_summary()
    await message.answer(summary)

@dp.message()
async def balans_cmd(message: types.Message):
    text = (message.text or "").strip()
    if not text:
        return
    lowered = text.lower()
    if not (lowered.startswith("/баланс") or lowered.split()[0] == "баланс"):
        return
    if GROUP_ID is None or message.chat.id != GROUP_ID:
        return
    parts = text.split()
    if len(parts) == 1:
        b = await get_bot_stars()
        await message.answer(f"💰 Баланс бота (реальные звёзд): <b>{b}</b>")
        return
    if len(parts) >= 3:
        user_token = parts[1]
        amount_token = parts[2]
        target_id = None
        if user_token.lstrip("-").isdigit():
            target_id = int(user_token)
        elif user_token.startswith("@"):
            try:
                chat = await bot.get_chat(user_token)
                target_id = chat.id
            except Exception:
                await message.answer("Не удалось найти пользователя.")
                return
        else:
            try:
                chat = await bot.get_chat(user_token)
                target_id = chat.id
            except Exception:
                await message.answer("Не удалось найти пользователя.")
                return
        if not amount_token.lstrip("-").isdigit():
            await message.answer("Неверный формат числа.")
            return
        amount = int(amount_token)
        await set_user_virtual(target_id, amount)
        await message.answer(f"Баланс пользователя {target_id} установлен: <b>{amount}⭐</b>")
        return
    await message.answer("Использование: баланс OR баланс <user> <amount> (только в группе)")

# --------------------
# Web app endpoint to auto-copy link
# --------------------
WEBAPP_HTML = r"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Копировать ссылку</title>
  <style>
    body { font-family: Arial, sans-serif; padding: 20px; text-align:center; }
    button { font-size:16px; padding:10px 16px; margin-top:12px; }
    #msg { margin-top:12px; color:#333; }
  </style>
</head>
<body>
  <h2>Копирование ссылки</h2>
  <div id="status">Попытка скопировать ссылку...</div>
  <div id="msg"></div>
  <button id="btn" style="display:none">Закрыть</button>
<script>
function getQueryParam(name){ const params = new URLSearchParams(location.search); return params.get(name); }
async function doCopy(){
  const link = getQueryParam('link') || '';
  if(!link){ document.getElementById('status').innerText='Ссылка не найдена'; document.getElementById('btn').style.display='block'; return; }
  try{
    await navigator.clipboard.writeText(decodeURIComponent(link));
    document.getElementById('status').innerText='Готово — ссылка скопирована в буфер обмена!';
    document.getElementById('msg').innerText=decodeURIComponent(link);
    document.getElementById('btn').style.display='block';
    if(window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.close){
      setTimeout(()=>{ try{ window.Telegram.WebApp.close(); }catch(e){} }, 700);
    }
  }catch(err){
    document.getElementById('status').innerText='Не удалось скопировать автоматически. Скопируйте вручную:';
    document.getElementById('msg').innerText=decodeURIComponent(link);
    document.getElementById('btn').style.display='block';
  }
}
document.getElementById('btn').addEventListener('click', ()=>{
  try{ if(window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.close) window.Telegram.WebApp.close(); } catch(e){}
  window.close();
});
doCopy();
</script>
</body>
</html>
"""

async def webapp_copy_handler(request):
    link = request.query.get('link', '')
    return web.Response(text=WEBAPP_HTML, content_type='text/html')

# --------------------
# Health & web server
# --------------------
async def handle_health(request):
    return web.Response(text="OK")

async def start_web():
    app = web.Application()
    app.add_routes([
        web.get("/", handle_health),
        web.get("/health", handle_health),
        web.get("/webapp/copy", webapp_copy_handler)
    ])
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
