import asyncio
import logging
import os

import asyncpg
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
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
GROUP_ID_RAW = os.getenv("GROUP_ID")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

GROUP_ID = None
if GROUP_ID_RAW and GROUP_ID_RAW.lstrip("-").isdigit():
    GROUP_ID = int(GROUP_ID_RAW)

# --------------------
# BOT
# --------------------
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

# --------------------
# DB
# --------------------
db_pool: asyncpg.Pool | None = None
bot_balance: int = 0

# --------------------
# UI
# --------------------
def start_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏀 5 мячей • 1⭐", callback_data="play_5")]
    ])

START_TEXT = (
    "<b>🏀 баскетбол за подарки</b>\n\n"
    "попади мячом в кольцо каждым броском и получи крутой подарок 🎁"
)

# --------------------
# DB INIT
# --------------------
async def init_db():
    global db_pool, bot_balance

    if not DATABASE_URL:
        log.error("DATABASE_URL НЕ УСТАНОВЛЕН")
        return

    try:
        db_pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=5,
            ssl="require",  # 🔥 КЛЮЧЕВО ДЛЯ NEON
            timeout=15
        )

        async with db_pool.acquire() as conn:
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value BIGINT NOT NULL
            )
            """)

            row = await conn.fetchrow(
                "SELECT value FROM bot_state WHERE key='balance'"
            )

            if row:
                bot_balance = int(row["value"])
            else:
                await conn.execute(
                    "INSERT INTO bot_state (key, value) VALUES ('balance', 0)"
                )
                bot_balance = 0

        log.info(f"✅ DB CONNECTED. Balance = {bot_balance}")

    except Exception:
        log.exception("❌ DB CONNECTION FAILED")
        db_pool = None

# --------------------
# BALANCE
# --------------------
async def set_balance(value: int):
    global bot_balance
    bot_balance = value

    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE bot_state SET value=$1 WHERE key='balance'",
                    value
                )
        except Exception:
            log.exception("FAILED TO UPDATE BALANCE")

    if GROUP_ID:
        try:
            await bot.send_message(
                GROUP_ID,
                f"💰 Баланс бота: <b>{bot_balance}</b>"
            )
        except Exception:
            pass

# --------------------
# /start
# --------------------
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(START_TEXT, reply_markup=start_kb())

# --------------------
# GAME
# --------------------
@dp.callback_query(F.data == "play_5")
async def play_game(call: types.CallbackQuery):
    await call.answer()

    messages = await asyncio.gather(*[
        bot.send_dice(call.message.chat.id, emoji="🏀")
        for _ in range(5)
    ])

    hits = 0
    results = []

    for msg in messages:
        value = msg.dice.value
        results.append(value)

        await set_balance(bot_balance + 1)

        if value >= 4:
            hits += 1

    if hits == 5:
        await set_balance(bot_balance - 15)

    # ⏳ ЖЁСТКАЯ ПАУЗА
    await asyncio.sleep(5)

    text = ["🎯 <b>Результаты бросков:</b>\n"]
    for i, v in enumerate(results, 1):
        text.append(
            f"{i}. {'✅ Попал' if v >= 4 else '❌ Промах'} ( {v} )"
        )

    await bot.send_message(call.message.chat.id, "\n".join(text))

    await asyncio.sleep(1)
    await bot.send_message(
        call.message.chat.id,
        "✅ ПОПАДАНИЕ!" if hits == 5 else "🟡 Не все попали. Попробуем ещё?"
    )

    await asyncio.sleep(1)
    await bot.send_message(
        call.message.chat.id,
        START_TEXT,
        reply_markup=start_kb()
    )

# --------------------
# /баланс (ЧИНИМ)
# --------------------
@dp.message(F.text.startswith("/баланс"))
async def cmd_balance(message: types.Message):
    parts = message.text.split()

    if len(parts) > 1 and parts[1].lstrip("-").isdigit():
        await set_balance(int(parts[1]))
        await message.answer(f"💰 Баланс установлен: <b>{bot_balance}</b>")
    else:
        await message.answer(f"💰 Текущий баланс: <b>{bot_balance}</b>")

# --------------------
# WEB (Render)
# --------------------
async def handle(request):
    return web.Response(text="OK")

async def start_web():
    app = web.Application()
    app.add_routes([web.get("/", handle)])
    port = int(os.getenv("PORT", "8000"))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

# --------------------
# MAIN
# --------------------
async def main():
    log.info("🚀 BOT STARTING")
    await init_db()
    await start_web()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
