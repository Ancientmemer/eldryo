# main.py
# Manglish comments inside
import os
import asyncio
import traceback
from typing import Optional
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
import httpx
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel
from datetime import datetime, timedelta

# local modules
from db import MongoDB
from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_API, DB_CHANNEL_ID,
    MONGO_URI, OWNER_ID, FORCE_SUB_CHANNEL, AUTO_DELETE_SECONDS
)

app = FastAPI()
mongo = MongoDB(MONGO_URI)
# Ensure db connection on startup
@app.on_event("startup")
async def startup():
    await mongo.connect()

@app.on_event("shutdown")
async def shutdown():
    await mongo.close()

# small helper to send telegram messages
async def tg_post(path: str, data: dict):
    url = f"{TELEGRAM_API}/{path}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(url, data=data)
        return r

async def forward_to_db_channel(chat_id:int, message_id:int):
    """
    Manglish: Forward the message to DB channel and return the forwarded message_id.
    """
    data = {
        "chat_id": DB_CHANNEL_ID,
        "from_chat_id": chat_id,
        "message_id": message_id
    }
    resp = await tg_post("forwardMessage", data)
    if resp.status_code != 200:
        raise RuntimeError(f"Forward failed: {resp.status_code} {resp.text}")
    return (await resp.json())["result"]["message_id"]

