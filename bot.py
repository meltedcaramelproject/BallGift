# bot.py
import asyncio
import logging
import os
import time
from typing import Optional

import asyncpg
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode, ChatType
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
log = logging.getLogger("bot")

# --------------------
# ENV
# --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GROUP_ID_RAW = os.getenv("GROUP_ID", "")
PAYMENTS_PROVIDER_TOKEN = os.getenv("PAYMENTS_PROVIDER_TOKEN")  # optional
ADMIN_ID = os.getenv("ADMIN_ID")  # optional — если хочешь ограничить /баланс <user> <amount>

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

GROUP_ID: Optional[int] = None
try:
    if GROUP_ID_RAW:
        GROUP_ID = int(GROUP_ID_RAW)
except Exception:
    GROUP_ID = None

# --------------------
# BOT & DP
# --------------------
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# --------------------
# DB
# --------------------
db_pool: Optional[asyncpg.Pool] = None
bot_balance: int = 0  # global bot balance (keeps track of sums from purchases / penalties)

# --------------------
# CONFIG / BUTTONS
# --------------------
# (count, cost_in_stars)
BUTTONS = [
    (6, 0),   # бесплатный (cooldown 3 minutes)
    (5, 1),
    (4, 2),
    (3, 4),   # updated price
    (2, 6),   # updated price
    (1, 8),
]

def word_form_mяч(count: int) -> str:
    # 1..4 -> "мяча", 5..6 -> "мячей"
    return "мяча" if 1 <= count <= 4 else "мячей"

def build_main_keyboard(user_id: Optional[int] = None) -> InlineKeyboardMarkup:
    kb = []
    for count, cost in BUTTONS:
        cost_text = "бесплатно" if cost == 0 else f"{cost}⭐"
        noun = word_form_mяч(count)
        text = f"🏀 {count} {noun} • {cost_text}"
        cb = f"play_{count}_{cost}"
        kb.append([InlineKeyboardButton(text=text, callback_data=cb)])
    # referral button renamed to "+3⭐ за друга"
    kb.append([InlineKeyboardButton(text="+3⭐ за друга", callback_data="ref_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

START_TEXT_TEMPLATE = (
    "<b>🏀 БАСКЕТБОЛ ЗА ПОДАРКИ 🏀</b>\n\n"
    "🎯 ПОПАДИ мячом в кольцо каждым броском — и получи КРУТОЙ ПОДАРОК 🎁\n\n"
    "💰 Баланс: <b>{stars}</b>"
)

REF_TEXT_TEMPLATE = (
    "<b>+3⭐ ЗА ДРУГА</b>\n\n"
    "Получай +3⭐ на баланс за каждого приглашённого пользователя!"
)

# referral sub-menu keyboard
def build_ref_keyboard(user_id: int) -> InlineKeyboardMarkup:
    # share url for quick sharing
    try:
        me = asyncio.run_coroutine_threadsafe(bot.get_me(), asyncio.get_event_loop()).result(timeout=2)
        bot_username = me.username or ""
    except Exception:
        bot_username = ""
    link = f"https://t.me/{bot_username}?start={user_id}" if bot_username else f"/start {user_id}"
    # share via t.me/share/url opens share dialog
    share_url = f"https://t.me/share/url?url={link}&text=Приглашаю сыграть в баскет! {link}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➡️ Отправить другу", url=share_url)],
        [InlineKeyboardButton(text="🔗 Скопировать ссылку", callback_data=f"copy_ref_{user_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="ref_back")]
    ])
    return kb

# reply keyboard (left-bottom menu)
REPLY_MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="🏀 Сыграть в баскет")]],
    resize_keyboard=True,
    one_time_keyboard=False
)

