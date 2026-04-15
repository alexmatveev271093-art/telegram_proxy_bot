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
# ФАЙЛЫ
# =========================
SETTINGS_FILE = "settings.json"
CACHE_FILE = "cache.json"
USERS_FILE = "users.json"
LOGS_FILE = "logs.json"
BANS_FILE = "bans.json"
RESERVE_FILE = "reserve_proxies.json"

# =========================
# ДЕФОЛТ
# =========================
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

# АНТИСПАМ
ANTI_SPAM_SECONDS = 30
USER_TIMER_TASKS = {}

# =========================
# ЛОГИ
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
# JSON UTILS
# =========================
def load_json(filename, default):
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def save_json(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_settings():
    settings = load_json(SETTINGS_FILE, DEFAULT_SETTINGS)
    for k, v in DEFAULT_SETTINGS.items():
        settings.setdefault(k, v)
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
    save_json(LOGS_FILE, logs[:200])

def is_banned(user_id):
    return user_id in load_json(BANS_FILE, [])

def ban_user(user_id):
    bans = load_json(BANS_FILE, [])
    if user_id not in bans:
        bans.append(user_id)
        save_json(BANS_FILE, bans)

# =========================
# SAFE SEND + УДАЛЕНИЕ
# =========================
LAST_MESSAGES = {}

async def safe_send(chat_id, text, reply_markup=None):
    try:
        # удаляем прошлое сообщение
        if chat_id in LAST_MESSAGES:
            try:
                await bot.delete_message(chat_id, LAST_MESSAGES[chat_id])
            except:
                pass

        msg = await bot.send_message(
            chat_id,
            text,
            reply_markup=reply_markup,
            protect_content=True,
            disable_web_page_preview=True
        )

        LAST_MESSAGES[chat_id] = msg.message_id

    except Exception as e:
        logger.error(f"safe_send error: {e}")

# =========================
# АНТИСПАМ
# =========================
USER_LAST_REQUEST = {}

def check_antispam(user_id):
    now = time.time()
    last = USER_LAST_REQUEST.get(user_id, 0)

    if now - last < ANTI_SPAM_SECONDS:
        return False

    USER_LAST_REQUEST[user_id] = now
    return True

# =========================
# ОЧЕРЕДЬ
# =========================
QUEUE = asyncio.Queue()

async def worker():
    while True:
        chat_id = await QUEUE.get()

        try:
            await send_proxies(chat_id)
        except Exception as e:
            logger.error(f"QUEUE error: {e}")

        await asyncio.sleep(1)  # защита от нагрузки
        QUEUE.task_done()

# =========================
# КНОПКИ
# =========================
def start_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Дай прокси 🔥")]],
        resize_keyboard=True
    )

def check_sub_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Проверить подписку ✅")]],
        resize_keyboard=True
    )

def proxy_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Дай прокси 🔥")]],
        resize_keyboard=True
    )

def cancel_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True
    )

# =========================
# LOAD PROXY
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
        logger.error(f"Ошибка загрузки прокси: {e}")
        return []


# =========================
# CHECK PROXY
# =========================
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

    except (OSError, asyncio.TimeoutError, Exception):
        return None


# =========================
# BEST PROXIES
# =========================
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


# =========================
# BUILD LINK
# =========================
def build_mtproto_link(proxy):
    return (
        f"tg://proxy?"
        f"server={proxy['server']}"
        f"&port={proxy['port']}"
        f"&secret={proxy['secret']}"
    )


