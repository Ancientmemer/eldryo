# config.py
import os

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
MONGO_URI = os.getenv("MONGO_URI", "")
DB_NAME = os.getenv("DB_NAME", "telegram_bot_db")
DB_CHANNEL_ID = int(os.getenv("DB_CHANNEL_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
EXPOSED_URL = os.getenv("EXPOSED_URL", "")
AUTO_DELETE_SECONDS = int(os.getenv("AUTO_DELETE_SECONDS", "300"))
FORCE_SUB_CHANNEL_ID = os.getenv("FORCE_SUB_CHANNEL_ID", "")
FORCE_SUB_OPTIONAL = os.getenv("FORCE_SUB_OPTIONAL", "false").lower() == "true"
