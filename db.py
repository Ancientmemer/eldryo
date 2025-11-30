# db.py
import os
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime
from typing import List
from pymongo.errors import ConfigurationError

class MongoDB:
    def __init__(self, uri: str, db_name: str = None):
        self.uri = uri
        self.db_name = db_name or os.getenv("DB_NAME", "telegram_bot_db")
        self.client = None
        self.db = None

    async def connect(self):
        self.client = AsyncIOMotorClient(self.uri)
        # prefer explicit DB name
        if self.db_name:
            self.db = self.client[self.db_name]
        else:
            try:
                self.db = self.client.get_default_database()
                if self.db is None:
                    raise ConfigurationError("No default database in URI")
            except ConfigurationError:
                self.db = self.client["telegram_bot_db"]

        await self.db.users.create_index("user_id", unique=True)
        await self.db.chats.create_index("chat_id", unique=True)
        await self.db.files.create_index([("file_meta.file_id", 1)])
        await self.db.sessions.create_index("user_id", unique=True)

    async def close(self):
        if self.client:
            self.client.close()