# --------------------
# DB INIT
# --------------------
async def init_db():
    global db_pool, bot_balance
    if not DATABASE_URL:
        log.warning("DATABASE_URL not set — running without persistent DB (in-memory fallback)")
        return

    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=8, timeout=15)
        async with db_pool.acquire() as conn:
            # tables:
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value BIGINT NOT NULL
            );
            """)
            await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                stars BIGINT NOT NULL DEFAULT 0,
                free_next_at BIGINT NOT NULL DEFAULT 0  -- epoch seconds when free button becomes available
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

            row = await conn.fetchrow("SELECT value FROM bot_state WHERE key='balance'")
            if row:
                bot_balance = int(row["value"])
            else:
                await conn.execute("INSERT INTO bot_state (key, value) VALUES ('balance', 0) ON CONFLICT (key) DO NOTHING")
                bot_balance = 0
        log.info("✅ DB initialized")
    except Exception:
        log.exception("DB init failed, using in-memory fallback")
        db_pool = None

# --------------------
# DB helpers: user operations
# --------------------
async def ensure_user_record(user_id: int):
    """ Ensure users row exists; return (stars:int, free_next_at:int) """
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("SELECT stars, free_next_at FROM users WHERE user_id=$1", user_id)
                if row:
                    return int(row["stars"]), int(row["free_next_at"])
                await conn.execute("INSERT INTO users (user_id, stars, free_next_at) VALUES ($1, 0, 0) ON CONFLICT DO NOTHING", user_id)
                return 0, 0
        except Exception:
            log.exception("ensure_user_record DB failed")
            # fallback to in-memory
    # in-memory fallback:
    if not hasattr(bot, "_mem_users"):
        bot._mem_users = {}
    rec = bot._mem_users.get(user_id, {"stars": 0, "free_next_at": 0})
    bot._mem_users.setdefault(user_id, rec)
    return rec["stars"], rec["free_next_at"]

async def change_user_stars(user_id: int, delta: int) -> int:
    """ Change user's stars by delta, return new stars (clamped >=0) """
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("UPDATE users SET stars = GREATEST(stars + $1, 0) WHERE user_id=$2 RETURNING stars", delta, user_id)
                if row:
                    return int(row["stars"])
                # if not exists - insert default then update
                await conn.execute("INSERT INTO users (user_id, stars) VALUES ($1, 0) ON CONFLICT (user_id) DO NOTHING", user_id)
                row2 = await conn.fetchrow("UPDATE users SET stars = GREATEST(stars + $1, 0) WHERE user_id=$2 RETURNING stars", delta, user_id)
                return int(row2["stars"])
        except Exception:
            log.exception("change_user_stars DB failed")
    # in-memory
    if not hasattr(bot, "_mem_users"):
        bot._mem_users = {}
    rec = bot._mem_users.setdefault(user_id, {"stars": 0, "free_next_at": 0})
    rec["stars"] = max(rec["stars"] + delta, 0)
    return rec["stars"]

async def set_user_stars(user_id: int, value: int) -> int:
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("INSERT INTO users (user_id, stars, free_next_at) VALUES ($1, $2, 0) ON CONFLICT (user_id) DO UPDATE SET stars = $2", user_id, value)
                row = await conn.fetchrow("SELECT stars FROM users WHERE user_id=$1", user_id)
                return int(row["stars"])
        except Exception:
            log.exception("set_user_stars DB failed")
    if not hasattr(bot, "_mem_users"):
        bot._mem_users = {}
    rec = bot._mem_users.setdefault(user_id, {"stars": 0, "free_next_at": 0})
    rec["stars"] = max(value, 0)
    return rec["stars"]

async def get_user_stars(user_id: int) -> int:
    s, _ = await ensure_user_record(user_id)
    return s

async def get_user_free_next(user_id: int) -> int:
    """return epoch seconds"""
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
    rec = bot._mem_users.setdefault(user_id, {"stars": 0, "free_next_at": 0})
    rec["free_next_at"] = epoch_ts

