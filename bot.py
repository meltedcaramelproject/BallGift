import asyncio
import logging
import os
import time
from typing import Optional

import asyncpg
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiohttp import web

# --------------------
# ЛОГИ
# --------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# --------------------
# ENV
# --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GROUP_ID_RAW = os.getenv("GROUP_ID", "0")

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
# DB
# --------------------
db_pool: Optional[asyncpg.Pool] = None
bot_balance: int = 0

# --------------------
# UI / КНОПКИ и конфиг
# --------------------
# Формат: (кол-во мячей, стоимость/звёзды)
# Обновлённые цены: 3 -> 4⭐, 2 -> 6⭐
BUTTONS = [
    (6, 0),  # бесплатно (использует free_throws у пользователя)
    (5, 1),
    (4, 2),
    (3, 4),  # changed to 4⭐
    (2, 6),  # changed to 6⭐
    (1, 8),
]

def word_form_mяч(count: int) -> str:
    # для 1..4 -> "мяча", для 5..6 -> "мячей"
    if 1 <= count <= 4:
        return "мяча"
    return "мячей"

def build_main_keyboard():
    kb = []
    for count, cost in BUTTONS:
        if cost == 0:
            cost_text = "бесплатно"
        else:
            cost_text = f"{cost}⭐"
        noun = word_form_mяч(count)
        text = f"🏀 {count} {noun} • {cost_text}"
        cb = f"play_{count}_{cost}"
        kb.append([InlineKeyboardButton(text=text, callback_data=cb)])
    kb.append([InlineKeyboardButton(text="👥 +Бросок за друга", callback_data="ref_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

START_TEXT_TEMPLATE = (
    "<b>🏀 БАСКЕТБОЛ ЗА ПОДАРКИ 🏀</b>\n\n"
    "🎯 ПОПАДИ мячом в кольцо каждым броском — и получи КРУТОЙ ПОДАРОК 🎁\n\n"
    "🔥 Бесплатных бросков: <b>{free_throws}</b>"
)

REF_TEXT_TEMPLATE = (
    "<b>👥 +БРОСОК ЗА ДРУГА 👥</b>\n\n"
    "Добавьте друга по этой ссылке чтобы получить +1 бесплатный бросок\n\n"
    "<b>👇 Ваша ссылка 👇</b>\n"
    "<code>{link}</code>"
)

REF_BACK_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="◀️ Назад", callback_data="ref_back")]
])

REPLY_MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="🏀 Сыграть в баскет")]],
    resize_keyboard=True,
    one_time_keyboard=False
)

