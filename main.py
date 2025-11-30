import os
import logging
import asyncio
from typing import Optional

from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
import httpx
from motor.motor_asyncio import AsyncIOMotorClient
from bson.objectid import ObjectId
from datetime import datetime, timezone, timedelta

# config from env
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
EXPOSED_URL = os.getenv("EXPOSED_URL", "")
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")  # MUST be provided
DB_CHANNEL_ID = int(os.getenv("DB_CHANNEL_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
AUTO_DELETE_SECONDS = int(os.getenv("AUTO_DELETE_SECONDS", "300"))

# Force-sub settings (new / ensured present)
# Provide channel id (e.g. -1001234567890) or username (@channelname)
FORCE_SUB_CHANNEL_ID = os.getenv("FORCE_SUB_CHANNEL_ID")  # optional: can be channel id or @username
FORCE_SUB_OPTIONAL = os.getenv("FORCE_SUB_OPTIONAL", "false").lower() == "true"

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN env var")
if not MONGO_URI:
    raise RuntimeError("Set MONGO_URI env var")
if not DB_NAME:
    raise RuntimeError("Set DB_NAME env var")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("eldro-bot")

app = FastAPI()
client = AsyncIOMotorClient(MONGO_URI)
db = client[DB_NAME]

http_client = httpx.AsyncClient(timeout=30.0)


async def tg_send_message(chat_id: int, text: str, reply_markup: dict = None, parse_mode="HTML"):
    data = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        data["reply_markup"] = reply_markup
    resp = await http_client.post(f"{TELEGRAM_API}/sendMessage", data=data)
    return resp.json()


async def tg_forward(chat_id: int, from_chat_id: int, message_id: int):
    data = {"chat_id": chat_id, "from_chat_id": from_chat_id, "message_id": message_id}
    resp = await http_client.post(f"{TELEGRAM_API}/forwardMessage", data=data)
    return resp.json()


async def tg_delete(chat_id: int, message_id: int):
    data = {"chat_id": chat_id, "message_id": message_id}
    resp = await http_client.post(f"{TELEGRAM_API}/deleteMessage", data=data)
    return resp.json()


async def tg_get_chat_member(chat_id: str | int, user_id: int):
    # chat_id can be -100... or @channelusername
    resp = await http_client.get(f"{TELEGRAM_API}/getChatMember", params={"chat_id": chat_id, "user_id": user_id})
    return resp.json()


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
        await tg_delete(chat_id, message_id)
        log.info(f"Deleted message {message_id} from {chat_id}")
    except Exception as e:
        log.exception("Error deleting message")


@app.on_event("startup")
async def startup_event():
    # ensure indexes
    await db.files.create_index("file_id")
    await db.chats.create_index("chat_id", unique=True)
    await db.users.create_index("user_id", unique=True)
    log.info("App startup complete")


# helper to record chat and user
async def record_chat_and_user(msg):
    from_user = msg.get("from", {})
    chat = msg.get("chat", {})
    # store chat
    chat_doc = {"chat_id": chat.get("id"), "type": chat.get("type"), "title": chat.get("title"), "first_seen": datetime.now(timezone.utc)}
    if chat.get("id"):
        await db.chats.update_one({"chat_id": chat.get("id")}, {"$setOnInsert": chat_doc}, upsert=True)
    # store user
    if from_user.get("id"):
        user_doc = {"user_id": from_user.get("id"), "username": from_user.get("username"), "first_seen": datetime.now(timezone.utc)}
        await db.users.update_one({"user_id": from_user.get("id")}, {"$setOnInsert": user_doc}, upsert=True)


async def index_file_message(msg):
    # msg: Telegram message object that contains file
    chat = msg.get("chat", {})
    message_id = msg.get("message_id")
    from_user = msg.get("from", {})
    file_meta = {}
    # prefer document > photo > video
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
        # take largest
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
        "created_at": datetime.now(timezone.utc)
    }
    res = await db.files.insert_one(record)
    return res.inserted_id


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    update = await request.json()
    # only handle messages + callbacks for simplicity
    if "message" in update:
        msg = update["message"]
        await record_chat_and_user(msg)

        # COMMANDS handled in message.text
        text = msg.get("text", "") or ""
        chat_id = msg["chat"]["id"]
        from_user = msg.get("from", {})
        user_id = from_user.get("id")

        # ---------- FORCE-SUB CHECK (ensured present) ----------
        if FORCE_SUB_CHANNEL_ID:
            try:
                sub_resp = await tg_get_chat_member(FORCE_SUB_CHANNEL_ID, user_id)
                ok = sub_resp.get("ok", False)
                status = sub_resp.get("result", {}).get("status")
                # treat as "not a member" if API call didn't return ok or status is left/kicked/none
                is_member = ok and status in ("member", "creator", "administrator")
                if not is_member:
                    # If optional, just warn and continue
                    if FORCE_SUB_OPTIONAL:
                        await tg_send_message(chat_id, "⚠️ Please consider joining the required channel to get full bot access.")
                    else:
                        await tg_send_message(chat_id, "You must join the required channel to use this bot. Please join and try again.")
                        return {"ok": True}
            except Exception:
                # If the getChatMember fails (API or network), we log and optionally allow depending on FORCE_SUB_OPTIONAL
                log.exception("Force-sub check failed (getChatMember)")
                if not FORCE_SUB_OPTIONAL:
                    # be conservative and block
                    await tg_send_message(chat_id, "Can't verify subscription right now. Please try again later.")
                    return {"ok": True}
        # -------------------------------------------------------

        # start
        if text.startswith("/start"):
            await tg_send_message(chat_id, "Hello! I am Eldryo Auto Filter Bot. Use /help to see commands. \n\nPowered by: @jb_links", reply_markup=buttons_for_start())
            return {"ok": True}

        # help
        if text.startswith("/help"):
            help_text = (
                "/start - open menu\n"
                "/help - this message\n"
                "/stats - show total files/users/groups (owner or admin)\n"
                "/broadcast - owner only (send broadcast to all indexed chats)\n"
                "Forward a file to any group - bot will index & forward to DB channel.\n"
                "To delete a file: forward the DB-channel copy to the bot in private and reply with /deletefile\n"
            )
            await tg_send_message(chat_id, help_text)
            return {"ok": True}

        # stats
        if text.startswith("/stats"):
            # allow only owner or chat admins for sensitive usage; here allow owner only
            if user_id != OWNER_ID:
                await tg_send_message(chat_id, "Only owner can use /stats.")
                return {"ok": True}
            files_count = await db.files.count_documents({})
            users_count = await db.users.count_documents({})
            chats_count = await db.chats.count_documents({"type": "group"}) + await db.chats.count_documents({"type": "supergroup"})
            stats_text = f"Files: {files_count}\nUsers: {users_count}\nGroups: {chats_count}"
            await tg_send_message(chat_id, stats_text)
            return {"ok": True}

        # broadcast (owner only) - start a broadcast flow
        if text.startswith("/broadcast"):
            if user_id != OWNER_ID:
                await tg_send_message(chat_id, "Only owner can use /broadcast.")
                return {"ok": True}
            # Mark in a "sessions" collection that owner is starting broadcast
            await db.sessions.update_one({"user_id": user_id}, {"$set": {"broadcast_pending": True, "created_at": datetime.now(timezone.utc)}}, upsert=True)
            await tg_send_message(chat_id, "Send the message to broadcast now (reply with text or forward message). It will be sent to all indexed chats.")
            return {"ok": True}

        # deletefile logic (same as before)
        if text.startswith("/deletefile"):
            if "reply_to_message" in msg:
                fwd = msg["reply_to_message"]
                original_forward_chat = fwd.get("forward_from_chat", {})
                if original_forward_chat and original_forward_chat.get("id") == DB_CHANNEL_ID:
                    forwarded_msg_id = fwd.get("message_id")
                    doc = await db.files.find_one({"db_forward.chat_id": DB_CHANNEL_ID, "db_forward.message_id": forwarded_msg_id})
                    if not doc:
                        await tg_send_message(chat_id, "File record not found in DB for this forwarded message.")
                        return {"ok": True}
                    orig_chat_id = doc.get("chat_id")
                    orig_msg_id = doc.get("message_id")
                    d1 = await tg_delete(orig_chat_id, orig_msg_id)
                    d2 = await tg_delete(DB_CHANNEL_ID, forwarded_msg_id)
                    await tg_send_message(chat_id, "Attempted deletion. Check DB channel & original chat. Note: bot must be admin to delete messages.")
                    return {"ok": True}
            await tg_send_message(chat_id, "Reply to the forwarded DB-channel message (the one bot forwarded) with /deletefile to delete.")
            return {"ok": True}

        # broadcast send (owner flow)
        session = await db.sessions.find_one({"user_id": user_id})
        if session and session.get("broadcast_pending"):
            await db.sessions.delete_one({"user_id": user_id})
            cur = db.chats.find({})
            targets = []
            async for c in cur:
                targets.append(c["chat_id"])
            sent = 0
            for t in targets:
                try:
                    if "forward_from_message_id" in msg and "forward_from" in msg:
                        if text:
                            await tg_send_message(t, text)
                    else:
                        if text:
                            await tg_send_message(t, text)
                    sent += 1
                except Exception:
                    log.exception("broadcast failed for %s", t)
            await tg_send_message(chat_id, f"Broadcast sent to {sent} chats.")
            return {"ok": True}

        # indexing + forwarding file messages
        if any(k in msg for k in ("document", "photo", "video")):
            inserted_id = await index_file_message(msg)
            try:
                fwd_resp = await tg_forward(DB_CHANNEL_ID, msg["chat"]["id"], msg["message_id"])
                if fwd_resp.get("ok"):
                    await db.files.update_one({"_id": ObjectId(inserted_id)}, {"$set": {"db_forward": {"chat_id": DB_CHANNEL_ID, "message_id": fwd_resp["result"]["message_id"]}}})
                else:
                    log.warning("forward failed: %s", fwd_resp)
            except Exception:
                log.exception("forward exception")

            chat_type = msg["chat"].get("type")
            if chat_type in ("group", "supergroup"):
                background_tasks.add_task(schedule_delete_original, msg["chat"]["id"], msg["message_id"], AUTO_DELETE_SECONDS)

            await tg_send_message(chat_id, "File indexed and forwarded to DB channel.")
            return {"ok": True}

    # Callback query handling
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
                chats_count = await db.chats.count_documents({"type": "group"}) + await db.chats.count_documents({"type": "supergroup"})
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
