# main.py
import os
import logging
import asyncio
from typing import Optional, Any, Dict

from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
import httpx
from motor.motor_asyncio import AsyncIOMotorClient
from bson.objectid import ObjectId
from datetime import datetime, timezone

# --- CONFIG via env ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
EXPOSED_URL = os.getenv("EXPOSED_URL", "")
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")  # MUST be provided
DB_CHANNEL_ID = int(os.getenv("DB_CHANNEL_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
AUTO_DELETE_SECONDS = int(os.getenv("AUTO_DELETE_SECONDS", "300"))
# FORCE_SUB_CHANNEL_ID: channel id (like -100...) or username @channel
FORCE_SUB_CHANNEL_ID = os.getenv("FORCE_SUB_CHANNEL_ID", "")
# If you want force-sub to be optional (only warn), set FORCE_SUB_OPTIONAL true
FORCE_SUB_OPTIONAL = os.getenv("FORCE_SUB_OPTIONAL", "false").lower() == "true"

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN env var")
if not MONGO_URI:
    raise RuntimeError("Set MONGO_URI env var")
if not DB_NAME:
    raise RuntimeError("Set DB_NAME env var")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# --- logging & app ---
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("eldro-bot")

app = FastAPI()
client = AsyncIOMotorClient(MONGO_URI)
db = client[DB_NAME]

# Reuse httpx client
http_client = httpx.AsyncClient(timeout=30.0)


# --- TELEGRAM helpers (with checks) ---
async def tg_request(path: str, method: str = "post", params: dict = None, data: dict = None) -> Dict[str, Any]:
    url = f"{TELEGRAM_API}/{path}"
    try:
        if method.lower() == "get":
            r = await http_client.get(url, params=params)
        else:
            r = await http_client.post(url, json=data)
        # try json
        try:
            resp = r.json()
        except Exception:
            resp = {"ok": False, "status_code": r.status_code, "text": r.text}
        if not resp.get("ok", False):
            log.warning("TG %s %s returned not ok: %s", method.upper(), path, resp)
        return resp
    except Exception as e:
        log.exception("tg_request failed %s %s", method, path)
        return {"ok": False, "error": str(e)}


async def tg_send_message(chat_id: int, text: str, reply_markup: dict = None, parse_mode: str = "HTML"):
    data = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        data["reply_markup"] = reply_markup
    resp = await tg_request("sendMessage", data=data)
    return resp


async def tg_forward(chat_id: int, from_chat_id: int, message_id: int):
    data = {"chat_id": chat_id, "from_chat_id": from_chat_id, "message_id": message_id}
    return await tg_request("forwardMessage", data=data)


async def tg_delete(chat_id: int, message_id: int):
    data = {"chat_id": chat_id, "message_id": message_id}
    return await tg_request("deleteMessage", data=data)


async def tg_get_chat_member(chat_id: str | int, user_id: int):
    # chat_id can be username or id
    params = {"chat_id": chat_id, "user_id": user_id}
    return await tg_request("getChatMember", method="get", params=params)


# --- Utilities ---
def buttons_for_start():
    keyboard = {
        "inline_keyboard": [
            [{"text": "Help", "callback_data": "help"}],
            [{"text": "Stats", "callback_data": "stats"}],
            [{"text": "Broadcast (owner)", "callback_data": "broadcast"}]
        ]
    }
    return keyboard


async def schedule_delete_original(chat_id: int, message_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        d = await tg_delete(chat_id, message_id)
        if not d.get("ok"):
            log.warning("Scheduled delete failed: %s", d)
        else:
            log.info("Deleted message %s from %s", message_id, chat_id)
    except Exception:
        log.exception("Error deleting message")


# --- Startup: ensure indexes ---
@app.on_event("startup")
async def startup_event():
    # create indexes (idempotent)
    await db.files.create_index("file_meta.file_id")
    await db.chats.create_index("chat_id", unique=True)
    await db.users.create_index("user_id", unique=True)
    await db.sessions.create_index("user_id", unique=True)
    log.info("App startup complete")


# record chat & user
async def record_chat_and_user(msg: dict):
    from_user = msg.get("from", {})
    chat = msg.get("chat", {})
    if chat.get("id") is not None:
        chat_doc = {
            "chat_id": chat.get("id"),
            "type": chat.get("type"),
            "title": chat.get("title"),
            "first_seen": datetime.now(timezone.utc)
        }
        await db.chats.update_one({"chat_id": chat.get("id")}, {"$setOnInsert": chat_doc}, upsert=True)
    if from_user.get("id") is not None:
        user_doc = {
            "user_id": from_user.get("id"),
            "username": from_user.get("username"),
            "first_seen": datetime.now(timezone.utc)
        }
        await db.users.update_one({"user_id": from_user.get("id")}, {"$setOnInsert": user_doc}, upsert=True)


# index a file message
async def index_file_message(msg: dict):
    chat = msg.get("chat", {})
    message_id = msg.get("message_id")
    from_user = msg.get("from", {})
    file_meta = {}

    if "document" in msg:
        doc = msg["document"]
        file_meta = {
            "kind": "document",
            "file_id": doc.get("file_id"),
            "file_name": doc.get("file_name"),
            "mime_type": doc.get("mime_type"),
            "file_size": doc.get("file_size")
        }
    elif "photo" in msg:
        photos = msg["photo"]
        largest = max(photos, key=lambda p: p.get("file_size", 0))
        file_meta = {"kind": "photo", "file_id": largest.get("file_id")}
    elif "video" in msg:
        vid = msg["video"]
        file_meta = {"kind": "video", "file_id": vid.get("file_id")}
    else:
        return None

    record = {
        "chat_id": chat.get("id"),
        "chat_type": chat.get("type"),
        "message_id": message_id,
        "from_user_id": from_user.get("id"),
        "from_username": from_user.get("username"),
        "caption": msg.get("caption"),
        "file_meta": file_meta,
        "db_forward": None,
        "created_at": datetime.now(timezone.utc)
    }
    res = await db.files.insert_one(record)
    return res.inserted_id


# --- webhook handler ---
@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    update = await request.json()
    # messages
    if "message" in update:
        msg = update["message"]
        await record_chat_and_user(msg)

        text = msg.get("text", "") or ""
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        from_user = msg.get("from", {})
        user_id = from_user.get("id")

        # FORCE SUB check (optional)
        if FORCE_SUB_CHANNEL_ID:
            try:
                sub_resp = await tg_get_chat_member(FORCE_SUB_CHANNEL_ID, user_id)
                ok = sub_resp.get("ok", False)
                status = sub_resp.get("result", {}).get("status")
                if not ok or status not in ("member", "creator", "administrator"):
                    if FORCE_SUB_OPTIONAL:
                        await tg_send_message(chat_id, "Please join the required channel to use bot features.")
                    else:
                        await tg_send_message(chat_id, "You must join the required channel to use this bot. Please subscribe and try again.")
                        return {"ok": True}
            except Exception:
                log.exception("Force-sub check failed (ignored)")

        # /start
        if text.startswith("/start"):
            await tg_send_message(chat_id, "Hello! I am Eldro Auto Filter Bot. Use /help to see commands.", reply_markup=buttons_for_start())
            return {"ok": True}

        # /help
        if text.startswith("/help"):
            help_text = (
                "/start - open menu\n"
                "/help - this message\n"
                "/stats - files/users/groups (owner only)\n"
                "/broadcast - owner only (flow)\n"
                "/deletefile - reply to DB-channel forwarded message (in private) to remove\n\n"
                "Send/forward files to a group or PM and bot will forward them to DB channel and index them. Group files are auto-deleted after the configured time."
            )
            await tg_send_message(chat_id, help_text)
            return {"ok": True}

        # /stats
        if text.startswith("/stats"):
            if user_id != OWNER_ID:
                await tg_send_message(chat_id, "Only owner can use /stats.")
                return {"ok": True}
            files_count = await db.files.count_documents({})
            users_count = await db.users.count_documents({})
            groups_count = await db.chats.count_documents({"type": {"$in": ["group", "supergroup"]}})
            await tg_send_message(chat_id, f"Files: {files_count}\nUsers: {users_count}\nGroups: {groups_count}")
            return {"ok": True}

        # /broadcast (start)
        if text.startswith("/broadcast"):
            if user_id != OWNER_ID:
                await tg_send_message(chat_id, "Only owner can use /broadcast.")
                return {"ok": True}
            await db.sessions.update_one({"user_id": user_id}, {"$set": {"broadcast_pending": True, "created_at": datetime.now(timezone.utc)}}, upsert=True)
            await tg_send_message(chat_id, "Send the message to broadcast now (text or forward).")
            return {"ok": True}

        # /deletefile
        if text.startswith("/deletefile"):
            # require reply_to_message to a forwarded DB channel message
            reply = msg.get("reply_to_message")
            if reply:
                original_forward_chat = reply.get("forward_from_chat") or reply.get("forward_from")
                # detect forwarded DB-channel message
                fwd_chat = None
                if reply.get("forward_from_chat") and reply["forward_from_chat"].get("id") == DB_CHANNEL_ID:
                    fwd_chat = DB_CHANNEL_ID
                elif reply.get("forward_from") and isinstance(reply.get("forward_from"), dict) and reply["forward_from"].get("id") == DB_CHANNEL_ID:
                    fwd_chat = DB_CHANNEL_ID

                if fwd_chat:
                    forwarded_msg_id = reply.get("message_id")
                    # find file doc with that forwarded id
                    doc = await db.files.find_one({"db_forward.chat_id": DB_CHANNEL_ID, "db_forward.message_id": forwarded_msg_id})
                    if not doc:
                        await tg_send_message(chat_id, "File record not found for that forwarded message.")
                        return {"ok": True}
                    # attempt delete original & db forwarded
                    orig_chat_id = doc.get("chat_id")
                    orig_msg_id = doc.get("message_id")
                    del1 = await tg_delete(orig_chat_id, orig_msg_id)
                    del2 = await tg_delete(DB_CHANNEL_ID, forwarded_msg_id)
                    # mark in DB
                    await db.files.update_many({"db_forward.message_id": forwarded_msg_id}, {"$set": {"deleted_from_db": True, "deleted_at": datetime.now(timezone.utc)}})
                    await tg_send_message(chat_id, f"Attempted deletion. original: {del1}, db_copy: {del2}")
                    return {"ok": True}
            await tg_send_message(chat_id, "Reply to the forwarded DB-channel message (in private) with /deletefile to delete it.")
            return {"ok": True}

        # If owner had a broadcast pending, consume it and broadcast
        session = await db.sessions.find_one({"user_id": user_id})
        if session and session.get("broadcast_pending"):
            await db.sessions.delete_one({"user_id": user_id})
            # gather target chats (we'll use chats collection)
            cur = db.chats.find({})
            targets = []
            async for c in cur:
                targets.append(c["chat_id"])
            sent = 0
            for t in targets:
                try:
                    # forward or send text
                    if msg.get("text"):
                        await tg_send_message(t, msg["text"])
                        sent += 1
                    else:
                        # if forwarded message, for simplicity send caption or text
                        await tg_send_message(t, msg.get("caption") or "Broadcast message")
                        sent += 1
                except Exception:
                    log.exception("broadcast failed for %s", t)
            await tg_send_message(chat_id, f"Broadcast sent to {sent} chats.")
            return {"ok": True}

        # If message contains a file (document/photo/video) => index and forward to DB channel
        if any(k in msg for k in ("document", "photo", "video")):
            inserted_id = await index_file_message(msg)
            # forward to DB channel
            try:
                fwd_resp = await tg_forward(DB_CHANNEL_ID, msg["chat"]["id"], msg["message_id"])
                if fwd_resp.get("ok"):
                    await db.files.update_one({"_id": ObjectId(inserted_id)}, {"$set": {"db_forward": {"chat_id": DB_CHANNEL_ID, "message_id": fwd_resp["result"]["message_id"]}}})
                else:
                    log.warning("forward failed: %s", fwd_resp)
            except Exception:
                log.exception("forward exception")
            # schedule deletion of original if group
            chat_type = msg["chat"].get("type")
            if chat_type in ("group", "supergroup"):
                background_tasks.add_task(schedule_delete_original, msg["chat"]["id"], msg["message_id"], AUTO_DELETE_SECONDS)
            await tg_send_message(chat_id, "File indexed and forwarded to DB channel.")
            return {"ok": True}

    # callback queries
    if "callback_query" in update:
        cb = update["callback_query"]
        data = cb.get("data")
        from_id = cb["from"]["id"]
        chat_id = cb["message"]["chat"]["id"]
        if data == "help":
            await tg_send_message(chat_id, "Use /help for commands.")
        elif data == "stats":
            if from_id != OWNER_ID:
                await tg_send_message(chat_id, "Only owner can view stats.")
            else:
                files_count = await db.files.count_documents({})
                users_count = await db.users.count_documents({})
                chats_count = await db.chats.count_documents({"type": {"$in": ["group", "supergroup"]}})
                await tg_send_message(chat_id, f"Files: {files_count}\nUsers: {users_count}\nGroups: {chats_count}")
        elif data == "broadcast":
            if from_id != OWNER_ID:
                await tg_send_message(chat_id, "Only owner can broadcast.")
            else:
                await db.sessions.update_one({"user_id": from_id}, {"$set": {"broadcast_pending": True, "created_at": datetime.now(timezone.utc)}}, upsert=True)
                await tg_send_message(chat_id, "Send the broadcast message now (text or forward).")
        return {"ok": True}

    return {"ok": True}


@app.get("/set_webhook")
async def set_webhook():
    if not EXPOSED_URL:
        raise HTTPException(status_code=400, detail="Set EXPOSED_URL env var first.")
    webhook_url = f"{EXPOSED_URL}/webhook"
    resp = await http_client.get(f"{TELEGRAM_API}/setWebhook", params={"url": webhook_url, "allowed_updates": '["message","callback_query"]'})
    return resp.json()
