# bot.py
import os
import logging
import asyncio
import random
import sqlite3
from aiogram import Bot, Dispatcher, types, executor

# === Настройки логов ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === Токен бота (из переменных окружения) ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logger.error("Переменная окружения BOT_TOKEN не установлена")
    raise SystemExit("Установите BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

DB_PATH = "bot_balance.db"
DB_KEY = "bot"  # ключ-идентификатор для единственной записи


# === Работа с базой (очень простая sqlite) ===
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS balances (
            key TEXT PRIMARY KEY,
            amount INTEGER NOT NULL
        )
    """)
    # если пусто — создаём запись с нулём
    cur.execute("SELECT amount FROM balances WHERE key = ?", (DB_KEY,))
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO balances (key, amount) VALUES (?, ?)", (DB_KEY, 0))
    conn.commit()
    conn.close()


def get_balance() -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT amount FROM balances WHERE key = ?", (DB_KEY,))
    row = cur.fetchone()
    conn.close()
    return int(row[0]) if row else 0


def set_balance_value(value: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE balances SET amount = ? WHERE key = ?", (int(value), DB_KEY))
    conn.commit()
    conn.close()


def change_balance(delta: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT amount FROM balances WHERE key = ?", (DB_KEY,))
    row = cur.fetchone()
    current = int(row[0]) if row else 0
    new = current + int(delta)
    cur.execute("UPDATE balances SET amount = ? WHERE key = ?", (new, DB_KEY))
    conn.commit()
    conn.close()
    return new


# === Хелпер для стартового сообщения (чтобы можно было пересылать) ===
def start_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("5 мячей • 1⭐", callback_data="play_5"))
    return kb


START_TEXT = "<b>🏀 баскетбол за подарки</b>\n\nпопади мячом в кольцо каждым броском и получи крутой подарок 🎁"


# === Handlers ===
@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    await message.answer(START_TEXT, parse_mode="HTML", reply_markup=start_keyboard())


@dp.callback_query_handler(lambda c: c.data == "play_5")
async def handle_play_5(callback_query: types.CallbackQuery):
    chat_id = callback_query.message.chat.id
    user = callback_query.from_user

    # сразу начисляем +1 к балансу (по условию)
    new_balance = change_balance(1)

    try:
        await bot.answer_callback_query(callback_query.id, text="Добавлен +1⭐ к балансу бота!")
    except Exception:
        # иногда answer_callback_query может упасть — игнорируем
        pass

    # Отправляем 5 отдельных сообщений с эмодзи чтобы каждая анимация сработала
    ball_emoji = "🏀"
    for i in range(5):
        try:
            await bot.send_message(chat_id, ball_emoji)
        except Exception as e:
            logger.exception("Не удалось отправить эмодзи: %s", e)
        # небольшая задержка между ними — помогает визуально и по доставке
        await asyncio.sleep(0.2)

    # Ждём 5 секунд перед результатом (по условию)
    await asyncio.sleep(5)

    # Генерируем результат для каждого броска — вероятность попадания 50%
    results = []
    for _ in range(5):
        hit = random.random() < 0.5  # 50% шанс
        results.append(hit)

    # Формируем текст с результатами (шестое сообщение)
    lines = []
    for idx, hit in enumerate(results, start=1):
        lines.append(f"{idx}. {'✅ Попал!' if hit else '❌ Промах'}")
    result_text = "Результаты бросков:\n" + "\n".join(lines)

    # Если все 5 - попал, снимаем 15 с баланса бота
    if all(results):
        new_balance_after_penalty = change_balance(-15)
        result_text += f"\n\n🎯 Все пять попали — с баланса бота снято 15⭐ (текущий баланс: {new_balance_after_penalty}⭐)."
    else:
        # добавим текущий баланс для информации
        current = get_balance()
        result_text += f"\n\nТекущий баланс бота: {current}⭐."

    await bot.send_message(chat_id, result_text)

    # Через 1 секунду — сообщение "В этот раз не забили..."
    await asyncio.sleep(1)
    await bot.send_message(chat_id, "🟡 В этот раз не забили... Попробуем ещё раз?")

    # Через ещё 1 секунду — отправляем стартовое сообщение снова (как будто нажали старт)
    await asyncio.sleep(1)
    await bot.send_message(chat_id, START_TEXT, parse_mode="HTML", reply_markup=start_keyboard())


@dp.message_handler(commands=["баланс"])
async def cmd_balance(message: types.Message):
    # команда может быть "/баланс" или "/баланс 123"
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) == 1:
        # показываем текущий баланс
        current = get_balance()
        await message.reply(f"Текущий баланс бота: {current}⭐")
        return

    # если указан аргумент — пытаемся установить
    arg = parts[1].strip()
    try:
        value = int(arg)
    except ValueError:
        await message.reply("Неправильный формат. Используй: /баланс <целое число> или просто /баланс")
        return

    set_balance_value(value)
    await message.reply(f"Баланс бота установлен: {value}⭐")


# === Запуск бота ===
if __name__ == "__main__":
    init_db()
    logger.info("Бот запущен")
    executor.start_polling(dp, skip_updates=True)
