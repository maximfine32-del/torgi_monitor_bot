#!/usr/bin/env python3
"""
Telegram бот мониторинга torgi.gov.ru + FastAPI для Render
"""

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import List, Dict, Any

import aiosqlite
import httpx
import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from fastapi import FastAPI
from loguru import logger
from pydantic import BaseModel, Field

# ====================== CONFIG ======================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = int(os.getenv("CHAT_ID", "0"))
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))

TORGI_BASE = "https://torgi.gov.ru"
LOTCARDS_API = f"{TORGI_BASE}/new/api/public/lotcards"
LOT_PAGE = f"{TORGI_BASE}/new/public/lots/lot"

DEFAULT_SETTINGS = {
    "area_min": 600,
    "area_max": 1300,
    "deal_types": "rent,sale",
    "cat_codes": "104,7,302,307,609",
    "keywords": "",
    "regions": "",
    "enabled": True
}

DB_PATH = "torgi_bot.db"

# ====================== FastAPI ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(start_bot())
    yield

app = FastAPI(lifespan=lifespan)

@app.get("/health")
async def health():
    return {"status": "healthy", "bot": "running"}

# ====================== БОТ ======================
class BotSettings(BaseModel):
    area_min: int = Field(default=600)
    area_max: int = Field(default=1300)
    deal_types: str = Field(default="rent,sale")
    cat_codes: str = Field(default="104,7,302,307,609")
    keywords: str = Field(default="")
    regions: str = Field(default="")
    enabled: bool = Field(default=True)

dp = Dispatcher()
bot_instance = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS processed_lots (lot_id TEXT PRIMARY KEY)")
        await db.commit()

async def get_settings() -> BotSettings:
    async with aiosqlite.connect(DB_PATH) as db:
        row = await db.execute_fetchall("SELECT value FROM settings WHERE key='filters'")
        if row:
            try:
                return BotSettings(**json.loads(row[0][0]))
            except:
                pass
    return BotSettings()

async def save_settings(settings: BotSettings):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('filters', ?)",
                         (json.dumps(settings.model_dump(), ensure_ascii=False),))
        await db.commit()

def get_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📐 Площадь", callback_data="set_area")],
        [InlineKeyboardButton(text="🔄 Тип сделки", callback_data="set_type")],
        [InlineKeyboardButton(text="🏷️ CatCodes", callback_data="set_cat")],
        [InlineKeyboardButton(text="🔍 Ключевые слова", callback_data="set_keywords")],
        [InlineKeyboardButton(text="🌍 Регионы", callback_data="set_regions")],
        [InlineKeyboardButton(text="🔄 Проверить сейчас", callback_data="check_now")],
    ])

@dp.message(CommandStart())
async def cmd_start(message: Message):
    if message.chat.id != CHAT_ID:
        return
    await message.answer("👋 Бот мониторинга torgi.gov.ru запущен!", reply_markup=get_main_keyboard())

@dp.message(Command("debug"))
async def cmd_debug(message: Message):
    if message.chat.id != CHAT_ID:
        return
    await message.answer("🔍 Загружаю последние лоты...")
    lots = await fetch_recent_lots(15)
    text = "📋 Последние лоты:\n\n"
    for lot in lots:
        area = f"{lot.get('area', '—')} м²" if lot.get('area') else "—"
        text += f"• {lot.get('name', '—')[:90]}\n  Площадь: {area}\n  {lot.get('link', '')}\n\n"
    await message.answer(text[:4000] or "Лотов не найдено")

async def fetch_recent_lots(limit: int = 20) -> List[Dict]:
    params = {
        "size": str(limit),
        "sort": "firstVersionPublicationDate,desc",
        "byFirstVersion": "true"
    }
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    try:
        async with httpx.AsyncClient(timeout=25) as client:
            resp = await client.get(LOTCARDS_API, params=params, headers=headers)
            data = resp.json()
            items = data.get("content", []) if isinstance(data, dict) else []
            return [normalize_lot(item) for item in items if normalize_lot(item)]
    except Exception as e:
        logger.error(f"API Error: {e}")
        return []

def normalize_lot(raw: dict) -> Dict:
    try:
        lot_id = str(raw.get("id") or raw.get("lotId", ""))
        name = raw.get("name", "") or raw.get("lotName", "")
        desc = raw.get("description", "")
        area = raw.get("area") or parse_area(name + " " + desc)
        link = f"{LOT_PAGE}/{lot_id}_1" if lot_id else ""

        return {
            "id": lot_id,
            "name": name,
            "area": area,
            "link": link,
            "region": raw.get("subjectRFName", "")
        }
    except:
        return {}

def parse_area(text: str) -> float | None:
    if not text: return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:м²|м2|кв\.?м)", text.lower())
    return float(match.group(1)) if match else None

async def start_bot():
    await init_db()
    if not await get_settings():
        await save_settings(BotSettings())
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_new_lots, IntervalTrigger(minutes=CHECK_INTERVAL_MINUTES), args=[bot_instance])
    scheduler.start()
    await asyncio.sleep(10)
    await check_new_lots(bot_instance)
    await dp.start_polling(bot_instance)

async def check_new_lots(bot: Bot):
    settings = await get_settings()
    if not settings.enabled:
        return
    lots = await fetch_recent_lots(80)
    # Здесь можно добавить фильтрацию и отправку
    logger.info(f"Проверено {len(lots)} лотов")

if __name__ == "__main__":
    logger.info("🚀 Запуск бота + FastAPI")
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))