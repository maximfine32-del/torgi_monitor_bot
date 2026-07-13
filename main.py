#!/usr/bin/env python3
"""
Telegram бот для мониторинга torgi.gov.ru v2
"""

import asyncio
import json
import os
import re
from datetime import datetime
from typing import Optional, List, Dict, Any

import aiosqlite
import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from loguru import logger
from pydantic import BaseModel, Field

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = int(os.getenv("CHAT_ID", "0"))
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))

TORGI_BASE = "https://torgi.gov.ru"
LOTCARDS_API = f"{TORGI_BASE}/new/api/public/lotcards"
LOT_PAGE_URL = f"{TORGI_BASE}/new/public/lots/lot"

DEFAULT_SETTINGS = {
    "area_min": 600, "area_max": 1300,
    "deal_types": "rent,sale",
    "cat_codes": "104,7,302,307,609",
    "lot_statuses": "PUBLISHED,APPLICATIONS_SUBMISSION,DETERMINING_WINNER",
    "keywords": "", "regions": "",
    "enabled": True
}

DB_PATH = "torgi_bot.db"

class BotSettings(BaseModel):
    area_min: int = 600
    area_max: int = 1300
    deal_types: str = "rent,sale"
    cat_codes: str = "104,7,302,307,609"
    lot_statuses: str = "PUBLISHED,APPLICATIONS_SUBMISSION,DETERMINING_WINNER"
    keywords: str = ""
    regions: str = ""
    enabled: bool = True

# ====================== DB ======================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS processed_lots (lot_id TEXT PRIMARY KEY)")
        await db.commit()

async def get_setting(key: str, default=None):
    async with aiosqlite.connect(DB_PATH) as db:
        row = await db.execute_fetchall("SELECT value FROM settings WHERE key=?", (key,))
        if row:
            try:
                return json.loads(row[0][0])
            except:
                return row[0][0]
        return default

async def set_setting(key: str, value):
    async with aiosqlite.connect(DB_PATH) as db:
        val = json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, val))
        await db.commit()

# ====================== API ======================
def parse_area(text: str) -> Optional[float]:
    if not text: return None
    text = text.lower().replace(",", ".")
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:м²|м2|кв\.?м)", text)
    if match:
        return float(match.group(1))
    return None

async def fetch_lots(limit: int = 30) -> List[Dict]:
    params = {
        "lotStatus": "PUBLISHED,APPLICATIONS_SUBMISSION,DETERMINING_WINNER",
        "byFirstVersion": "true",
        "sort": "firstVersionPublicationDate,desc",
        "size": str(limit),
    }
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(LOTCARDS_API, params=params, headers=headers)
            data = resp.json()
            items = data.get("content", []) if isinstance(data, dict) else []
            return [normalize_lot(item) for item in items if (lot := normalize_lot(item))]
    except Exception as e:
        logger.error(f"API error: {e}")
        return []

def normalize_lot(raw: dict) -> dict:
    try:
        lot_id = str(raw.get("id") or raw.get("lotId", ""))
        name = raw.get("name") or ""
        desc = raw.get("description") or ""
        area = raw.get("area") or parse_area(f"{name} {desc}")
        link = f"{LOT_PAGE_URL}/{lot_id}_1"

        return {
            "id": lot_id,
            "name": name[:250],
            "area": area,
            "deal_type": "rent" if "аренд" in str(raw.get("biddType","")).lower() else "sale",
            "region": raw.get("subjectRFName", ""),
            "link": link,
            "raw": raw
        }
    except:
        return None

# ====================== КНОПКИ ======================
def get_settings_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📐 Изменить площадь", callback_data="edit_area")],
        [InlineKeyboardButton(text="🔄 Типы сделок", callback_data="edit_types")],
        [InlineKeyboardButton(text="🏷️ CatCodes", callback_data="edit_cat")],
        [InlineKeyboardButton(text="🔍 Ключевые слова", callback_data="edit_keywords")],
        [InlineKeyboardButton(text="🌍 Регионы", callback_data="edit_regions")],
        [InlineKeyboardButton(text="🔄 Проверить сейчас", callback_data="check_now")],
        [InlineKeyboardButton(text="⏸ Пауза / ▶️ Запуск", callback_data="toggle")]
    ])

# ====================== КОМАНДЫ ======================
dp = Dispatcher()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

@dp.message(CommandStart())
async def cmd_start(message: Message):
    if message.chat.id != CHAT_ID: return
    await message.answer("👋 Бот мониторинга torgi.gov.ru", reply_markup=get_settings_keyboard())

@dp.message(Command("debug"))
async def cmd_debug(message: Message):
    if message.chat.id != CHAT_ID: return
    await message.answer("🔍 Загружаю последние лоты...")
    lots = await fetch_lots(15)
    text = "📋 Последние лоты (для настройки):\n\n"
    for lot in lots[:10]:
        area = f"{lot['area']} м²" if lot.get('area') else "—"
        text += f"• {lot['name'][:80]}\n  Площадь: {area} | {lot['link']}\n\n"
    await message.answer(text[:4000] or "Лотов не найдено")

@dp.message(Command("check_now"))
async def cmd_check_now(message: Message):
    if message.chat.id != CHAT_ID: return
    await message.answer("🔄 Проверяю...")
    await check_new_lots(bot)  # использует фильтры

@dp.callback_query()
async def callback_handler(callback):
    # Здесь можно добавить обработку кнопок (пока просто заглушка)
    await callback.answer("Функция в разработке")

# ====================== МОНИТОРИНГ ======================
async def check_new_lots(bot: Bot):
    settings = BotSettings(**(await get_setting("filters", DEFAULT_SETTINGS)))
    if not settings.enabled:
        return

    lots = await fetch_lots(100)
    new_matching = []
    for lot in lots:
        if await is_lot_processed(lot["id"]):
            continue
        if lot.get("area") and settings.area_min <= lot["area"] <= settings.area_max:
            new_matching.append(lot)
            await mark_lot_processed(lot["id"])

    for lot in new_matching[:5]:  # не больше 5 за раз
        await send_notification(bot, lot)

async def send_notification(bot: Bot, lot: dict):
    text = f"🆕 Новый лот!\n\n{lot['name']}\nПлощадь: {lot.get('area')} м²\n{lot['link']}"
    await bot.send_message(CHAT_ID, text)

async def on_startup():
    await init_db()
    if not await get_setting("filters"):
        await set_setting("filters", DEFAULT_SETTINGS)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_new_lots, IntervalTrigger(minutes=CHECK_INTERVAL_MINUTES), args=[bot])
    scheduler.start()
    await asyncio.sleep(5)
    await check_new_lots(bot)

async def main():
    await on_startup()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())