# --------------------
# Referrals
# --------------------
async def try_register_referral(referred_user: int, inviter: int) -> bool:
    """
    Register referral entry if not exists.
    Notify inviter that someone visited (but do not notify referred).
    Return True if inserted, False if already existed.
    """
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                res = await conn.execute(
                    "INSERT INTO referrals (referred_user, inviter, plays, rewarded) VALUES ($1, $2, 0, FALSE) ON CONFLICT (referred_user) DO NOTHING",
                    referred_user, inviter
                )
                if res and res.endswith(" 1"):
                    # notify inviter that their link was used (but per request, for the referred user DO NOT send them any message)
                    try:
                        # include username/link to referred user (clickable)
                        txt = "🔗 По вашей ссылке перешёл "
                        try:
                            r = await bot.get_chat(referred_user)
                            # show mention (if username) or name with tg link
                            if r.username:
                                mention = f"<a href=\"tg://user?id={referred_user}\">{r.username}</a>"
                            else:
                                name = (r.first_name or "") + (" " + r.last_name if getattr(r, "last_name", None) else "")
                                mention = f"<a href=\"tg://user?id={referred_user}\">{name.strip()}</a>"
                        except Exception:
                            mention = f"<a href=\"tg://user?id={referred_user}\">user</a>"
                        txt += f"{mention}\nВы получите <b>+3⭐</b> на баланс после того, как он сыграет 5 раз в баскетбол"
                        await bot.send_message(inviter, txt, parse_mode=ParseMode.HTML)
                    except Exception:
                        log.exception("Failed to notify inviter of referral visit")
                    return True
                return False
        except Exception:
            log.exception("try_register_referral DB failed")
            return False
    # in-memory
    if not hasattr(bot, "_mem_referrals"):
        bot._mem_referrals = {}
    if referred_user in bot._mem_referrals:
        return False
    bot._mem_referrals[referred_user] = {"inviter": inviter, "plays": 0, "rewarded": False}
    # notify inviter
    try:
        try:
            r = await bot.get_chat(referred_user)
            if r.username:
                mention = f"@{r.username}"
            else:
                mention = r.first_name or "user"
        except Exception:
            mention = "user"
        txt = f"🔗 По вашей ссылке перешёл {mention}\nВы получите <b>+3⭐</b> на баланс после того, как он сыграет 5 раз в баскетбол"
        await bot.send_message(inviter, txt, parse_mode=ParseMode.HTML)
    except Exception:
        log.exception("Failed to notify inviter in-memory")
    return True

async def increment_referred_play_if_any(user_id: int):
    """If user is a referred_user and not yet rewarded, increment plays count and reward inviter when reaches 5."""
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("SELECT inviter, plays, rewarded FROM referrals WHERE referred_user=$1", user_id)
                if not row:
                    return
                inviter, plays, rewarded = row["inviter"], row["plays"], row["rewarded"]
                if rewarded:
                    return
                plays += 1
                if plays >= 5:
                    # reward inviter +3 stars
                    await conn.execute("UPDATE referrals SET plays=$1, rewarded=TRUE WHERE referred_user=$2", plays, user_id)
                    await change_user_stars(inviter, 3)
                    try:
                        await bot.send_message(inviter, "🔥 Вам начислено +3⭐ — приглашённый сыграл 5 раз!")
                    except Exception:
                        log.exception("Failed to notify inviter about reward")
                else:
                    await conn.execute("UPDATE referrals SET plays=$1 WHERE referred_user=$2", plays, user_id)
        except Exception:
            log.exception("increment_referred_play_if_any DB failed")
    else:
        # in-memory
        mem = getattr(bot, "_mem_referrals", {})
        rec = mem.get(user_id)
        if not rec or rec.get("rewarded"):
            return
        rec["plays"] = rec.get("plays", 0) + 1
        if rec["plays"] >= 5:
            inviter = rec["inviter"]
            rec["rewarded"] = True
            await change_user_stars(inviter, 3)
            try:
                await bot.send_message(inviter, "🔥 Вам начислено +3⭐ — приглашённый сыграл 5 раз!")
            except Exception:
                log.exception("Failed to notify inviter in-memory")

# --------------------
# Bot-wide balance change
# --------------------
async def change_bot_balance(delta: int):
    global bot_balance, db_pool
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("UPDATE bot_state SET value = value + $1 WHERE key='balance' RETURNING value", delta)
                if row:
                    bot_balance = int(row["value"])
                else:
                    await conn.execute("INSERT INTO bot_state (key, value) VALUES ('balance', $1) ON CONFLICT (key) DO UPDATE SET value = bot_state.value + $1", delta)
                    bot_balance = int(await conn.fetchval("SELECT value FROM bot_state WHERE key='balance'"))
        except Exception:
            log.exception("change_bot_balance failed")
            bot_balance += delta
    else:
        bot_balance += delta
    # optionally notify group only on major events (we'll notify on -15 penalties elsewhere)
    return bot_balance

