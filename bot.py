import os
import json
import time
import asyncio
import logging
import aiohttp
import struct
import random

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
DEAD_PROXIES_FILE = "dead_proxies.json"
BACKUP_PROXIES_FILE = "backup_proxies.json"  # Резервные прокси

# =========================
# ДЕФОЛТ
# =========================
DEFAULT_SETTINGS = {
    "sponsor_link": "https://t.me/+T8J7eXlfvfc5NWNi",
    "sponsor_channel_id": -1002174184458,
    "pin_code": "7080",
    "max_ping": 750,
    "top_count": 5
}

PROXIES_URL = (
    "https://raw.githubusercontent.com/"
    "alexmatveev271093-art/telegram_proxy_bot/"
    "main/proxies.txt"
)

CHECK_LIMIT = 50

# ОПТИМИЗАЦИЯ: Параллельная проверка и кэширование
MAX_CONCURRENT_CHECKS = 8  # Максимум одновременных проверок
PROXY_CACHE_TTL = 1800  # Кэш на 30 минут в секундах
PROXY_CHECK_TIMEOUT = 3.0  # Таймаут подключения (увеличен для надежности)
PROXY_READ_TIMEOUT = 3.0  # Таймаут чтения (увеличен для надежности)

# Глобальный Semaphore для ограничения одновременных проверок
check_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

# Кэш результатов с TTL
proxy_cache = {
    "data": [],
    "timestamp": 0
}

# Время последней автоматической проверки
last_background_check = 0

# АНТИСПАМ - увеличиваем лимит
ANTI_SPAM_SECONDS = 60
USER_TIMER_TASKS = {}

