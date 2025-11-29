# db.py
# Manglish: DB helpers using Motor (async MongoDB driver)

from motor.motor_asyncio import AsyncIOMotorDatabase
from typing import Optional, List
from pymongo import IndexModel, ASCENDING

class DB:
    def __init__(self, database: AsyncIOMotorDatabase):
        self.db = database
        self.users = self.db["users"]
        self.files = self.db["files"]
        self.groups = self.db["groups"]
        self.filters = self.db["filters"]
        self.channels = self.db["channels"]  # store forwarding channel config

    async def ensure_indexes(self):
        # unique on user id
        await self.users.create_index([("user_id", ASCENDING)], unique=True)
        await self.groups.create_index([("chat_id", ASCENDING)], unique=True)
        await self.files.create_index([("file_id", ASCENDING)])
        # filters: word index
        await self.filters.create_index([("word", ASCENDING)], unique=True)

    # Users
    async def add_user(self, user_id: int, info: dict):
        await self.users.update_one(
            {"user_id": user_id},
            {"$set": {"info": info}, "$setOnInsert": {"first_seen": {"$date": True}}},
            upsert=True
        )

    async def count_users(self) -> int:
        return await self.users.count_documents({})

    # Groups
    async def add_group(self, chat_id: int, info: dict):
        await self.groups.update_one({"chat_id": chat_id}, {"$set": info}, upsert=True)

    async def count_groups(self) -> int:
        return await self.groups.count_documents({})

    # Files
    async def save_file(self, file_meta: dict):
        # file_meta: {file_id, file_unique_id, from_id, chat_id, file_name, mime_type, size, tg_message_id}
        await self.files.insert_one(file_meta)

    async def count_files(self) -> int:
        return await self.files.count_documents({})

    # Filters
    async def add_banned_word(self, word: str):
        await self.filters.update_one({"word": word.lower()}, {"$set": {"word": word.lower()}}, upsert=True)

    async def remove_banned_word(self, word: str):
        await self.filters.delete_one({"word": word.lower()})

    async def list_banned(self) -> List[str]:
        cur = self.filters.find({}, {"_id":0,"word":1})
        return [d["word"] async for d in cur]

    async def is_banned(self, text: str) -> bool:
        # naive contains check, case-insensitive
        words = await self.list_banned()
        low = text.lower()
        for w in words:
            if w and w in low:
                return True
        return False

    # Channel config for forwarding files
    async def set_upload_channel(self, chat_id: int):
        await self.channels.update_one({"_id":"uploads_channel"}, {"$set": {"chat_id": chat_id}}, upsert=True)

    async def get_upload_channel(self) -> Optional[int]:
        doc = await self.channels.find_one({"_id":"uploads_channel"})
        return doc["chat_id"] if doc else None
