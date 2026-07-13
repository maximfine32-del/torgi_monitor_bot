#!/usr/bin/env python3
"""
Telegram бот для мониторинга лотов на torgi.gov.ru
Фокус: земельные участки 600-1300 м² (аренда + продажа)
Гибкая настройка через команды Telegram
"""

import asyncio
import json
import os
import re
from datetime import datetime
from typing import Optional, List, Dict, Any

import aiosqlite
import httpx
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from loguru import logger
from pydantic import BaseModel, Field

# ====================== КОНФИГУРАЦИЯ ======================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = int(os.getenv("CHAT_ID", "0"))
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в .env")

TORGI_BASE = "https://torgi.gov.ru"
LOTCARDS_API = f"{TORGI_BASE}/new/api/public/lotcards"
LOT_PAGE_URL = f"{TORGI_BASE}/new/public/lots/lot"

DEFAULT_SETTINGS = {
    "area_min": 600,
    "area_max": 1300,
    "deal_types": "rent,sale",
    "cat_codes": "104,7,302,307,609",
    "lot_statuses": "PUBLISHED,APPLICATIONS_SUBMISSION,DETERMINING_WINNER",
    "keywords": "",
    "regions": "",
    "enabled": True,
    "notify_not_held": False,
}

DB_PATH = "torgi_bot.db"

# ====================== МОДЕЛИ ======================
class BotSettings(BaseModel):
    area_min: int = Field(default=600)
    area_max: int = Field(default=1300)
    deal_types: str = Field(default="rent,sale")
    cat_codes: str = Field(default="104,7,302,307,609")
    lot_statuses: str = Field(default="PUBLISHED,APPLICATIONS_SUBMISSION,DETERMINING_WINNER")
    keywords: str = Field(default="")
    regions: str = Field(default="")
    enabled: bool = Field(default=True)
    notify_not_held: bool = Field(default=False)

# ====================== БАЗА ДАННЫХ ======================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS processed_lots (
                lot_id TEXT PRIMARY KEY,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.commit()

async def get_setting(key: str, default: Any = None) -> Any:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        if row:
            try:
                return json.loads(row[0])
            except:
                return row[0]
        return default

async def set_setting(key: str, value: Any):
    async with aiosqlite.connect(DB_PATH) as db:
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()

