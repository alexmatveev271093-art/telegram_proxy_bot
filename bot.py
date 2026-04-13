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
# ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("Не задан BOT_TOKEN")


# =========================
# FILES
# =========================
SETTINGS_FILE = "settings.json"
CACHE_FILE = "cache.json"
USERS_FILE = "users.json"
LOGS_FILE = "logs.json"
BANS_FILE = "bans.json"
RESERVE_FILE = "reserve_proxies.json"


DEFAULT_SETTINGS = {
    "sponsor_link": "https://t.me/+T8J7eXlfvfc5NWNi",
    "sponsor_channel_id": -1002174184458,
    "pin_code": "7080",
    "max_ping": 250,
    "top_count": 5
}

PROXY_SOURCE_URL = (
    "https://raw.githubusercontent.com/"
    "SoliSpirit/mtproto/master/all_proxies.txt"
)

CHECK_LIMIT = 50
PROXY_COOLDOWN_SECONDS = 30


# =========================
# LOGGING
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# =========================
# BOT
# =========================
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML")
)

storage = MemoryStorage()
dp = Dispatcher(storage=storage)


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
# JSON UTILS
# =========================
def load_json(filename, default):
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return default


def save_json(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_settings():
    settings = load_json(SETTINGS_FILE, DEFAULT_SETTINGS)

    for k, v in DEFAULT_SETTINGS.items():
        if k not in settings:
            settings[k] = v

    save_json(SETTINGS_FILE, settings)
    return settings


def save_settings(settings):
    save_json(SETTINGS_FILE, settings)


# =========================
# USERS / LOGS / BANS
# =========================
def add_user(user_id):
    users = load_json(USERS_FILE, [])

    if user_id not in users:
        users.append(user_id)
        save_json(USERS_FILE, users)


def add_log(user_id, action):
    logs = load_json(LOGS_FILE, [])

    logs.insert(0, {
        "user_id": user_id,
        "action": action,
        "time": time.strftime("%d.%m %H:%M")
    })

    logs = logs[:200]
    save_json(LOGS_FILE, logs)


def is_banned(user_id):
    bans = load_json(BANS_FILE, [])
    return user_id in bans


def ban_user(user_id):
    bans = load_json(BANS_FILE, [])

    if user_id not in bans:
        bans.append(user_id)
        save_json(BANS_FILE, bans)


# =========================
# ANTIFLOOD
# =========================
proxy_cooldowns = {}


def can_request_proxy(user_id):
    now = time.time()
    last_time = proxy_cooldowns.get(user_id, 0)

    if now - last_time < PROXY_COOLDOWN_SECONDS:
        remaining = int(PROXY_COOLDOWN_SECONDS - (now - last_time))
        return False, remaining

    proxy_cooldowns[user_id] = now
    return True, 0


# =========================
# SAFE SEND
# =========================
last_messages = {}


async def safe_send(chat_id, text, reply_markup=None):
    try:
        if chat_id in last_messages:
            try:
                await bot.delete_message(chat_id, last_messages[chat_id])
            except:
                pass

        msg = await bot.send_message(
            chat_id,
            text,
            reply_markup=reply_markup
        )

        last_messages[chat_id] = msg.message_id

    except Exception as e:
        logger.error(f"safe_send error: {e}")


# =========================
# KEYBOARDS
# =========================
start_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Проверить подписку ✅")]
    ],
    resize_keyboard=True
)

check_sub_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Проверить подписку ✅")]
    ],
    resize_keyboard=True
)

proxy_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Дай прокси 🔥")]
    ],
    resize_keyboard=True
)

cancel_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="❌ Отмена")]
    ],
    resize_keyboard=True
)


# =========================
# LOG MIDDLEWARE
# =========================
@dp.update.outer_middleware()
async def log_updates(handler, event, data):
    try:
        if hasattr(event, "message") and event.message:
            logger.info(
                f"{event.message.text} "
                f"от {event.message.from_user.id}"
            )
    except:
        pass

    return await handler(event, data)


# =========================
# SUB CHECK
# =========================
async def is_subscribed(user_id):
    settings = get_settings()

    if not settings.get("sponsor_link"):
        return True

    try:
        member = await bot.get_chat_member(
            settings["sponsor_channel_id"],
            user_id
        )

        return member.status in [
            "member",
            "administrator",
            "creator"
        ]

    except:
        return False


