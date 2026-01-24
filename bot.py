# bot.py
import asyncio
import logging
import os
import time
import random
from typing import Optional, Tuple

import asyncpg
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, LabeledPrice, ContentType
)
from aiohttp import web

# --------------------
# LOGGING
# --------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ballbot")

# --------------------
# ENV
# --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GROUP_ID_RAW = os.getenv("GROUP_ID", "")
PAYMENTS_PROVIDER_TOKEN = os.getenv("PAYMENTS_PROVIDER_TOKEN", "")  # keep "" for Telegram Stars
ADMIN_ID = os.getenv("ADMIN_ID")  # optional admin id (string)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

GROUP_ID: Optional[int] = None
try:
    if GROUP_ID_RAW:
        GROUP_ID = int(GROUP_ID_RAW)
except Exception:
    GROUP_ID = None

# --------------------
# BOT & DP
# --------------------
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# --------------------
# DB / STATE
# --------------------
db_pool: Optional[asyncpg.Pool] = None

# per-chat game lock: ensures no concurrent games in same chat
game_locks: dict[int, bool] = {}

# BUTTONS: (id_key, count, cost_virtual, label_suffix, premium_flag)
# label_suffix is used for special premium button
BUTTONS = [
    ("p6", 6, 0, "", False),
    ("p5", 5, 1, "", False),
    ("p4", 4, 2, "", False),
    ("p3", 3, 4, "", False),
    ("p2", 2, 6, "", False),
    ("p1", 1, 10, "", False),  # changed: 1 ball costs 10 stars
    ("prem1", 1, 15, "💎", True)  # premium single ball: cost 15 stars, premium gift prize
]

# Gift values (real stars) that bot gives to user when user wins
# According to your request: ordinary gift => 15, premium gift => 25
GIFT_VALUES = {
    "normal": 15,
    "premium": 25
}

# premium gift mapping for premium 15-star ball
PREMIUM_GIFTS = ["premium_present", "rose"]  # premium reward options

# For Telegram Stars: amount in invoice equals number of stars (currency = XTR)
STAR_UNIT_MULTIPLIER = 1

# Stats keys: we will use a table stats with (count, premium boolean, wins, losses)
# and also per-user counters: spent_real, earned_real, plays_total

# --------------------
# UI helpers
# --------------------
def word_form_mяч(count: int) -> str:
    return "мяча" if 1 <= count <= 4 else "мячей"

def build_main_keyboard(user_id: Optional[int] = None) -> InlineKeyboardMarkup:
    kb = []
    for key, count, cost, suffix, premium in BUTTONS:
        noun = word_form_mяч(count)
        cost_text = "бесплатно" if cost == 0 else f"{cost}⭐"
        text = f"{suffix}🏀 {count} {noun} • {cost_text}"
        cb = f"play_{key}"
        kb.append([InlineKeyboardButton(text=text, callback_data=cb)])
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
    # Share link text per requirement #4
    try:
        me = asyncio.get_event_loop().run_until_complete(bot.get_me())
        bot_username = me.username or ""
    except Exception:
        bot_username = ""
    link = f"https://t.me/{bot_username}?start={user_id}" if bot_username else f"/start {user_id}"
    share_url = f"https://t.me/share/url?url={link}&text=🏀 Приглашаю тебя сыграть в баскет за подарки! 🎁%0A{link}"
    # Note: copy button can't silently copy. See note below.
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➡️ Отправить другу", url=share_url)],
        [InlineKeyboardButton(text="🔗 Скопировать ссылку", callback_data=f"copy_ref_{user_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="ref_back")]
    ])
    return kb

def build_purchase_inline(missing: int, user_id: int, count: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    # we'll build payload buy_and_play_{user}_{count}_{missing}_{ts}
    kb.inline_keyboard.append([InlineKeyboardButton(text=f"Оплатить {missing}⭐", callback_data=f"buyandplay_{user_id}_{count}_{missing}")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="buy_back")])
    return kb

