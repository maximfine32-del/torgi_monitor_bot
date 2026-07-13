#!/usr/bin/env python3
import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import List, Dict

import aiosqlite
import httpx
import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from fastapi import FastAPI
from loguru import logger

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = int(os.getenv("CHAT_ID", "0"))
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))

TORGI_BASE = "https://torgi.gov.ru"
LOTCARDS_API = f"{TORGI_BASE}/new/api/public/lotcards"

# ====================== FastAPI ======================
@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(start_bot())
    yield

app = FastAPI(lifespan=lifespan)

@app.get("/health")
async def health():
    return {"status": "healthy"}

# ====================== БОТ ======================
dp = Dispatcher()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

async def init_db():
    async with aiosqlite.connect("torgi_bot.db") as db:
        await db.execute("CREATE TABLE IF NOT EXISTS processed_lots (lot_id TEXT PRIMARY KEY)")
        await db.commit()

async def fetch_recent_lots(limit: int = 20) -> List[Dict]:
    params = {
        "lotStatus": "PUBLISHED,APPLICATIONS_SUBMISSION,DETERMINING_WINNER",
        "byFirstVersion": "true",
        "sort": "firstVersionPublicationDate,desc",
        "size": str(limit),
        "matchPhrase": "false",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{TORGI_BASE}/new/public/lots/reg",
        "Origin": TORGI_BASE,
    }

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(LOTCARDS_API, params=params, headers=headers)
            logger.info(f"API Status: {resp.status_code}")
            if resp.status_code != 200:
                logger.error(f"Ответ: {resp.text[:500]}")
                return []
            data = resp.json()
            items = data.get("content", []) if isinstance(data, dict) else data
            logger.info(f"Получено {len(items)} лотов")
            return [normalize_lot(item) for item in items if normalize_lot(item)]
    except Exception as e:
        logger.error(f"API Error: {e}")
        return []

def normalize_lot(raw: dict) -> Dict | None:
    try:
        lot_id = str(raw.get("id") or raw.get("lotId", ""))
        if not lot_id:
            return None
        return {
            "id": lot_id,
            "name": raw.get("name", "")[:150],
            "area": raw.get("area") or parse_area(raw.get("name", "") + " " + raw.get("description", "")),
            "link": f"{TORGI_BASE}/new/public/lots/lot/{lot_id}_1",
            "region": raw.get("subjectRFName", "")
        }
    except:
        return None

def parse_area(text: str):
    if not text: return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:м²|м2|кв\.?м)", text.lower())
    return float(match.group(1)) if match else None

@dp.message(CommandStart())
async def cmd_start(message: Message):
    if message.chat.id != CHAT_ID: return
    await message.answer("✅ Бот запущен!\n/debug — показать лоты")

@dp.message(Command("debug"))
async def cmd_debug(message: Message):
    if message.chat.id != CHAT_ID: return
    await message.answer("🔍 Загружаю лоты...")
    lots = await fetch_recent_lots(15)
    if not lots:
        await message.answer("❌ Лотов не найдено. API может быть недоступен.")
        return
    text = "📋 Последние лоты:\n\n"
    for lot in lots:
        area = f"{lot['area']} м²" if lot.get('area') else "—"
        text += f"• {lot['name']}\n  Площадь: {area}\n  {lot['link']}\n\n"
    await message.answer(text[:4000])

async def start_bot():
    await init_db()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_new_lots, IntervalTrigger(minutes=CHECK_INTERVAL_MINUTES), args=[bot])
    scheduler.start()
    await asyncio.sleep(10)
    await check_new_lots(bot)
    await dp.start_polling(bot)

async def check_new_lots(bot: Bot):
    lots = await fetch_recent_lots(50)
    logger.info(f"Проверено {len(lots)} лотов")

if __name__ == "__main__":
    logger.info("🚀 Запуск...")
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))