# =========================
# BUILD POST
# =========================
def build_post(proxies):
    reserve = load_json(RESERVE_FILE, [])

    text = (
        '<a href="https://t.me/+T8J7eXlfvfc5NWNi">🔥 <b>Good Place AI</b> 🤖</a>\n\n'
        "⚡️ Здесь: AI • Мемы • Польза\n\n"
        '📱 <a href="https://www.tiktok.com/@good_place_67">TikTok</a> | '
        '▶️ <a href="https://www.youtube.com/@gd_place">YouTube</a>\n\n'
        "━━━━━━━━━━━━━━━\n\n"
        "🚀 <b>СВЕЖИЕ прокси для Telegram 👇</b>\n\n"
        "💡 ЖМИ и подключай — работает сразу\n"
        "(если не зашёл 🤔 — попробуй следующий 😉 — разлетаются как пирожки 🔥)\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "🔥 <b>ТОП (самые стабильные):</b>\n\n"
    )

    # основные прокси
    for i, item in enumerate(proxies, start=1):
        link = build_mtproto_link(item["proxy"])

        text += (
            f"{i}️⃣ "
            f'<a href="{link}">Подключить прокси 👈</a>\n\n'
        )

    # резерв
    if reserve:
        text += "━━━━━━━━━━━━━━━\n\n📌 <b>НАШ РЕЗЕРВ 👇</b>\n\n"

        for i, link in enumerate(reserve[:5], start=1):
            text += (
                f"{i}️⃣ "
                f'<a href="{link}">Резервный прокси ⚡️</a>\n\n'
            )

    text += (
        "━━━━━━━━━━━━━━━\n\n"
        "✅ <b>Поделись с друзьями ботом — пригодится 😉</b>"
    )

    return text


# =========================
# SEND PROXIES
# =========================
async def send_proxies(chat_id):
    try:
        await safe_send(
            chat_id,
            "🔍 Ищу самые быстрые прокси...\nПодожди 10–30 сек ⏳"
        )

        proxies = await find_best_proxies()

        if not proxies:
            await safe_send(
                chat_id,
                "❌ Не удалось найти рабочие прокси 😢\nПопробуй позже"
            )
            return

        text = build_post(proxies)

        await safe_send(
            chat_id,
            text,
            reply_markup=proxy_kb()
        )

    except Exception as e:
        logger.error(f"send_proxies error: {e}")

        await safe_send(
            chat_id,
            f"❌ Ошибка:\n{str(e)}"
        )


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
        return member.status in ["member", "administrator", "creator"]
    except:
        return False


# =========================
# /start
# =========================
@dp.message(Command("start"))
async def start_handler(message: types.Message):
    try:
        await message.delete()
    except:
        pass
    
    user_id = message.from_user.id

    if is_banned(user_id):
        await safe_send(user_id, "⛔ Вы заблокированы")
        return

    add_user(user_id)
    add_log(user_id, "/start")

    settings = get_settings()

    # если спонсор выключен
    if not settings.get("sponsor_link"):
        await safe_send(
            user_id,
            "🚀 Готово! Жми кнопку ниже 👇",
            reply_markup=proxy_kb()
        )
        return

    # проверка подписки
    if await is_subscribed(user_id):
        await safe_send(
            user_id,
            "✅ Подписка уже есть!\nЖми кнопку 👇",
            reply_markup=proxy_kb()
        )
    else:
        await safe_send(
            user_id,
            "📢 Подпишись на канал спонсора 👇\n\n"
            f"{settings['sponsor_link']}",
            reply_markup=check_sub_kb()
        )


# =========================
# CHECK SUB BUTTON
# =========================
@dp.message(F.text == "Проверить подписку ✅")
async def check_sub_handler(message: types.Message):
    try:
        await message.delete()
    except:
        pass

    user_id = message.from_user.id

    if await is_subscribed(user_id):
        await safe_send(
            user_id,
            "✅ Подписка подтверждена!\nТеперь жми 👇",
            reply_markup=proxy_kb()
        )
    else:
        await safe_send(
            user_id,
            "❌ Ты ещё не подписан\n\n"
            "Подпишись и нажми кнопку снова 👇",
            reply_markup=check_sub_kb()
        )


# =========================
# PROXY BUTTON
# =========================
@dp.message(F.text == "Дай прокси 🔥")
async def proxy_handler(message: types.Message):
    try:
        await message.delete()
    except:
        pass
    
    user_id = message.from_user.id

    if is_banned(user_id):
        await safe_send(user_id, "⛔ Вы заблокированы")
        return

    settings = get_settings()

    now = time.time()
    last = USER_LAST_REQUEST.get(user_id, 0)
    remaining = ANTI_SPAM_SECONDS - (now - last)

    # ⛔ если спамит
    if remaining > 0:
        # уже есть таймер — не создаём новый
        if user_id in USER_TIMER_TASKS:
            return

        await safe_send(
            user_id,
            f"⏳ Подожди немного перед следующим запросом ({int(remaining)} сек)",
            reply_markup=None
        )

        # создаём таймер
        async def delayed_send():
            await asyncio.sleep(remaining)

            USER_LAST_REQUEST[user_id] = time.time()

            await send_proxies(user_id)

            USER_TIMER_TASKS.pop(user_id, None)

        task = asyncio.create_task(delayed_send())
        USER_TIMER_TASKS[user_id] = task

        return

    # ✅ первый нормальный запрос
    USER_LAST_REQUEST[user_id] = now

    add_log(user_id, "запросил прокси")

    await safe_send(
        user_id,
        "⏳ Ищу прокси...",
        reply_markup=None
    )

    await send_proxies(user_id)



