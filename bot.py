# bot.py
import asyncio
import logging
import os
import time
import random
from typing import Optional

import asyncpg
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, LabeledPrice, ContentType
)
from aiohttp import web

# --------------------
# CONFIG / LOGS
# --------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ballbot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GROUP_ID_RAW = os.getenv("GROUP_ID", "")
PAYMENTS_PROVIDER_TOKEN = os.getenv("PAYMENTS_PROVIDER_TOKEN", "")  # keep empty string for Telegram Stars
ADMIN_ID = os.getenv("ADMIN_ID")  # optional

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

GROUP_ID: Optional[int] = None
try:
    if GROUP_ID_RAW:
        GROUP_ID = int(GROUP_ID_RAW)
except Exception:
    GROUP_ID = None

# --------------------
# Bot & Dispatcher
# --------------------
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# --------------------
# DB / in-memory fallback
# --------------------
db_pool: Optional[asyncpg.Pool] = None

# Gift costs (real bot stars). When user wins, bot spends its real stars to buy gift.
GIFT_COSTS = {
    "teddy": 5,   # 5 Stars
    "heart": 3,   # 3 Stars
}

# BUTTONS: (count, cost_in_virtual_stars)
BUTTONS = [
    (6, 0),  # free (cooldown)
    (5, 1),
    (4, 2),
    (3, 4),
    (2, 6),
    (1, 8),
]

# If your provider requires smallest-unit multiplier, change this.
# For Telegram Stars (XTR) typical usage is amount = number_of_stars (1 => 1 star).
STAR_UNIT_MULTIPLIER = 1  # amount in invoice = missing * STAR_UNIT_MULTIPLIER

# --------------------
# UI Helpers
# --------------------
def word_form_mяч(count: int) -> str:
    return "мяча" if 1 <= count <= 4 else "мячей"

def build_main_keyboard(user_id: Optional[int] = None) -> InlineKeyboardMarkup:
    kb = []
    for count, cost in BUTTONS:
        cost_text = "бесплатно" if cost == 0 else f"{cost}⭐"
        noun = word_form_mяч(count)
        text = f"🏀 {count} {noun} • {cost_text}"
        cb = f"play_{count}_{cost}"
        kb.append([InlineKeyboardButton(text=text, callback_data=cb)])
    kb.append([InlineKeyboardButton(text="+3⭐ за друга", callback_data="ref_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

START_TEXT_TEMPLATE = (
    "<b>🏀 БАСКЕТБОЛ ЗА ПОДАРКИ 🏀</b>\n\n"
    "🎯 ПОПАДИ мячом в кольцо каждым броском — и получи КРУТОЙ ПОДАРОК 🎁\n\n"
    "💰 Баланс: <b>{virtual_stars}</b>"
)

REF_TEXT_HTML = (
    "<b>+3⭐ ЗА ДРУГА</b>\n\n"
    "Получай +3⭐ на баланс за каждого приглашённого пользователя!"
)

REPLY_MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="🏀 Сыграть в баскет")]],
    resize_keyboard=True,
    one_time_keyboard=False
)

def build_ref_keyboard(user_id: int) -> InlineKeyboardMarkup:
    # build share url
    try:
        me = asyncio.get_event_loop().run_until_complete(bot.get_me())
        bot_username = me.username or ""
    except Exception:
        bot_username = ""
    link = f"https://t.me/{bot_username}?start={user_id}" if bot_username else f"/start {user_id}"
    share_url = f"https://t.me/share/url?url={link}&text=Приглашаю сыграть в баскет! {link}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➡️ Отправить другу", url=share_url)],
        [InlineKeyboardButton(text="🔗 Скопировать ссылку", callback_data=f"copy_ref_{user_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="ref_back")]
    ])

def build_purchase_kb(missing: int, user_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    # provider_token intentionally set to empty string for Stars
    kb.inline_keyboard.append([InlineKeyboardButton(text=f"Оплатить {missing}⭐", callback_data=f"pay_virtual_{missing}")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="buy_back")])
    return kb

