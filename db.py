# db.py
# Manglish comments: DB connection and helpers
from pymongo import MongoClient
import os

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "telegram_bot_db")

if not MONGO_URI:
    raise RuntimeError("Set MONGO_URI env var first")

client = MongoClient(MONGO_URI)
db = client[DB_NAME]

# Collections
users_col = db["users"]     # stores chat info (users + groups)
files_col = db["files"]     # stores metadata for uploaded files
broadcasts_col = db["broadcasts"]  # optional history of broadcasts

# Helper upserts
def upsert_chat(chat):
    """
    chat: dict with keys id, type, title, username, first_name, last_name
    """
    users_col.update_one(
        {"chat_id": chat["id"]},
        {"$set": chat},
        upsert=True
    )

def add_file(meta: dict):
    """
    meta: dict describing file (file_id, file_unique_id, file_type, chat_id, from_id, date, caption)
    """
    files_col.insert_one(meta)

def stats():
    return {
        "total_users": users_col.count_documents({"type": "private"}),
        "total_groups": users_col.count_documents({"type": {"$ne": "private"}}),
        "total_files": files_col.count_documents({})
    }

def get_all_user_chat_ids(batch_size=1000):
    # Only return private chats (users), for broadcast
    cursor = users_col.find({"type": "private"}, {"chat_id": 1})
    return [r["chat_id"] for r in cursor]
