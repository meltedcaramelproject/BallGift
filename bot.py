# bot.py
# Упрощённый бот: когда пользователь отправляет число — бот создаёт и отправляет инвойс
# Требования: aiogram==3.24.0
# Ожидаемые переменные окружения: BOT_TOKEN, PAYMENTS_PROVIDER_TOKEN
import os
import asyncio
import logging
import time
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.types import LabeledPrice, PreCheckoutQuery, ContentType
from aiogram import F
from aiohttp import web

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYMENTS_PROVIDER_TOKEN = os.getenv("PAYMENTS_PROVIDER_TOKEN")
# Сколько "мелких единиц" = 1 звезда. Установите 1 если провайдер ожидает именно количество звёзд.
STAR_UNIT_MULTIPLIER = int(os.getenv("STAR_UNIT_MULTIPLIER", "1"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в окружении")
if not PAYMENTS_PROVIDER_TOKEN:
    # не останавливаем импорт/запуск здесь — но при попытке отправить инвойс будет понятная ошибка
    log.warning("PAYMENTS_PROVIDER_TOKEN не задан. Отправка инвойсов не будет работать пока не установите его.")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties())
dp = Dispatcher()

# Вспомогательная проверка: является ли строка положительным целым числом
def parse_positive_int(s: str) -> int | None:
    s = (s or "").strip()
    if not s:
        return None
    # допускаем также числа с + или пробелами
    if s.startswith("+"):
        s = s[1:]
    if s.isdigit():
        try:
            v = int(s)
            if v > 0:
                return v
        except Exception:
            return None
    return None

@dp.message(F.text)
async def on_text(message: types.Message):
    """
    Если текст — положительное целое число X,
    отправляем инвойс пользователю на X ⭐ (в личку).
    Иначе — игнорируем (ничего не отправляем).
    """
    text = (message.text or "").strip()
    amount_stars = parse_positive_int(text)
    if amount_stars is None:
        # Не число — просто игнорируем (можно не отвечать)
        return

    user_id = message.from_user.id

    if not PAYMENTS_PROVIDER_TOKEN:
        # информируем пользователя, что оплата временно недоступна
        try:
            await message.reply("⚠️ Платежная система временно не настроена. Обратитесь к администратору.")
        except Exception:
            pass
        return

    # Создаём LabeledPrice — провайдер ожидает сумму в "мелких единицах"
    amount_units = int(amount_stars * STAR_UNIT_MULTIPLIER)
    if amount_units <= 0:
        await message.reply("Неверная сумма для оплаты.")
        return

    prices = [LabeledPrice(label=f"Оплата {amount_stars}⭐", amount=amount_units)]

    title = f"Покупка {amount_stars}⭐"
    description = "Оплата звёзд для бота"
    payload = f"user_purchase:{user_id}:{amount_stars}:{int(time.time())}"

    try:
        # Отправляем инвойс в личку пользователя (chat_id = user_id)
        invoice_msg = await bot.send_invoice(
            chat_id=user_id,
            title=title,
            description=description,
            provider_token=PAYMENTS_PROVIDER_TOKEN,
            currency="XTR",  # используем XTR как в старом проекте; при необходимости замените
            prices=prices,
            payload=payload,
            start_parameter="simplepurchase"
        )
        # Короткое уведомление в чате, где пользователь ввёл число (необязательно)
        try:
            await message.reply(f"🧾 Инвойс на {amount_stars}⭐ отправлен вам в личные сообщения.")
        except Exception:
            pass
    except Exception as e:
        log.exception("send_invoice failed")
        try:
            await message.reply("❌ Не удалось создать платёж. Проверьте настройки PAYMENTS_PROVIDER_TOKEN.")
        except Exception:
            pass

@dp.pre_checkout_query()
async def precheckout(pre_q: PreCheckoutQuery):
    # Подтверждаем pre-checkout
    try:
        await bot.answer_pre_checkout_query(pre_q.id, ok=True)
    except Exception:
        log.exception("answer_pre_checkout_query failed")

@dp.message(F.content_type == ContentType.SUCCESSFUL_PAYMENT)
async def successful_payment(message: types.Message):
    sp = message.successful_payment
    payload = getattr(sp, "invoice_payload", "") or ""
    payer = message.from_user
    # подтверждение пользователю
    try:
        await message.answer(f"✅ Платёж принят. Спасибо, {payer.first_name or payer.username or payer.id}!")
    except Exception:
        pass
    # Здесь можно добавить логику зачисления звёзд в БД — но по задаче это не требуется.

# Минимальный health endpoint (полезно при деплое)
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

async def main():
    await start_web()
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
