# config.py
import os

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # bot token
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/telegram_bot_db")
DB_CHANNEL_ID = int(os.getenv("DB_CHANNEL_ID", "-1001771131381"))  # provided by you
OWNER_ID = os.getenv("OWNER_ID")  # put your Telegram user id
FORCE_SUB_CHANNEL = os.getenv("FORCE_SUB_CHANNEL")  # optional channel username or id
AUTO_DELETE_SECONDS = int(os.getenv("AUTO_DELETE_SECONDS", "300"))  # default 5 minutes