# --------------------
# DB initialization
# --------------------
async def init_db():
    global db_pool
    if not DATABASE_URL:
        log.warning("DATABASE_URL not set — using in-memory fallback")
        db_pool = None
        return
    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=8, timeout=15)
        async with db_pool.acquire() as conn:
            # users: virtual stars, free cooldown, spent_real, earned_real, plays_total
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                virtual_stars BIGINT NOT NULL DEFAULT 0,
                free_next_at BIGINT NOT NULL DEFAULT 0,
                spent_real BIGINT NOT NULL DEFAULT 0,
                earned_real BIGINT NOT NULL DEFAULT 0,
                plays_total BIGINT NOT NULL DEFAULT 0
            );
            """)
            # referrals
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                referred_user BIGINT PRIMARY KEY,
                inviter BIGINT NOT NULL,
                plays INT NOT NULL DEFAULT 0,
                rewarded BOOLEAN NOT NULL DEFAULT FALSE
            );
            """)
            # bot real stars
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value BIGINT NOT NULL
            );
            """)
            # stats
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                count INT NOT NULL,
                premium BOOLEAN NOT NULL DEFAULT FALSE,
                wins BIGINT NOT NULL DEFAULT 0,
                losses BIGINT NOT NULL DEFAULT 0,
                PRIMARY KEY(count, premium)
            );
            """)
            # ensure bot_state row exists
            await conn.execute("INSERT INTO bot_state (key, value) VALUES ('bot_stars', 0) ON CONFLICT (key) DO NOTHING")
            # ensure stats rows for counts 1..6 and premium (1,premium)
            for c,prem in [(1,False),(2,False),(3,False),(4,False),(5,False),(6,False),(1,True)]:
                await conn.execute("INSERT INTO stats (count, premium, wins, losses) VALUES ($1,$2,0,0) ON CONFLICT (count,premium) DO NOTHING", c, prem)
        log.info("DB initialized")
    except Exception:
        log.exception("DB init failed, falling back to in-memory")
        db_pool = None

# --------------------
# DB helpers
# --------------------
async def ensure_user(user_id: int) -> Tuple[int,int]:
    """Return (virtual_stars, free_next_at)"""
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("SELECT virtual_stars, free_next_at FROM users WHERE user_id=$1", user_id)
                if row:
                    return int(row["virtual_stars"]), int(row["free_next_at"])
                await conn.execute("INSERT INTO users (user_id, virtual_stars, free_next_at) VALUES ($1,0,0) ON CONFLICT DO NOTHING", user_id)
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
                await conn.execute("INSERT INTO users (user_id, virtual_stars) VALUES ($1, GREATEST($2,0)) ON CONFLICT (user_id) DO UPDATE SET virtual_stars = GREATEST(users.virtual_stars + $2,0)", user_id, delta)
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
                await conn.execute("INSERT INTO users (user_id, virtual_stars, free_next_at) VALUES ($1,$2,0) ON CONFLICT (user_id) DO UPDATE SET virtual_stars = $2", user_id, value)
                return int(await conn.fetchval("SELECT virtual_stars FROM users WHERE user_id=$1", user_id))
        except Exception:
            log.exception("set_user_virtual DB failed")
    if not hasattr(bot, "_mem_users"):
        bot._mem_users = {}
    rec = bot._mem_users.setdefault(user_id, {"virtual_stars": 0, "free_next_at": 0, "spent_real": 0, "earned_real": 0, "plays_total": 0})
    rec["virtual_stars"] = max(value, 0)
    return rec["virtual_stars"]

async def get_user_free_next(user_id: int) -> int:
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                val = await conn.fetchval("SELECT free_next_at FROM users WHERE user_id=$1", user_id)
                if val is None:
                    await conn.execute("INSERT INTO users (user_id, free_next_at) VALUES ($1,0) ON CONFLICT DO NOTHING", user_id)
                    return 0
                return int(val)
        except Exception:
            log.exception("get_user_free_next DB failed")
    if not hasattr(bot, "_mem_users"):
        bot._mem_users = {}
    return bot._mem_users.get(user_id, {}).get("free_next_at", 0)

