# main.py
import os
import asyncio
import httpx
import traceback
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from typing import Dict, Any
from config import TELEGRAM_BOT_TOKEN, DB_CHANNEL_ID, OWNER_ID, EXPOSED_URL, AUTO_DELETE_SECONDS, FORCE_SUB_CHANNEL_ID
from db import mongo

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN env var")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

app = FastAPI()

# util: send simple message
async def send_msg(chat_id: int, text: str, reply_markup: Dict | None = None):
    async with httpx.AsyncClient(timeout=30) as client:
        data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup is not None:
            data["reply_markup"] = reply_markup
        r = await client.post(f"{TELEGRAM_API}/sendMessage", json=data)
        return r.json()

async def send_photo_by_file_id(chat_id: int, file_id: str, caption: str = ""):
    async with httpx.AsyncClient(timeout=60) as client:
        data = {"chat_id": chat_id, "photo": file_id, "caption": caption}
        r = await client.post(f"{TELEGRAM_API}/sendPhoto", json=data)
        return r.json()

async def delete_message(chat_id: int, message_id: int):
    async with httpx.AsyncClient(timeout=20) as client:
        await client.post(f"{TELEGRAM_API}/deleteMessage", json={"chat_id": chat_id, "message_id": message_id})

# startup / shutdown
@app.on_event("startup")
async def startup_event():
    await mongo.connect()
    # optionally set webhook (we'll keep a route to set webhook)
    print("Connected to Mongo, ready")

@app.on_event("shutdown")
async def shutdown_event():
    await mongo.close()

# helper: check subscription if FORCE_SUB_CHANNEL_ID set
async def user_is_member(user_id: int) -> bool:
    if not FORCE_SUB_CHANNEL_ID:
        return True
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(f"{TELEGRAM_API}/getChatMember", params={"chat_id": FORCE_SUB_CHANNEL_ID, "user_id": user_id})
        j = resp.json()
        ok = j.get("ok", False)
        if not ok:
            return False
        status = j["result"]["status"]
        return status not in ("left", "kicked")

# command helpers
async def cmd_start(chat_id: int, user_id: int, first_name: str | None):
    # button example
    markup = {
        "inline_keyboard": [
            [{"text": "Help", "callback_data": "help_cb"}],
            [{"text":"Subscribe check","callback_data":"sub_check"}]
        ]
    }
    text = f"Hello {first_name or ''}! I am the AutoFilter bot.\n\nAvailable commands: /help /stats \n\nPowered by: @jb_links"
    await send_msg(chat_id, text, reply_markup=markup)

async def cmd_help(chat_id: int):
    text = (
        "/start - start\n"
        "/help - this message\n"
        "/stats - bot statistics (users/files/groups)\n"
        "/broadcast <text> - owner only\n\n"
        "Forward a file/message to this bot in PM to index it. Use /deletefile <id> to delete the forwarded message (bot will delete it from where it was forwarded)."
    )
    await send_msg(chat_id, text)

async def cmd_stats(chat_id: int):
    users_count = await mongo.db.users.count_documents({})
    files_count = await mongo.db.files.count_documents({})
    groups_count = await mongo.db.groups.count_documents({})
    text = f"Stats:\nUsers: {users_count}\nIndexed files: {files_count}\nTracked groups: {groups_count}"
    await send_msg(chat_id, text)

# broadcast (owner only)
async def cmd_broadcast(text: str):
    cursor = mongo.db.users.find({}, {"user_id": 1})
    async for u in cursor:
        try:
            await send_msg(int(u["user_id"]), text)
        except Exception:
            pass

# background auto-delete scheduling
async def schedule_auto_delete(chat_id: int, message_id: int, delay_seconds: int):
    await asyncio.sleep(delay_seconds)
    try:
        await delete_message(chat_id, message_id)
    except Exception:
        pass