async def delete_message(chat_id:int, message_id:int):
    await tg_post("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

async def send_message(chat_id:int, text:str, reply_markup:Optional[dict]=None):
    data = {"chat_id": chat_id, "text": text}
    if reply_markup:
        data["reply_markup"] = reply_markup
    await tg_post("sendMessage", data)

# Background task: schedule delete a message after AUTO_DELETE_SECONDS (only groups)
async def schedule_auto_delete(chat_id:int, message_id:int):
    await asyncio.sleep(AUTO_DELETE_SECONDS)
    try:
        await delete_message(chat_id, message_id)
    except Exception:
        # ignore delete failures
        pass

# Handle user commands and all messages
async def handle_agent(update: dict, background_tasks: BackgroundTasks):
    msg = update.get("message") or {}
    if not msg:
        return

    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type", "private")
    from_user = msg.get("from", {})
    user_id = from_user.get("id")
    text = msg.get("text", "") or msg.get("caption", "") or ""
    message_id = msg.get("message_id")

    # Force-sub check
    if FORCE_SUB_CHANNEL:
        # check membership
        resp = await tg_post("getChatMember", {"chat_id": FORCE_SUB_CHANNEL, "user_id": user_id})
        if resp.status_code != 200:
            # treat as not member
            await send_message(chat_id, "You must join the required channel to use this bot.")
            return
        res = await resp.json()
        status = res.get("result", {}).get("status")
        if status in ("left", "kicked", None):
            await send_message(chat_id, "Please join the channel to use the bot.")
            return

    # save user / group
    try:
        if chat_type == "private":
            await mongo.upsert_user(user_id, from_user)
        else:
            await mongo.upsert_group(chat_id, chat)
    except Exception:
        traceback.print_exc()

    # commands
    if text.startswith("/start"):
        # show buttons (inline keyboard)
        keyboard = {
            "inline_keyboard": [
                [{"text": "Help", "callback_data": "help"}],
                [{"text": "Stats", "callback_data": "stats"}]
            ]
        }
        await send_message(chat_id, "Hello! I'm your Auto-Filter Bot. Use /help to see commands.", reply_markup=keyboard)
        return

    if text.startswith("/help"):
        help_text = (
            "/start - Start & buttons\n"
            "/help - This message\n"
            "/stats - Total files, users, groups\n"
            "/broadcast <text> - Owner only, send to all users\n"
            "/deletefile - Reply to a forwarded DB-channel message in PM to delete it from DB channel\n\n"
            "File behavior: When you send or forward files to the bot (in group or PM), I will forward them to the DB channel and store metadata. Group messages are auto-deleted after configured time (5 minutes)."
        )
        await send_message(chat_id, help_text)
        return

    if text.startswith("/stats"):
        files_count = await mongo.count_files()
        users_count = await mongo.count_users()
        groups_count = await mongo.count_groups()
        await send_message(chat_id, f"Files: {files_count}\nUsers: {users_count}\nGroups: {groups_count}")
        return

    if text.startswith("/broadcast"):
        # owner-only
        if str(user_id) != str(OWNER_ID):
            await send_message(chat_id, "Only owner can use /broadcast")
            return
        payload = text.split(" ", 1)
        if len(payload) < 2:
            await send_message(chat_id, "Usage: /broadcast <message text>")
            return
        message = payload[1]
        # broadcast to users only to avoid spam in groups (you can modify)
        users = await mongo.get_all_user_ids()
        sent = 0
        for u in users:
            try:
                await send_message(u, message)
                sent += 1
            except Exception:
                pass
        await send_message(chat_id, f"Broadcast sent to {sent} users.")
        return

    # /deletefile: only works in private chat when replying to a forwarded db-channel message
    if text.startswith("/deletefile"):
        # must be reply_to_message
        reply = msg.get("reply_to_message")
        if not reply:
            await send_message(chat_id, "Reply to the forwarded DB-channel message you want to delete.")
            return
        # We expect the replied message to be a forwarded message from DB channel
        fwd = reply.get("forward_from_chat") or reply.get("forward_from")
        # If user forwarded the DB-channel message to bot, the forwarded message has forward_from_chat.id == DB_CHANNEL_ID
        if (reply.get("forward_from_chat") and reply["forward_from_chat"].get("id") == DB_CHANNEL_ID) or reply.get("forward_from") and isinstance(reply.get("forward_from"), dict) and reply["forward_from"].get("id") == DB_CHANNEL_ID:
            target_mid = reply.get("message_id")
            try:
                await delete_message(DB_CHANNEL_ID, target_mid)
                # Update DB record status
                await mongo.mark_file_deleted_by_forward(DB_CHANNEL_ID, target_mid)
                await send_message(chat_id, "Deleted from DB channel.")
            except Exception as e:
                await send_message(chat_id, f"Delete failed: {e}")
        else:
            await send_message(chat_id, "That message is not a forwarded DB-channel message.")
        return

    # If message contains a file (photo, document, audio, video, voice, sticker)
    file_keys = ["photo", "document", "video", "audio", "voice", "sticker"]
    has_file = any(k in msg for k in file_keys)
    if has_file or msg.get("forward_from") or msg.get("forward_from_chat"):
        # store file metadata
        saved = None
        try:
            fwd_from = msg.get("forward_from_chat") or msg.get("forward_from")
            orig_chat = None
            orig_msg_id = None
            if fwd_from:
                # it's forwarded from some chat
                orig_chat = fwd_from.get("id") if isinstance(fwd_from, dict) else None
                orig_msg_id = msg.get("forward_from_message_id")
            forwarded_message_id = None
            try:
                forwarded_message_id = await forward_to_db_channel(chat_id, message_id)
            except Exception as e:
                traceback.print_exc()
                forwarded_message_id = None

            # index info
            file_record = {
                "user_id": user_id,
                "from_chat_id": chat_id,
                "chat_type": chat_type,
                "message_id": message_id,
                "forwarded_to_db": forwarded_message_id,
                "original_chat": orig_chat,
                "original_message_id": orig_msg_id,
                "timestamp": datetime.utcnow()
            }
            await mongo.insert_file(file_record)
            # if forwarded_message_id exists, save index mapping
            if forwarded_message_id and orig_chat and orig_msg_id:
                await mongo.insert_index({
                    "original_chat": orig_chat,
                    "original_message_id": orig_msg_id,
                    "db_channel_message_id": forwarded_message_id,
                    "added_at": datetime.utcnow()
                })
        except Exception:
            traceback.print_exc()

        # If message is in a group, schedule auto-delete
        if chat_type != "private":
            background_tasks.add_task(schedule_auto_delete, chat_id, message_id)

        # reply ack
        await send_message(chat_id, "Saved and forwarded to DB channel ✅")
        return

    # fallback: simple echo/ignore
    return

@app.post("/webhook")
async def webhook(req: Request, background_tasks: BackgroundTasks):
    update = await req.json()
    background_tasks.add_task(handle_agent, update, background_tasks)
    return {"ok": True}

@app.get("/set_webhook")
async def set_webhook():
    exposed = os.getenv("EXPOSED_URL")
    if not exposed:
        raise HTTPException(status_code=400, detail="Set EXPOSED_URL env var first")
    webhook_url = f"{exposed}/webhook"
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{TELEGRAM_API}/setWebhook", params={"url": webhook_url})
        return resp.json()
