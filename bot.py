import os
import json
import time
import asyncio
import logging
import aiohttp

from urllib.parse import urlparse, parse_qs

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State


# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN missing")


SETTINGS_FILE = "settings.json"
USERS_FILE = "users.json"
LOGS_FILE = "logs.json"
BANS_FILE = "bans.json"
CACHE_FILE = "cache.json"
RESERVE_FILE = "reserve_proxies.json"


DEFAULT_SETTINGS = {
    "sponsor_link": "https://t.me/+T8J7eXlfvfc5NWNi",
    "sponsor_channel_id": -1002174184458,
    "pin_code": "7080",
    "max_ping": 250,
    "top_count": 5
}

PROXY_SOURCE_URL = "https://raw.githubusercontent.com/SoliSpirit/mtproto/master/all_proxies.txt"
CHECK_LIMIT = 50


# =========================
# LOGGING
# =========================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())


# =========================
# FSM
# =========================
class UserStates(StatesGroup):
    waiting_admin_pin = State()
    waiting_sponsor_link = State()
    waiting_proxy = State()
    waiting_broadcast = State()   
    waiting_ban_id = State()
    waiting_unban_id = State()
    waiting_new_pin = State()
    waiting_new_ping = State()


# =========================
# JSON
# =========================
def load_json(f, default):
    try:
        with open(f, "r", encoding="utf-8") as x:
            return json.load(x)
    except:
        return default


def save_json(f, data):
    with open(f, "w", encoding="utf-8") as x:
        json.dump(data, x, ensure_ascii=False, indent=2)


def get_settings():
    s = load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
    for k, v in DEFAULT_SETTINGS.items():
        if k not in s:
            s[k] = v
    save_json(SETTINGS_FILE, s)
    return s


# =========================
# USERS / LOGS / BANS
# =========================
def add_user(uid):
    u = load_json(USERS_FILE, [])
    if uid not in u:
        u.append(uid)
        save_json(USERS_FILE, u)


def add_log(uid, action):
    l = load_json(LOGS_FILE, [])
    l.insert(0, {"user_id": uid, "action": action, "time": time.strftime("%d.%m %H:%M")})
    save_json(LOGS_FILE, l[:200])


def is_banned(uid):
    return uid in load_json(BANS_FILE, [])


def ban_user(uid):
    b = load_json(BANS_FILE, [])
    if uid not in b:
        b.append(uid)
    save_json(BANS_FILE, b)


# =========================
# SAFE MESSAGE (1 MSG ONLY)
# =========================
last_msg = {}

async def safe_send(chat_id, text, kb=None):
    try:
        if chat_id in last_msg:
            try:
                await bot.delete_message(chat_id, last_msg[chat_id])
            except:
                pass

        msg = await bot.send_message(chat_id, text, reply_markup=kb)
        last_msg[chat_id] = msg.message_id

    except Exception as e:
        logger.error(e)


# =========================
# ANTIFLOOD
# =========================
cooldown = {}
CD = 15

def check_cd(uid):
    now = time.time()
    last = cooldown.get(uid, 0)

    if now - last < CD:
        return False, int(CD - (now - last))

    cooldown[uid] = now
    return True, 0


# =========================
# KEYBOARDS (как у тебя)
# =========================
start_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Проверить подписку ✅")]],
    resize_keyboard=True,
    is_persistent=True
)

proxy_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Дай прокси 🔥")]],
    resize_keyboard=True,
    is_persistent=True
)

check_kb = start_kb


# =========================
# SUB CHECK
# =========================
async def is_subscribed(uid):
    s = get_settings()
    try:
        m = await bot.get_chat_member(s["sponsor_channel_id"], uid)
        return m.status in ["member", "administrator", "creator"]
    except:
        return False