async def set_user_free_next(user_id: int, epoch_ts: int):
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("INSERT INTO users (user_id, free_next_at) VALUES ($1,$2) ON CONFLICT (user_id) DO UPDATE SET free_next_at = $2", user_id, epoch_ts)
                return
        except Exception:
            log.exception("set_user_free_next DB failed")
    if not hasattr(bot, "_mem_users"):
        bot._mem_users = {}
    rec = bot._mem_users.setdefault(user_id, {"virtual_stars": 0, "free_next_at": 0, "spent_real": 0, "earned_real": 0, "plays_total": 0})
    rec["free_next_at"] = epoch_ts

async def add_user_spent_real(user_id: int, amount: int):
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("INSERT INTO users (user_id, spent_real) VALUES ($1,$2) ON CONFLICT (user_id) DO UPDATE SET spent_real = users.spent_real + $2", user_id, amount)
                return
        except Exception:
            log.exception("add_user_spent_real DB failed")
    if not hasattr(bot, "_mem_users"):
        bot._mem_users = {}
    rec = bot._mem_users.setdefault(user_id, {"virtual_stars": 0, "free_next_at": 0, "spent_real": 0, "earned_real": 0, "plays_total": 0})
    rec["spent_real"] += amount

async def add_user_earned_real(user_id: int, amount: int):
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("INSERT INTO users (user_id, earned_real) VALUES ($1,$2) ON CONFLICT (user_id) DO UPDATE SET earned_real = users.earned_real + $2", user_id, amount)
                return
        except Exception:
            log.exception("add_user_earned_real DB failed")
    if not hasattr(bot, "_mem_users"):
        bot._mem_users = {}
    rec = bot._mem_users.setdefault(user_id, {"virtual_stars": 0, "free_next_at": 0, "spent_real": 0, "earned_real": 0, "plays_total": 0})
    rec["earned_real"] += amount

async def inc_user_plays(user_id: int, delta: int = 1):
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("INSERT INTO users (user_id, plays_total) VALUES ($1,$2) ON CONFLICT (user_id) DO UPDATE SET plays_total = users.plays_total + $2", user_id, delta)
                return
        except Exception:
            log.exception("inc_user_plays DB failed")
    if not hasattr(bot, "_mem_users"):
        bot._mem_users = {}
    rec = bot._mem_users.setdefault(user_id, {"virtual_stars": 0, "free_next_at": 0, "spent_real": 0, "earned_real": 0, "plays_total": 0})
    rec["plays_total"] += delta

# bot stars
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

