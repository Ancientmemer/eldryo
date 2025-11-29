# db.py
from motor.motor_asyncio import AsyncIOMotorClient
from config import MONGO_URI, DB_NAME

class Mongo:
    def __init__(self):
        self.client = None
        self.db = None

    async def connect(self):
        if not MONGO_URI:
            raise RuntimeError("MONGO_URI env var not set")
        self.client = AsyncIOMotorClient(MONGO_URI)
        # ensure db name:
        self.db = self.client.get_default_database() if self.client.get_default_database() else self.client[DB_NAME]
        # ensure indexes
        await self.db.users.create_index("user_id", unique=True)
        await self.db.files.create_index("file_id")
        await self.db.groups.create_index("chat_id", unique=True)

    async def close(self):
        if self.client:
            self.client.close()

mongo = Mongo()
