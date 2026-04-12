import os
import json
import time
import asyncio
import logging
import aiohttp
from urllib.parse import urlparse, parse_qs

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# =========================
# Railway ENV
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("Не задан BOT_TOKEN")

# =========================
# Настройки
# =========================
SPONSOR_CHANNEL_ID = -1002174184458

PROXY_SOURCE_URL = (
    "https://raw.githubusercontent.com/"
    "SoliSpirit/mtproto/master/all_proxies.txt"
)

MAX_PING = 250
TOP_COUNT = 5
CHECK_LIMIT = 50
CACHE_FILE = "cache.json"

# =========================
# Логи
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

# =========================
# Bot
# =========================
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML")
)

dp = Dispatcher()

# =========================
# Кнопки
# =========================
start_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Поехали 🚀")]
    ],
    resize_keyboard=True
)

proxy_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Дай прокси")]
    ],
    resize_keyboard=True
)

# =========================
# Middleware логов
# =========================
@dp.update.outer_middleware()
async def log_updates(handler, event, data):
    try:
        if hasattr(event, "message") and event.message:
            logger.info(
                f"Сообщение: {event.message.text} "
                f"от {event.message.from_user.id}"
            )
    except:
        pass

    return await handler(event, data)


# =========================
# Проверка подписки
# =========================
async def is_subscribed(user_id):
    try:
        member = await bot.get_chat_member(SPONSOR_CHANNEL_ID, user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception as e:
        logger.error(f"Ошибка проверки подписки: {e}")
        return False


# =========================
# Кэш
# =========================
def save_cache(data):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Ошибка записи cache: {e}")


def load_cache():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []


# =========================
# Загрузка прокси
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

        logger.info(f"Загружено прокси: {len(proxies)}")
        return proxies

    except Exception as e:
        logger.error(f"Ошибка загрузки списка прокси: {e}")
        return []


# =========================
# Проверка прокси
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

    except:
        return None


# =========================
# Поиск лучших прокси
# =========================
async def find_best_proxies():
    proxy_list = await load_proxy_list()

    if not proxy_list:
        logger.warning("GitHub недоступен. Использую cache.")
        return load_cache()

    tasks = [check_proxy(proxy) for proxy in proxy_list[:CHECK_LIMIT]]
    results = await asyncio.gather(*tasks)

    working = [
        x for x in results
        if x and x["ping"] <= MAX_PING
    ]

    working.sort(key=lambda x: x["ping"])
    working = working[:TOP_COUNT]

    save_cache(working)

    logger.info(f"Рабочих прокси найдено: {len(working)}")
    return working


# =========================
# Telegram-ссылка
# =========================
def build_mtproto_link(proxy):
    return (
        f"tg://proxy?"
        f"server={proxy['server']}"
        f"&port={proxy['port']}"
        f"&secret={proxy['secret']}"
    )


# =========================
# Красивый пост
# =========================
def build_post(proxies):
    text = """
<a href="https://t.me/+T8J7eXlfvfc5NWNi">🔥 <b>Good Place AI</b> 🤖</a>

⚡️ Здесь: AI • Мемы • Полезные фишки, которые реально упрощают жизнь

📱 <a href="https://www.tiktok.com/@good_place_67">TikTok</a> | ▶️ <a href="https://www.youtube.com/@gd_place">YouTube</a>

━━━━━━━━━━━━━━━

🚀 <b>Свежие прокси для Telegram</b>

💡 ЖМИ и подключай — работает сразу  
(если не зашёл — просто попробуй следующий)

━━━━━━━━━━━━━━━

🔥 <b>ТОП-5 (самые стабильные, разлетаются как пирожки, поэтому не зевай)</b>

"""

    for i, item in enumerate(proxies, start=1):
        link = build_mtproto_link(item["proxy"])
        text += (
            f"{i}️⃣ "
            f'<a href="{link}">Подключить прокси⚡️</a>\n\n'
        )

    text += """
━━━━━━━━━━━━━━━

📌 <b>СОХРАНИ ПОСТ</b>, чтобы не потерять  
🔁 ПОДЕЛИСЬ с друзьями — пригодится
"""

    return text


# =========================
# Отправка прокси
# =========================
async def send_proxies_to_user(chat_id):
    try:
        proxies = await find_best_proxies()

        if not proxies:
            text = "❌ Рабочих MTProto-прокси не найдено"
        else:
            text = build_post(proxies)

        await bot.send_message(
            chat_id,
            text,
            disable_web_page_preview=True
        )

    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")

        await bot.send_message(
            chat_id,
            f"❌ Ошибка:\n{str(e)}"
        )


# =========================
# /start
# =========================
@dp.message(Command("start"))
async def start_handler(message: types.Message):
    await message.answer(
        "👋 Добро пожаловать!\n\n"
        "Нажми кнопку ниже, чтобы начать.",
        reply_markup=start_kb
    )


# =========================
# Кнопка "Поехали"
# =========================
@dp.message(lambda message: message.text == "Поехали 🚀")
async def go_handler(message: types.Message):
    user_id = message.from_user.id

    if not await is_subscribed(user_id):
        await message.answer(
            "❌ Сначала подпишись на канал нашего спонсора:\n"
            "https://t.me/+T8J7eXlfvfc5NWNi"
        )
        return

    await message.answer(
        "✅ Отлично! Теперь нажми кнопку ниже 👇",
        reply_markup=proxy_kb
    )


# =========================
# Кнопка "Дай прокси"
# =========================
@dp.message(lambda message: message.text == "Дай прокси")
async def proxy_handler(message: types.Message):
    user_id = message.from_user.id

    if not await is_subscribed(user_id):
        await message.answer(
            "❌ Сначала подпишись на канал нашего спонсора:\n"
            "https://t.me/+T8J7eXlfvfc5NWNi"
        )
        return

    await message.answer(
        "🔍 Начинаю сканировать прокси-адреса...\n"
        "Это может занять 1–2 минуты."
    )

    await send_proxies_to_user(message.chat.id)


# =========================
# Main
# =========================
async def main():
    print("БОТ ЗАПУЩЕН")
    logger.info("БОТ ЗАПУЩЕН")

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Webhook удалён")

    scheduler = AsyncIOScheduler()
    scheduler.start()

    logger.info("Планировщик запущен")
    logger.info("Запускаю polling...")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())