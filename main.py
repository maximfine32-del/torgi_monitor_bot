#!/usr/bin/env python3
import asyncio
import io
import json
import os
import re
import traceback
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
LOTCARDS_API = f"{TORGI_BASE}/new/api/public/lotcards"
EXPORT_API = f"{LOTCARDS_API}/export/excel"

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(start_bot())
    yield

app = FastAPI(lifespan=lifespan)

@app.get("/health")
async def health():
    return {"status": "healthy"}

dp = Dispatcher()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

async def init_db():
    async with aiosqlite.connect("torgi_bot.db") as db:
        await db.execute("CREATE TABLE IF NOT EXISTS processed_lots (lot_id TEXT PRIMARY KEY)")
        await db.commit()

async def fetch_with_retry(url: str, params: dict, headers: dict, max_retries: int = 5) -> httpx.Response | None:
    """Ретраи + игнорирование SSL + длинный таймаут"""
    for attempt in range(max_retries):
        try:
            timeout = 40 + (attempt * 20)   # 40 → 60 → 80 → 100 → 120 сек
            async with httpx.AsyncClient(
                timeout=timeout,
                verify=False,                    # ← Игнорируем проблемы с SSL-сертификатом
                follow_redirects=True
            ) as client:
                resp = await client.get(url, params=params, headers=headers)
                return resp
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as e:
            logger.warning(f"Попытка {attempt + 1}/{max_retries} — таймаут: {type(e).__name__}")
            if attempt < max_retries - 1:
                await asyncio.sleep(4 + attempt * 3)
            else:
                logger.error(f"Все попытки исчерпаны: {e}")
                return None
        except Exception as e:
            logger.error(f"Неизвестная ошибка: {e}\n{traceback.format_exc()}")
            return None
    return None

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
        "Accept-Language": "ru-RU,ru;q=0.9",
    }

    # === JSON ===
    resp = await fetch_with_retry(LOTCARDS_API, params, headers)
    if resp and resp.status_code == 200:
        try:
            data = resp.json()
            items = data.get("content", []) if isinstance(data, dict) else data
            lots = [normalize_lot(item) for item in items if normalize_lot(item)]
            if lots:
                logger.info(f"JSON успех: {len(lots)} лотов")
                return lots
        except Exception as e:
            logger.warning(f"Ошибка парсинга JSON: {e}")

    # === Excel ===
    resp = await fetch_with_retry(EXPORT_API, params, headers)
    if resp and resp.status_code == 200:
        try:
            df = pd.read_excel(io.BytesIO(resp.content))
            lots = []
            for _, row in df.head(limit).iterrows():
                lot = normalize_row(row)
                if lot:
                    lots.append(lot)
            if lots:
                logger.info(f"Excel успех: {len(lots)} лотов")
                return lots
        except Exception as e:
            logger.error(f"Ошибка парсинга Excel: {e}")

    return []

def normalize_lot(raw: dict) -> Dict | None:
    try:
        lot_id = str(raw.get("id") or raw.get("lotId", ""))
        if not lot_id: return None
        return {
            "id": lot_id,
            "name": str(raw.get("name", ""))[:180],
            "area": raw.get("area") or parse_area(str(raw.get("name", "")) + " " + str(raw.get("description", ""))),
            "link": f"{TORGI_BASE}/new/public/lots/lot/{lot_id}_1",
            "region": raw.get("subjectRFName", "")
        }
    except:
        return None

def normalize_row(row) -> Dict | None:
    try:
        lot_id = str(row.iloc[0] if hasattr(row, 'iloc') else row.get("ID", "")).strip()
        if not lot_id or lot_id in ["nan", ""]: return None
        name = str(row.get("Наименование", "") or row.get("name", ""))[:180]
        area = parse_area(name)
        return {
            "id": lot_id,
            "name": name,
            "area": area,
            "link": f"{TORGI_BASE}/new/public/lots/lot/{lot_id}_1",
            "region": str(row.get("Регион", "") or "")
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
    await message.answer("✅ Бот запущен (с ретраями + игнор SSL)")

@dp.message(Command("debug"))
async def cmd_debug(message: Message):
    if message.chat.id != CHAT_ID: return
    await message.answer("🔍 Пробую получить лоты (с ретраями)...")
    lots = await fetch_recent_lots(12)
    if not lots:
        await message.answer("❌ Лотов нет. Смотри логи в Render.")
        return
    text = "📋 Последние лоты:\n\n"
    for lot in lots:
        area = f"{lot.get('area')} м²" if lot.get('area') else "—"
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
    lots = await fetch_recent_lots(30)
    logger.info(f"Проверено {len(lots)} лотов")

if __name__ == "__main__":
    logger.info("🚀 Запуск (ретраи + verify=False)")
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))