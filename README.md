# Telegram бот для мониторинга лотов torgi.gov.ru

Бот мониторит новые земельные участки на torgi.gov.ru и присылает уведомления.

## Быстрый старт на Render.com

1. Создай бота у @BotFather → скопируй токен
2. Узнай свой Chat ID (через getUpdates)
3. Создай Web Service на Render
4. Добавь переменные окружения:
   - `BOT_TOKEN`
   - `CHAT_ID`
   - `CHECK_INTERVAL_MINUTES=5`
5. **Build Command**: `pip install -r requirements.txt`
6. **Start Command**: `python main.py`
7. Добавь пинг через UptimeRobot (чтобы не засыпал)

После запуска напиши боту `/start`