# =========================
# PROXY LOAD
# =========================
async def load_proxy_list():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(PROXY_SOURCE_URL, timeout=20) as resp:
                text = await resp.text()

        proxies = []

        for line in text.splitlines():
            line = line.strip()

            if not line:
                continue

            parsed = urlparse(line)
            params = parse_qs(parsed.query)

            if not all(k in params for k in ["server", "port", "secret"]):
                continue

            proxies.append({
                "server": params["server"][0],
                "port": int(params["port"][0]),
                "secret": params["secret"][0]
            })

        return proxies

    except Exception as e:
        logger.error(f"Ошибка загрузки: {e}")
        return []


async def check_proxy(proxy):
    try:
        start = time.perf_counter()

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(proxy["server"], proxy["port"]),
            timeout=5
        )

        ping = (time.perf_counter() - start) * 1000

        writer.close()
        await writer.wait_closed()

        return {
            "proxy": proxy,
            "ping": round(ping, 2)
        }

    except:
        return None


async def find_best_proxies():
    settings = get_settings()

    proxy_list = await load_proxy_list()

    if not proxy_list:
        return load_json(CACHE_FILE, [])

    tasks = [
        check_proxy(proxy)
        for proxy in proxy_list[:CHECK_LIMIT]
    ]

    results = await asyncio.gather(*tasks)

    working = [
        x for x in results
        if x and x["ping"] <= settings["max_ping"]
    ]

    working.sort(key=lambda x: x["ping"])
    working = working[:settings["top_count"]]

    save_json(CACHE_FILE, working)

    return working


def build_mtproto_link(proxy):
    return (
        f"tg://proxy?"
        f"server={proxy['server']}"
        f"&port={proxy['port']}"
        f"&secret={proxy['secret']}"
    )


def build_post(proxies):
    reserve = load_json(RESERVE_FILE, [])

    text = "🔥 Свежие MTProto прокси:\n\n"

    for i, item in enumerate(proxies, start=1):
        link = build_mtproto_link(item["proxy"])

        text += (
            f"{i}️⃣ "
            f'<a href="{link}">Подключить прокси</a>\n'
            f"⚡ ping: {item['ping']} ms\n\n"
        )

    if reserve:
        text += "\n📌 Резерв:\n\n"

        for i, link in enumerate(reserve[:5], start=1):
            text += f"{i}️⃣ <a href=\"{link}\">Резервный прокси</a>\n\n"

    return text


async def send_proxies(chat_id):
    try:
        proxies = await find_best_proxies()

        if not proxies:
            await safe_send(
                chat_id,
                "❌ Рабочих прокси не найдено",
                reply_markup=proxy_kb
            )
            return

        await safe_send(
            chat_id,
            build_post(proxies),
            reply_markup=proxy_kb
        )

    except Exception as e:
        logger.error(f"Ошибка send_proxies: {e}")

        await safe_send(
            chat_id,
            f"❌ Ошибка:\n{str(e)}",
            reply_markup=proxy_kb
        )


# =========================
# START
# =========================
@dp.message(Command("start"))
async def start_handler(message: types.Message):
    user_id = message.from_user.id

    if is_banned(user_id):
        await safe_send(message.chat.id, "⛔ Вы заблокированы")
        return

    add_user(user_id)
    add_log(user_id, "/start")

    settings = get_settings()

    if settings.get("sponsor_link"):
        await safe_send(
            message.chat.id,
            f"👋 Добро пожаловать!\n\n"
            f"Подпишись:\n{settings['sponsor_link']}\n\n"
            f"Потом нажми кнопку:",
            reply_markup=check_sub_kb
        )
    else:
        await safe_send(
            message.chat.id,
            "👋 Добро пожаловать!",
            reply_markup=proxy_kb
        )


# =========================
# CHECK SUB
# =========================
@dp.message(F.text == "Проверить подписку ✅")
async def check_sub_handler(message: types.Message):
    user_id = message.from_user.id

    if await is_subscribed(user_id):
        await safe_send(
            message.chat.id,
            "✅ Подписка подтверждена\nТеперь можешь получить прокси 👇",
            reply_markup=proxy_kb
        )
    else:
        settings = get_settings()

        await safe_send(
            message.chat.id,
            f"❌ Подпишись:\n{settings['sponsor_link']}",
            reply_markup=check_sub_kb
        )