# --------------------
# DB: инициализация, схемы
# --------------------
async def init_db():
    global db_pool, bot_balance
    if not DATABASE_URL:
        log.warning("DATABASE_URL not set — бот будет работать без БД (in-memory users & balance).")
        return

    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, timeout=15)
        async with db_pool.acquire() as conn:
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value BIGINT NOT NULL
            );
            """)
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                free_throws INT NOT NULL DEFAULT 1
            );
            """)
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                referred_user BIGINT PRIMARY KEY,
                inviter BIGINT NOT NULL
            );
            """)
            row = await conn.fetchrow("SELECT value FROM bot_state WHERE key = 'balance'")
            if row:
                bot_balance = int(row["value"])
            else:
                await conn.execute("INSERT INTO bot_state (key, value) VALUES ('balance', 0) ON CONFLICT (key) DO NOTHING")
                bot_balance = 0
        log.info(f"✅ DB CONNECTED. Balance = {bot_balance}")
    except Exception:
        log.exception("❌ DB INIT FAILED — falling back to in-memory.")
        db_pool = None

# --------------------
# Пользовательские операции
# --------------------
async def ensure_user(user_id: int):
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("SELECT free_throws FROM users WHERE user_id = $1", user_id)
                if row:
                    return int(row["free_throws"])
                await conn.execute("INSERT INTO users (user_id, free_throws) VALUES ($1, 1) ON CONFLICT DO NOTHING", user_id)
                return 1
        except Exception:
            log.exception("ensure_user DB failed")
            return 1
    else:
        if not hasattr(bot, "_in_memory_users"):
            bot._in_memory_users = {}
        if user_id not in bot._in_memory_users:
            bot._in_memory_users[user_id] = 1
        return bot._in_memory_users[user_id]

async def change_user_free_throws(user_id: int, delta: int):
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("UPDATE users SET free_throws = free_throws + $1 WHERE user_id = $2 RETURNING free_throws", delta, user_id)
                if row and row.get("free_throws") is not None:
                    val = int(row["free_throws"])
                    return max(val, 0)
                await conn.execute("INSERT INTO users (user_id, free_throws) VALUES ($1, 1) ON CONFLICT (user_id) DO NOTHING", user_id)
                row2 = await conn.fetchrow("UPDATE users SET free_throws = free_throws + $1 WHERE user_id = $2 RETURNING free_throws", delta, user_id)
                if row2:
                    return max(int(row2["free_throws"]), 0)
                return 1
        except Exception:
            log.exception("change_user_free_throws DB failed")
    # in-memory fallback
    if not hasattr(bot, "_in_memory_users"):
        bot._in_memory_users = {}
    cur = bot._in_memory_users.get(user_id, 1)
    cur += delta
    if cur < 0:
        cur = 0
    bot._in_memory_users[user_id] = cur
    return cur

async def get_user_free_throws(user_id: int) -> int:
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("SELECT free_throws FROM users WHERE user_id = $1", user_id)
                if row:
                    return int(row["free_throws"])
                await conn.execute("INSERT INTO users (user_id, free_throws) VALUES ($1, 1) ON CONFLICT (user_id) DO NOTHING", user_id)
                return 1
        except Exception:
            log.exception("get_user_free_throws DB failed")
            return 1
    else:
        if not hasattr(bot, "_in_memory_users"):
            bot._in_memory_users = {}
        return bot._in_memory_users.get(user_id, 1)

async def try_add_referral(referred_user: int, inviter_user: int) -> bool:
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                res = await conn.execute("INSERT INTO referrals (referred_user, inviter) VALUES ($1, $2) ON CONFLICT (referred_user) DO NOTHING", referred_user, inviter_user)
                if res and res.endswith(" 1"):
                    # award inviter
                    await change_user_free_throws(inviter_user, 1)
                    # notify inviter only
                    try:
                        await bot.send_message(inviter_user, "🔥 Вы получили +1 бесплатный бросок за приглашённого друга")
                    except Exception:
                        log.exception("Failed to notify inviter about referral")
                    return True
                return False
        except Exception:
            log.exception("try_add_referral DB failed")
            return False
    else:
        if not hasattr(bot, "_in_memory_referrals"):
            bot._in_memory_referrals = {}
        if referred_user in bot._in_memory_referrals:
            return False
        bot._in_memory_referrals[referred_user] = inviter_user
        await change_user_free_throws(inviter_user, 1)
        try:
            await bot.send_message(inviter_user, "🔥 Вы получили +1 бесплатный бросок за приглашённого друга")
        except Exception:
            log.exception("Failed to notify inviter in-memory")
        return True

# --------------------
# Баланс — атомарные операции
# --------------------
async def change_balance(delta: int, notify_group: bool = True, note: Optional[str] = None):
    global bot_balance, db_pool
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("UPDATE bot_state SET value = value + $1 WHERE key='balance' RETURNING value", delta)
                if row and row.get("value") is not None:
                    bot_balance = int(row["value"])
                else:
                    await conn.execute("INSERT INTO bot_state (key, value) VALUES ('balance', $1) ON CONFLICT (key) DO UPDATE SET value = bot_state.value + $1", delta)
                    bot_balance = int(await conn.fetchval("SELECT value FROM bot_state WHERE key='balance'"))
        except Exception:
            log.exception("CHANGE_BALANCE DB failed — fallback")
            bot_balance += delta
    else:
        bot_balance += delta

    if notify_group and GROUP_ID:
        try:
            prefix = f"{note}\n" if note else ""
            await bot.send_message(GROUP_ID, f"{prefix}💰 Баланс бота: <b>{bot_balance}</b>")
        except Exception:
            log.exception("Failed to notify group about balance change")
    return bot_balance

async def set_balance_absolute(value: int, notify_group: bool = True):
    global bot_balance, db_pool
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("INSERT INTO bot_state (key, value) VALUES ('balance', $1) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", value)
                bot_balance = int(await conn.fetchval("SELECT value FROM bot_state WHERE key='balance'"))
        except Exception:
            log.exception("SET_BALANCE DB failed")
            bot_balance = value
    else:
        bot_balance = value

    if notify_group and GROUP_ID:
        try:
            await bot.send_message(GROUP_ID, f"💰 Баланс бота установлен: <b>{bot_balance}</b>")
        except Exception:
            log.exception("Failed to notify group about set balance")
    return bot_balance

# --------------------
# /start handler — учитываем payload (реферал)
# --------------------
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user = message.from_user
    user_id = user.id
    # ensure user exists (creates with 1 free throw by default)
    await ensure_user(user_id)

    # payload handling (no message to the referred user)
    payload = ""
    try:
        text = (message.text or "").strip()
        parts = text.split()
        if len(parts) > 1:
            payload = parts[1]
    except Exception:
        payload = ""

    if payload:
        try:
            inviter_id = int(payload)
            if inviter_id != user_id:
                # try add referral; inviter is notified inside try_add_referral
                await try_add_referral(user_id, inviter_id)
        except Exception:
            pass

    # send reply-menu hint (NOT "Добро пожаловать..."), and main inline menu
    free = await get_user_free_throws(user_id)
    start_text = START_TEXT_TEMPLATE.format(free_throws=free)
    # Send reply keyboard hint (short neutral text)
    try:
        await message.answer("Меню внизу — откройте для быстрого доступа.", reply_markup=REPLY_MENU)
    except Exception:
        log.exception("Failed to send reply menu hint")
    # Send main inline message
    try:
        await message.answer(start_text, reply_markup=build_main_keyboard())
    except Exception:
        log.exception("Failed to send main start message")

# --------------------
# Обработчик reply-кнопки "🏀 Сыграть в баскет"
# --------------------
@dp.message(F.text == "🏀 Сыграть в баскет")
async def open_main_menu_message(message: types.Message):
    user_id = message.from_user.id
    free = await get_user_free_throws(user_id)
    start_text = START_TEXT_TEMPLATE.format(free_throws=free)
    await message.answer(start_text, reply_markup=build_main_keyboard())

# --------------------
# Callback: реферальное меню и назад
# --------------------
@dp.callback_query(lambda c: c.data == "ref_menu")
async def ref_menu(call: types.CallbackQuery):
    user_id = call.from_user.id
    try:
        me = await bot.get_me()
        bot_username = me.username or ""
    except Exception:
        bot_username = ""
    link = f"https://t.me/{bot_username}?start={user_id}" if bot_username else f"/start {user_id}"
    text = REF_TEXT_TEMPLATE.format(link=link)
    try:
        await call.message.edit_text(text, reply_markup=REF_BACK_KB, parse_mode=ParseMode.HTML)
    except Exception:
        await call.message.answer(text, reply_markup=REF_BACK_KB)

@dp.callback_query(lambda c: c.data == "ref_back")
async def ref_back(call: types.CallbackQuery):
    user_id = call.from_user.id
    free = await get_user_free_throws(user_id)
    start_text = START_TEXT_TEMPLATE.format(free_throws=free)
    try:
        await call.message.edit_text(start_text, reply_markup=build_main_keyboard(), parse_mode=ParseMode.HTML)
    except Exception:
        await call.message.answer(start_text, reply_markup=build_main_keyboard())

# --------------------
# Обработчик нажатий play_{count}_{cost}
# --------------------
@dp.callback_query(lambda c: c.data and c.data.startswith("play_"))
async def play_various(call: types.CallbackQuery):
    await call.answer()
    user_id = call.from_user.id
    data = call.data or ""
    parts = data.split("_")
    try:
        count = int(parts[1]) if len(parts) > 1 else 5
    except Exception:
        count = 5
    try:
        cost = int(parts[2]) if len(parts) > 2 else 1
    except Exception:
        cost = 1

    if count < 1:
        count = 1
    if count > 20:
        count = 20

    # Free button handling (cost == 0)
    if cost == 0:
        free = await get_user_free_throws(user_id)
        if free < 1:
            # show referral screen
            try:
                me = await bot.get_me()
                bot_username = me.username or ""
            except Exception:
                bot_username = ""
            link = f"https://t.me/{bot_username}?start={user_id}" if bot_username else f"/start {user_id}"
            text = REF_TEXT_TEMPLATE.format(link=link)
            try:
                await call.message.edit_text(text, reply_markup=REF_BACK_KB, parse_mode=ParseMode.HTML)
            except Exception:
                await call.message.answer(text, reply_markup=REF_BACK_KB)
            return
        else:
            # consume one free throw silently (no extra message)
            await change_user_free_throws(user_id, -1)
    else:
        # add cost to bot balance (one-time per press)
        note = f"➕ +{cost} за нажатие ({count} {word_form_mяч(count)})"
        await change_balance(cost, notify_group=True, note=note)

    # send dice with 0.5s intervals
    messages = []
    first_send_time = None
    for i in range(count):
        try:
            msg = await bot.send_dice(call.message.chat.id, emoji="🏀")
        except Exception:
            log.exception("Failed to send dice")
            continue
        if first_send_time is None:
            first_send_time = time.monotonic()
        messages.append(msg)
        await asyncio.sleep(0.5)

    # wait until 5s since first send
    if first_send_time is None:
        first_send_time = time.monotonic()
    elapsed = time.monotonic() - first_send_time
    wait_for = 5.0 - elapsed
    if wait_for > 0:
        await asyncio.sleep(wait_for)

    # collect results (no numbering, only Попал/Промах)
    results = []
    hits = 0
    for msg in messages:
        val = getattr(msg, "dice", None)
        value = getattr(val, "value", 0) if val else 0
        results.append(int(value))
        if int(value) >= 4:
            hits += 1

    sent_count = len(results)

    # if all sent balls hit -> -15 and notify group
    if sent_count > 0 and hits == sent_count:
        new_bal = await change_balance(-15, notify_group=False)
        if GROUP_ID:
            try:
                await bot.send_message(GROUP_ID, f"⚠️ Произведено списание: <b>-15</b>\n💰 Текущий баланс после списания: <b>{new_bal}</b>")
            except Exception:
                log.exception("Failed to send group message about -15")

    # send results in chat (simple lines)
    text_lines = ["🎯 <b>Результаты бросков:</b>\n"]
    for v in results:
        text_lines.append("✅ Попал" if v >= 4 else "❌ Промах")
    if not results:
        text_lines.append("⚠️ Не удалось отправить ни одного мяча.")
    await bot.send_message(call.message.chat.id, "\n".join(text_lines))

    await asyncio.sleep(1)
    await bot.send_message(
        call.message.chat.id,
        "✅ ПОПАДАНИЕ!" if sent_count > 0 and hits == sent_count else "🟡 Не все попали. Попробуем ещё?"
    )

    await asyncio.sleep(1)
    # send updated main menu with current free throws
    free_now = await get_user_free_throws(user_id)
    start_text = START_TEXT_TEMPLATE.format(free_throws=free_now)
    await bot.send_message(call.message.chat.id, start_text, reply_markup=build_main_keyboard())

# --------------------
# Команда "баланс" — показывает или устанавливает
# --------------------
@dp.message()
async def handle_balance_commands(message: types.Message):
    text = (message.text or "").strip()
    if not text:
        return
    lowered = text.lower()
    if not (lowered.startswith("/баланс") or lowered.split()[0] == "баланс"):
        return
    parts = text.split()
    if len(parts) >= 2 and parts[1].lstrip("-").isdigit():
        new_value = int(parts[1])
        await set_balance_absolute(new_value, notify_group=True)
        await message.answer(f"💰 Баланс установлен: <b>{bot_balance}</b>")
    else:
        await message.answer(f"💰 Текущий баланс: <b>{bot_balance}</b>")

# --------------------
# WEB (health) для Render
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
    log.info("🚀 BOT STARTING")
    await init_db()
    try:
        await bot.get_me()
    except Exception:
        pass
    await start_web()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