# --------------------
# Helper: build purchase keyboard for missing stars
# --------------------
def build_purchase_kb(missing: int, user_id: int):
    # Buttons: pay (if provider token present) — otherwise show copy of a "manual purchase" link
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    if PAYMENTS_PROVIDER_TOKEN:
        # callback to initiate invoice generation
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"Оплатить {missing}⭐", callback_data=f"buystars_{missing}")])
    else:
        # fallback: show the referral copy / instruction via alert
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"🔗 Получить инструкцию", callback_data=f"buyinfo_{missing}")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="buy_back")])
    return kb

# --------------------
# START handler (with payload processing)
# --------------------
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user = message.from_user
    user_id = user.id
    # don't send "Добро пожаловать..." per request
    # ensure user exists (creates user row)
    await ensure_user_record(user_id)

    # process payload (referral)
    payload = ""
    try:
        text = (message.text or "").strip()
        parts = text.split()
        if len(parts) > 1:
            payload = parts[1]
    except Exception:
        payload = ""

    if payload:
        try:
            inviter_id = int(payload)
            if inviter_id != user_id:
                # register referral; referree should NOT be notified
                await try_register_referral(user_id, inviter_id)
        except Exception:
            pass

    # send reply-menu hint (neutral) and main inline menu showing user's stars
    stars = await get_user_stars(user_id)
    start_text = START_TEXT_TEMPLATE.format(stars=stars)
    # reply hint (short)
    try:
        await message.answer("Меню внизу — откройте для быстрого доступа.", reply_markup=REPLY_MENU)
    except Exception:
        log.exception("Failed to send reply hint")
    try:
        await message.answer(start_text, reply_markup=build_main_keyboard(user_id))
    except Exception:
        log.exception("Failed to send main menu on /start")

# --------------------
# Reply menu handler
# --------------------
@dp.message(F.text == "🏀 Сыграть в баскет")
async def open_main_menu_message(message: types.Message):
    user_id = message.from_user.id
    stars = await get_user_stars(user_id)
    start_text = START_TEXT_TEMPLATE.format(stars=stars)
    await message.answer(start_text, reply_markup=build_main_keyboard(user_id))

# --------------------
# Referral menu callbacks
# --------------------
@dp.callback_query(lambda c: c.data == "ref_menu")
async def ref_menu(call: types.CallbackQuery):
    user_id = call.from_user.id
    # show new text + buttons as requested
    try:
        await call.message.edit_text(REF_TEXT_TEMPLATE, reply_markup=build_ref_keyboard(user_id), parse_mode=ParseMode.HTML)
    except Exception:
        await call.message.answer(REF_TEXT_TEMPLATE, reply_markup=build_ref_keyboard(user_id), parse_mode=ParseMode.HTML)

@dp.callback_query(lambda c: c.data == "ref_back")
async def ref_back(call: types.CallbackQuery):
    user_id = call.from_user.id
    stars = await get_user_stars(user_id)
    start_text = START_TEXT_TEMPLATE.format(stars=stars)
    try:
        await call.message.edit_text(start_text, reply_markup=build_main_keyboard(user_id), parse_mode=ParseMode.HTML)
    except Exception:
        await call.message.answer(start_text, reply_markup=build_main_keyboard(user_id), parse_mode=ParseMode.HTML)

@dp.callback_query(lambda c: c.data and c.data.startswith("copy_ref_"))
async def copy_ref(call: types.CallbackQuery):
    # show the link in an alert for user to copy (can't write to clipboard)
    try:
        user_id = int(call.data.split("_", 2)[2])
    except Exception:
        user_id = call.from_user.id
    try:
        me = await bot.get_me()
        bot_username = me.username or ""
    except Exception:
        bot_username = ""
    link = f"https://t.me/{bot_username}?start={user_id}" if bot_username else f"/start {user_id}"
    # show alert with link so user can copy
    await call.answer(text=f"Ссылка: {link}", show_alert=True)