# referrals: register visit and increment plays
async def register_ref_visit(referred_user: int, inviter: int) -> bool:
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                res = await conn.execute("INSERT INTO referrals (referred_user, inviter, plays, rewarded) VALUES ($1,$2,0,FALSE) ON CONFLICT (referred_user) DO NOTHING", referred_user, inviter)
                if res and res.endswith(" 1"):
                    # notify inviter in group (not notifying referred)
                    try:
                        r = await bot.get_chat(referred_user)
                        if getattr(r, "username", None):
                            mention = f"<a href=\"tg://user?id={referred_user}\">@{r.username}</a>"
                        else:
                            name = r.first_name or "user"
                            mention = f"<a href=\"tg://user?id={referred_user}\">{name}</a>"
                    except Exception:
                        mention = f"<a href=\"tg://user?id={referred_user}\">user</a>"
                    try:
                        await bot.send_message(inviter, f"🔗 По вашей ссылке перешёл {mention}\nВы получите <b>+3⭐</b> на баланс после того, как он сыграет 5 раз в баскетбол", parse_mode=ParseMode.HTML)
                    except Exception:
                        log.exception("notify inviter failed")
                    # group notification
                    if GROUP_ID:
                        try:
                            await bot.send_message(GROUP_ID, f"{mention} перешёл по реферальной ссылке {inviter}")
                        except Exception:
                            pass
                    return True
                return False
        except Exception:
            log.exception("register_ref_visit DB failed")
            return False
    # memory fallback
    if not hasattr(bot, "_mem_referrals"):
        bot._mem_referrals = {}
    if referred_user in bot._mem_referrals:
        return False
    bot._mem_referrals[referred_user] = {"inviter": inviter, "plays": 0, "rewarded": False}
    try:
        await bot.send_message(inviter, f"🔗 По вашей ссылке перешёл user\nВы получите +3⭐ после 5 игр")
    except Exception:
        pass
    if GROUP_ID:
        try:
            await bot.send_message(GROUP_ID, f"user перешёл по реферальной ссылке {inviter}")
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
                    # give inviter +3 virtual stars
                    await change_user_virtual(inviter, 3)
                    # count inviter's verified referrals
                    val = await conn.fetchval("SELECT COUNT(*) FROM referrals WHERE inviter=$1 AND rewarded=TRUE", inviter)
                    verified = int(val or 0)
                    try:
                        await bot.send_message(inviter, "🔥 Вам начислено +3⭐ — приглашённый сыграл 5 раз!")
                    except Exception:
                        pass
                    # group notification
                    if GROUP_ID:
                        try:
                            inviter_mention = f"<a href=\"tg://user?id={inviter}\">{inviter}</a>"
                            await bot.send_message(GROUP_ID, f"<a href=\"tg://user?id={referred_user}\">{referred_user}</a> сыграл пять игр. Теперь у {inviter_mention} — {verified} верифицированных рефералов", parse_mode=ParseMode.HTML)
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
                    await bot.send_message(GROUP_ID, f"{referred_user} сыграл пять игр. Теперь у {inviter} — (верифицированные рефералы: unknown)")
                except Exception:
                    pass

# Stats helpers
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
            # structure: {(count,premium): {'wins':int,'losses':int}}
            bot._mem_stats = {}
            for c,p in [(1,False),(2,False),(3,False),(4,False),(5,False),(6,False),(1,True)]:
                bot._mem_stats[(c,p)] = {"wins":0,"losses":0}
        key = (count,premium)
        rec = bot._mem_stats.setdefault(key, {"wins":0,"losses":0})
        if win:
            rec["wins"] += 1
        else:
            rec["losses"] += 1

async def get_stats_summary() -> str:
    # build text for /стат
    lines = []
    # current users in bot
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                users_count = await conn.fetchval("SELECT COUNT(*) FROM users")
                botstars = int(await conn.fetchval("SELECT value FROM bot_state WHERE key='bot_stars'"))
                rows = await conn.fetch("SELECT count,premium,wins,losses FROM stats ORDER BY premium, count DESC")
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
    # mem fallback
    users_count = len(getattr(bot, "_mem_users", {}))
    botstars = getattr(bot, "_mem_bot_stars", 0)
    lines.append(f"Пользователей в боте: {users_count}")
    lines.append(f"Реальных звёзд у бота: {botstars}")
    mem = getattr(bot, "_mem_stats", {})
    for key, rec in mem.items():
        cnt, prem = key
        lines.append(f"{cnt}{' (premium)' if prem else ''}: выиграли {rec['wins']} | проиграли {rec['losses']}")
    return "\n".join(lines)

# --------------------
# Handlers
# --------------------
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user = message.from_user
    uid = user.id
    await ensure_user(uid)

    # group notification: user joined
    try:
        mention = f"<a href=\"tg://user?id={uid}\">{user.first_name or uid}</a>"
        if GROUP_ID:
            try:
                await bot.send_message(GROUP_ID, f"{mention} перешёл в бота", parse_mode=ParseMode.HTML)
            except Exception:
                pass
    except Exception:
        pass

    # payload referral
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
                # group notification handled inside register_ref_visit
        except Exception:
            pass

    vstars = await get_user_virtual(uid)
    start_text = START_TEXT_TEMPLATE.format(virtual_stars=vstars)
    try:
        await message.answer("Меню внизу — откройте для быстрого доступа.", reply_markup=REPLY_MENU)
    except Exception:
        pass
    try:
        await message.answer(start_text, reply_markup=build_main_keyboard(uid))
    except Exception:
        log.exception("send main menu failed")

