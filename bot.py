# bot.py
# Единственная функция бота:
# Если пользователь отправляет число — создаётся инвойс на это количество ⭐

import os
import asyncio
import logging

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.types import LabeledPrice, ContentType

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYMENTS_PROVIDER_TOKEN = os.getenv("PAYMENTS_PROVIDER_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан")

if not PAYMENTS_PROVIDER_TOKEN:
    raise RuntimeError("PAYMENTS_PROVIDER_TOKEN не задан")

# Как и в исходном коде
STAR_UNIT_MULTIPLIER = 1

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties())
dp = Dispatcher()


def parse_positive_int(text: str):
    if not text:
        return None
    text = text.strip()
    if text.isdigit():
        value = int(text)
        if value > 0:
            return value
    return None


@dp.message(F.text)
async def handle_number(message: types.Message):
    amount = parse_positive_int(message.text)
    if amount is None:
        return

    prices = [
        LabeledPrice(
            label=f"Пополнение бота на {amount}⭐",
            amount=int(amount * STAR_UNIT_MULTIPLIER)
        )
    ]

    payload = f"buy:{message.from_user.id}:{amount}"

    await bot.send_invoice(
        chat_id=message.chat.id,
        title=f"{amount}⭐",
        description="Пополнение виртуальных звёзд",
        provider_token=PAYMENTS_PROVIDER_TOKEN,
        currency="XTR",
        prices=prices,
        payload=payload,
        start_parameter="buyvirtual"
    )


@dp.pre_checkout_query()
async def pre_checkout(pre_checkout_query: types.PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@dp.message(F.content_type == ContentType.SUCCESSFUL_PAYMENT)
async def successful_payment(message: types.Message):
    await message.answer("✅ Платёж успешно получен!")


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
