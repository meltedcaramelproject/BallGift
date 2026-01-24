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
    ReplyKeyboardMarkup, KeyboardButton, LabeledPrice
)
from aiohttp import web

# --------------------
# LOGGING
# --------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ballbot")

# --------------------
# ENV
# --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GROUP_ID_RAW = os.getenv("GROUP_ID", "")
PAYMENTS_PROVIDER_TOKEN = os.getenv("PAYMENTS_PROVIDER_TOKEN")  # optional
ADMIN_ID = os.getenv("ADMIN_ID")  # optional admin for sensitive ops

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

GROUP_ID: Optional[int] = None
try:
    if GROUP_ID_RAW:
        GROUP_ID = int(GROUP_ID_RAW)
except Exception:
    GROUP_ID = None

# --------------------
# BOT & DISPATCHER
# --------------------
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# --------------------
# DB / STATE
# --------------------
db_pool: Optional[asyncpg.Pool] = None

# Gift costs (real bot stars)
GIFT_COSTS = {
    "teddy": 5,   # cost in real stars
    "heart": 3,
}

# --------------------
# UI / BUTTONS config
# --------------------
BUTTONS = [
    (6, 0),   # free (cooldown)
    (5, 1),
    (4, 2),
    (3, 4),
    (2, 6),
    (1, 8),
]

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
    # create share url
    bot_username = ""
    try:
        # try to get cached bot info; this is async, but acceptable here in handler
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

# Purchase keyboard for missing virtual stars (uses payments)
def build_purchase_kb(missing: int, user_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    if PAYMENTS_PROVIDER_TOKEN:
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"Оплатить {missing}⭐", callback_data=f"pay_virtual_{missing}")])
    else:
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"🔗 Инструкция", callback_data=f"buyinfo_{missing}")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="buy_back")])
    return kb