# --------------------
# Play buttons handling
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

    # guard
    if count < 1:
        count = 1
    if count > 20:
        count = 20

    # If free (cost==0), check cooldown (3 minutes)
    if cost == 0:
        now_ts = int(time.time())
        free_next = await get_user_free_next(user_id)
        if now_ts < free_next:
            # show notification (toast) with remaining time
            rem = free_next - now_ts
            mins = rem // 60
            secs = rem % 60
            # localized strings per request
            smin = "минут" if mins != 1 else "минуту"
            ssec = "секунд" if secs != 1 else "секунду"
            await call.answer(text=f"🏀 До следующего бесплатного броска осталось {mins} {smin} и {secs} {ssec}", show_alert=False)
            return
        # allow: set next available
        await set_user_free_next(user_id, now_ts + 3 * 60)
        # no extra message "Использован бесплатный..." per request
    else:
        # check user stars
        stars = await get_user_stars(user_id)
        if stars < cost:
            # show purchase menu for missing stars
            missing = cost - stars
            text = (
                f"Получай крутой подарок за попадание 🏀 в кольцо\n\n"
                f"Товары:\n"
                f"{count} {word_form_mяч(count)} — цена: {missing}⭐ (недостающие звезды)"
            )
            try:
                await call.message.answer(text, reply_markup=build_purchase_kb(missing, user_id))
            except Exception:
                await call.message.reply(text, reply_markup=build_purchase_kb(missing, user_id))
            return
        # otherwise deduct cost from user's stars and proceed
        new_stars = await change_user_stars(user_id, -cost)
        # add cost to bot_balance
        await change_bot_balance(cost)

    # send dice with 0.5s delay between sends
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

    # ensure at least 5 seconds after first send
    if first_send_time is None:
        first_send_time = time.monotonic()
    elapsed = time.monotonic() - first_send_time
    wait_for = 5.0 - elapsed
    if wait_for > 0:
        await asyncio.sleep(wait_for)

    # collect results
    hits = 0
    results = []
    for msg in messages:
        val = getattr(msg, "dice", None)
        value = getattr(val, "value", 0) if val else 0
        results.append(int(value))
        if int(value) >= 4:
            hits += 1

    sent_count = len(results)

    # increment referral play count for this user (if referred)
    if sent_count > 0:
        await increment_referred_play_if_any(user_id)

    # if all sent hits -> penalty -15 to bot and notify group
    if sent_count > 0 and hits == sent_count:
        new_bot_bal = await change_bot_balance(-15)
        if GROUP_ID:
            try:
                await bot.send_message(GROUP_ID, f"⚠️ Произведено списание: <b>-15</b>\n💰 Текущий баланс бота: <b>{new_bot_bal}</b>")
            except Exception:
                log.exception("Failed to notify group about -15")

    # send results (only "Попал"/"Промах", no numbering)
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
    # send updated main menu with current user's stars
    stars_now = await get_user_stars(user_id)
    start_text = START_TEXT_TEMPLATE.format(stars=stars_now)
    await bot.send_message(call.message.chat.id, start_text, reply_markup=build_main_keyboard(user_id))

# --------------------
# Callbacks for purchase flow
# --------------------
@dp.callback_query(lambda c: c.data and c.data.startswith("buystars_"))
async def buystars_callback(call: types.CallbackQuery):
    # data: buystars_<missing>
    await call.answer()
    if not PAYMENTS_PROVIDER_TOKEN:
        await call.message.answer("Платёжный провайдер не настроен (PAYMENTS_PROVIDER_TOKEN отсутствует).")
        return
    try:
        missing = int(call.data.split("_", 1)[1])
    except Exception:
        await call.message.answer("Неверные данные покупки.")
        return
    user = call.from_user
    # create invoice: title, description, prices in smallest currency units
    # NOTE: this uses Telegram Payments — you must configure PAYMENT_TOKEN in ENV and bot must be enabled by provider
    title = f"Покупка {missing}⭐"
    description = f"Покупка {missing} звёзд для игры"
    # For demo: price in cents. You must change currency/prices according to your provider.
    amount_per_star_cents = 100  # example: 1 star = 1.00 (currency) -> 100 cents
    total_cents = missing * amount_per_star_cents
    prices = [LabeledPrice(label=f"{missing}⭐", amount=total_cents)]
    try:
        await bot.send_invoice(
            chat_id=user.id,
            title=title,
            description=description,
            payload=f"buy_{user.id}_{missing}_{int(time.time())}",
            provider_token=PAYMENTS_PROVIDER_TOKEN,
            currency="USD",  # change to provider currency
            prices=prices,
            start_parameter="buystars"
        )
    except Exception:
        log.exception("send_invoice failed")
        await call.message.answer("Не удалось создать платёж. Проверьте PAYMENTS_PROVIDER_TOKEN и настройки платёжей.")

@dp.pre_checkout_query()
async def on_precheckout(pre_q: types.PreCheckoutQuery):
    # always accept for now
    await bot.answer_pre_checkout_query(pre_q.id, ok=True)