# =========================
# ADMIN SESSIONS
# =========================
admin_sessions = {}
admin_attempts = {}

def is_admin(user_id):
    return admin_sessions.get(user_id, False)


# =========================
# ADMIN KEYBOARDS
# =========================
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

def sponsor_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✏️ Изменить ссылку")],
            [KeyboardButton(text="🗑 Удалить ссылку")],
            [KeyboardButton(text="↩️ Назад")]
        ],
        resize_keyboard=True
    )

def settings_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔑 Изменить PIN")],
            [KeyboardButton(text="📶 Изменить лимит пинга")],
            [KeyboardButton(text="🧹 Очистить кэш")],
            [KeyboardButton(text="↩️ Назад")]
        ],
        resize_keyboard=True
    )

def ban_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Забанить ID")],
            [KeyboardButton(text="🔓 Разбанить")],
            [KeyboardButton(text="🗑 Очистить бан-лист")],
            [KeyboardButton(text="↩️ Назад")]
        ],
        resize_keyboard=True
    )


# =========================
# /admin
# =========================
@dp.message(Command("admin"))
async def admin_command(message: types.Message, state: FSMContext):
    try:
        await message.delete()
    except:
        pass

    if is_banned(message.from_user.id):
        return

    await state.set_state(UserStates.waiting_admin_pin)

    await safe_send(
        message.chat.id,
        "Введите PIN код:",
        reply_markup=cancel_kb()
    )


