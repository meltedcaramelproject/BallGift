import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.client.default import DefaultBotProperties
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from aiohttp import web

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

# --------------------
# ГЛОБАЛЬНЫЙ БАЛАНС БОТА
# --------------------
bot_balance = 0

# --------------------
# КНОПКИ
# --------------------
def start_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🏀 5 мячей • 1⭐",
                callback_data="play_5"
            )
        ]
    ])

# --------------------
# ТЕКСТ СТАРТА
# --------------------
START_TEXT = (
    "<b>🏀 баскетбол за подарки</b>\n\n"
    "попади мячом в кольцо каждым броском и получи крутой подарок 🎁"
)

# --------------------
# /start
# --------------------
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        START_TEXT,
        reply_markup=start_kb()
    )

# --------------------
# КНОПКА ИГРЫ
# --------------------
@dp.callback_query(F.data == "play_5")
async def play_game(call: types.CallbackQuery):
    global bot_balance

    await call.answer()

    bot_balance += 1
    dice_results = []

    for _ in range(5):
        msg = await bot.send_dice(
            chat_id=call.message.chat.id,
            emoji="🏀"
        )
        dice_results.append(msg.dice.value)
        await asyncio.sleep(0.3)

    await asyncio.sleep(5)

    result_lines = []
    hits = 0

    for i, value in enumerate(dice_results, start=1):
        if value == 6:
            result_lines.append(f"{i}. ✅ Попал!")
            hits += 1
        else:
            result_lines.append(f"{i}. ❌ Промах")

    if hits == 5:
        bot_balance -= 15

    await bot.send_message(
        chat_id=call.message.chat.id,
        text="🎯 <b>Результаты бросков:</b>\n\n" + "\n".join(result_lines)
    )

    await asyncio.sleep(1)
    await bot.send_message(
        chat_id=call.message.chat.id,
        text="🟡 В этот раз не забили... Попробуем ещё раз?"
    )

    await asyncio.sleep(1)
    await bot.send_message(
        chat_id=call.message.chat.id,
        text=START_TEXT,
        reply_markup=start_kb()
    )

# --------------------
# /баланс [число]
# --------------------
@dp.message(Command("баланс"))
async def cmd_balance(message: types.Message):
    global bot_balance

    parts = message.text.split()

    if len(parts) == 2 and parts[1].lstrip("-").isdigit():
        bot_balance = int(parts[1])
        await message.answer(
            f"💰 Баланс бота установлен: <b>{bot_balance}</b>"
        )
    else:
        await message.answer(
            f"💰 Текущий баланс бота: <b>{bot_balance}</b>"
        )

# --------------------
# Мини-сервер для Render
# --------------------
async def handle(request):
    return web.Response(text="OK")

async def start_web_server():
    app = web.Application()
    app.add_routes([web.get("/", handle)])

    port = int(os.getenv("PORT", 8000))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Web server started on port {port}")

# --------------------
# START BOT
# --------------------
async def main():
    # Запускаем веб-сервер (чтобы Render был доволен)
    await start_web_server()

    # Запускаем polling бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
