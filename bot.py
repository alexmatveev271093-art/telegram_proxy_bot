import os
import json
import time
import asyncio
import logging
import aiohttp
from urllib.parse import urlparse, parse_qs

from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

# =========================
# Загрузка env
# =========================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))

# =========================
# Настройки
# =========================
PROXY_SOURCE_URL = (
    "https://raw.githubusercontent.com/"
    "SoliSpirit/mtproto/master/all_proxies.txt"
)

MAX_PING = 1500
TOP_COUNT = 5
CHECK_LIMIT = 50
CACHE_FILE = "cache.json"

# =========================
# Логи
# =========================
logging.basicConfig(
    filename="bot.log",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# =========================
# Bot
# =========================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()


# =========================
# Проверка подписки
# =========================
async def is_subscribed(user_id):
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False


# =========================
# Кэш
# =========================
def save_cache(data):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


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

        logging.info(f"Загружено прокси: {len(proxies)}")
        return proxies

    except Exception as e:
        logging.error(f"Ошибка загрузки: {e}")
        return []


# =========================
# Проверка порта
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
# Поиск лучших
# =========================
async def find_best_proxies():
    proxy_list = await load_proxy_list()

    if not proxy_list:
        logging.warning("GitHub недоступен, использую cache")
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

    return working


# =========================
# TG ссылки
# =========================
def build_mtproto_link(proxy):
    return (
        f"tg://proxy?"
        f"server={proxy['server']}"
        f"&port={proxy['port']}"
        f"&secret={proxy['secret']}"
    )


# =========================
# Пост
# =========================
def build_post(proxies):
    text = """
<a href="https://t.me/+T8J7eXlfvfc5NWNi">🔥 <b>Good Place AI</b> 🤖</a>

⚡️ Здесь: AI • Мемы • Полезные фишки, которые реально упрощают жизнь

📱 <a href="https://www.tiktok.com/@good_place_67">TikTok</a> | ▶️ <a href="https://www.youtube.com/@gd_place">YouTube</a>

━━━━━━━━━━━━━━━

🚀 <b>Свежие прокси для Telegram</b>

💡 Жми и подключай — работает сразу
(если не зашёл — просто попробуй следующий)

━━━━━━━━━━━━━━━

🔥 <b>ТОП (самые стабильные)</b>

"""

    for i, item in enumerate(proxies, start=1):
        link = build_mtproto_link(item["proxy"])
        text += f'{i}️⃣ <a href="{link}">Подключить прокси ⚡️</a>\n\n'

    text += """
━━━━━━━━━━━━━━━

📌 <b>Сохрани пост</b>, чтобы не потерять
🔁 Поделись с друзьями — пригодится

🚀 <a href="https://t.me/+T8J7eXlfvfc5NWNi">Подписаться на Good Place AI</a>
"""

    return text


# =========================
# Отправка
# =========================
async def send_daily_proxies():
    try:
        proxies = await find_best_proxies()

        if not proxies:
            text = "❌ Рабочих MTProto не найдено"
        else:
            text = build_post(proxies)

        await bot.send_message(ADMIN_ID, text, disable_web_page_preview=True)
        await bot.send_message(CHANNEL_ID, text, disable_web_page_preview=True)

        logging.info("Прокси отправлены")

    except Exception as e:
        logging.error(f"Ошибка отправки: {e}")


# =========================
# Команды
# =========================
@dp.message(Command("start"))
async def start_handler(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        if not await is_subscribed(message.from_user.id):
            await message.answer(
                "❌ Сначала подпишитесь на канал:\n"
                "https://t.me/+T8J7eXlfvfc5NWNi"
            )
            return

    await message.answer("✅ Бот работает")


@dp.message(Command("check"))
async def check_handler(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Доступ запрещён")
        return

    await message.answer("🔍 Проверяю прокси...")
    await send_daily_proxies()


# =========================
# Main
# =========================
async def main():
    scheduler = AsyncIOScheduler()

    scheduler.add_job(send_daily_proxies, "cron", hour=9, minute=0)
    scheduler.add_job(send_daily_proxies, "cron", hour=18, minute=0)

    scheduler.start()

    logging.info("Бот запущен")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())