@dp.message(UserStates.waiting_admin_pin)
async def admin_pin_input(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    settings = get_settings()

    if message.text == "❌ Отмена":
        await state.clear()
        await safe_send(message.chat.id, "Отмена", reply_markup=start_kb())
        return

    if message.text == settings["pin_code"]:
        admin_sessions[user_id] = True
        admin_attempts[user_id] = 0

        add_log(user_id, "вошёл в админку")

        await state.clear()

        await safe_send(
            message.chat.id,
            "🔐 Админ-панель",
            reply_markup=admin_main_kb()
        )
        return

    # ошибки
    admin_attempts[user_id] = admin_attempts.get(user_id, 0) + 1

    if admin_attempts[user_id] >= 3:
        ban_user(user_id)

        await state.clear()

        await safe_send(message.chat.id, "⛔ Вы заблокированы")
        return

    await safe_send(message.chat.id, "❌ Неверный PIN", reply_markup=cancel_kb())


@dp.message(F.text == "🔒 Выйти")
async def exit_admin(message: types.Message):
    admin_sessions[message.from_user.id] = False

    await safe_send(
        message.chat.id,
        "Вы вышли из админки",
        reply_markup=start_kb()
    )



@dp.message(F.text == "📊 Статистика")
async def stats_handler(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    users = load_json(USERS_FILE, [])
    logs = load_json(LOGS_FILE, [])
    bans = load_json(BANS_FILE, [])

    proxy_requests = len([x for x in logs if "прокси" in x["action"]])

    text = (
        f"📊 Статистика\n\n"
        f"👥 Пользователей: {len(users)}\n"
        f"🚀 Запросов прокси: {proxy_requests}\n"
        f"🚫 Забанено: {len(bans)}"
    )

    await safe_send(message.chat.id, text, reply_markup=admin_main_kb())


@dp.message(F.text == "📜 Логи")
async def logs_handler(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    logs = load_json(LOGS_FILE, [])[:20]

    text = "📜 Последние действия:\n\n"

    for log in logs:
        text += f"{log['time']} — {log['user_id']} — {log['action']}\n"

    await safe_send(message.chat.id, text, reply_markup=admin_main_kb())


@dp.message(F.text == "🔑 Изменить PIN")
async def change_pin_start(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.set_state(UserStates.waiting_new_pin)
    await safe_send(message.chat.id, "Введите новый PIN:", reply_markup=cancel_kb())


@dp.message(UserStates.waiting_new_pin)
async def save_new_pin(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await safe_send(message.chat.id, "Отмена", reply_markup=settings_kb())
        return

    settings = get_settings()
    settings["pin_code"] = message.text
    save_settings(settings)

    await state.clear()

    await safe_send(message.chat.id, "✅ PIN обновлён", reply_markup=settings_kb())


@dp.message(F.text == "📶 Изменить лимит пинга")
async def ping_change_start(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.set_state(UserStates.waiting_new_ping)
    await safe_send(message.chat.id, "Введите новый пинг:", reply_markup=cancel_kb())


@dp.message(UserStates.waiting_new_ping)
async def save_new_ping(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await safe_send(message.chat.id, "Отмена", reply_markup=settings_kb())
        return

    if not message.text.isdigit():
        await safe_send(message.chat.id, "Введите число")
        return

    settings = get_settings()
    settings["max_ping"] = int(message.text)
    save_settings(settings)

    await state.clear()

    await safe_send(message.chat.id, "✅ Обновлено", reply_markup=settings_kb())


@dp.message(F.text == "🚫 Бан-лист")
async def banlist_handler(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    bans = load_json(BANS_FILE, [])

    text = "🚫 Бан-лист:\n\n"

    if not bans:
        text += "Пусто"
    else:
        for i, uid in enumerate(bans, 1):
            text += f"{i}. {uid}\n"

    await safe_send(message.chat.id, text, reply_markup=ban_kb())


@dp.message(F.text == "➕ Забанить ID")
async def ban_start(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.set_state(UserStates.waiting_ban_id)
    await safe_send(message.chat.id, "Введите ID:", reply_markup=cancel_kb())


@dp.message(UserStates.waiting_ban_id)
async def save_ban(message: types.Message, state: FSMContext):
    try:
        uid = int(message.text)
        ban_user(uid)

        await state.clear()
        await safe_send(message.chat.id, "✅ Забанен", reply_markup=ban_kb())

    except:
        await safe_send(message.chat.id, "Ошибка ID")


@dp.message(F.text == "🔓 Разбанить")
async def unban_start(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.set_state(UserStates.waiting_unban_id)
    await safe_send(message.chat.id, "Введите ID:", reply_markup=cancel_kb())


@dp.message(UserStates.waiting_unban_id)
async def save_unban(message: types.Message, state: FSMContext):
    try:
        uid = int(message.text)

        bans = load_json(BANS_FILE, [])
        if uid in bans:
            bans.remove(uid)
            save_json(BANS_FILE, bans)

        await state.clear()
        await safe_send(message.chat.id, "✅ Разбан", reply_markup=ban_kb())

    except:
        await safe_send(message.chat.id, "Ошибка ID")



@dp.message(F.text == "🗑 Очистить бан-лист")
async def clear_banlist(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    save_json(BANS_FILE, [])

    await safe_send(
        message.chat.id,
        "✅ Бан-лист очищен",
        reply_markup=ban_kb()
    )


@dp.message(F.text == "� Пользователи")
async def users_handler(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    users = load_json(USERS_FILE, [])

    text = f"👥 Всего пользователей: {len(users)}"

    await safe_send(message.chat.id, text, reply_markup=admin_main_kb())


@dp.message(F.text == "�📢 Рассылка")
async def broadcast_start(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.set_state(UserStates.waiting_broadcast)
    await safe_send(message.chat.id, "Введите текст:", reply_markup=cancel_kb())


@dp.message(UserStates.waiting_broadcast)
async def broadcast_send(message: types.Message, state: FSMContext):
    users = load_json(USERS_FILE, [])

    sent = 0

    for uid in users:
        try:
            await bot.send_message(uid, message.text)
            sent += 1
        except Exception as e:
            logger.warning(f"Failed to send broadcast to {uid}: {e}")

    await state.clear()

    await safe_send(
        message.chat.id,
        f"✅ Отправлено: {sent}",
        reply_markup=admin_main_kb()
    )


@dp.message(F.text == "↩️ Назад")
async def universal_back(message: types.Message, state: FSMContext):
    try:
        await message.delete()
    except:
        pass
    
    await state.clear()

    if is_admin(message.from_user.id):
        await safe_send(
            message.chat.id,
            "🔐 Админ-панель",
            reply_markup=admin_main_kb()
        )
    else:
        await safe_send(
            message.chat.id,
            "Главное меню",
            reply_markup=start_kb()
        )


@dp.message(F.text == "❌ Отмена")
async def cancel_handler(message: types.Message, state: FSMContext):
    try:
        await message.delete()
    except:
        pass

    await state.clear()

    if is_admin(message.from_user.id):
        await safe_send(message.chat.id, "Отмена", reply_markup=admin_main_kb())
    else:
        await safe_send(message.chat.id, "Отмена", reply_markup=start_kb())


# =========================
# ADMIN: НАСТРОЙКИ
# =========================
@dp.message(F.text == "🧹 Очистить кэш")
async def clear_cache(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    save_json(CACHE_FILE, [])

    await safe_send(
        message.chat.id,
        "✅ Кэш очищен",
        reply_markup=settings_kb()
    )


@dp.message(F.text == "⚙️ Настройки")
async def open_settings(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    await safe_send(
        message.chat.id,
        "⚙️ Настройки",
        reply_markup=settings_kb()
    )


# =========================
# ADMIN: СПОНСОР
# =========================
@dp.message(F.text == "🔗 Ссылка спонсора")
async def sponsor_menu(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    settings = get_settings()
    link = settings.get("sponsor_link") or "❌ Не задана"

    await safe_send(
        message.chat.id,
        f"🔗 Текущая ссылка:\n{link}",
        reply_markup=sponsor_kb()
    )


@dp.message(F.text == "✏️ Изменить ссылку")
async def change_sponsor_start(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.set_state(UserStates.waiting_sponsor_link)

    await safe_send(
        message.chat.id,
        "Вставь новую ссылку:",
        reply_markup=cancel_kb()
    )


@dp.message(UserStates.waiting_sponsor_link)
async def save_sponsor_link(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await safe_send(message.chat.id, "Отмена", reply_markup=sponsor_kb())
        return

    settings = get_settings()
    settings["sponsor_link"] = message.text
    save_settings(settings)

    await state.clear()

    await safe_send(
        message.chat.id,
        "✅ Ссылка обновлена",
        reply_markup=sponsor_kb()
    )


@dp.message(F.text == "🗑 Удалить ссылку")
async def delete_sponsor(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    settings = get_settings()
    settings["sponsor_link"] = ""
    save_settings(settings)

    await safe_send(
        message.chat.id,
        "✅ Ссылка удалена",
        reply_markup=sponsor_kb()
    )


# =========================
# ADMIN: ПРОВЕРИТЬ ПРОКСИ
# =========================
@dp.message(F.text == "🔄 Проверить сейчас")
async def check_now(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    await safe_send(message.chat.id, "🔍 Проверяю прокси...")

    proxies = await find_best_proxies()

    await safe_send(
        message.chat.id,
        f"✅ Найдено: {len(proxies)} прокси",
        reply_markup=admin_main_kb()
    )


# =========================
# ADMIN: ДОБАВИТЬ ПРОКСИ
# =========================
@dp.message(F.text == "➕ Добавить прокси")
async def add_proxy_start(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    await state.set_state(UserStates.waiting_proxy)

    await safe_send(
        message.chat.id,
        "Вставь ссылку прокси (tg://proxy?...):",
        reply_markup=cancel_kb()
    )


@dp.message(UserStates.waiting_proxy)
async def save_proxy(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await safe_send(message.chat.id, "Отмена", reply_markup=admin_main_kb())
        return

    reserve = load_json(RESERVE_FILE, [])
    reserve.insert(0, message.text)

    save_json(RESERVE_FILE, reserve[:20])

    await state.clear()

    await safe_send(
        message.chat.id,
        "✅ Прокси добавлен в резерв",
        reply_markup=admin_main_kb()
    )


# =========================
# MAIN
# =========================
async def main():
    print("БОТ ЗАПУЩЕН")
    logger.info("БОТ ЗАПУЩЕН")

    # удаляем webhook (важно для polling)
    await bot.delete_webhook(drop_pending_updates=True)

    # 🚀 запускаем очередь (worker)
    asyncio.create_task(worker())

    # запускаем бота
    await dp.start_polling(bot)


# =========================
# RUN
# =========================
if __name__ == "__main__":
    asyncio.run(main())