# webhook endpoint for Telegram
@app.post("/webhook")
async def webhook(req: Request, background_tasks: BackgroundTasks):
    update = await req.json()
    # only handle message updates (simplified)
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return {"ok": True}
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    from_user = msg.get("from", {})
    user_id = from_user.get("id")
    text = msg.get("text", "") or msg.get("caption", "") or ""
    # store user in DB
    if user_id:
        await mongo.db.users.update_one({"user_id": user_id}, {"$set": {
            "user_id": user_id,
            "first_name": from_user.get("first_name"),
            "username": from_user.get("username")
        }}, upsert=True)

    # commands in private chat or group (support both)
    if text and text.startswith("/"):
        parts = text.split(None, 1)
        cmd = parts[0].split("@")[0]
        arg = parts[1] if len(parts) > 1 else ""
        # /start
        if cmd == "/start":
            # optional subscription check
            allowed = await user_is_member(user_id)
            if not allowed:
                await send_msg(chat_id, "Please join the required channel first.")
                return {"ok": True}
            await cmd_start(chat_id, user_id, from_user.get("first_name"))
            return {"ok": True}
        if cmd == "/help":
            await cmd_help(chat_id)
            return {"ok": True}
        if cmd == "/stats":
            await cmd_stats(chat_id)
            return {"ok": True}
        if cmd == "/broadcast":
            if int(user_id) != int(OWNER_ID):
                await send_msg(chat_id, "You are not allowed.")
                return {"ok": True}
            if not arg:
                await send_msg(chat_id, "Usage: /broadcast <message>")
                return {"ok": True}
            await send_msg(chat_id, "Broadcast started.")
            asyncio.create_task(cmd_broadcast(arg))
            return {"ok": True}
        if cmd == "/deletefile":
            # arg can be DB id (string) or file_id
            key = arg.strip()
            if not key:
                await send_msg(chat_id, "Usage: /deletefile <dbid|file_id>")
                return {"ok": True}
            # try find by _id (string) or file_id
            found = await mongo.db.files.find_one({"_id": key}) or await mongo.db.files.find_one({"file_id": key})
            if not found:
                await send_msg(chat_id, "No such indexed file.")
                return {"ok": True}
            target_chat = found.get("original_chat_id")
            target_msg_id = found.get("original_message_id")
            # delete from where it was posted
            try:
                await delete_message(target_chat, target_msg_id)
                await send_msg(chat_id, "File deleted from chat.")
            except Exception as e:
                await send_msg(chat_id, f"Failed to delete: {e}")
            return {"ok": True}

    # handle forwarded messages (indexing)
    if msg.get("forward_from") or msg.get("forward_from_chat") or msg.get("forward_sender_name"):
        # index the file or the message
        # prefer photo, document, video - store file_id
        file_id = None
        file_type = None
        if "photo" in msg:
            # photos are an array; take largest
            photo = msg["photo"][-1]
            file_id = photo.get("file_id")
            file_type = "photo"
        elif "document" in msg:
            file_id = msg["document"].get("file_id")
            file_type = "document"
        elif "video" in msg:
            file_id = msg["video"].get("file_id")
            file_type = "video"
        # store index into DB and (optionally) forward to DB_CHANNEL_ID
        doc = {
            "file_id": file_id,
            "file_type": file_type,
            "indexed_by": user_id,
            "original_chat_id": chat_id,
            "original_message_id": msg.get("message_id"),
            "text": msg.get("caption", "") or msg.get("text", ""),
            "forwarded_at": msg.get("date"),
        }
        res = await mongo.db.files.insert_one(doc)
        # forward the message to DB_CHANNEL if configured
        if DB_CHANNEL_ID:
            # forward original message to DB channel and store forwarded message details
            async with httpx.AsyncClient(timeout=30) as client:
                try:
                    r = await client.post(f"{TELEGRAM_API}/forwardMessage", json={
                        "chat_id": DB_CHANNEL_ID,
                        "from_chat_id": chat_id,
                        "message_id": msg.get("message_id")
                    })
                    jr = r.json()
                    if jr.get("ok"):
                        forwarded = jr["result"]
                        # save forward detail
                        await mongo.db.files.update_one({"_id": res.inserted_id}, {"$set": {
                            "forwarded_chat_id": forwarded["chat"]["id"],
                            "forwarded_message_id": forwarded["message_id"]
                        }})
                except Exception:
                    pass
        # ack to user
        await send_msg(chat_id, f"Indexed file. id: {str(res.inserted_id)}")
        return {"ok": True}

    # if normal group message and group auto-delete turned on: schedule deletion
    # We want automatic message deletion of non-command messages in groups (per user request).
    if chat.get("type") in ("group", "supergroup"):
        # track group in DB
        await mongo.db.groups.update_one({"chat_id": chat_id}, {"$set": {"chat_id": chat_id, "title": chat.get("title")}}, upsert=True)
        # schedule delete for this message after AUTO_DELETE_SECONDS (if >0)
        if AUTO_DELETE_SECONDS and AUTO_DELETE_SECONDS > 0 and msg.get("message_id"):
            background_tasks.add_task(schedule_auto_delete, chat_id, msg["message_id"], AUTO_DELETE_SECONDS)
        # we are done
        return {"ok": True}

    return {"ok": True}

# helper route to set webhook (call once after deploy)
@app.get("/set_webhook")
async def set_webhook():
    if not EXPOSED_URL:
        raise HTTPException(status_code=400, detail="Set EXPOSED_URL env var first")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{TELEGRAM_API}/setWebhook", params={"url": f"{EXPOSED_URL}/webhook", "allowed_updates": ["message","edited_message"]})
        return resp.json()
