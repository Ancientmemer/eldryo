import os
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx
from config import BOT_TOKEN, OWNER_ID, DB_CHANNEL_ID, AUTO_DELETE_SECONDS
from db import mongo
import asyncio

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("eldro-bot")

BOT_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = FastAPI()

# ---------------------- TELEGRAM SEND MESSAGE -------------------------------- #

async def send_message(chat_id, text, reply_markup=None):
    async with httpx.AsyncClient() as client:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        r = await client.post(f"{BOT_API}/sendMessage", json=payload)
        if r.status_code != 200:
            log.error(f"Telegram send error: {r.text}")
        return r.json()

# ---------------------- STARTUP EVENT -------------------------------- #

@app.on_event("startup")
async def startup_event():
    log.info("Connecting to MongoDB...")
    await mongo.connect()
    log.info("App startup complete")

# ---------------------- WEBHOOK SETUP -------------------------------- #

@app.get("/set_webhook")
async def set_webhook():
    webhook_url = f"{os.getenv('EXPOSED_URL')}/webhook"
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{BOT_API}/setWebhook",
            params={"url": webhook_url, "allowed_updates": ["message", "callback_query"]}
        )
        return JSONResponse(r.json())

# ---------------------- MAIN WEBHOOK -------------------------------- #

@app.post("/webhook")
async def webhook(req: Request):
    data = await req.json()

    if "message" in data:
        message = data["message"]
        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        text = message.get("text", "")

        # -------------------- /start -------------------- #
        if text == "/start":
            buttons = {
                "inline_keyboard": [
                    [{"text": "Help ⚙️", "callback_data": "help"}],
                    [{"text": "About 👑", "callback_data": "about"}]
                ]
            }
            await send_message(chat_id, "<b>Welcome to Eldro Auto Filter Bot</b>", buttons)

        # -------------------- /help -------------------- #
        elif text == "/help":
            await send_message(chat_id,
                               "<b>Available Commands:</b>\n/start\n/help\n/stats\n/broadcast")

        # -------------------- /stats -------------------- #
        elif text == "/stats":
            total_files = await mongo.count_files()
            total_users = await mongo.count_users()
            total_groups = await mongo.count_groups()

            msg = (
                f"<b>📊 Bot Stats</b>\n\n"
                f"Total Files: <b>{total_files}</b>\n"
                f"Total Users: <b>{total_users}</b>\n"
                f"Total Groups: <b>{total_groups}</b>"
            )
            await send_message(chat_id, msg)

        # -------------------- BROADCAST -------------------- #
        elif text.startswith("/broadcast") and str(user_id) == OWNER_ID:
            bc_msg = text.replace("/broadcast", "").strip()
            if not bc_msg:
                return await send_message(chat_id, "Usage:\n/broadcast Your message here")
            users = await mongo.get_all_users()
            for uid in users:
                await send_message(uid, bc_msg)
                await asyncio.sleep(0.1)
            await send_message(chat_id, "Broadcast completed ✔️")

        # -------------------- FILE INDEXING (Forward to DB Channel) -------------------- #
        elif "document" in message or "video" in message or "photo" in message:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{BOT_API}/forwardMessage",
                    data={
                        "chat_id": DB_CHANNEL_ID,
                        "from_chat_id": chat_id,
                        "message_id": message["message_id"]
                    }
                )
            await send_message(chat_id, "File saved to index ✔️")

        # -------------------- AUTO DELETE MESSAGE -------------------- #
        if AUTO_DELETE_SECONDS > 0:
            async with httpx.AsyncClient() as client:
                await asyncio.sleep(AUTO_DELETE_SECONDS)
                await client.get(
                    f"{BOT_API}/deleteMessage",
                    params={"chat_id": chat_id, "message_id": message["message_id"]}
                )

    return {"ok": True}

@app.get("/")
def home():
    return {"status": "Running Eldro Auto Filter Bot"}