# =========================
# PROXY
# =========================
@dp.message(F.text == "Дай прокси 🔥")
async def proxy_handler(message: types.Message):
    user_id = message.from_user.id

    if is_banned(user_id):
        await safe_send(message.chat.id, "⛔ Вы заблокированы")
        return

    allowed, wait_time = can_request_proxy(user_id)

    if not allowed:
        await safe_send(
            message.chat.id,
            f"⏳ Подожди {wait_time} сек.",
            reply_markup=proxy_kb
        )
        return

    settings = get_settings()

    if settings.get("sponsor_link"):
        subscribed = await is_subscribed(user_id)

        if not subscribed:
            await safe_send(
                message.chat.id,
                "❌ Сначала подпишись",
                reply_markup=check_sub_kb
            )
            return

    add_log(user_id, "запросил прокси")

    await safe_send(
        message.chat.id,
        "🔍 Ищу лучшие прокси...\nПодождите 10–30 секунд.",
        reply_markup=proxy_kb
    )

    await send_proxies(message.chat.id)


# =========================
# CANCEL
# =========================
@dp.message(F.text == "❌ Отмена")
async def cancel_handler(message: types.Message, state: FSMContext):
    await state.clear()

    await safe_send(
        message.chat.id,
        "Действие отменено",
        reply_markup=start_kb
    )


# =========================
# ADMIN
# =========================
admin_sessions = {}
admin_attempts = {}


def is_admin(user_id):
    return admin_sessions.get(user_id, False)


def admin_main_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="🔄 Проверить сейчас")],
            [KeyboardButton(text="🔒 Выйти")]
        ],
        resize_keyboard=True
    )


@dp.message(Command("admin"))
async def admin_command(message: types.Message, state: FSMContext):
    if is_banned(message.from_user.id):
        await safe_send(message.chat.id, "⛔ Вы заблокированы")
        return

    await state.set_state(UserStates.waiting_admin_pin)

    await safe_send(
        message.chat.id,
        "Введите PIN:",
        reply_markup=cancel_kb
    )


@dp.message(UserStates.waiting_admin_pin)
async def admin_pin_input(message: types.Message, state: FSMContext):
    settings = get_settings()
    user_id = message.from_user.id

    if message.text == settings["pin_code"]:
        admin_sessions[user_id] = True
        admin_attempts[user_id] = 0

        await state.clear()

        await safe_send(
            message.chat.id,
            "🔐 Админка",
            reply_markup=admin_main_kb()
        )
        return

    admin_attempts[user_id] = admin_attempts.get(user_id, 0) + 1

    if admin_attempts[user_id] >= 3:
        ban_user(user_id)

        await state.clear()

        await safe_send(
            message.chat.id,
            "⛔ Вы заблокированы"
        )
        return

    await safe_send(
        message.chat.id,
        "❌ Неверный PIN",
        reply_markup=cancel_kb
    )


@dp.message(F.text == "📊 Статистика")
async def stats_handler(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    users = load_json(USERS_FILE, [])
    bans = load_json(BANS_FILE, [])
    logs = load_json(LOGS_FILE, [])

    proxy_requests = len([
        x for x in logs if "прокси" in x["action"]
    ])

    text = (
        f"📊 Статистика\n\n"
        f"👥 Пользователей: {len(users)}\n"
        f"🚀 Запросов прокси: {proxy_requests}\n"
        f"🚫 Бан: {len(bans)}"
    )

    await safe_send(
        message.chat.id,
        text,
        reply_markup=admin_main_kb()
    )


@dp.message(F.text == "🔄 Проверить сейчас")
async def admin_check_now(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    await safe_send(
        message.chat.id,
        "🔍 Проверка...",
        reply_markup=admin_main_kb()
    )

    await send_proxies(message.chat.id)


@dp.message(F.text == "🔒 Выйти")
async def exit_admin(message: types.Message):
    admin_sessions[message.from_user.id] = False

    await safe_send(
        message.chat.id,
        "Вы вышли",
        reply_markup=start_kb
    )


# =========================
# MAIN
# =========================
async def main():
    logger.info("БОТ ЗАПУЩЕН")

    await bot.delete_webhook(drop_pending_updates=True)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())