# --------------------
# DB init
# --------------------
async def init_db():
    global db_pool
    if not DATABASE_URL:
        log.warning("DATABASE_URL not set — using in-memory fallback")
        db_pool = None
        return
    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=6, timeout=15)
        async with db_pool.acquire() as conn:
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                virtual_stars BIGINT NOT NULL DEFAULT 0,
                free_next_at BIGINT NOT NULL DEFAULT 0
            );
            """)
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                referred_user BIGINT PRIMARY KEY,
                inviter BIGINT NOT NULL,
                plays INT NOT NULL DEFAULT 0,
                rewarded BOOLEAN NOT NULL DEFAULT FALSE
            );
            """)
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value BIGINT NOT NULL
            );
            """)
            # ensure bot_stars row
            await conn.execute("INSERT INTO bot_state (key, value) VALUES ('bot_stars', 0) ON CONFLICT (key) DO NOTHING")
        log.info("DB initialized")
    except Exception:
        log.exception("DB init failed; using in-memory")
        db_pool = None

# --------------------
# DB helpers
# --------------------
async def ensure_user(user_id: int):
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("SELECT virtual_stars, free_next_at FROM users WHERE user_id=$1", user_id)
                if row:
                    return int(row["virtual_stars"]), int(row["free_next_at"])
                await conn.execute("INSERT INTO users (user_id, virtual_stars, free_next_at) VALUES ($1, 0, 0) ON CONFLICT DO NOTHING", user_id)
                return 0, 0
        except Exception:
            log.exception("ensure_user DB failed")
    if not hasattr(bot, "_mem_users"):
        bot._mem_users = {}
    rec = bot._mem_users.setdefault(user_id, {"virtual_stars": 0, "free_next_at": 0})
    return rec["virtual_stars"], rec["free_next_at"]

async def get_user_virtual(user_id: int) -> int:
    v, _ = await ensure_user(user_id)
    return v

async def change_user_virtual(user_id: int, delta: int) -> int:
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("UPDATE users SET virtual_stars = GREATEST(virtual_stars + $1, 0) WHERE user_id=$2 RETURNING virtual_stars", delta, user_id)
                if row:
                    return int(row["virtual_stars"])
                # insert if missing
                await conn.execute("INSERT INTO users (user_id, virtual_stars) VALUES ($1, GREATEST($2,0)) ON CONFLICT (user_id) DO UPDATE SET virtual_stars = GREATEST(users.virtual_stars + $2, 0)", user_id, delta)
                val = await conn.fetchval("SELECT virtual_stars FROM users WHERE user_id=$1", user_id)
                return int(val or 0)
        except Exception:
            log.exception("change_user_virtual DB failed")
    if not hasattr(bot, "_mem_users"):
        bot._mem_users = {}
    rec = bot._mem_users.setdefault(user_id, {"virtual_stars": 0, "free_next_at": 0})
    rec["virtual_stars"] = max(rec["virtual_stars"] + delta, 0)
    return rec["virtual_stars"]

async def set_user_virtual(user_id: int, value: int) -> int:
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("INSERT INTO users (user_id, virtual_stars, free_next_at) VALUES ($1, $2, 0) ON CONFLICT (user_id) DO UPDATE SET virtual_stars = $2", user_id, value)
                val = await conn.fetchval("SELECT virtual_stars FROM users WHERE user_id=$1", user_id)
                return int(val or 0)
        except Exception:
            log.exception("set_user_virtual DB failed")
    if not hasattr(bot, "_mem_users"):
        bot._mem_users = {}
    rec = bot._mem_users.setdefault(user_id, {"virtual_stars": 0, "free_next_at": 0})
    rec["virtual_stars"] = max(value, 0)
    return rec["virtual_stars"]

async def get_user_free_next(user_id: int) -> int:
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                val = await conn.fetchval("SELECT free_next_at FROM users WHERE user_id=$1", user_id)
                if val is None:
                    await conn.execute("INSERT INTO users (user_id, free_next_at) VALUES ($1, 0) ON CONFLICT DO NOTHING", user_id)
                    return 0
                return int(val)
        except Exception:
            log.exception("get_user_free_next DB failed")
    if not hasattr(bot, "_mem_users"):
        bot._mem_users = {}
    return bot._mem_users.get(user_id, {}).get("free_next_at", 0)

async def set_user_free_next(user_id: int, epoch_ts: int):
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("INSERT INTO users (user_id, free_next_at) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET free_next_at = $2", user_id, epoch_ts)
                return
        except Exception:
            log.exception("set_user_free_next DB failed")
    if not hasattr(bot, "_mem_users"):
        bot._mem_users = {}
    rec = bot._mem_users.setdefault(user_id, {"virtual_stars": 0, "free_next_at": 0})
    rec["free_next_at"] = epoch_ts

async def get_bot_stars() -> int:
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                val = await conn.fetchval("SELECT value FROM bot_state WHERE key='bot_stars'")
                return int(val or 0)
        except Exception:
            log.exception("get_bot_stars DB failed")
    return getattr(bot, "_mem_bot_stars", 0)

async def change_bot_stars(delta: int) -> int:
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("UPDATE bot_state SET value = GREATEST(value + $1, 0) WHERE key='bot_stars' RETURNING value", delta)
                if row:
                    return int(row["value"])
                await conn.execute("INSERT INTO bot_state (key, value) VALUES ('bot_stars', GREATEST($1,0)) ON CONFLICT (key) DO UPDATE SET value = GREATEST(bot_state.value + $1, 0)", delta)
                val = await conn.fetchval("SELECT value FROM bot_state WHERE key='bot_stars'")
                return int(val or 0)
        except Exception:
            log.exception("change_bot_stars DB failed")
    cur = getattr(bot, "_mem_bot_stars", 0)
    cur = max(cur + delta, 0)
    bot._mem_bot_stars = cur
    return cur

# Referrals
async def register_ref_visit(referred_user: int, inviter: int) -> bool:
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                res = await conn.execute("INSERT INTO referrals (referred_user, inviter, plays, rewarded) VALUES ($1, $2, 0, FALSE) ON CONFLICT (referred_user) DO NOTHING", referred_user, inviter)
                if res and res.endswith(" 1"):
                    # notify inviter
                    try:
                        r = await bot.get_chat(referred_user)
                        if getattr(r, "username", None):
                            mention = f"<a href=\"tg://user?id={referred_user}\">@{r.username}</a>"
                        else:
                            name = r.first_name or "user"
                            mention = f"<a href=\"tg://user?id={referred_user}\">{name}</a>"
                    except Exception:
                        mention = f"<a href=\"tg://user?id={referred_user}\">user</a>"
                    try:
                        await bot.send_message(inviter, f"🔗 По вашей ссылке перешёл {mention}\nВы получите <b>+3⭐</b> на баланс после того, как он сыграет 5 раз в баскетбол", parse_mode=ParseMode.HTML)
                    except Exception:
                        log.exception("notify inviter failed")
                    return True
                return False
        except Exception:
            log.exception("register_ref_visit DB failed")
            return False
    # memory fallback
    if not hasattr(bot, "_mem_referrals"):
        bot._mem_referrals = {}
    if referred_user in bot._mem_referrals:
        return False
    bot._mem_referrals[referred_user] = {"inviter": inviter, "plays": 0, "rewarded": False}
    try:
        await bot.send_message(inviter, f"🔗 По вашей ссылке перешёл user\nВы получите <b>+3⭐</b> на баланс после того, как он сыграет 5 раз в баскетбол", parse_mode=ParseMode.HTML)
    except Exception:
        pass
    return True

async def increment_referred_play(referred_user: int):
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("SELECT inviter, plays, rewarded FROM referrals WHERE referred_user=$1", referred_user)
                if not row:
                    return
                inviter, plays, rewarded = row["inviter"], row["plays"], row["rewarded"]
                if rewarded:
                    return
                plays += 1
                if plays >= 5:
                    await conn.execute("UPDATE referrals SET plays=$1, rewarded=TRUE WHERE referred_user=$2", plays, referred_user)
                    await change_user_virtual(inviter, 3)
                    try:
                        await bot.send_message(inviter, "🔥 Вам начислено +3⭐ — приглашённый сыграл 5 раз!")
                    except Exception:
                        pass
                else:
                    await conn.execute("UPDATE referrals SET plays=$1 WHERE referred_user=$2", plays, referred_user)
        except Exception:
            log.exception("increment_referred_play DB failed")
    else:
        mem = getattr(bot, "_mem_referrals", {})
        rec = mem.get(referred_user)
        if not rec or rec.get("rewarded"):
            return
        rec["plays"] = rec.get("plays", 0) + 1
        if rec["plays"] >= 5:
            rec["rewarded"] = True
            inviter = rec["inviter"]
            await change_user_virtual(inviter, 3)
            try:
                await bot.send_message(inviter, "🔥 Вам начислено +3⭐ — приглашённый сыграл 5 раз!")
            except Exception:
                pass

# --------------------
# Handlers
# --------------------
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user = message.from_user
    uid = user.id
    await ensure_user(uid)

    # process payload (referral)
    payload = ""
    try:
        txt = (message.text or "").strip()
        parts = txt.split()
        if len(parts) > 1:
            payload = parts[1]
    except Exception:
        payload = ""

    if payload:
        try:
            inviter = int(payload)
            if inviter != uid:
                await register_ref_visit(uid, inviter)
        except Exception:
            pass

    vstars = await get_user_virtual(uid)
    start_text = START_TEXT_TEMPLATE.format(virtual_stars=vstars)
    try:
        await message.answer("Меню внизу — откройте для быстрого доступа.", reply_markup=REPLY_MENU)
    except Exception:
        pass
    try:
        await message.answer(start_text, reply_markup=build_main_keyboard(uid))
    except Exception:
        log.exception("send main menu failed")

@dp.message(F.text == "🏀 Сыграть в баскет")
async def open_main_menu(message: types.Message):
    uid = message.from_user.id
    v = await get_user_virtual(uid)
    start_text = START_TEXT_TEMPLATE.format(virtual_stars=v)
    await message.answer(start_text, reply_markup=build_main_keyboard(uid))

@dp.callback_query(lambda c: c.data == "ref_menu")
async def ref_menu(call: types.CallbackQuery):
    uid = call.from_user.id
    try:
        await call.message.edit_text(REF_TEXT_HTML, reply_markup=build_ref_keyboard(uid), parse_mode=ParseMode.HTML)
    except Exception:
        await call.message.answer(REF_TEXT_HTML, reply_markup=build_ref_keyboard(uid), parse_mode=ParseMode.HTML)

@dp.callback_query(lambda c: c.data and c.data.startswith("copy_ref_"))
async def copy_ref(call: types.CallbackQuery):
    try:
        t = call.data.split("_", 2)[2]
        uid = int(t)
    except Exception:
        uid = call.from_user.id
    try:
        me = await bot.get_me()
        bot_username = me.username or ""
    except Exception:
        bot_username = ""
    link = f"https://t.me/{bot_username}?start={uid}" if bot_username else f"/start {uid}"
    await call.answer(text=f"Ссылка: {link}", show_alert=True)

@dp.callback_query(lambda c: c.data == "ref_back")
async def ref_back(call: types.CallbackQuery):
    uid = call.from_user.id
    v = await get_user_virtual(uid)
    start_text = START_TEXT_TEMPLATE.format(virtual_stars=v)
    try:
        await call.message.edit_text(start_text, reply_markup=build_main_keyboard(uid), parse_mode=ParseMode.HTML)
    except Exception:
        await call.message.answer(start_text, reply_markup=build_main_keyboard(uid), parse_mode=ParseMode.HTML)

# Play buttons
@dp.callback_query(lambda c: c.data and c.data.startswith("play_"))
async def play_handler(call: types.CallbackQuery):
    await call.answer()
    uid = call.from_user.id
    data = call.data or ""
    parts = data.split("_")
    try:
        count = int(parts[1]) if len(parts) > 1 else 5
    except Exception:
        count = 5
    try:
        cost = int(parts[2]) if len(parts) > 2 else 1
    except Exception:
        cost = 1

    if count < 1: count = 1
    if count > 20: count = 20

    # FREE handling
    if cost == 0:
        now = int(time.time())
        free_next = await get_user_free_next(uid)
        if now < free_next:
            rem = free_next - now
            mins = rem // 60
            secs = rem % 60
            min_word = "минут" if mins != 1 else "минуту"
            sec_word = "секунд" if secs != 1 else "секунду"
            await call.answer(text=f"🏀 До следующего бесплатного броска осталось {mins} {min_word} и {secs} {sec_word}", show_alert=False)
            return
        await set_user_free_next(uid, now + 3 * 60)
        # proceed
    else:
        vstars = await get_user_virtual(uid)
        if vstars < cost:
            missing = cost - vstars
            text = f"Получай крутой подарок за попадание 🏀 в кольцо\n\nТовар: {count} {word_form_mяч(count)} — требуется {missing}⭐"
            try:
                await call.message.answer(text, reply_markup=build_purchase_kb(missing, uid))
            except Exception:
                await call.message.reply(text, reply_markup=build_purchase_kb(missing, uid))
            return
        # deduct user's virtual stars and credit bot_stars
        await change_user_virtual(uid, -cost)
        await change_bot_stars(cost)

    # send dice messages with 0.5s between
    messages = []
    first_send_time = None
    for i in range(count):
        try:
            msg = await bot.send_dice(call.message.chat.id, emoji="🏀")
        except Exception:
            log.exception("send_dice failed")
            continue
        if first_send_time is None:
            first_send_time = time.monotonic()
        messages.append(msg)
        await asyncio.sleep(0.5)

    # wait until 5 seconds from first send
    if first_send_time is None:
        first_send_time = time.monotonic()
    elapsed = time.monotonic() - first_send_time
    if elapsed < 5.0:
        await asyncio.sleep(5.0 - elapsed)

    # collect results
    hits = 0
    results = []
    for msg in messages:
        value = getattr(msg.dice, "value", 0) if getattr(msg, "dice", None) else 0
        results.append(int(value))
        if int(value) >= 4:
            hits += 1

    sent_count = len(results)

    # increment referral plays if any
    if sent_count > 0:
        await increment_referred_play(uid)

    # if all hits -> bot gives gift (costs bot_stars)
    if sent_count > 0 and hits == sent_count:
        gift = random.choice(["teddy", "heart"])
        cost_g = GIFT_COSTS.get(gift, 3)
        bot_stars_now = await get_bot_stars()
        if bot_stars_now < cost_g:
            try:
                await bot.send_message(call.message.chat.id, "⚠️ У бота недостаточно звёзд для покупки подарка.")
            except Exception:
                pass
        else:
            await change_bot_stars(-cost_g)
            gift_text = "🎁 Вы выиграли: " + ("🐻 Мишка" if gift == "teddy" else "💖 Сердечко")
            try:
                await bot.send_message(call.message.chat.id, gift_text)
            except Exception:
                log.exception("send gift failed")

    # send results summary (no numbering)
    text_lines = ["🎯 <b>Результаты бросков:</b>\n"]
    if results:
        for v in results:
            text_lines.append("✅ Попал" if v >= 4 else "❌ Промах")
    else:
        text_lines.append("⚠️ Не удалось отправить ни одного мяча.")
    await bot.send_message(call.message.chat.id, "\n".join(text_lines))

    await asyncio.sleep(1)
    await bot.send_message(call.message.chat.id, "✅ ПОПАДАНИЕ!" if sent_count > 0 and hits == sent_count else "🟡 Не все попали. Попробуем ещё?")

    await asyncio.sleep(1)
    vnow = await get_user_virtual(uid)
    start_text = START_TEXT_TEMPLATE.format(virtual_stars=vnow)
    await bot.send_message(call.message.chat.id, start_text, reply_markup=build_main_keyboard(uid))

# --------------------
# Payment callbacks
# --------------------
@dp.callback_query(lambda c: c.data and c.data.startswith("pay_virtual_"))
async def pay_virtual_cb(call: types.CallbackQuery):
    await call.answer()
    try:
        missing = int(call.data.split("_", 2)[2])
    except Exception:
        await call.message.answer("Ошибка данных покупки.")
        return
    user = call.from_user

    # For Telegram Stars use provider_token = "" and currency="XTR"
    # amount should be integer in smallest currency units.
    # For XTR we assume 1 star -> amount 1 (STAR_UNIT_MULTIPLIER = 1).
    amount = int(missing * STAR_UNIT_MULTIPLIER)
    prices = [LabeledPrice(label=f"{missing}⭐", amount=amount)]
    payload = f"buy_virtual_{user.id}_{missing}_{int(time.time())}"
    try:
        await bot.send_invoice(
            chat_id=user.id,
            title=f"Покупка {missing}⭐",
            description=f"Покупка {missing} виртуальных звёзд для игры",
            provider_token=PAYMENTS_PROVIDER_TOKEN,  # empty string "" for Telegram Stars
            currency="XTR",
            prices=prices,
            payload=payload,
            start_parameter="buyvirtual"
        )
    except Exception:
        log.exception("send_invoice failed")
        await call.message.answer("Не удалось создать платёж. Проверьте настройки Payments в BotFather.")

@dp.pre_checkout_query()
async def pre_checkout_handler(pre_q: types.PreCheckoutQuery):
    # Always accept for now
    await bot.answer_pre_checkout_query(pre_q.id, ok=True)

@dp.message(F.content_type == ContentType.SUCCESSFUL_PAYMENT)
async def on_successful_payment(message: types.Message):
    # message.successful_payment exists
    sp = message.successful_payment
    payload = getattr(sp, "invoice_payload", "") or ""
    try:
        if payload.startswith("buy_virtual_"):
            # payload = buy_virtual_{user}_{missing}_{ts}
            parts = payload.split("_")
            # support both buy_virtual_user_missing_ts and buy_virtual__user_missing_ts variants
            if len(parts) >= 4:
                _, _, uid_str, missing_str, *_ = parts
            elif len(parts) >= 3:
                _, uid_str, missing_str = parts[1:4]
            else:
                uid_str = str(message.from_user.id)
                missing_str = "0"
            target_user = int(uid_str)
            missing = int(missing_str)
            # credit virtual stars to user and also add to bot_stars (user paid real money -> bot gets real stars)
            await change_user_virtual(target_user, missing)
            await change_bot_stars(missing)
            try:
                await bot.send_message(target_user, f"Оплата принята — вам начислено {missing}⭐. Удачи в игре!")
            except Exception:
                pass
        else:
            # fallback
            await message.answer("Платёж принят. Спасибо!")
    except Exception:
        log.exception("on_successful_payment handling failed")
        await message.answer("Платёж обработан, но возникла ошибка учёта.")

@dp.callback_query(lambda c: c.data and c.data.startswith("buyinfo_"))
async def buyinfo_cb(call: types.CallbackQuery):
    await call.answer(text="Платёжный провайдер не настроен.", show_alert=True)

@dp.callback_query(lambda c: c.data == "buy_back")
async def buy_back_cb(call: types.CallbackQuery):
    uid = call.from_user.id
    v = await get_user_virtual(uid)
    text = START_TEXT_TEMPLATE.format(virtual_stars=v)
    try:
        await call.message.edit_text(text, reply_markup=build_main_keyboard(uid), parse_mode=ParseMode.HTML)
    except Exception:
        await call.message.answer(text, reply_markup=build_main_keyboard(uid), parse_mode=ParseMode.HTML)

# --------------------
# Command "баланс" (only in GROUP_ID)
# - "баланс" -> show real bot stars
# - "баланс <user> <amount>" -> set virtual stars for user
# --------------------
@dp.message()
async def balans_cmd(message: types.Message):
    text = (message.text or "").strip()
    if not text:
        return
    lowered = text.lower()
    if not (lowered.startswith("/баланс") or lowered.split()[0] == "баланс"):
        return
    # only in group
    if GROUP_ID is None or message.chat.id != GROUP_ID:
        return
    parts = text.split()
    if len(parts) == 1:
        b = await get_bot_stars()
        await message.answer(f"💰 Баланс бота (реальные звёзды): <b>{b}</b>")
        return
    if len(parts) >= 3:
        user_token = parts[1]
        amount_token = parts[2]
        target_id = None
        if user_token.lstrip("-").isdigit():
            target_id = int(user_token)
        elif user_token.startswith("@"):
            try:
                chat = await bot.get_chat(user_token)
                target_id = chat.id
            except Exception:
                await message.answer("Не удалось найти пользователя.")
                return
        else:
            try:
                chat = await bot.get_chat(user_token)
                target_id = chat.id
            except Exception:
                await message.answer("Не удалось найти пользователя.")
                return
        if not amount_token.lstrip("-").isdigit():
            await message.answer("Неверный формат числа.")
            return
        amount = int(amount_token)
        await set_user_virtual(target_id, amount)
        await message.answer(f"Баланс пользователя {target_id} установлен: <b>{amount}⭐</b>")
        return
    await message.answer("Использование: баланс OR баланс <user> <amount> (только в группе)")

# --------------------
# Health (for Render)
# --------------------
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

# --------------------
# MAIN
# --------------------
async def main():
    log.info("Starting bot")
    await init_db()
    try:
        await bot.get_me()
    except Exception:
        pass
    await start_web()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
