
import asyncio
import os
import random

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.enums import ParseMode


# 🔐 Чтение из Render ENV
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_ID = os.getenv("GROUP_ID")

if not BOT_TOKEN or not GROUP_ID:
    raise RuntimeError("❌ Не заданы BOT_TOKEN или GROUP_ID в Render ENV")

GROUP_ID = int(GROUP_ID)


bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

# Хранилище ID сообщений (в памяти)
messages_pool: list[int] = []


# 🟢 Сохраняем все сообщения из группы
@dp.message(F.chat.id == GROUP_ID)
async def collect_messages(message: Message):
    messages_pool.append(message.message_id)


# 🟢 /start — отправка случайного сообщения
@dp.message(Command("start"))
async def start_cmd(message: Message):
    if not messages_pool:
        await message.answer("❌ В группе пока нет сообщений.")
        return

    random_message_id = random.choice(messages_pool)

    await bot.copy_message(
        chat_id=message.chat.id,
        from_chat_id=GROUP_ID,
        message_id=random_message_id
    )


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
