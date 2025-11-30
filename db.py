# optional: not required if above main.py used
from motor.motor_asyncio import AsyncIOMotorClient
import os

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")

client = AsyncIOMotorClient(MONGO_URI)
db = client[DB_NAME]