# --------------------
# DB initialization
# --------------------
async def init_db():
    global db_pool
    if not DATABASE_URL:
        log.warning("DATABASE_URL not set — running with in-memory fallback")
        db_pool = None
        return
    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=6, timeout=15)
        async with db_pool.acquire() as conn:
            # bot_stars = real stars on bot account
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value BIGINT NOT NULL
            );
            """)
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
            # ensure bot_stars record exists
            await conn.execute("INSERT INTO bot_state (key, value) VALUES ('bot_stars', 0) ON CONFLICT (key) DO NOTHING")
        log.info("DB ready")
    except Exception:
        log.exception("DB init failed; falling back to in-memory")
        db_pool = None

# --------------------
# DB helpers: users, bot_stars, referrals
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
    # in-memory fallback
    if not hasattr(bot, "_mem_users"):
        bot._mem_users = {}
    rec = bot._mem_users.setdefault(user_id, {"virtual_stars": 0, "free_next_at": 0})
    return rec["virtual_stars"], rec["free_next_at"]

async def get_user_virtual(user_id: int) -> int:
    vs, _ = await ensure_user(user_id)
    return vs

async def change_user_virtual(user_id: int, delta: int) -> int:
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("UPDATE users SET virtual_stars = GREATEST(virtual_stars + $1, 0) WHERE user_id=$2 RETURNING virtual_stars", delta, user_id)
                if row:
                    return int(row["virtual_stars"])
                await conn.execute("INSERT INTO users (user_id, virtual_stars) VALUES ($1, GREATEST($2,0)) ON CONFLICT (user_id) DO UPDATE SET virtual_stars = GREATEST(virtual_stars + $2, 0)", user_id, delta)
                val = await conn.fetchval("SELECT virtual_stars FROM users WHERE user_id=$1", user_id)
                return int(val or 0)
        except Exception:
            log.exception("change_user_virtual DB failed")
    # in-memory
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
                return int(await conn.fetchval("SELECT virtual_stars FROM users WHERE user_id=$1", user_id))
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
                row = await conn.fetchrow("SELECT free_next_at FROM users WHERE user_id=$1", user_id)
                if row:
                    return int(row["free_next_at"])
                await conn.execute("INSERT INTO users (user_id, free_next_at) VALUES ($1, 0) ON CONFLICT DO NOTHING", user_id)
                return 0
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

# bot real stars
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
                return int(await conn.fetchval("SELECT value FROM bot_state WHERE key='bot_stars'"))
        except Exception:
            log.exception("change_bot_stars DB failed")
    cur = getattr(bot, "_mem_bot_stars", 0)
    cur = max(cur + delta, 0)
    bot._mem_bot_stars = cur
    return cur

# referrals
async def register_ref_visit(referred_user: int, inviter: int) -> bool:
    """
    Insert referral record if not exists. Notify inviter that someone visited.
    Return True if inserted (new), False if already existed.
    """
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                res = await conn.execute("INSERT INTO referrals (referred_user, inviter, plays, rewarded) VALUES ($1, $2, 0, FALSE) ON CONFLICT (referred_user) DO NOTHING", referred_user, inviter)
                if res and res.endswith(" 1"):
                    # notify inviter (include clickable mention if possible)
                    try:
                        # attempt to get referred user's name
                        r = await bot.get_chat(referred_user)
                        if getattr(r, "username", None):
                            mention = f"<a href=\"tg://user?id={referred_user}\">@{r.username}</a>"
                        else:
                            name = r.first_name or ""
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
    # in-memory
    if not hasattr(bot, "_mem_referrals"):
        bot._mem_referrals = {}
    if referred_user in bot._mem_referrals:
        return False
    bot._mem_referrals[referred_user] = {"inviter": inviter, "plays": 0, "rewarded": False}
    try:
        await bot.send_message(inviter, f"🔗 По вашей ссылке перешёл user\nВы получите <b>+3⭐</b> на баланс после того, как он сыграет 5 раз в баскетбол", parse_mode=ParseMode.HTML)
    except Exception:
        log.exception("notify inviter in-memory failed")
    return True

async def increment_referred_play(referred_user: int):
    """
    Increment plays count for referred_user, reward inviter +3 virtual stars when reach 5 plays.
    """
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
                    # give inviter +3 virtual stars
                    await change_user_virtual(inviter, 3)
                    try:
                        await bot.send_message(inviter, "🔥 Вам начислено +3⭐ — приглашённый сыграл 5 раз!")
                    except Exception:
                        log.exception("notify inviter reward failed")
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
                log.exception("notify inviter in-memory failed")

# --------------------
# START handler (with payload processing for referral)
# --------------------
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user = message.from_user
    user_id = user.id
    # ensure record
    await ensure_user(user_id)

    # parse payload
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
            inviter_id = int(payload)
            if inviter_id != user_id:
                await register_ref_visit(user_id, inviter_id)
        except Exception:
            pass

    # send reply hint and main message showing user's virtual stars
    vstars = await get_user_virtual(user_id)
    start_text = START_TEXT_TEMPLATE.format(virtual_stars=vstars)
    # send reply menu hint (neutral)
    try:
        await message.answer("Меню внизу — откройте для быстрого доступа.", reply_markup=REPLY_MENU)
    except Exception:
        pass
    try:
        await message.answer(start_text, reply_markup=build_main_keyboard(user_id))
    except Exception:
        log.exception("send main menu failed on /start")

# --------------------
# Reply menu open
# --------------------
@dp.message(F.text == "🏀 Сыграть в баскет")
async def open_main_menu_message(message: types.Message):
    user_id = message.from_user.id
    vstars = await get_user_virtual(user_id)
    start_text = START_TEXT_TEMPLATE.format(virtual_stars=vstars)
    await message.answer(start_text, reply_markup=build_main_keyboard(user_id))

# --------------------
# Referral menu callbacks
# --------------------
@dp.callback_query(lambda c: c.data == "ref_menu")
async def ref_menu(call: types.CallbackQuery):
    user_id = call.from_user.id
    try:
        await call.message.edit_text(REF_TEXT_HTML, reply_markup=build_ref_keyboard(user_id), parse_mode=ParseMode.HTML)
    except Exception:
        await call.message.answer(REF_TEXT_HTML, reply_markup=build_ref_keyboard(user_id), parse_mode=ParseMode.HTML)

@dp.callback_query(lambda c: c.data and c.data.startswith("copy_ref_"))
async def copy_ref(call: types.CallbackQuery):
    # show link in alert (user can copy manually)
    try:
        uid = int(call.data.split("_", 2)[2])
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
    user_id = call.from_user.id
    vstars = await get_user_virtual(user_id)
    start_text = START_TEXT_TEMPLATE.format(virtual_stars=vstars)
    try:
        await call.message.edit_text(start_text, reply_markup=build_main_keyboard(user_id), parse_mode=ParseMode.HTML)
    except Exception:
        await call.message.answer(start_text, reply_markup=build_main_keyboard(user_id))

# --------------------
# Play handler: play_{count}_{cost}
# --------------------
@dp.callback_query(lambda c: c.data and c.data.startswith("play_"))
async def play_handler(call: types.CallbackQuery):
    await call.answer()
    user_id = call.from_user.id
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

    if count < 1:
        count = 1
    if count > 20:
        count = 20

    # free throw logic (cost == 0)
    if cost == 0:
        now_ts = int(time.time())
        free_next = await get_user_free_next(user_id)
        if now_ts < free_next:
            rem = free_next - now_ts
            mins = rem // 60
            secs = rem % 60
            min_word = "минут" if mins != 1 else "минуту"
            sec_word = "секунд" if secs != 1 else "секунду"
            await call.answer(text=f"🏀 До следующего бесплатного броска осталось {mins} {min_word} и {secs} {sec_word}", show_alert=False)
            return
        # set next free time (3 minutes)
        await set_user_free_next(user_id, now_ts + 3 * 60)
        # free throw consumed silently
    else:
        # cost > 0: verify user has virtual stars
        vstars = await get_user_virtual(user_id)
        if vstars < cost:
            # ask to purchase missing virtual stars
            missing = cost - vstars
            text = f"Получай крутой подарок за попадание 🏀 в кольцо\n\nТовар: {count} {word_form_mяч(count)} — требуется {missing}⭐"
            try:
                await call.message.answer(text, reply_markup=build_purchase_kb(missing, user_id))
            except Exception:
                await call.message.reply(text, reply_markup=build_purchase_kb(missing, user_id))
            return
        # deduct user's virtual stars and credit bot_stars
        new_v = await change_user_virtual(user_id, -cost)
        await change_bot_stars(cost)

    # send dice with 0.5s delay
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

    # wait until 5s since first send
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

    # increment referral counter if applicable
    if sent_count > 0:
        await increment_referred_play(user_id)

    # if user wins (all hits), bot gives a gift taken from bot_stars
    if sent_count > 0 and hits == sent_count:
        # choose gift randomly
        gift = random.choice(["teddy", "heart"])
        cost_g = GIFT_COSTS.get(gift, 3)
        bot_stars_now = await get_bot_stars()
        if bot_stars_now < cost_g:
            # insufficient bot stars
            try:
                await bot.send_message(call.message.chat.id, "⚠️ У бота недостаточно звёзд для покупки подарка.")
            except Exception:
                pass
        else:
            # deduct bot stars and "send" gift (text)
            await change_bot_stars(-cost_g)
            gift_text = "🎁 Вы выиграли: " + ("🐻 Мишка" if gift == "teddy" else "💖 Сердечко")
            try:
                await bot.send_message(call.message.chat.id, gift_text)
            except Exception:
                log.exception("send gift message failed")

    # send results summary (only Попал/Промах)
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
    # send updated main menu with user's virtual stars displayed
    v_now = await get_user_virtual(user_id)
    start_text = START_TEXT_TEMPLATE.format(virtual_stars=v_now)
    await bot.send_message(call.message.chat.id, start_text, reply_markup=build_main_keyboard(user_id))

# --------------------
# Purchase flow: create invoice for missing virtual stars
# --------------------
@dp.callback_query(lambda c: c.data and c.data.startswith("pay_virtual_"))
async def pay_virtual_cb(call: types.CallbackQuery):
    await call.answer()
    if not PAYMENTS_PROVIDER_TOKEN:
        await call.message.answer("Платёжный провайдер не настроен.")
        return
    try:
        missing = int(call.data.split("_", 2)[2])
    except Exception:
        await call.message.answer("Ошибка данных покупки.")
        return
    user = call.from_user
    # Example pricing: 1 star = 1.00 USD -> 100 cents. Adapt to your provider/currency.
    amount_cents = missing * 100  # e.g., $1 per star
    prices = [LabeledPrice(label=f"{missing}⭐", amount=amount_cents)]
    await bot.send_invoice(
        chat_id=user.id,
        title=f"Покупка {missing}⭐",
        description=f"Покупка {missing} виртуальных звёзд для игры",
        provider_token=PAYMENTS_PROVIDER_TOKEN,
        currency="USD",  # change if needed
        prices=prices,
        payload=f"buy_virtual_{user.id}_{missing}_{int(time.time())}",
        start_parameter="buystars"
    )

# accept precheckout
@dp.pre_checkout_query()
async def preq_handler(pre_q: types.PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_q.id, ok=True)

# successful payment handler
@dp.message(F.successful_payment)
async def on_successful_payment(message: types.Message):
    pay = message.successful_payment
    # payload format used: buy_virtual_<user>_<stars>_<ts>
    payload = getattr(pay, "invoice_payload", "") or ""
    try:
        parts = payload.split("_")
        if len(parts) >= 3 and parts[0] == "buy" and parts[1] == "virtual":
            # older variants? fallback
            pass
    except Exception:
        pass
    # our payload format used above: "buy_virtual_<user>_<missing>_<ts>"
    try:
        payload = pay.invoice_payload or ""
        if payload.startswith("buy_virtual_"):
            _, _, uid_str, missing_str, *_ = payload.split("_")
            target_user = int(uid_str)
            missing = int(missing_str)
            # credit user's virtual stars and also increase bot_stars by same (user paid real money -> bot got real stars)
            await change_user_virtual(target_user, missing)
            await change_bot_stars(missing)
            await bot.send_message(target_user, f"Оплата принята — вам начислено {missing}⭐. Удачи в игре!")
        else:
            # unknown payload, just ack
            await message.answer("Платёж принят. Спасибо!")
    except Exception:
        log.exception("on_successful_payment failed")
        await message.answer("Платёж принят (ошибка обработки payload).")

@dp.callback_query(lambda c: c.data and c.data.startswith("buyinfo_"))
async def buyinfo_cb(call: types.CallbackQuery):
    await call.answer(text="Платёжный провайдер не настроен. Свяжитесь с админом.", show_alert=True)

@dp.callback_query(lambda c: c.data == "buy_back")
async def buy_back_cb(call: types.CallbackQuery):
    user_id = call.from_user.id
    v = await get_user_virtual(user_id)
    text = START_TEXT_TEMPLATE.format(virtual_stars=v)
    try:
        await call.message.edit_text(text, reply_markup=build_main_keyboard(user_id), parse_mode=ParseMode.HTML)
    except Exception:
        await call.message.answer(text, reply_markup=build_main_keyboard(user_id), parse_mode=ParseMode.HTML)

# --------------------
# Command "баланс" — only in GROUP_ID
# - "баланс" -> show bot's real stars (bot_stars) (only in group)
# - "баланс <user> <num>" -> set virtual stars for user to num (only in group)
# --------------------
@dp.message()
async def balans_cmd(message: types.Message):
    text = (message.text or "").strip()
    if not text:
        return
    lowered = text.lower()
    if not (lowered.startswith("/баланс") or lowered.split()[0] == "баланс"):
        return
    # Only allow in the specific group
    if GROUP_ID is None or message.chat.id != GROUP_ID:
        # ignore elsewhere
        return
    parts = text.split()
    if len(parts) == 1:
        # show bot's real stars
        bstars = await get_bot_stars()
        await message.answer(f"💰 Баланс бота (реальные звёзды): <b>{bstars}</b>")
        return
    # if len >=3 and second is user and third is number -> set user's virtual stars
    if len(parts) >= 3:
        user_token = parts[1]
        amount_token = parts[2]
        # resolve user id
        target_id = None
        if user_token.lstrip("-").isdigit():
            target_id = int(user_token)
        elif user_token.startswith("@"):
            try:
                chat = await bot.get_chat(user_token)
                target_id = chat.id
            except Exception:
                await message.answer("Не удалось найти пользователя по имени.")
                return
        else:
            try:
                chat = await bot.get_chat(user_token)
                target_id = chat.id
            except Exception:
                await message.answer("Не удалось найти пользователя.")
                return
        if not amount_token.lstrip("-").isdigit():
            await message.answer("Неверное число.")
            return
        amount = int(amount_token)
        await set_user_virtual(target_id, amount)
        await message.answer(f"Баланс пользователя {target_id} установлен: <b>{amount}⭐</b>")
        return
    # fallback: show usage
    await message.answer("Использование: баланс OR баланс <user> <amount> (только в группе)")

# --------------------
# Health endpoint for Render
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
    log.info(f"Web server running on port {port}")

# --------------------
# MAIN
# --------------------
async def main():
    log.info("Starting bot")
    await init_db()
    # warm bot info
    try:
        await bot.get_me()
    except Exception:
        pass
    await start_web()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