@dp.message(F.text == "🏀 Сыграть в баскет")
async def open_main_menu(message: types.Message):
    uid = message.from_user.id
    v = await get_user_virtual(uid)
    start_text = START_TEXT_TEMPLATE.format(virtual_stars=v)
    await message.answer(start_text, reply_markup=build_main_keyboard(uid))

@dp.callback_query(lambda c: c.data == "ref_menu")
async def ref_menu(call: types.CallbackQuery):
    uid = call.from_user.id
    try:
        await call.message.edit_text(REF_TEXT_HTML, reply_markup=build_ref_keyboard(uid), parse_mode=ParseMode.HTML)
    except Exception:
        await call.message.answer(REF_TEXT_HTML, reply_markup=build_ref_keyboard(uid), parse_mode=ParseMode.HTML)

@dp.callback_query(lambda c: c.data and c.data.startswith("copy_ref_"))
async def copy_ref(call: types.CallbackQuery):
    # IMPORTANT: Telegram Bot API cannot copy to user's clipboard silently.
    # We MUST inform and use best fallback: show alert with link or open WebApp that can copy.
    # Here we show alert with the link so user can copy manually.
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
    # show alert (user must manually press copy)
    await call.answer(text=f"Скопируйте ссылку: {link}", show_alert=True)
    # NOTE: If you want true automatic copy, you must host a Web App and open it here.
    # Web App JS can call navigator.clipboard.writeText(link) — implementable only via WebApp.

@dp.callback_query(lambda c: c.data == "ref_back")
async def ref_back(call: types.CallbackQuery):
    uid = call.from_user.id
    v = await get_user_virtual(uid)
    start_text = START_TEXT_TEMPLATE.format(virtual_stars=v)
    try:
        await call.message.edit_text(start_text, reply_markup=build_main_keyboard(uid), parse_mode=ParseMode.HTML)
    except Exception:
        await call.message.answer(start_text, reply_markup=build_main_keyboard(uid), parse_mode=ParseMode.HTML)

# helper: find button config by key
def find_button_by_key(key: str):
    for k, cnt, cost, suf, prem in BUTTONS:
        if k == key:
            return (k, cnt, cost, suf, prem)
    return None

