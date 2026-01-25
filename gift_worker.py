#!/usr/bin/env python3
# coding: utf-8
"""
gift_worker.py
Worker который читает таблицу pending_gifts (Postgres) и пытается
купить/отправить Star Gifts через MTProto (Telethon).

Как использовать:
  1) pip install -r requirements.txt (aiogram не нужен здесь, нужен telethon + asyncpg + python-dotenv)
  2) создать .env с: API_ID, API_HASH, SESSION_NAME (например "gift_sender_session"),
                        DATABASE_URL (тот же, что использует бот)
  3) python gift_worker.py
"""
import os
import asyncio
import logging
import time
from typing import Optional

import asyncpg
from dotenv import load_dotenv
from telethon import TelegramClient, errors
from telethon.tl import functions, types

load_dotenv()

API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH") or ""
SESSION_NAME = os.getenv("SESSION_NAME") or "gift_sender_session"
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("DB_URL") or None

# Конфигурация
POLL_INTERVAL = 3.0      # сколько ждать между циклами если нет задач (сек)
BATCH_SIZE = 5           # сколько задач брать за итерацию
LOG_LEVEL = logging.INFO

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gift_worker")


# -------------------------
# DB helpers
# -------------------------
async def create_db_pool():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set in environment")
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return pool


async def fetch_and_lock_pending(pool: asyncpg.Pool, limit: int = BATCH_SIZE):
    """
    Возвращает список задач и помечает их как 'processing' внутри транзакции (FOR UPDATE SKIP LOCKED эквивалент).
    Это предотвращает гонки, если воркеров несколько.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Берём id-ы в транзакции и помечаем
            rows = await conn.fetch("""
                SELECT id, user_id, amount_stars, premium
                FROM pending_gifts
                WHERE status = 'pending'
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT $1
            """, limit)
            ids = [r["id"] for r in rows]
            if not ids:
                return []
            await conn.execute("""
                UPDATE pending_gifts SET status='processing', created_at = created_at
                WHERE id = ANY($1::int[])
            """, ids)
            # Возвращаем скопированные данные (работаем вне транзакции дальше)
            return [dict(r) for r in rows]


async def mark_sent(pool: asyncpg.Pool, gift_id: int):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE pending_gifts SET status='sent' WHERE id=$1", gift_id)


async def mark_failed(pool: asyncpg.Pool, gift_id: int, reason: str = ""):
    async with pool.acquire() as conn:
        await conn.execute("UPDATE pending_gifts SET status='failed' WHERE id=$1", gift_id)
        # логирование причины в отдельную таблицу можно добавить, оставляем простым
    log.warning("Marked gift %s as failed: %s", gift_id, reason)


async def refund_bot_stars(pool: asyncpg.Pool, amount: int):
    """
    Вернуть amount звёзд в bot_state ('bot_stars').
    Мы обновляем значение атомарно.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT value FROM bot_state WHERE key='bot_stars' FOR UPDATE")
            if not row:
                # если нет ключа — создаём
                await conn.execute("INSERT INTO bot_state (key, value) VALUES ('bot_stars', $1) ON CONFLICT (key) DO UPDATE SET value = bot_state.value + $1", amount)
                return
            await conn.execute("UPDATE bot_state SET value = GREATEST(value + $1, 0) WHERE key='bot_stars'", amount)
    log.info("Refunded %s stars to bot_state", amount)


async def get_bot_stars(pool: asyncpg.Pool) -> int:
    async with pool.acquire() as conn:
        val = await conn.fetchval("SELECT value FROM bot_state WHERE key='bot_stars'")
    return int(val or 0)


# -------------------------
# Telethon: покупки подарков
# -------------------------
async def find_gift_with_price(client: TelegramClient, price_stars: int):
    """
    Попытаться найти подарок с указанной ценой.
    Возвращает объект gift (raw) или None.
    ПРИМЕЧАНИЕ: структура ответа может отличаться. Логируем для отладки.
    """
    try:
        # Попробуем получить каталог подарков (market)
        res = await client(functions.payments.GetStarGiftsRequest(hash=0))
        gifts = getattr(res, "gifts", None)
        if gifts:
            for g in gifts:
                # у StarGift обычно есть поле 'stars' или 'price'
                stars = getattr(g, "stars", None)
                if stars is None:
                    # иногда цена хранится в g.price.amount или g.price
                    stars = getattr(g, "price", None)
                    if stars and hasattr(stars, "amount"):
                        stars = getattr(stars, "amount")
                try:
                    if stars == price_stars:
                        log.info("Found market gift: %s (stars=%s)", getattr(g, "id", None), stars)
                        return g
                except Exception:
                    continue
        else:
            log.debug("GetStarGiftsRequest returned no gifts or gifts is None")
    except Exception as e:
        log.debug("GetStarGiftsRequest failed: %s", e)

    # Если не нашли — пробуем saved gifts (у аккаунта)
    try:
        saved = await client(functions.payments.GetSavedStarGiftsRequest(peer=types.InputPeerSelf(), offset="0", limit=100))
        sgifts = getattr(saved, "gifts", None)
        if sgifts:
            for sg in sgifts:
                stars = getattr(sg, "stars", None)
                if stars == price_stars:
                    log.info("Found saved gift msg_id=%s stars=%s", getattr(sg, "msg_id", None), stars)
                    return sg
    except Exception as e:
        log.debug("GetSavedStarGiftsRequest failed: %s", e)

    return None


