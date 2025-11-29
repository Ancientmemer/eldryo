# db.py
# MongoDB helper (updated to handle URIs without default DB name)
import os
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime
from typing import List
from pymongo.errors import ConfigurationError

class MongoDB:
    def __init__(self, uri: str):
        self.uri = uri
        self.client = None
        self.db = None

    async def connect(self):
        # create client
        self.client = AsyncIOMotorClient(self.uri)

        # Prefer explicit env var DB name if provided
        db_name = os.getenv("MONGODB_DB")
        if db_name:
            self.db = self.client[db_name]
        else:
            # Try to use default DB from URI, but handle case where it's not present
            try:
                self.db = self.client.get_default_database()
                # get_default_database() returns None if not present in URI
                if self.db is None:
                    raise ConfigurationError("No default database in URI")
            except ConfigurationError:
                # fallback to a safe default name
                self.db = self.client["telegram_bot_db"]

        # create indexes for quick stats (idempotent)
        await self.db.users.create_index("id", unique=True)
        await self.db.groups.create_index("id", unique=True)
        await self.db.files.create_index([("user_id", 1)])
        await self.db.indexes.create_index([("original_chat", 1), ("original_message_id", 1)], unique=False)

    async def close(self):
        if self.client:
            self.client.close()

    async def upsert_user(self, user_id: int, user_obj: dict):
        await self.db.users.update_one(
            {"id": user_id},
            {"$set": {"id": user_id, "data": user_obj, "updated_at": datetime.utcnow()}},
            upsert=True
        )

    async def upsert_group(self, group_id: int, group_obj: dict):
        await self.db.groups.update_one(
            {"id": group_id},
            {"$set": {"id": group_id, "data": group_obj, "updated_at": datetime.utcnow()}},
            upsert=True
        )

    async def insert_file(self, doc: dict):
        await self.db.files.insert_one(doc)

    async def insert_index(self, doc: dict):
        await self.db.indexes.insert_one(doc)

    async def mark_file_deleted_by_forward(self, chat_id: int, message_id: int):
        await self.db.files.update_many(
            {"forwarded_to_db": message_id},
            {"$set": {"deleted_from_db": True, "deleted_at": datetime.utcnow()}}
        )

    async def count_files(self) -> int:
        return await self.db.files.count_documents({})

    async def count_users(self) -> int:
        return await self.db.users.count_documents({})

    async def count_groups(self) -> int:
        return await self.db.groups.count_documents({})

    async def get_all_user_ids(self) -> List[int]:
        cur = self.db.users.find({}, {"id": 1})
        return [d["id"] async for d in cur]
