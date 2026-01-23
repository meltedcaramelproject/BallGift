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

    # +1 к балансу (за игру)
    bot_balance += 1

    dice_results = []

    # отправляем 5 мячей (5 разных сообщений)
    for _ in range(5):
        msg = await bot.send_dice(
            chat_id=call.message.chat.id,
            emoji="🏀"
        )

        # ждём анимацию (обычно ~2.5-3s). даём небольшой запас.
        await asyncio.sleep(3)

        # защищённо читаем значение — может быть None в редких случаях
        value = None
        try:
            value = getattr(msg.dice, "value", None)
        except Exception:
            value = None

        # если значение не пришло — ставим 0 (будет считаться промахом)
        if value is None:
            logging.warning("Dice value is None for message id %s", msg.message_id)
            value = 0

        dice_results.append(int(value))

        # небольшой интервал между бросками (чтобы анимации не пересекались совсем)
        await asyncio.sleep(0.25)

    # формируем результат
    result_lines = []
    hits = 0

    for i, value in enumerate(dice_results, start=1):
        if value == 6:
            result_lines.append(f"{i}. ✅ Попал! (значение: {value})")
            hits += 1
        else:
            # показываем значение для диагностики
            result_lines.append(f"{i}. ❌ Промах (значение: {value})")

    # если все попали — минус 15
    if hits == 5:
        bot_balance -= 15

    await bot.send_message(
        chat_id=call.message.chat.id,
        text="🎯 <b>Результаты бросков:</b>\n\n" + "\n".join(result_lines)
    )

    # через 1 секунду — сообщение с предложением повторить
    await asyncio.sleep(1)
    await bot.send_message(
        chat_id=call.message.chat.id,
        text="🟡 В этот раз не забили... Попробуем ещё раз?"
    )

    # ещё через 1 секунду — старт заново
    await asyncio.sleep(1)
    await bot.send_message(
        chat_id=call.message.chat.id,
        text=START_TEXT,
        reply_markup=start_kb()
    )

# --------------------
# /баланс [число] — основной хендлер через Command
# --------------------
@dp.message(Command(commands=["баланс"]))
async def cmd_balance_command(message: types.Message):
    global bot_balance

    parts = (message.text or "").split()

    # если указали число: /баланс 123
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
# Резервный текстовый хендлер: на случай, если сообщение приходит как "баланс" без слеша
# --------------------
@dp.message()
async def fallback_text_handlers(message: types.Message):
    text = (message.text or "").strip()
    if not text:
        return

    parts = text.split()
    cmd = parts[0].lower()

    # поддерживаем варианты: "баланс" или "/баланс"
    if cmd in ("баланс", "/баланс"):
        global bot_balance

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
# Мини-сервер для Render (чтобы не было ошибки "No open ports detected")
# --------------------
async def handle(request):
    return web.Response(text="OK")

async def start_web_server():
    app = web.Application()
    app.add_routes([web.get("/", handle), web.get("/health", handle)])

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