async def purchase_and_send(client: TelegramClient, pool: asyncpg.Pool, task: dict) -> bool:
    """
    Попытка купить и отправить найденный подарок task: {id, user_id, amount_stars, premium}
    Возвращает True при успехе, False при неудаче.
    """
    gift_id = int(task["id"])
    target_user = int(task["user_id"])
    amount_stars = int(task["amount_stars"])
    premium = bool(task.get("premium", False))

    log.info("Processing gift task %s -> user %s (%s⭐) premium=%s", gift_id, target_user, amount_stars, premium)

    # Получаем input entity получателя
    try:
        target_input = await client.get_input_entity(target_user)
    except Exception as e:
        log.exception("Failed to resolve target entity %s: %s", target_user, e)
        return False

    # 1) найти gift с нужной ценой
    gift = await find_gift_with_price(client, amount_stars)
    if not gift:
        log.warning("No gift found with price %s — cannot buy", amount_stars)
        return False

    # 2) Попытаться собрать invoice / payment form
    try:
        # Варианты конструкций gift зависят от типа объекта.
        # Часто для сохранённых подарков нужен msg_id -> InputSavedStarGift
        msg_id = getattr(gift, "msg_id", None) or getattr(gift, "message_id", None)
        if msg_id:
            # InputSavedStarGiftUser ожидает структуру с msg_id (в некоторых обёртках)
            try:
                stargift = types.InputSavedStarGiftUser(msg_id=int(msg_id))
            except Exception:
                # fallback: иногда тип называется InputSavedStarGift
                try:
                    stargift = types.InputSavedStarGift(msg_id=int(msg_id))
                except Exception:
                    stargift = None
        else:
            # Если у gift есть id - пробуем работать с ним (зависит от реализации)
            stargift = None

        if stargift is None:
            log.debug("stargift not built from gift (msg_id missing); trying generic flow")

        # Формируем invoice-transfer object (InputInvoiceStarGiftTransfer или похожий)
        # В Telethon названия типов могут отличаться; пробуем наиболее типичный вариант:
        try:
            invoice = types.InputInvoiceStarGiftTransfer(
                stargift=stargift,
                to_id=types.InputUser(user_id=target_input.user_id, access_hash=getattr(target_input, "access_hash", 0))
            )
        except Exception:
            invoice = None

        if invoice is None:
            log.warning("Could not construct invoice object reliably from gift; aborting purchase attempt")
            return False

        # Получаем payment form
        form = await client(functions.payments.GetPaymentFormRequest(invoice=invoice))
        log.debug("Payment form: %s", getattr(form, "form_id", None))

        # Отправляем Stars (SendStarsForm)
        send_res = await client(functions.payments.SendStarsFormRequest(form_id=form.form_id, invoice=invoice))
        log.info("SendStarsFormRequest result: %s", type(send_res).__name__)
        # Если не бросило исключение — считаем успешным
        return True

    except errors.RPCError as rpc:
        log.exception("RPCError when purchasing/sending gift: %s", rpc)
        return False
    except Exception as e:
        log.exception("Unhandled error in purchase_and_send: %s", e)
        return False


# -------------------------
# Main worker loop
# -------------------------
async def worker_loop():
    if API_ID == 0 or not API_HASH:
        raise RuntimeError("API_ID/API_HASH не установлены в окружении")

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL не установлена в окружении")

    pool = await create_db_pool()
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

    # Запуск Telethon
    await client.start()  # при первом запуске запросит код/2FA в консоли
    log.info("Telethon client started with session '%s'", SESSION_NAME)

    try:
        while True:
            try:
                tasks = await fetch_and_lock_pending(pool, limit=BATCH_SIZE)
                if not tasks:
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                # Обрабатываем задачи по очереди
                for t in tasks:
                    t_id = int(t["id"])
                    try:
                        ok = await purchase_and_send(client, pool, t)
                        if ok:
                            await mark_sent(pool, t_id)
                            log.info("Task %s marked sent", t_id)
                        else:
                            # Ошибка покупки — ставим failed и возвращаем звёзды боту
                            await mark_failed(pool, t_id, reason="purchase_failed")
                            try:
                                await refund_bot_stars(pool, int(t["amount_stars"]))
                            except Exception:
                                log.exception("Refund failed for task %s", t_id)
                    except Exception as e:
                        log.exception("Unhandled exception for task %s: %s", t_id, e)
                        await mark_failed(pool, t_id, reason=str(e))
                        try:
                            await refund_bot_stars(pool, int(t["amount_stars"]))
                        except Exception:
                            log.exception("Refund failed for task %s after exception", t_id)

                # Небольшая пауза между батчами
                await asyncio.sleep(1.0)

            except Exception as e:
                log.exception("Worker iteration error: %s", e)
                await asyncio.sleep(5.0)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
        await pool.close()
        log.info("Worker stopped")


# -------------------------
# Entrypoint
# -------------------------
if __name__ == "__main__":
    try:
        asyncio.run(worker_loop())
    except KeyboardInterrupt:
        log.info("Interrupted by user, exiting.")
    except Exception as e:
        log.exception("Worker crashed: %s", e)
        raise