@dp.message(types.ContentTypes.SUCCESSFUL_PAYMENT)
async def on_successful_payment(message: types.Message):
    # After successful payment, extract payload if available and credit stars
    try:
        pay = message.successful_payment
        payload = pay.invoice_payload  # our payload
        # payload format: buy_<user_id>_<missing>_<ts>
        parts = payload.split("_")
        if len(parts) >= 3 and parts[0] == "buy":
            target_user = int(parts[1])
            missing = int(parts[2])
            # credit user with missing stars
            new_val = await change_user_stars(target_user, missing)
            await message.answer(f"Оплата прошла успешно. Вам начислено {missing}⭐. Текущий баланс: {new_val}⭐")
            # optionally send the balls immediately: emulate pressing that play (we'll not auto-send balls — safer to let user click)
        else:
            await message.answer("Платёж принят. Спасибо!")
    except Exception:
        log.exception("on_successful_payment failed")

@dp.callback_query(lambda c: c.data and c.data.startswith("buyinfo_"))
async def buyinfo_callback(call: types.CallbackQuery):
    # show info in alert with instructions how to pay externally
    await call.answer(text="Платёжный провайдер не настроен. Свяжитесь с админом для покупки.", show_alert=True)

@dp.callback_query(lambda c: c.data == "buy_back")
async def buy_back(call: types.CallbackQuery):
    user_id = call.from_user.id
    stars = await get_user_stars(user_id)
    start_text = START_TEXT_TEMPLATE.format(stars=stars)
    try:
        await call.message.edit_text(start_text, reply_markup=build_main_keyboard(user_id), parse_mode=ParseMode.HTML)
    except Exception:
        await call.message.answer(start_text, reply_markup=build_main_keyboard(user_id), parse_mode=ParseMode.HTML)

# --------------------
# Command "баланс" — show or set other user's stars
# Format variants:
# - "баланс" -> show own
# - "баланс <user>" -> show that user (id or @username)
# - "баланс <user> <amount>" -> set that user's stars to amount (if ADMIN_ID set only admin can)
# --------------------
@dp.message()
async def balance_commands(message: types.Message):
    text = (message.text or "").strip()
    if not text:
        return
    lowered = text.lower()
    if not (lowered.startswith("/баланс") or lowered.split()[0] == "баланс"):
        return
    parts = text.split()
    # helper to resolve user identifier to id
    async def resolve_user_identifier(token: str) -> Optional[int]:
        # numeric?
        if token.lstrip("-").isdigit():
            return int(token)
        # @username?
        if token.startswith("@"):
            try:
                chat = await bot.get_chat(token)
                return chat.id
            except Exception:
                return None
        # try as plain username
        try:
            chat = await bot.get_chat(token)
            return chat.id
        except Exception:
            return None

    # three cases
    if len(parts) == 1:
        # show own stars
        user_id = message.from_user.id
        stars = await get_user_stars(user_id)
        await message.answer(f"💰 Ваш баланс: <b>{stars}⭐</b>")
        return

    # at least 2 parts
    target_token = parts[1]
    target_id = await resolve_user_identifier(target_token)
    if target_id is None:
        await message.answer("Не удалось найти указанного пользователя.")
        return

    if len(parts) == 2:
        # show target's balance
        stars = await get_user_stars(target_id)
        await message.answer(f"💰 Баланс пользователя: <b>{stars}⭐</b>")
        return

    # len >=3 -> set value
    # optional admin check
    if ADMIN_ID:
        try:
            if str(message.from_user.id) != str(ADMIN_ID):
                await message.answer("У вас нет прав для установки баланса другому пользователю.")
                return
        except Exception:
            pass

    if len(parts) >= 3 and parts[2].lstrip("-").isdigit():
        amount = int(parts[2])
        newv = await set_user_stars(target_id, amount)
        await message.answer(f"💰 Баланс пользователя {target_id} установлен: <b>{newv}⭐</b>")
    else:
        await message.answer("Неверный формат. Используй: баланс <user> <amount>")

# --------------------
# HEALTH endpoint for Render
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
    log.info("BOT starting")
    await init_db()
    # warm bot username
    try:
        await bot.get_me()
    except Exception:
        pass
    await start_web()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