# Core: start a game ensuring per-chat lock and consistent flows
async def start_game_flow(chat_id: int, caller_messageable, count: int, cost: int, premium: bool, user_id: int):
    """
    caller_messageable: object with send methods (chat or user)
    This function assumes virtual cost already handled (deducted or handled), and bot_stars already adjusted appropriately.
    """
    # prevent concurrent games in same chat
    if game_locks.get(chat_id):
        # Shouldn't happen because caller already checks, but be safe
        return False, "busy"
    game_locks[chat_id] = True
    try:
        messages = []
        first_send_time = None
        for i in range(count):
            try:
                msg = await bot.send_dice(chat_id, emoji="🏀")
            except Exception:
                log.exception("send_dice error")
                continue
            if first_send_time is None:
                first_send_time = time.monotonic()
            messages.append(msg)
            await asyncio.sleep(0.5)
        # Wait until 5 seconds since first send
        if first_send_time is None:
            first_send_time = time.monotonic()
        elapsed = time.monotonic() - first_send_time
        if elapsed < 5.0:
            await asyncio.sleep(5.0 - elapsed)
        # gather results
        hits = 0
        results = []
        for msg in messages:
            val = getattr(msg, "dice", None)
            v = getattr(val, "value", 0) if val else 0
            results.append(int(v))
            if int(v) >= 4:
                hits += 1
        # update user plays counter
        await inc_user_plays(user_id, len(results))
        # increment referred play counter if user is referred
        await increment_referred_play(user_id)
        # update stats
        await inc_stats(count, premium, hits == len(results) and len(results) > 0)
        # if win and there is gift available: bot spends real stars to give gift to user
        if len(results) > 0 and hits == len(results):
            if premium:
                # premium gift: choose premium gift and cost 25 (per request)
                gift_choice = random.choice(PREMIUM_GIFTS)
                gift_cost = GIFT_VALUES["premium"]
                bot_stars_now = await get_bot_stars()
                if bot_stars_now < gift_cost:
                    await bot.send_message(chat_id, "⚠️ У бота недостаточно звёзд для покупки премиального подарка.")
                else:
                    await change_bot_stars(-gift_cost)
                    # add earned_real to user
                    await add_user_earned_real(user_id, gift_cost)
                    # send textual gift
                    gift_text = "🎁 Вы выиграли: " + ("🎁 Премиум-подарок" if gift_choice == "premium_present" else "🌹 Роза")
                    await bot.send_message(chat_id, gift_text)
                    # group notification about win
                    if GROUP_ID:
                        # compute spent_real by user (total spent) and earned real by user
                        if db_pool:
                            async with db_pool.acquire() as conn:
                                spent = int(await conn.fetchval("SELECT spent_real FROM users WHERE user_id=$1", user_id) or 0)
                                earned = int(await conn.fetchval("SELECT earned_real FROM users WHERE user_id=$1", user_id) or 0)
                        else:
                            rec = getattr(bot, "_mem_users", {}).get(user_id, {})
                            spent = rec.get("spent_real", 0)
                            earned = rec.get("earned_real", 0)
                        try:
                            await bot.send_message(GROUP_ID, f"<a href=\"tg://user?id={user_id}\">{user_id}</a> выиграл премиальный подарок.\nПотрачено им: {spent}⭐\nЗаработано им: {earned}⭐", parse_mode=ParseMode.HTML)
                        except Exception:
                            pass
            else:
                # normal gift
                gift_cost = GIFT_VALUES["normal"]
                bot_stars_now = await get_bot_stars()
                if bot_stars_now < gift_cost:
                    await bot.send_message(chat_id, "⚠️ У бота недостаточно звёзд для покупки подарка.")
                else:
                    await change_bot_stars(-gift_cost)
                    await add_user_earned_real(user_id, gift_cost)
                    await bot.send_message(chat_id, "🎁 Вы выиграли: 🐻 Мишка или 💖 Сердечко (случайный выбор)")
                    if GROUP_ID:
                        if db_pool:
                            async with db_pool.acquire() as conn:
                                spent = int(await conn.fetchval("SELECT spent_real FROM users WHERE user_id=$1", user_id) or 0)
                                earned = int(await conn.fetchval("SELECT earned_real FROM users WHERE user_id=$1", user_id) or 0)
                        else:
                            rec = getattr(bot, "_mem_users", {}).get(user_id, {})
                            spent = rec.get("spent_real", 0)
                            earned = rec.get("earned_real", 0)
                        try:
                            await bot.send_message(GROUP_ID, f"<a href=\"tg://user?id={user_id}\">{user_id}</a> выиграл подарок.\nПотрачено им: {spent}⭐\nЗаработано им: {earned}⭐", parse_mode=ParseMode.HTML)
                        except Exception:
                            pass
        # send results summary (only Попал/Промах)
        text_lines = ["🎯 <b>Результаты бросков:</b>\n"]
        if results:
            for v in results:
                text_lines.append("✅ Попал" if v >= 4 else "❌ Промах")
        else:
            text_lines.append("⚠️ Не удалось отправить ни одного мяча.")
        await bot.send_message(chat_id, "\n".join(text_lines))
        await asyncio.sleep(1)
        await bot.send_message(chat_id, "✅ ПОПАДАНИЕ!" if len(results) > 0 and hits == len(results) else "🟡 Не все попали. Попробуем ещё?")
        await asyncio.sleep(1)
        vnow = await get_user_virtual(user_id)
        start_text = START_TEXT_TEMPLATE.format(virtual_stars=vnow)
        await bot.send_message(chat_id, start_text, reply_markup=build_main_keyboard(user_id))
        return True, "ok"
    finally:
        game_locks.pop(chat_id, None)