async def is_lot_processed(lot_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT 1 FROM processed_lots WHERE lot_id = ?", (lot_id,))
        return await cursor.fetchone() is not None

async def mark_lot_processed(lot_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO processed_lots (lot_id) VALUES (?)", (lot_id,))
        await db.commit()

async def get_last_check_time() -> Optional[datetime]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT value FROM meta WHERE key = 'last_check_time'")
        row = await cursor.fetchone()
        if row and row[0]:
            return datetime.fromisoformat(row[0])
        return None

async def set_last_check_time(dt: datetime):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ("last_check_time", dt.isoformat()))
        await db.commit()

# ====================== РАБОТА С API ======================
def parse_area(text: str) -> Optional[float]:
    if not text:
        return None
    text = text.lower().replace(",", ".")
    patterns = [
        r"(\d+(?:\.\d+)?)\s*(?:м²|м2|кв\.?\s*м)",
        r"(\d+(?:\.\d+)?)\s*га",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            val = float(match.group(1))
            if "га" in pattern:
                val *= 10000
            return val
    return None

async def fetch_lots_from_api(settings: BotSettings, limit: int = 100) -> List[Dict]:
    params = {
        "lotStatus": settings.lot_statuses,
        "catCode": settings.cat_codes,
        "byFirstVersion": "true",
        "sort": "firstVersionPublicationDate,desc",
        "size": str(min(limit, 200)),
        "matchPhrase": "false",
    }
    if settings.regions:
        params["text"] = settings.regions

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Referer": f"{TORGI_BASE}/new/public/lots/reg",
    }

    lots = []
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(LOTCARDS_API, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("content", []) if isinstance(data, dict) else data
            for item in items:
                lot = normalize_lot(item)
                if lot:
                    lots.append(lot)
    except Exception as e:
        logger.error(f"Ошибка запроса к API: {e}")
    return lots

def normalize_lot(raw: Dict) -> Optional[Dict]:
    try:
        lot_id = str(raw.get("id") or raw.get("lotId") or raw.get("noticeId", ""))
        if not lot_id:
            return None

        name = raw.get("name") or raw.get("lotName") or ""
        description = raw.get("description") or raw.get("lotDescription") or ""

        area = None
        for key in ["area", "lotArea", "square"]:
            if key in raw and raw[key]:
                try:
                    area = float(raw[key])
                    break
                except:
                    pass
        if area is None:
            area = parse_area(f"{name} {description}")

        deal_type = "unknown"
        bidd = str(raw.get("biddType", "")).lower()
        if "аренд" in bidd:
            deal_type = "rent"
        elif "продаж" in bidd or "купли" in bidd:
            deal_type = "sale"

        link = f"{LOT_PAGE_URL}/{lot_id}_1" if lot_id else ""

        return {
            "id": lot_id,
            "name": name[:300],
            "description": description[:500],
            "area": area,
            "deal_type": deal_type,
            "price": raw.get("initialPrice") or raw.get("price"),
            "currency": raw.get("currencyCode") or "RUB",
            "status": raw.get("lotStatus") or raw.get("status", ""),
            "region": raw.get("subjectRFName") or raw.get("region", ""),
            "link": link,
        }
    except:
        return None

def lot_matches_filters(lot: Dict, settings: BotSettings) -> bool:
    if not lot.get("area"):
        return False
    if not (settings.area_min <= lot["area"] <= settings.area_max):
        return False

    deal = lot.get("deal_type", "unknown")
    allowed = settings.deal_types.split(",")
    if "both" not in allowed and deal not in allowed and deal != "unknown":
        return False

    if settings.keywords:
        text = f"{lot.get('name', '')} {lot.get('description', '')}".lower()
        kws = [k.strip().lower() for k in settings.keywords.split(",") if k.strip()]
        if kws and not any(kw in text for kw in kws):
            return False

    if settings.regions:
        regions = [r.strip().lower() for r in settings.regions.split(",") if r.strip()]
        region_text = f"{lot.get('region', '')} {lot.get('name', '')}".lower()
        if not any(reg in region_text for reg in regions):
            return False

    return True

# ====================== МОНИТОРИНГ ======================
async def check_new_lots(bot: Bot):
    logger.info("Проверка новых лотов...")
    settings_dict = await get_setting("filters", DEFAULT_SETTINGS)
    settings = BotSettings(**settings_dict)

    if not settings.enabled:
        return

    lots = await fetch_lots_from_api(settings, limit=150)
    new_matching = []
    for lot in lots:
        if await is_lot_processed(lot["id"]):
            continue
        if lot_matches_filters(lot, settings):
            new_matching.append(lot)
            await mark_lot_processed(lot["id"])

    for lot in new_matching:
        await send_lot_notification(bot, lot)

    await set_last_check_time(datetime.now())

async def send_lot_notification(bot: Bot, lot: Dict):
    area_str = f"{lot['area']:.0f} м²" if lot.get("area") else "площадь не указана"
    price_str = f"\n💰 {float(lot['price']):,.0f} {lot.get('currency')}" if lot.get("price") else ""

    deal_emoji = "🏠" if lot.get("deal_type") == "rent" else "💼" if lot.get("deal_type") == "sale" else "📦"
    deal_text = {"rent": "Аренда", "sale": "Продажа"}.get(lot.get("deal_type"), "—")

    text = (
        f"🆕 <b>Новый лот на torgi.gov.ru</b>\n\n"
        f"{deal_emoji} <b>{deal_text}</b> | {area_str}\n"
        f"📍 {lot.get('region', '—')}\n\n"
        f"<b>{lot.get('name', 'Без названия')}</b>\n\n"
        f"{lot.get('description', '')[:350]}{'...' if len(lot.get('description', '')) > 350 else ''}"
        f"{price_str}\n\n"
        f"🔗 <a href=\"{lot['link']}\">Открыть лот</a>\n"
        f"🆔 <code>{lot['id']}</code>"
    )

    try:
        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")

# ====================== TELEGRAM ======================
dp = Dispatcher()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

@dp.message(CommandStart())
async def cmd_start(message: Message):
    if message.chat.id != CHAT_ID:
        await message.answer("Бот только для владельца.")
        return
    await message.answer(
        "👋 <b>Бот мониторинга torgi.gov.ru</b>\n\n"
        "Команды:\n"
        "/settings — настройки\n"
        "/set_area 600 1300\n"
        "/set_types rent,sale\n"
        "/set_cat_codes 104,7,302\n"
        "/set_keywords ИЖС\n"
        "/set_regions Московская\n"
        "/check_now — проверить сейчас\n"
        "/pause /resume\n"
        "/debug — тестовый запрос"
    )

@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    if message.chat.id != CHAT_ID:
        return
    s = BotSettings(**(await get_setting("filters", DEFAULT_SETTINGS)))
    await message.answer(
        f"⚙️ <b>Настройки</b>\n\n"
        f"Площадь: {s.area_min}–{s.area_max} м²\n"
        f"Типы: {s.deal_types}\n"
        f"CatCodes: {s.cat_codes}\n"
        f"Ключевые слова: {s.keywords or '—'}\n"
        f"Регионы: {s.regions or 'все'}\n"
        f"Мониторинг: {'ВКЛ' if s.enabled else 'ВЫКЛ'}"
    )

@dp.message(Command("set_area"))
async def cmd_set_area(message: Message):
    if message.chat.id != CHAT_ID:
        return
    args = message.text.split()
    if len(args) != 3:
        await message.answer("Пример: /set_area 600 1300")
        return
    try:
        settings = await get_setting("filters", DEFAULT_SETTINGS)
        settings["area_min"] = int(args[1])
        settings["area_max"] = int(args[2])
        await set_setting("filters", settings)
        await message.answer(f"✅ Площадь: {args[1]}–{args[2]} м²")
    except:
        await message.answer("Ошибка")

@dp.message(Command("set_types"))
async def cmd_set_types(message: Message):
    if message.chat.id != CHAT_ID:
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return
    settings = await get_setting("filters", DEFAULT_SETTINGS)
    settings["deal_types"] = args[1].strip()
    await set_setting("filters", settings)
    await message.answer(f"✅ Типы: {args[1]}")

@dp.message(Command("set_cat_codes"))
async def cmd_set_cat_codes(message: Message):
    if message.chat.id != CHAT_ID:
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        return
    settings = await get_setting("filters", DEFAULT_SETTINGS)
    settings["cat_codes"] = args[1].strip()
    await set_setting("filters", settings)
    await message.answer(f"✅ CatCodes: {args[1]}")

@dp.message(Command("set_keywords"))
async def cmd_set_keywords(message: Message):
    if message.chat.id != CHAT_ID:
        return
    args = message.text.split(maxsplit=1)
    settings = await get_setting("filters", DEFAULT_SETTINGS)
    settings["keywords"] = args[1].strip() if len(args) > 1 else ""
    await set_setting("filters", settings)
    await message.answer("✅ Ключевые слова обновлены")

@dp.message(Command("set_regions"))
async def cmd_set_regions(message: Message):
    if message.chat.id != CHAT_ID:
        return
    args = message.text.split(maxsplit=1)
    settings = await get_setting("filters", DEFAULT_SETTINGS)
    settings["regions"] = args[1].strip() if len(args) > 1 else ""
    await set_setting("filters", settings)
    await message.answer("✅ Регионы обновлены")

@dp.message(Command("pause"))
async def cmd_pause(message: Message):
    if message.chat.id != CHAT_ID:
        return
    settings = await get_setting("filters", DEFAULT_SETTINGS)
    settings["enabled"] = False
    await set_setting("filters", settings)
    await message.answer("⏸ Мониторинг остановлен")

@dp.message(Command("resume"))
async def cmd_resume(message: Message):
    if message.chat.id != CHAT_ID:
        return
    settings = await get_setting("filters", DEFAULT_SETTINGS)
    settings["enabled"] = True
    await set_setting("filters", settings)
    await message.answer("▶️ Мониторинг запущен")

@dp.message(Command("check_now"))
async def cmd_check_now(message: Message):
    if message.chat.id != CHAT_ID:
        return
    await message.answer("🔄 Проверяю...")
    await check_new_lots(bot)
    await message.answer("✅ Готово")

@dp.message(Command("debug"))
async def cmd_debug(message: Message):
    if message.chat.id != CHAT_ID:
        return
    await message.answer("🔍 Тестовый запрос...")
    settings = BotSettings(**(await get_setting("filters", DEFAULT_SETTINGS)))
    lots = await fetch_lots_from_api(settings, limit=5)
    if not lots:
        await message.answer("Лотов нет или ошибка API")
        return
    text = "Примеры лотов:\n\n"
    for lot in lots[:3]:
        text += f"• {lot.get('name', '—')[:60]}\n  Площадь: {lot.get('area')} м² | {lot.get('link')}\n\n"
    await message.answer(text[:4000])

# ====================== ЗАПУСК ======================
async def on_startup():
    await init_db()
    if not await get_setting("filters"):
        await set_setting("filters", DEFAULT_SETTINGS)

    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(check_new_lots, IntervalTrigger(minutes=CHECK_INTERVAL_MINUTES), args=[bot], replace_existing=True)
    scheduler.start()

    await asyncio.sleep(8)
    await check_new_lots(bot)
    logger.info("Бот запущен")

async def main():
    logger.remove()
    logger.add(sink=print, level=LOG_LEVEL)
    await on_startup()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())