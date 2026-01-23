import asyncio
import logging
import os
import time

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
GROUP_ID_RAW = os.getenv("GROUP_ID", "0")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

GROUP_ID = None
try:
    if GROUP_ID_RAW is not None and GROUP_ID_RAW != "":
        GROUP_ID = int(GROUP_ID_RAW)
except Exception:
    GROUP_ID = None

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
# UI / КНОПКИ
# --------------------
# Формат: (кол-во мячей, стоимость/звёзды)
BUTTONS = [
    (5, 1),
    (4, 2),
    (3, 3),
    (2, 4),
    (1, 8),
]

def start_kb():
    keyboard = []
    for count, cost in BUTTONS:
        text = f"🏀 {count} мяч{'а' if count==1 else 'ей'} • {cost}⭐"
        callback = f"play_{count}_{cost}"
        keyboard.append([InlineKeyboardButton(text=text, callback_data=callback)])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

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
        log.warning("DATABASE_URL not set — бот будет работать без БД (in-memory).")
        return

    try:
        db_pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=5,
            timeout=15
        )

        async with db_pool.acquire() as conn:
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value BIGINT NOT NULL
            )
            """)
            row = await conn.fetchrow("SELECT value FROM bot_state WHERE key='balance'")
            if row:
                bot_balance = int(row["value"])
            else:
                await conn.execute(
                    "INSERT INTO bot_state (key, value) VALUES ('balance', 0) ON CONFLICT (key) DO NOTHING"
                )
                bot_balance = 0

        log.info(f"✅ DB CONNECTED. Balance = {bot_balance}")

    except Exception:
        log.exception("❌ DB INIT FAILED — бот будет использовать in-memory баланс (temp).")
        db_pool = None

# --------------------
# Баланс — атомарные операции
# --------------------
async def change_balance(delta: int, notify_group: bool = True, note: str | None = None):
    global bot_balance, db_pool

    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "UPDATE bot_state SET value = value + $1 WHERE key='balance' RETURNING value",
                    delta
                )
                if row and row.get("value") is not None:
                    bot_balance = int(row["value"])
                else:
                    # случай, если записи не было
                    await conn.execute(
                        "INSERT INTO bot_state(key, value) VALUES('balance', $1) ON CONFLICT (key) DO UPDATE SET value = bot_state.value + $1",
                        delta
                    )
                    bot_balance = int(await conn.fetchval("SELECT value FROM bot_state WHERE key='balance'"))
        except Exception:
            log.exception("CHANGE_BALANCE FAILED — using in-memory fallback")
            bot_balance += delta
    else:
        bot_balance += delta

    if notify_group and GROUP_ID:
        try:
            prefix = f"{note}\n" if note else ""
            await bot.send_message(GROUP_ID, f"{prefix}💰 Баланс бота: <b>{bot_balance}</b>")
        except Exception:
            log.exception("Failed to send group balance message")

    return bot_balance

async def set_balance_absolute(value: int, notify_group: bool = True):
    global bot_balance, db_pool

    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO bot_state (key, value) VALUES ('balance', $1) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    value
                )
                bot_balance = int(await conn.fetchval("SELECT value FROM bot_state WHERE key='balance'"))
        except Exception:
            log.exception("SET_BALANCE FAILED — using in-memory fallback")
            bot_balance = value
    else:
        bot_balance = value

    if notify_group and GROUP_ID:
        try:
            await bot.send_message(GROUP_ID, f"💰 Баланс бота установлен: <b>{bot_balance}</b>")
        except Exception:
            log.exception("Failed to send group set-balance message")

    return bot_balance

# --------------------
# /start
# --------------------
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(START_TEXT, reply_markup=start_kb())

# --------------------
# Обработчик нажатий на кнопки вида play_{count}_{cost}
# --------------------
@dp.callback_query(lambda c: c.data and c.data.startswith("play_"))
async def play_various(call: types.CallbackQuery):
    await call.answer()

    data = call.data or ""
    parts = data.split("_")
    # ожидаем play_{count}_{cost}
    try:
        count = int(parts[1]) if len(parts) > 1 else 5
    except Exception:
        count = 5
    try:
        cost = int(parts[2]) if len(parts) > 2 else 1
    except Exception:
        cost = 1

    # ограничения
    if count < 1:
        count = 1
    if count > 20:
        count = 20  # предохранитель

    # 1) начисляем стоимость за нажатие (одно начисление)
    note = f"➕ +{cost} за нажатие ({count} мяч{'а' if count==1 else 'ей'})"
    await change_balance(cost, notify_group=True, note=note)

    # 2) отправляем count мячей с паузой 0.5s между ними
    messages = []
    first_send_time = None
    for i in range(count):
        try:
            msg = await bot.send_dice(call.message.chat.id, emoji="🏀")
        except Exception:
            log.exception("Failed to send dice")
            # если не получилось отправить — пропускаем
            continue
        if first_send_time is None:
            first_send_time = time.monotonic()
        messages.append(msg)
        await asyncio.sleep(0.5)

    # 3) ждать до 5 секунд от первого отправленного мяча
    if first_send_time is None:
        first_send_time = time.monotonic()
    elapsed = time.monotonic() - first_send_time
    wait_for = 5.0 - elapsed
    if wait_for > 0:
        await asyncio.sleep(wait_for)

    # 4) анализ результатов (учитываем именно отправленные месседжи)
    results = []
    hits = 0
    for msg in messages:
        val = getattr(msg, "dice", None)
        value = getattr(val, "value", 0) if val else 0
        results.append(int(value))
        if int(value) >= 4:
            hits += 1

    sent_count = len(results)

    # 5) если все отправленные мячи попали (и было хотя бы 1 отправлено) — списание -15
    if sent_count > 0 and hits == sent_count:
        new_bal = await change_balance(-15, notify_group=False)
        if GROUP_ID:
            try:
                await bot.send_message(GROUP_ID, f"⚠️ Произведено списание: <b>-15</b>\n💰 Текущий баланс после списания: <b>{new_bal}</b>")
            except Exception:
                log.exception("Failed to send group message about -15")

    # 6) отправляем результаты в чат
    text_lines = ["🎯 <b>Результаты бросков:</b>\n"]
    for i, v in enumerate(results, start=1):
        text_lines.append(f"{i}. {'✅ Попал' if v >= 4 else '❌ Промах'} ( {v} )")
    if not results:
        text_lines.append("⚠️ Не удалось отправить ни одного мяча.")

    await bot.send_message(call.message.chat.id, "\n".join(text_lines))

    await asyncio.sleep(1)
    await bot.send_message(
        call.message.chat.id,
        "✅ ПОПАДАНИЕ!" if sent_count > 0 and hits == sent_count else "🟡 Не все попали. Попробуем ещё?"
    )

    await asyncio.sleep(1)
    await bot.send_message(call.message.chat.id, START_TEXT, reply_markup=start_kb())

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
# WEB (health)
# --------------------
async def handle(request):
    return web.Response(text="OK")

async def start_web():
    app = web.Application()
    app.add_routes([web.get("/", handle), web.get("/health", handle)])
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
    await start_web()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