# =========================
# PROXY LOAD
# =========================
async def load_proxies():
    async with aiohttp.ClientSession() as s:
        async with s.get(PROXY_SOURCE_URL) as r:
            txt = await r.text()

    res = []
    for line in txt.splitlines():
        p = urlparse(line)
        q = parse_qs(p.query)

        if not all(k in q for k in ["server", "port", "secret"]):
            continue

        res.append({
            "server": q["server"][0],
            "port": int(q["port"][0]),
            "secret": q["secret"][0]
        })

    return res


async def check_proxy(p):
    try:
        start = time.perf_counter()
        r, w = await asyncio.wait_for(
            asyncio.open_connection(p["server"], p["port"]),
            timeout=5
        )
        w.close()
        await w.wait_closed()

        return {"proxy": p, "ping": (time.perf_counter() - start) * 1000}
    except:
        return None


async def find_best():
    s = get_settings()
    plist = await load_proxies()

    tasks = [check_proxy(p) for p in plist[:CHECK_LIMIT]]
    res = await asyncio.gather(*tasks)

    ok = [x for x in res if x and x["ping"] <= s["max_ping"]]
    ok.sort(key=lambda x: x["ping"])

    return ok[:s["top_count"]]


# =========================
# LINK
# =========================
def link(p):
    return f"tg://proxy?server={p['server']}&port={p['port']}&secret={p['secret']}"


# =========================
# START FLOW (НОВЫЙ)
# =========================
@dp.message(Command("start"))
async def start(m: types.Message):
    uid = m.from_user.id

    if is_banned(uid):
        return await safe_send(m.chat.id, "⛔ banned")

    add_user(uid)
    add_log(uid, "/start")

    s = get_settings()

    if await is_subscribed(uid):
        return await safe_send(m.chat.id, "👇", proxy_kb)

    await safe_send(m.chat.id, f"Подпишись:\n{s['sponsor_link']}", check_kb)


# =========================
# CHECK SUB
# =========================
@dp.message(F.text == "Проверить подписку ✅")
async def check(m: types.Message):
    uid = m.from_user.id

    if await is_subscribed(uid):
        return await safe_send(m.chat.id, "OK", proxy_kb)

    return await safe_send(m.chat.id, "❌ нет подписки", check_kb)


# =========================
# PROXY BUTTON
# =========================
@dp.message(F.text == "Дай прокси 🔥")
async def proxy(m: types.Message):
    uid = m.from_user.id

    if is_banned(uid):
        return

    ok, wait = check_cd(uid)
    if not ok:
        return await safe_send(m.chat.id, f"⏳ {wait}s", proxy_kb)

    if not await is_subscribed(uid):
        return await safe_send(m.chat.id, "❌ подпишись", check_kb)

    await safe_send(m.chat.id, "🔍 ищу...", proxy_kb)

    proxies = await find_best()

    if not proxies:
        return await safe_send(m.chat.id, "❌ нет прокси", proxy_kb)

    text = "🔥 PROXIES:\n\n"
    for i, p in enumerate(proxies, 1):
        text += f"{i}. <a href='{link(p['proxy'])}'>CONNECT</a>\n"

    await safe_send(m.chat.id, text, proxy_kb)


# =========================
# 🔐 ADMIN CORE
# =========================

admin_sessions = {}
admin_attempts = {}


def is_admin(uid):
    return admin_sessions.get(uid, False)


def admin_main_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="📜 Логи")],
            [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="🔗 Ссылка спонсора")],
            [KeyboardButton(text="🔄 Проверить сейчас"), KeyboardButton(text="➕ Добавить прокси")],
            [KeyboardButton(text="🚫 Бан-лист"), KeyboardButton(text="📢 Рассылка")],
            [KeyboardButton(text="👥 Пользователи"), KeyboardButton(text="🔒 Выйти")]
        ],
        resize_keyboard=True
    )


# =========================
# /admin вход
# =========================

@dp.message(Command("admin"))
async def admin_start(message: types.Message, state: FSMContext):
    uid = message.from_user.id

    await state.set_state(UserStates.waiting_admin_pin)

    await message.answer(
        "🔐 Введите PIN-код:",
        reply_markup=cancel_kb
    )


