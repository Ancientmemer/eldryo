# main.py
# FastAPI webhook for Telegram bot
# Manglish: Simple webhook server for Telegram, uses hf_client style layout

import os
import asyncio
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
import httpx
from motor.motor_asyncio import AsyncIOMotorClient
from bson.objectid import ObjectId

from db import DB
from bot_logic import (
    handle_update,
    set_webhook_telegram
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
EXPOSED_URL = os.getenv("EXPOSED_URL")  # https://<your-app>.koyeb.app
PORT = int(os.getenv("PORT", "8080"))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN env var first")
if not MONGO_URI:
    raise RuntimeError("Set MONGO_URI env var first")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

app = FastAPI()
# init global db
db_client = AsyncIOMotorClient(MONGO_URI)
db = DB(db_client.get_default_database())


@app.on_event("startup")
async def startup():
    # ensure indexes
    await db.ensure_indexes()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(req: Request, background_tasks: BackgroundTasks):
    update = await req.json()
    # quick ack
    background_tasks.add_task(handle_update, update, db, TELEGRAM_API)
    return {"ok": True}


@app.get("/set_webhook")
async def set_webhook():
    if not EXPOSED_URL:
        raise HTTPException(status_code=400, detail="Set EXPOSED_URL env var first")
    webhook_url = f"{EXPOSED_URL}/webhook"
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{TELEGRAM_API}/setWebhook",
            params={"url": webhook_url}
        )
        return resp.json()


# small helper to send messages from outside
@app.post("/admin/broadcast")
async def admin_broadcast(payload: Request):
    """
    Optional admin HTTP endpoint to broadcast via POST json:
    { "secret": "admin-secret", "text": "hello users" }
    (we don't implement secret here - use environment or protect in Koyeb)
    """
    body = await payload.json()
    text = body.get("text")
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    # find admin user from env
    admin_id = os.getenv("ADMIN_ID")
    if not admin_id:
        raise HTTPException(status_code=403, detail="ADMIN_ID not set")
    # do broadcast in background
    asyncio.create_task(set_webhook_telegram(text, db, TELEGRAM_API))
    return {"ok": True, "started": True}
