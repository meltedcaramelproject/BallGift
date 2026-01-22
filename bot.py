# bot.py
import os
import asyncio
import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram.types import Message
from aiogram.filters import CommandStart

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_ID = os.getenv("GROUP_ID")  # ожидается строка вроде "-1001234567890"

if not BOT_TOKEN or not GROUP_ID:
    raise RuntimeError("Требуется задать переменные окружения BOT_TOKEN и GROUP_ID")

# приводим GROUP_ID к int
GROUP_ID = int(GROUP_ID)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

DB_PATH = "messages.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                UNIQUE(chat_id, message_id)
            )
            """
        )
        await db.commit()

# Хендлер, который сохраняет все сообщения из указанной группы
@dp.message()
async def store_group_message(message: Message):
    try:
        if message.chat and message.chat.id == GROUP_ID:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT OR IGNORE INTO messages(chat_id, message_id) VALUES(?, ?)",
                    (GROUP_ID, message.message_id),
                )
                await db.commit()
    except Exception as e:
        # не падаем на ошибке записи в БД; логируем в консоль
        print("DB insert error:", e)

# /start — выбираем случайный message_id из БД и копируем в чат пользователя
@dp.message(CommandStart())
async def cmd_start(message: Message):
    user_chat = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT message_id FROM messages WHERE chat_id = ? ORDER BY RANDOM() LIMIT 1",
            (GROUP_ID,),
        ) as cursor:
            row = await cursor.fetchone()

    if not row:
        await message.answer(
            "Пока нет сохранённых сообщений из группы. "
            "Добавь бота в группу и пусть там появятся сообщения (бот должен видеть их)."
        )
        return

    chosen_message_id = row[0]

    try:
        # copy_message создаёт новое сообщение от имени бота -> автор скрыт
        await bot.copy_message(chat_id=user_chat, from_chat_id=GROUP_ID, message_id=chosen_message_id)
    except Exception as e:
        # например: сообщение было удалено, или бот потерял доступ
        await message.answer(f"Не удалось отправить сообщение: {e}")

async def main():
    await init_db()
    print("DB ready, бот запущен.")
    # long polling
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