# =========================
# ЛОГИ
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("bot.log", mode="a", encoding="utf-8"),
        logging.StreamHandler()
    ]
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
    """
    Загружает прокси из GitHub репозитория (proxies.txt)
    Каждая строка - это ссылка на прокси в формате: tg://proxy?server=...&port=...&secret=...
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(PROXIES_URL, timeout=20) as resp:
                text = await resp.text()

        proxies = []

        for line in text.splitlines():
            line = line.strip()
            
            # Пропускаем пустые строки и комментарии
            if not line or line.startswith('#'):
                continue

            try:
                parsed = urlparse(line)
                params = parse_qs(parsed.query)

                if not all(k in params for k in ["server", "port", "secret"]):
                    logger.debug(f"Неправильный формат: {line[:50]}...")
                    continue

                proxies.append({
                    "server": params["server"][0],
                    "port": int(params["port"][0]),
                    "secret": params["secret"][0]
                })
            except Exception as e:
                logger.debug(f"Ошибка парсинга строки: {e}")
                continue

        logger.info(f"Загружено {len(proxies)} прокси с GitHub")
        return proxies

    except Exception as e:
        logger.error(f"Ошибка загрузки прокси: {e}")
        return []


# =========================
# ФУНКЦИИ ДЛЯ РАБОТЫ С МЕРТВЫМИ ПРОКСИ
# =========================
def load_dead_proxies():
    """Загрузить список мертвых прокси"""
    return load_json(DEAD_PROXIES_FILE, {})

def save_dead_proxies(dead_list):
    """Сохранить список мертвых прокси"""
    save_json(DEAD_PROXIES_FILE, dead_list)

def mark_proxy_dead(proxy_key):
    """Отметить прокси как мертвый"""
    dead = load_dead_proxies()
    dead[proxy_key] = time.time()
    save_dead_proxies(dead)

def is_proxy_dead(proxy_key):
    """Проверить, мертвый ли прокси"""
    dead = load_dead_proxies()
    if proxy_key not in dead:
        return False
    
    # Забываем о мертвых прокси через 24 часа
    if time.time() - dead[proxy_key] > 86400:
        dead.pop(proxy_key, None)
        save_dead_proxies(dead)
        return False
    
    return True

def get_proxy_key(proxy):
    """Получить уникальный ключ прокси"""
    return f"{proxy['server']}:{proxy['port']}"


# =========================
# CHECK PROXY - MTProto с Semaphore
# =========================
async def check_proxy(proxy):
    """
    Проверить MTProto прокси через простой handshake.
    Использует Semaphore для ограничения параллельных проверок.
    Возвращает dict с информацией о прокси и пингом, или None если прокси мертвый
    """
    proxy_key = get_proxy_key(proxy)
    
    # Пропускаем уже известные мертвые прокси
    if is_proxy_dead(proxy_key):
        logger.debug(f"Прокси {proxy_key} уже в списке мертвых")
        return None
    
    logger.debug(f"Начинаю проверку: {proxy_key}")
    
    # Ограничиваем одновременные проверки
    async with check_semaphore:
        try:
            start = time.perf_counter()
            
            # Подключаемся к прокси-серверу с более строгим таймаутом
            logger.debug(f"  Подключаюсь к {proxy['server']}:{proxy['port']}...")
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(proxy["server"], proxy["port"]),
                timeout=PROXY_CHECK_TIMEOUT
            )
            logger.debug(f"  ✓ Подключение установлено")
            
            # MTProto handshake: отправляем простой ReqPQ
            req_pq = bytes([0xc6, 0x11, 0xa4, 0x51]) + b'\x00' * 16
            
            try:
                logger.debug(f"  Отправляю handshake...")
                writer.write(req_pq)
                await asyncio.wait_for(writer.drain(), timeout=PROXY_READ_TIMEOUT)
                
                # Пытаемся получить ответ
                logger.debug(f"  Жду ответа...")
                response = await asyncio.wait_for(
                    reader.readexactly(8),
                    timeout=PROXY_READ_TIMEOUT
                )
                
                ping = (time.perf_counter() - start) * 1000
                logger.info(f"✓ {proxy_key} - пинг {ping:.1f}мс")
                
                writer.close()
                await writer.wait_closed()
                
                # Если получили ответ - прокси хороший
                return {
                    "proxy": proxy,
                    "ping": round(ping, 2)
                }
                
            except asyncio.TimeoutError:
                logger.warning(f"⏱ {proxy_key} - таймаут (нет ответа)")
                writer.close()
                await writer.wait_closed()
                mark_proxy_dead(proxy_key)
                return None
        
        except OSError as e:
            logger.warning(f"❌ {proxy_key} - ошибка подключения ({type(e).__name__})")
            mark_proxy_dead(proxy_key)
            return None
        
        except (ConnectionRefusedError, ConnectionResetError):
            logger.warning(f"❌ {proxy_key} - соединение отклонено")
            mark_proxy_dead(proxy_key)
            return None
        
        except Exception as e:
            logger.warning(f"❌ {proxy_key} - ошибка: {type(e).__name__}: {e}")
            mark_proxy_dead(proxy_key)
            return None


# =========================
# BEST PROXIES
# =========================
def save_backup_proxies(proxies):
    """Сохранить резервные прокси"""
    save_json(BACKUP_PROXIES_FILE, proxies)

def load_backup_proxies():
    """Лоадить резервные прокси"""
    return load_json(BACKUP_PROXIES_FILE, [])

async def find_best_proxies():
    """
    Найти лучшие прокси с кэшированием результатов (TTL).
    Не переворяет прокси если кэш еще свежий.
    """
    global proxy_cache, last_background_check
    
    settings = get_settings()
    current_time = time.time()
    
    # Проверяем кэш - если свежий, возвращаем его
    if proxy_cache["data"] and (current_time - proxy_cache["timestamp"]) < PROXY_CACHE_TTL:
        logger.info("Возвращаем прокси из кэша")
        return proxy_cache["data"]
    
    # Кэш устарел - начинаем проверку
    logger.info("Кэш устарел, начинаем проверку прокси...")
    
    proxy_list = await load_proxy_list()

    if not proxy_list:
        return load_json(CACHE_FILE, [])
    
    # Фильтруем уже известные мертвые прокси
    dead_proxies = load_dead_proxies()
    alive_proxies = [
        p for p in proxy_list[:CHECK_LIMIT]
        if get_proxy_key(p) not in dead_proxies
    ]
    
    if not alive_proxies:
        return load_json(CACHE_FILE, [])

    # Проверяем прокси (Semaphore ограничит одновременные)
    tasks = [
        check_proxy(proxy)
        for proxy in alive_proxies
    ]

    results = await asyncio.gather(*tasks)

    working = [
        x for x in results
        if x and x["ping"] <= settings["max_ping"]
    ]

    working.sort(key=lambda x: x["ping"])
    working = working[:settings["top_count"]]

    # Очищаем кэш мертвых прокси если нашли живых
    if working:
        dead = load_dead_proxies()
        # Удаляем найденные живые прокси из списка мертвых
        for item in working:
            dead.pop(get_proxy_key(item["proxy"]), None)
        save_dead_proxies(dead)
        
        # Сохраняем как резервные для будущих использований
        save_backup_proxies(working)

    save_json(CACHE_FILE, working)
    
    # Обновляем в-памяти кэш
    proxy_cache["data"] = working
    proxy_cache["timestamp"] = current_time

    return working


# =========================
# ФОНОВАЯ ПРОВЕРКА ПРОКСИ
# =========================
async def background_proxy_checker():
    """
    Фоновая задача для периодического обновления прокси каждые 25 минут.
    Не блокирует пользовательские запросы.
    """
    global last_background_check
    
    while True:
        try:
            current_time = time.time()
            
            # Проверяем каждые 25 минут (1500 сек)
            if current_time - last_background_check > 1500:
                logger.info("Запускаем фоновую проверку прокси...")
                
                # Сбрасываем кэш, чтобы найти_best_proxies выполнила проверку
                proxy_cache["timestamp"] = 0
                
                try:
                    await find_best_proxies()
                    logger.info("Фоновая проверка завершена")
                except Exception as e:
                    logger.error(f"Ошибка фоновой проверки: {e}")
                
                last_background_check = current_time
            
            # Спим 5 минут перед siguiente проверкой
            await asyncio.sleep(300)
            
        except Exception as e:
            logger.error(f"Критическая ошибка в background_proxy_checker: {e}")
            await asyncio.sleep(60)


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
    """
    Строит сообщение с прокси.
    Выводит максимум 5 прокси без информации о пинге.
    Список перетасовывается каждый раз.
    """
    # Копируем и перетасовываем прокси
    shuffled_proxies = proxies.copy()
    random.shuffle(shuffled_proxies)
    
    # Ограничиваем 5 лучшими
    top_proxies = shuffled_proxies[:5]

    text = (
        '<a href="https://t.me/+T8J7eXlfvfc5NWNi">🔥 <b>Good Place AI</b> 🤖</a>\n\n'
        "⚡️ Здесь: AI • Мемы • Польза\n\n"
        '📱 <a href="https://www.tiktok.com/@good_place_67">TikTok</a> | '
        '▶️ <a href="https://www.youtube.com/@gd_place">YouTube</a>\n\n'
        "━━━━━━━━━━━━━━━\n\n"
        "🚀 <b>СВЕЖИЕ прокси для Telegram 👇</b>\n\n"
        "(если не зашёл 🤔 — попробуй следующий 😉)\n\n"
        "━━━━━━━━━━━━━━━\n\n"
    )

    # Выводим прокси без пинга и статуса с пробелами между ними
    for i, item in enumerate(top_proxies, start=1):
        link = build_mtproto_link(item["proxy"])
        text += (
            f"{i}️⃣ "
            f'<a href="{link}">Подключить прокси 👈</a>\n'
        )
        # Добавляем пробел между прокси
        if i < len(top_proxies):
            text += "\n"

    text += (
        "\n━━━━━━━━━━━━━━━\n\n"
        "✅ <b>ПОДЕЛИСЬ с друзьями 😉</b>"
    )

    return text


# =========================
# SEND PROXIES
# =========================
def build_progress_bar(seconds):
    """Строит прогресс-бар с шагом в 10 сек"""
    filled = seconds // 10
    total = 3  # 30 сек максимум (3 шага по 10 сек)
    
    bar = "█" * filled + "░" * (total - filled)
    return f"[{bar}] {seconds} сек ⏳"


async def send_proxies(chat_id):
    try:
        # Отправляем начальное сообщение
        initial_msg = await safe_send(
            chat_id,
            f"🔍 Ищу самые быстрые прокси...\n\n{build_progress_bar(0)}"
        )
        
        # Запускаем поиск прокси и обновление прогресса одновременно
        start_time = time.time()
        
        # Создаем таск поиска прокси
        search_task = asyncio.create_task(find_best_proxies())
        
        # Флаг для отслеживания обновлений
        last_update = 0
        
        # Обновляем прогресс каждые 10 сек пока не будут найдены прокси
        while not search_task.done():
            elapsed = int(time.time() - start_time)
            
            # Обновляем сообщение каждые 10 сек (или при первом обновлении)
            if elapsed > 0 and (elapsed % 10 == 0 and elapsed != last_update):
                try:
                    progress_text = f"🔍 Ищу самые быстрые прокси...\n\n{build_progress_bar(elapsed)}"
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=initial_msg.message_id,
                        text=progress_text
                    )
                    last_update = elapsed
                except Exception:
                    pass  # Игнорируем ошибки при обновлении
            
            # Даем задаче время на выполнение
            try:
                proxies = await asyncio.wait_for(search_task, timeout=1)
                break
            except asyncio.TimeoutError:
                continue
            await asyncio.sleep(0.1)  # Чтобы не загружать CPU

        # Получаем результат поиска
        proxies = await search_task

        # Если не найдены свежие прокси - берем резервные
        if not proxies:
            proxies = load_backup_proxies()
            if proxies:
                logger.info(f"Свежих прокси не найдено, показываем резервные ({len(proxies)})")

        # Формируем финальное сообщение
        if proxies:
            text = build_post(proxies)
        else:
            # Если вообще нет ни свежих ни резервных прокси
            text = (
                "⚠️ К сожалению, на данный момент нет доступных прокси.\n"
                "Бот постоянно проверяет источники 🔄\n\n"
                "Попробуй позже"
            )

        # Отправляем финальное сообщение
        await safe_send(
            chat_id,
            text,
            reply_markup=proxy_kb() if proxies else start_kb()
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

        # Отправляем начальное сообщение с прогресс-баром
        timer_msg = await safe_send(
            user_id,
            f"⏳ Подожди перед следующим запросом\n\n{build_progress_bar(int(remaining))}",
            reply_markup=None
        )

        # создаём таймер с обновлением прогресса
        async def delayed_send():
            start_time = time.time()
            total_wait = remaining
            last_update = 0
            
            while True:
                elapsed = time.time() - start_time
                remaining_now = total_wait - elapsed
                
                if remaining_now <= 0:
                    break
                
                # Обновляем каждые 10 сек
                if int(remaining_now) % 10 != last_update % 10:
                    last_update = int(remaining_now)
                    try:
                        await bot.edit_message_text(
                            chat_id=user_id,
                            message_id=timer_msg.message_id,
                            text=f"⏳ Подожди перед следующим запросом\n\n{build_progress_bar(int(remaining_now))}"
                        )
                    except Exception:
                        pass
                
                await asyncio.sleep(1)

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
            [KeyboardButton(text="📶 Максимальный пинг для прокси")],
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


@dp.message(F.text == "📶 Максимальный пинг для прокси")
async def ping_change_start(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    settings = get_settings()
    current = settings.get("max_ping", 250)

    await state.set_state(UserStates.waiting_new_ping)
    await safe_send(
        message.chat.id,
        f"Текущее значение: {current}мс\n\nВведите новый максимальный пинг для отображения прокси:",
        reply_markup=cancel_kb()
    )


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
        "Вставь ссылку прокси (tg://proxy?...)\n\n"
        "Можешь отправить несколько — по одной в строке\n"
        "Прокси сразу добавятся пользователям 🚀",
        reply_markup=cancel_kb()
    )


@dp.message(UserStates.waiting_proxy)
async def save_proxy(message: types.Message, state: FSMContext):
    """
    Обработка добавления прокси без проверки.
    Админ отвечает за валидность ссылок.
    """
    if message.text == "❌ Отмена":
        await state.clear()
        await safe_send(message.chat.id, "Отмена", reply_markup=admin_main_kb())
        return

    logger.info(f"=== ДОБАВЛЕНИЕ ПРОКСИ (админ) ===")

    await safe_send(
        message.chat.id,
        "⏳ Добавляю прокси..."
    )

    # Парсим текст - может быть несколько прокси
    lines = message.text.strip().split('\n')
    proxy_links = [line.strip() for line in lines if line.strip()]
    logger.info(f"Получено ссылок: {len(proxy_links)}")
    
    added_count = 0
    failed_count = 0
    
    # Добавляем каждый прокси без проверки
    for idx, link in enumerate(proxy_links, 1):
        try:
            # Парсим ссылку
            parsed = urlparse(link)
            params = parse_qs(parsed.query)
            
            if not all(k in params for k in ["server", "port", "secret"]):
                failed_count += 1
                logger.warning(f"[{idx}] Неправильный формат: {link[:60]}...")
                continue
            
            proxy_dict = {
                "server": params["server"][0],
                "port": int(params["port"][0]),
                "secret": params["secret"][0]
            }
            
            proxy_key = get_proxy_key(proxy_dict)
            
            # Добавляем в основной список БЕЗ проверки
            proxies_list = load_json(CACHE_FILE, [])
            
            # Проверяем не добавлен ли уже
            if any(get_proxy_key(p["proxy"]) == proxy_key for p in proxies_list):
                logger.info(f"[{idx}] {proxy_key} уже в списке")
                failed_count += 1
                continue
            
            # Добавляем с пустым пингом (админ гарантирует валидность)
            proxies_list.append({
                "proxy": proxy_dict,
                "ping": 0  # Админ добавил - не проверяем
            })
            
            proxies_list.sort(key=lambda x: x["ping"])
            proxies_list = proxies_list[:20]  # Ограничиваем до 20
            save_json(CACHE_FILE, proxies_list)
            
            # Сохраняем как резервные прокси для будущего использования
            save_backup_proxies(proxies_list)
            
            added_count += 1
            logger.info(f"[{idx}] ✅ Добавлен: {proxy_key}")
        
        except Exception as e:
            failed_count += 1
            logger.error(f"[{idx}] Ошибка парсинга: {e}")
    
    # Формируем ответ
    logger.info(f"=== ИТОГ: добавлено={added_count}, ошибки={failed_count} ===")
    
    response_text = f"✅ Результат:\n\n"
    response_text += f"✔️ Добавлено: {added_count}\n"
    response_text += f"❌ Ошибки: {failed_count}\n"
    
    await state.clear()
    await safe_send(message.chat.id, response_text, reply_markup=admin_main_kb())



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
    

    # 🔄 запускаем фоновую проверку прокси
    asyncio.create_task(background_proxy_checker())
    
    logger.info("✅ Все сервисы запущены: очередь и фоновая проверка прокси")

    # запускаем бота
    await dp.start_polling(bot)


# =========================
# RUN
# =========================
if __name__ == "__main__":
    asyncio.run(main())