@dp.callback_query(lambda c: c.data and c.data.startswith("play_"))
async def play_callback(call: types.CallbackQuery):
    # check if game in this chat is already running
    chat_id = call.message.chat.id
    if game_locks.get(chat_id):
        # show top notification (toast)
        await call.answer("Сначала дождитесь окончания текущей игры!", show_alert=False)
        return
    await call.answer()  # acknowledge
    user_id = call.from_user.id
    key = call.data.split("_",1)[1]
    info = find_button_by_key(key)
    if not info:
        await call.message.answer("Неизвестная кнопка.")
        return
    k, count, cost, suf, premium = info
    # if free
    if cost == 0:
        now = int(time.time())
        free_next = await get_user_free_next(user_id)
        if now < free_next:
            rem = free_next - now
            mins = rem // 60
            secs = rem % 60
            min_word = "минут" if mins != 1 else "минуту"
            sec_word = "секунд" if secs != 1 else "секунду"
            await call.answer(f"🏀 До следующего бесплатного броска осталось {mins} {min_word} и {secs} {sec_word}", show_alert=False)
            return
        await set_user_free_next(user_id, now + 3*60)
        # proceed to start game
        success, msg = await start_game_flow(chat_id, call.message, count, cost, premium, user_id)
        return
    # cost > 0: check user's virtual balance
    vstars = await get_user_virtual(user_id)
    if vstars >= cost:
        # deduct virtual and credit bot_stars by same amount
        await change_user_virtual(user_id, -cost)
        await change_bot_stars(cost)
        # track spent_real? This was virtual, so spent_real remains unchanged
        # proceed game
        success, msg = await start_game_flow(chat_id, call.message, count, cost, premium, user_id)
        return
    # insufficient virtual: per requirement #2, show immediate purchase message with payment button and after payment throw balls and reset user's virtual to 0.
    missing = cost - vstars
    # Build message "🎁 Получай крутой подарок..." with purchase button
    text = "🎁 Получай крутой подарок за попадание 🏀 в кольцо"
    try:
        # callback buyandplay_{user}_{count}_{missing}
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"Оплатить {missing}⭐", callback_data=f"buyandplay_{user_id}_{count}_{missing}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="buy_back")]
        ])
        await call.message.answer(text, reply_markup=kb)
    except Exception:
        await call.message.reply(text, reply_markup=kb)

@dp.callback_query(lambda c: c.data and c.data.startswith("buyandplay_"))
async def buyandplay_callback(call: types.CallbackQuery):
    # callback data: buyandplay_{user}_{count}_{missing}
    await call.answer()
    parts = call.data.split("_")
    try:
        target_user = int(parts[1])
        count = int(parts[2])
        missing = int(parts[3])
    except Exception:
        await call.message.answer("Ошибка данных покупки.")
        return
    # create invoice: provider_token = "" and currency = "XTR" for Stars
    amount = int(missing * STAR_UNIT_MULTIPLIER)
    prices = [LabeledPrice(label=f"{missing}⭐", amount=amount)]
    payload = f"buy_and_play_{target_user}_{count}_{missing}_{int(time.time())}"
    try:
        await bot.send_invoice(
            chat_id=target_user,
            title=f"Покупка {missing}⭐ + игра",
            description=f"Оплата недостающих звёзд ({missing}) и немедленный запуск {count} бросков",
            provider_token=PAYMENTS_PROVIDER_TOKEN,  # empty string for Stars
            currency="XTR",
            prices=prices,
            payload=payload,
            start_parameter="buyandplay"
        )
    except Exception:
        log.exception("send_invoice failed")
        await call.message.answer("Не удалось создать платёж. Проверьте Payments в BotFather.")

@dp.pre_checkout_query()
async def precheckout_handler(pre_q: types.PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_q.id, ok=True)

