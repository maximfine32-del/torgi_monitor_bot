#!/usr/bin/env python3
import asyncio
import io
import json
import os
import re
from contextlib import asynccontextmanager
from typing import List, Dict

import aiosqlite
import httpx
import pandas as pd
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
EXPORT_API = f"{TORGI_BASE}/new/api/public/lotcards/export/excel"

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(start_bot())
    yield

app = FastAPI(lifespan=lifespan)

@app.get("/health")
async def health():
    return {"status": "healthy", "bot": "running"}

dp = Dispatcher()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

async def init_db():
    async with aiosqlite.connect("torgi_bot.db") as db:
        await db.execute("CREATE TABLE IF NOT EXISTS processed_lots (lot_id TEXT PRIMARY KEY)")
        await db.commit()

async def fetch_recent_lots(limit: int = 30) -> List[Dict]:
    """Используем экспорт в Excel — самый надёжный способ"""
    params = {
        "lotStatus": "PUBLISHED,APPLICATIONS_SUBMISSION,DETERMINING_WINNER",
        "byFirstVersion": "true",
        "sort": "firstVersionPublicationDate,desc",
        "size": str(limit),
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "Referer": f"{TORGI_BASE}/new/public/lots/reg",
    }

    try:
        async with httpx.AsyncClient(timeout=40, follow_redirects=True) as client:
            resp = await client.get(EXPORT_API, params=params, headers=headers)
            if resp.status_code != 200:
                logger.error(f"Excel API status: {resp.status_code}")
                return []

            # Читаем Excel из памяти
            df = pd.read_excel(io.BytesIO(resp.content))

            logger.info(f"Получено строк из Excel: {len(df)}")

            lots = []
            for _, row in df.head(limit).iterrows():
                lot = normalize_row(row)
                if lot:
                    lots.append(lot)
            return lots

    except Exception as e:
        logger.error(f"Excel API Error: {e}")
        return []

def normalize_row(row) -> Dict | None:
    try:
        lot_id = str(row.get("ID лота") or row.get("id") or row.iloc[0] or "")
        if not lot_id or lot_id == "nan":
            return None

        name = str(row.get("Наименование", "") or row.get("name", ""))[:200]
        area = None
        for col in ["Площадь", "area", "Площадь, м2", "Площадь (м²)"]:
            if col in row and pd.notna(row[col]):
                try:
                    area = float(row[col])
                    break
                except:
                    pass

        if area is None:
            area = parse_area(name)

        link = f"{TORGI_BASE}/new/public/lots/lot/{lot_id}_1"

        return {
            "id": lot_id,
            "name": name,
            "area": area,
            "link": link,
            "region": str(row.get("Регион", "") or row.get("Субъект РФ", ""))
        }
    except:
        return None

def parse_area(text: str):
    if not text: return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:м²|м2|кв\.?м)", str(text).lower())
    return float(match.group(1)) if match else None

@dp.message(CommandStart())
async def cmd_start(message: Message):
    if message.chat.id != CHAT_ID: return
    await message.answer("✅ Бот запущен! Используй /debug")

@dp.message(Command("debug"))
async def cmd_debug(message: Message):
    if message.chat.id != CHAT_ID: return
    await message.answer("🔍 Загружаю последние лоты через Excel...")
    lots = await fetch_recent_lots(12)
    if not lots:
        await message.answer("❌ Лотов не получено. Попробую позже.")
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
    await asyncio.sleep(8)
    await check_new_lots(bot)
    await dp.start_polling(bot)

async def check_new_lots(bot: Bot):
    lots = await fetch_recent_lots(40)
    logger.info(f"Проверено {len(lots)} лотов")

if __name__ == "__main__":
    logger.info("🚀 Запуск бота (Excel режим)")
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))