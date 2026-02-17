import asyncio
import logging
import os
import time

from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ContentType
from aiogram.types import LabeledPrice, PreCheckoutQuery

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# --- CONFIG ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYMENTS_PROVIDER_TOKEN = os.getenv("PAYMENTS_PROVIDER_TOKEN")  # как в изначальном коде
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set in environment")
if not PAYMENTS_PROVIDER_TOKEN:
    # Не запрещаем запуск, но отправка инвойсов будет падать — оставил поведение близкое к исходному.
    log.warning("PAYMENTS_PROVIDER_TOKEN is not set. Создание инвойсов будет неработоспособно.")

# множитель: сколько "самых мелких единиц валюты" = 1 звезде (в исходном коде был STAR_UNIT_MULTIPLIER = 1)
STAR_UNIT_MULTIPLIER = 1

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# payload -> (invoice_chat_id, invoice_message_id)
invoice_map: dict[str, tuple[int, int]] = {}

# --- Web health (минимальный) ---
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

# --- /start (инструкции) ---
@dp.message(F.text == "/start")
async def cmd_start(message: types.Message):
    try:
        await message.answer(
            "Привет! Отправь любое целое число — и я создам инвойс на оплату этого количества звёзд Telegram.\n\n"
            "Пример: отправьте `100` чтобы получить счёт на 100⭐.",
            parse_mode=ParseMode.HTML
        )
    except Exception:
        pass

# --- Основной обработчик: если пользователь отправил число, создать инвойс ---
@dp.message()
async def number_to_invoice(message: types.Message):
    text = (message.text or "").strip()
    # проверяем, что сообщение — целое положительное число
    if not text:
        return
    # позволим числам с пробелами/знаками и т.п. — но только целые положительные
    try:
        # допускаем, что пользователь пишет, например, "100" или "+100"
        if text.startswith("+"):
            text_num = text[1:]
        else:
            text_num = text
        # отбросим лишние пробелы внутри
        text_num = text_num.strip()
        amount_stars = int(text_num)
    except Exception:
        # если сообщение не число — игнорируем (единственная функция бота)
        return

    if amount_stars <= 0:
        try:
            await message.answer("Введите положительное целое число звёзд для создания инвойса.")
        except Exception:
            pass
        return

    user_id = message.from_user.id

    # подготовка инвойса — тот же принцип что и в исходном коде
    title = f"Пополнение на {amount_stars}⭐"
    description = f"Оплата {amount_stars} звёзд Telegram"
    price_amount = int(amount_stars * STAR_UNIT_MULTIPLIER)
    prices = [LabeledPrice(label=f"{amount_stars}⭐", amount=price_amount)]
    ts = int(time.time())
    payload = f"buy_virtual_{user_id}_{amount_stars}_{ts}"

    try:
        invoice_msg = await bot.send_invoice(
            chat_id=user_id,
            title=title,
            description=description,
            provider_token=PAYMENTS_PROVIDER_TOKEN,
            currency="XTR",
            prices=prices,
            payload=payload,
            start_parameter="topup"
        )
        # сохраняем mapping чтобы удалить сообщение-инвойс при необходимости
        try:
            invoice_map[payload] = (invoice_msg.chat.id, invoice_msg.message_id)
        except Exception:
            # в редких случаях send_invoice может вернуть другой тип — игнорируем silently
            pass
    except Exception:
        log.exception("send_invoice failed")
        try:
            await message.answer("Не удалось создать платёж. Проверьте настройки Payments (PAYMENTS_PROVIDER_TOKEN).")
        except Exception:
            pass

# --- Pre-checkout (стандартно подтверждаем) ---
@dp.pre_checkout_query()
async def precheckout_handler(pre_q: PreCheckoutQuery):
    try:
        await bot.answer_pre_checkout_query(pre_q.id, ok=True)
    except Exception:
        log.exception("answer_pre_checkout_query failed")

# --- Успешная оплата: подтверждаем пользователю и чистим инвойс из mapping ---
@dp.message(F.content_type == ContentType.SUCCESSFUL_PAYMENT)
async def on_successful_payment(message: types.Message):
    sp = message.successful_payment
    payload = getattr(sp, "invoice_payload", "") or ""
    payer_id = message.from_user.id

    # Попытка удалить старое сообщение-инвойс (если мы его сохранили)
    try:
        mapping = invoice_map.pop(payload, None)
        if mapping:
            inv_chat_id, inv_msg_id = mapping
            try:
                await bot.delete_message(inv_chat_id, inv_msg_id)
            except Exception:
                log.exception("Failed to delete invoice message")
    except Exception:
        log.exception("invoice_map handling error")

    # вычислим сколько звёзд оплатили (учитывая STAR_UNIT_MULTIPLIER)
    try:
        paid_amount_raw = int(sp.total_amount or 0)
    except Exception:
        paid_amount_raw = 0
    try:
        paid_stars = int(paid_amount_raw // STAR_UNIT_MULTIPLIER)
    except Exception:
        paid_stars = paid_amount_raw

    try:
        await message.answer(f"✅ Платёж принят. Вы оплатили {paid_stars}⭐. Спасибо!")
    except Exception:
        pass

# --- MAIN ---
async def main():
    log.info("Starting simple invoice bot")
    try:
        await bot.get_me()
    except Exception:
        pass
    await start_web()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