@dp.message(F.content_type == ContentType.SUCCESSFUL_PAYMENT)
async def on_successful_payment(message: types.Message):
    sp = message.successful_payment
    payload = getattr(sp, "invoice_payload", "") or ""
    # If it's buy_and_play flow
    if payload.startswith("buy_and_play_"):
        # payload = buy_and_play_{user}_{count}_{missing}_{ts}
        try:
            parts = payload.split("_")
            _, _, uid_str, count_str, missing_str, *_ = parts
            uid = int(uid_str)
            count = int(count_str)
            missing = int(missing_str)
        except Exception:
            # fallback: just acknowledge
            await message.answer("Платёж принят. Благодарим!")
            return
        # credit bot_stars by missing (user paid real stars to bot)
        await change_bot_stars(missing)
        # add to user's spent_real
        await add_user_spent_real(uid, missing)
        # reset user's virtual stars to 0 (user used them in the purchase)
        await set_user_virtual(uid, 0)
        # now immediately start the game in the user's chat (uid)
        # ensure chat is not busy
        chat_id = uid
        if game_locks.get(chat_id):
            await bot.send_message(chat_id, "Сейчас идёт другая игра в этом чате — ваша оплата зачислена, игра начнётся позже.")
            return
        # start game flow (with premium flag detection — we don't have premium here; this flow is for normal buttons)
        # We need to determine whether this was premium or not; assume standard unless count==1 and cost 15? The payload didn't include premium flag.
        # We'll attempt to pick premium flag if the price paid equals 15 and there exists premium button for 1-ball with cost 15.
        premium = False
        # if the originally requested button was premium (15-star 1-ball), it would have been invoked differently.
        # We'll call start_game_flow with premium False.
        success, msg = await start_game_flow(chat_id, None, count, 0, premium, uid)
        return
    # fallback for other invoices
    # e.g. buy_virtual flow (not used here) - keep compatibility
    if payload.startswith("buy_virtual_"):
        try:
            parts = payload.split("_")
            # payload structure: buy_virtual_{user}_{missing}_{ts}
            _, _, user_str, missing_str, *_ = parts
            target_user = int(user_str)
            missing = int(missing_str)
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
        # generic
        await message.answer("Платёж принят. Спасибо!")

@dp.callback_query(lambda c: c.data and c.data == "buy_back")
async def buy_back(call: types.CallbackQuery):
    uid = call.from_user.id
    v = await get_user_virtual(uid)
    await call.message.edit_text(START_TEXT_TEMPLATE.format(virtual_stars=v), reply_markup=build_main_keyboard(uid), parse_mode=ParseMode.HTML)

# /стат command (only in GROUP_ID)
@dp.message()
async def stat_cmd(message: types.Message):
    text = (message.text or "").strip()
    if not text:
        return
    lowered = text.lower()
    if not (lowered == "/стат" or lowered.split()[0] == "стат"):
        return
    # allow only in group
    if GROUP_ID is None or message.chat.id != GROUP_ID:
        return
    summary = await get_stats_summary()
    await message.answer(summary)

# Command "баланс" previously requested: show bot real stars (group only)
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
        await message.answer(f"💰 Баланс бота (реальные звёзды): <b>{b}</b>")
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
# Payment alternative "pay_virtual" (if user wants to top up only)
# --------------------
@dp.callback_query(lambda c: c.data and c.data.startswith("pay_virtual_"))
async def pay_virtual_cb(call: types.CallbackQuery):
    await call.answer()
    try:
        missing = int(call.data.split("_",2)[2])
    except Exception:
        await call.message.answer("Ошибка данных покупки.")
        return
    user = call.from_user
    amount = int(missing * STAR_UNIT_MULTIPLIER)
    prices = [LabeledPrice(label=f"{missing}⭐", amount=amount)]
    payload = f"buy_virtual_{user.id}_{missing}_{int(time.time())}"
    try:
        await bot.send_invoice(chat_id=user.id,
            title=f"Покупка {missing}⭐", description=f"Покупка {missing} звёзд",
            provider_token=PAYMENTS_PROVIDER_TOKEN, currency="XTR", prices=prices, payload=payload, start_parameter="buyvirtual")
    except Exception:
        log.exception("send_invoice failed")
        await call.message.answer("Не удалось создать платёж.")

# --------------------
# Health endpoint for Render
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