# =========================
# PIN проверка
# =========================

@dp.message(UserStates.waiting_admin_pin)
async def admin_pin_handler(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    settings = get_settings()

    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Отмена", reply_markup=start_kb)
        return

    if message.text == settings["pin_code"]:
        admin_sessions[uid] = True
        admin_attempts[uid] = 0

        await state.clear()

        await message.answer(
            "🔐 Админ-панель",
            reply_markup=admin_main_kb()
        )
        return

    admin_attempts[uid] = admin_attempts.get(uid, 0) + 1

    if admin_attempts[uid] >= 3:
        ban_user(uid)
        await state.clear()
        await message.answer("⛔ Заблокирован")
        return

    await message.answer("❌ Неверный PIN")


# =========================
# ВЫХОД ИЗ АДМИНКИ
# =========================

@dp.message(F.text == "🔒 Выйти")
async def admin_exit(message: types.Message):
    admin_sessions[message.from_user.id] = False
    await message.answer("Вы вышли", reply_markup=start_kb)


# =========================
# СТАТИСТИКА
# =========================

@dp.message(F.text == "📊 Статистика")
async def admin_stats(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    users = load_json(USERS_FILE, [])
    bans = load_json(BANS_FILE, [])
    logs = load_json(LOGS_FILE, [])

    proxy_requests = len([x for x in logs if "прокси" in x["action"]])

    await message.answer(
        f"📊 Статистика\n\n"
        f"👥 Пользователи: {len(users)}\n"
        f"🚀 Прокси запросы: {proxy_requests}\n"
        f"🚫 Баны: {len(bans)}",
        reply_markup=admin_main_kb()
    )


# =========================
# ЛОГИ
# =========================

@dp.message(F.text == "📜 Логи")
async def admin_logs(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    logs = load_json(LOGS_FILE, [])[:20]

    text = "📜 Последние действия:\n\n"

    for log in logs:
        text += f"{log['time']} | {log['user_id']} | {log['action']}\n"

    await message.answer(text, reply_markup=admin_main_kb())


# =========================
# ПОЛЬЗОВАТЕЛИ
# =========================

@dp.message(F.text == "👥 Пользователи")
async def admin_users(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    users = load_json(USERS_FILE, [])[-20:]

    await message.answer(
        "\n".join(map(str, users)),
        reply_markup=admin_main_kb()
    )


# =========================
# БАН-ЛИСТ
# =========================

@dp.message(F.text == "🚫 Бан-лист")
async def admin_bans(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    bans = load_json(BANS_FILE, [])

    text = "🚫 Бан-лист:\n\n" + ("\n".join(map(str, bans)) if bans else "Пусто")

    await message.answer(text, reply_markup=admin_main_kb())


# =========================
# РАССЫЛКА
# =========================

@dp.message(F.text == "📢 Рассылка")
async def admin_broadcast(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.set_state(UserStates.waiting_broadcast)
    await message.answer("Введите текст рассылки:", reply_markup=cancel_kb)


@dp.message(UserStates.waiting_broadcast)
async def admin_broadcast_send(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Отмена", reply_markup=admin_main_kb())
        return

    users = load_json(USERS_FILE, [])

    sent = 0

    for uid in users:
        try:
            await bot.send_message(uid, message.text)
            sent += 1
        except:
            pass

    await state.clear()

    await message.answer(
        f"✅ Отправлено: {sent}",
        reply_markup=admin_main_kb()
    )


# =========================
# ССЫЛКА СПОНСОРА
# =========================

@dp.message(F.text == "🔗 Ссылка спонсора")
async def admin_sponsor(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    s = get_settings()

    await message.answer(
        f"🔗 Текущая ссылка:\n{s['sponsor_link']}",
        reply_markup=admin_main_kb()
    )


# =========================
# ОБНОВЛЕНИЕ ПИНА / ПИНГ / КЭШ
# (если у тебя есть FSM states — просто подключается сюда)
# =========================


# =========================
# MAIN
# =========================
async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
