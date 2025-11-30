# main.py (complete patched version)
import os
import logging
import asyncio
import re
import urllib.parse
from typing import Optional, Any, Dict, List

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

# Legacy single DB channel (kept for compatibility)
DB_CHANNEL_ID = os.getenv("DB_CHANNEL_ID", "0")

# New: support multiple DB channels (space-separated), like VJ-FILTER-BOT's CHANNELS variable
# Example: CHANNELS="-100123... -100456..." or channel usernames
CHANNELS = os.getenv("CHANNELS", "").strip()
if CHANNELS:
    CHANNEL_LIST = [c.strip() for c in CHANNELS.split() if c.strip()]
else:
    CHANNEL_LIST = [DB_CHANNEL_ID] if DB_CHANNEL_ID and DB_CHANNEL_ID != "0" else []

OWNER_ID = int(os.getenv("OWNER_ID", "0"))
AUTO_DELETE_SECONDS = int(os.getenv("AUTO_DELETE_SECONDS", "300"))
FORCE_SUB_CHANNEL_ID = os.getenv("FORCE_SUB_CHANNEL_ID", "")
FORCE_SUB_OPTIONAL = os.getenv("FORCE_SUB_OPTIONAL", "false").lower() == "true"

# Basic premium stub (unused by default)
ENABLE_PREMIUM = os.getenv("ENABLE_PREMIUM", "false").lower() == "true"
PREMIUM_TOKENS = [t.strip() for t in os.getenv("PREMIUM_TOKENS", "").split(",") if t.strip()]

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

# Pagination constant
RESULTS_PER_PAGE = 8

# --- TELEGRAM helpers (with checks) ---
async def tg_request(path: str, method: str = "post", params: dict = None, data: dict = None) -> Dict[str, Any]:
    url = f"{TELEGRAM_API}/{path}"
    try:
        if method.lower() == "get":
            r = await http_client.get(url, params=params)
        else:
            # FIX: send JSON so reply_markup dicts are serialized correctly
            r = await http_client.post(url, json=data)
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
    return await tg_request("sendMessage", data=data)


async def tg_forward(chat_id: int, from_chat_id: int, message_id: int):
    data = {"chat_id": chat_id, "from_chat_id": from_chat_id, "message_id": message_id}
    return await tg_request("forwardMessage", data=data)


async def tg_delete(chat_id: int, message_id: int):
    data = {"chat_id": chat_id, "message_id": message_id}
    return await tg_request("deleteMessage", data=data)


async def tg_get_chat_member(chat_id: str | int, user_id: int):
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


# helper: find files in DB by filename (partial, case-insensitive)
async def search_files_by_name(query: str, limit: int = 100) -> List[dict]:
    cur = db.files.find({"file_meta.file_name": {"$regex": query, "$options": "i"}}).sort("created_at", -1).limit(limit)
    results = []
    async for doc in cur:
        fm = doc.get("file_meta", {})
        name = fm.get("file_name") or fm.get("file_id") or "(unknown)"
        results.append({"_id": str(doc.get("_id")), "name": name, "db_forward": doc.get("db_forward")})
    return results


# Pagination helpers
def make_page_keyboard(results: list, query: str, page: int):
    from math import ceil
    encoded_q = urllib.parse.quote(query, safe='')
    total = len(results)
    pages = max(1, ceil(total / RESULTS_PER_PAGE))
    page = max(1, min(page, pages))

    start = (page - 1) * RESULTS_PER_PAGE
    end = start + RESULTS_PER_PAGE
    slice_results = results[start:end]

    keyboard_rows = []
    # Send All button
    keyboard_rows.append([{"text": "Send All", "callback_data": f"sendall:{encoded_q}"}])

    # result buttons (8 per page)
    for r in slice_results:
        name = r["name"]
        dbf = r.get("db_forward")
        if dbf and dbf.get("message_id") and dbf.get("chat_id"):
            cb = f"filefetch:{dbf['chat_id']}:{dbf['message_id']}"
            # keep the button label reasonable length; Telegram truncates long labels
            keyboard_rows.append([{"text": (name[:70] + '...') if len(name) > 70 else name, "callback_data": cb}])
        else:
            keyboard_rows.append([{"text": f"{(name[:60] + '...') if len(name) > 60 else name} (no DB copy)", "callback_data": "noop"}])

    # navigation row: PREV | PAGE X/Y | NEXT
    nav_row = []
    if page > 1:
        nav_row.append({"text": "⏮ PREV", "callback_data": f"filepage:{encoded_q}:{page-1}"})
    nav_row.append({"text": f"PAGE {page}/{pages}", "callback_data": "noop"})
    if page < pages:
        nav_row.append({"text": "NEXT ⏭", "callback_data": f"filepage:{encoded_q}:{page+1}"})
    keyboard_rows.append(nav_row)

    return {"inline_keyboard": keyboard_rows}


# Heuristic to detect a search query (no /find needed)
def is_search_query(s: str) -> bool:
    s = s.strip()
    if not s:
        return False
    if s.startswith("/"):
        return False
    if s.startswith("http://") or s.startswith("https://") or "t.me/" in s:
        return False
    if len(s) < 3:
        return False
    if s.isdigit():
        return False
    # Count letters/digits/kerala unicode range to avoid emoji-only
    if len(re.findall(r"[A-Za-z0-9\u0D00-\u0D7F]", s)) < 2:
        return False
    # If it looks like filename with extension: accept
    if re.search(r"\.\w{2,5}(\s|$)", s):
        return True
    # multi-word title likely
    if len(s.split()) >= 2:
        return True
    # single word long enough (e.g., Inception)
    if len(s) >= 5:
        return True
    return False


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

        # --- implicit search (no /find) with group confirmation ---
        if is_search_query(text):
            q = text.strip()
            chat_type = chat.get("type", "")
            # Group -> ask for confirmation first
            if chat_type in ("group", "supergroup"):
                encoded_q = urllib.parse.quote(q, safe='')
                requester = from_user.get("id")
                keyboard = {"inline_keyboard": [
                    [
                        {"text": "✅ Yes", "callback_data": f"confirmsearch:yes:{requester}:{encoded_q}"},
                        {"text": "❌ No",  "callback_data": f"confirmsearch:no:{requester}:{encoded_q}"}
                    ]
                ]}
                try:
                    await tg_send_message(chat_id, f"Are you searching for \"{q}\"? (tap Yes to show results)", reply_markup=keyboard)
                except Exception:
                    log.exception("implicit-search: failed to send confirmation prompt")
                return {"ok": True}
            else:
                # Private chat -> search immediately and show paged results
                results = await search_files_by_name(q, limit=80)
                if not results:
                    await tg_send_message(chat_id, "No files found with that name.")
                    return {"ok": True}
                page = 1
                keyboard = make_page_keyboard(results, q, page)
                await tg_send_message(chat_id, f"The Results For 👉 {q}\nRequested By 👉 {from_user.get('first_name','')}\n\nTap a button to get the DB copy:", reply_markup=keyboard)
                return {"ok": True}

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
                "/clone <db_message_id> - clone a DB copy into this chat\n"
                "/find <filename> - search saved files (also works by typing name directly)\n"
                "/broadcast - owner only\n"
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

        # /clone <message_id>  - clone DB copy into this chat by DB message id
        if text.startswith("/clone"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                await tg_send_message(chat_id, "Usage: /clone <db_message_id>\nExample: /clone 12345")
                return {"ok": True}
            msgid_str = parts[1].strip()
            try:
                db_msg_id = int(msgid_str)
            except Exception:
                await tg_send_message(chat_id, "Invalid message id. Must be numeric Telegram message_id.")
                return {"ok": True}
            forwarded = False
            for ch in CHANNEL_LIST:
                try:
                    fwd = await tg_forward(chat_id, ch, db_msg_id)
                    if fwd.get("ok"):
                        forwarded = True
                        break
                except Exception:
                    log.exception("clone forward failed for channel %s", ch)
            if forwarded:
                await tg_send_message(chat_id, "Cloned file from DB channel.")
            else:
                await tg_send_message(chat_id, "Failed to clone: message not found or bot lacks permission to forward.")
            return {"ok": True}

        # /find <filename>  — explicit search command (paged)
        if text.startswith("/find "):
            q = text[len("/find "):].strip()
            if not q:
                await tg_send_message(chat_id, "Usage: /find <filename-or-part>")
                return {"ok": True}
            results = await search_files_by_name(q, limit=80)
            if not results:
                await tg_send_message(chat_id, "No files found with that name.")
                return {"ok": True}
            page = 1
            keyboard = make_page_keyboard(results, q, page)
            await tg_send_message(chat_id, f"The Results For 👉 {q}\nRequested By 👉 {from_user.get('first_name','')}\n\nTap a button to get the DB copy:", reply_markup=keyboard)
            return {"ok": True}

        # /deletefile (existing)
        if text.startswith("/deletefile"):
            reply = msg.get("reply_to_message")
            if reply:
                fwd_chat = None
                # handle DB_CHANNEL_ID or first channel in list
                try:
                    primary_db_ch = CHANNEL_LIST[0] if CHANNEL_LIST else DB_CHANNEL_ID
                except Exception:
                    primary_db_ch = DB_CHANNEL_ID
                if reply.get("forward_from_chat") and str(reply["forward_from_chat"].get("id")) == str(primary_db_ch):
                    fwd_chat = primary_db_ch
                elif reply.get("forward_from") and isinstance(reply.get("forward_from"), dict) and str(reply["forward_from"].get("id")) == str(primary_db_ch):
                    fwd_chat = primary_db_ch

                if fwd_chat:
                    forwarded_msg_id = reply.get("message_id")
                    doc = await db.files.find_one({"db_forward.chat_id": fwd_chat, "db_forward.message_id": forwarded_msg_id})
                    if not doc:
                        await tg_send_message(chat_id, "File record not found for that forwarded message.")
                        return {"ok": True}
                    orig_chat_id = doc.get("chat_id")
                    orig_msg_id = doc.get("message_id")
                    del1 = await tg_delete(orig_chat_id, orig_msg_id)
                    del2 = await tg_delete(fwd_chat, forwarded_msg_id)
                    await db.files.update_many({"db_forward.message_id": forwarded_msg_id}, {"$set": {"deleted_from_db": True, "deleted_at": datetime.now(timezone.utc)}})
                    await tg_send_message(chat_id, f"Attempted deletion. original: {del1}, db_copy: {del2}")
                    return {"ok": True}
            await tg_send_message(chat_id, "Reply to the forwarded DB-channel message (in private) with /deletefile to delete it.")
            return {"ok": True}

        # If owner had a broadcast pending, consume it and broadcast
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
                    if msg.get("text"):
                        await tg_send_message(t, msg["text"])
                        sent += 1
                    else:
                        await tg_send_message(t, msg.get("caption") or "Broadcast message")
                        sent += 1
                except Exception:
                    log.exception("broadcast failed for %s", t)
            await tg_send_message(chat_id, f"Broadcast sent to {sent} chats.")
            return {"ok": True}

        # If message contains a file (document/photo/video) => index and forward to DB channel(s)
        if any(k in msg for k in ("document", "photo", "video")):
            inserted_id = await index_file_message(msg)
            fwd_resp = None
            for ch in CHANNEL_LIST:
                try:
                    fwd_resp = await tg_forward(ch, msg["chat"]["id"], msg["message_id"])
                    if fwd_resp.get("ok"):
                        # Save the db_forward with actual channel id (string or numeric)
                        await db.files.update_one({"_id": inserted_id}, {"$set": {"db_forward": {"chat_id": ch, "message_id": fwd_resp["result"]["message_id"]}}})
                        break
                except Exception:
                    log.exception("forward exception to channel %s", ch)
            if not fwd_resp or not fwd_resp.get("ok"):
                log.warning("forward to DB channels failed: %s", fwd_resp)
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

        # simple callbacks
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

        # confirmsearch callbacks: "confirmsearch:yes:<requester_id>:<q_enc>"
        elif data and data.startswith("confirmsearch:"):
            try:
                parts = data.split(":", 3)
                action = parts[1]
                requester_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
                q_enc = parts[3] if len(parts) > 3 else ""
                q = urllib.parse.unquote(q_enc)
                dest_chat = cb["message"]["chat"]["id"]
                # Restrict confirmation to the original requester (safe)
                if requester_id and cb["from"].get("id") != requester_id:
                    await tg_send_message(dest_chat, "Only the user who asked can confirm the search.")
                    return {"ok": True}
                if action == "no":
                    await tg_send_message(dest_chat, "Search cancelled.")
                    return {"ok": True}
                # action == "yes" -> perform the search and show paged results
                results = await search_files_by_name(q, limit=80)
                if not results:
                    await tg_send_message(dest_chat, f"No files found for \"{q}\".")
                    return {"ok": True}
                keyboard = make_page_keyboard(results, q, 1)
                await tg_send_message(dest_chat, f"The Results For 👉 {q}\nRequested By 👉 {cb['from'].get('first_name','')}\n\nTap a button to get the DB copy:", reply_markup=keyboard)
            except Exception:
                log.exception("confirmsearch handling failed")
                await tg_send_message(cb["message"]["chat"]["id"], "Error while handling confirmation.")
            return {"ok": True}

        # filefetch callback: data format "filefetch:<db_chat>:<db_message_id>"
        elif data and data.startswith("filefetch:"):
            try:
                _, db_chat_str, db_msg_str = data.split(":", 2)
                db_msg_id = int(db_msg_str)
                dest_chat = cb["message"]["chat"]["id"]
                fwd = await tg_forward(dest_chat, db_chat_str, db_msg_id)
                if fwd.get("ok"):
                    await tg_send_message(dest_chat, "File forwarded from DB channel.")
                else:
                    await tg_send_message(dest_chat, f"Failed to forward file: {fwd.get('description') or fwd}")
            except Exception:
                log.exception("filefetch handling failed")
                await tg_send_message(cb["message"]["chat"]["id"], "Error while fetching file.")
            return {"ok": True}

        # sendall callback: data format "sendall:<q_enc>"
        elif data and data.startswith("sendall:"):
            try:
                _, q_enc = data.split(":", 1)
                q = urllib.parse.unquote(q_enc)
                dest_chat = cb["message"]["chat"]["id"]
                await tg_send_message(dest_chat, f"Sending top results for: {q} (this may take a few seconds)...")
                results = await search_files_by_name(q, limit=8)
                sent = 0
                for r in results:
                    dbf = r.get("db_forward")
                    if dbf and dbf.get("message_id") and dbf.get("chat_id"):
                        try:
                            fwd = await tg_forward(dest_chat, dbf["chat_id"], int(dbf["message_id"]))
                            if fwd.get("ok"):
                                sent += 1
                        except Exception:
                            log.exception("sendall forward failed for %s", dbf)
                await tg_send_message(dest_chat, f"Send All completed. Sent {sent}/{len(results)} files.")
            except Exception:
                log.exception("sendall handling failed")
                await tg_send_message(cb["message"]["chat"]["id"], "Error while sending all files.")
            return {"ok": True}

        # page navigation callback: "filepage:<q_enc>:<page>"
        elif data and data.startswith("filepage:"):
            try:
                parts = data.split(":", 2)
                q_enc = parts[1]
                page = int(parts[2]) if len(parts) > 2 else 1
                q = urllib.parse.unquote(q_enc)
                results = await search_files_by_name(q, limit=80)
                if not results:
                    await tg_send_message(chat_id, f"No files found for \"{q}\".")
                    return {"ok": True}
                keyboard = make_page_keyboard(results, q, page)
                # For simplicity we send a new message for the requested page
                await tg_send_message(chat_id, f"The Results For 👉 {q}\nRequested By 👉 {cb['from'].get('first_name','')}\n\nPage {page}:", reply_markup=keyboard)
            except Exception:
                log.exception("filepage handling failed")
                await tg_send_message(chat_id, "Error while changing page.")
            return {"ok": True}

        else:
            # noop or other callback; do nothing
            return {"ok": True}

    return {"ok": True}


@app.get("/set_webhook")
async def set_webhook():
    if not EXPOSED_URL:
        raise HTTPException(status_code=400, detail="Set EXPOSED_URL env var first.")
    webhook_url = f"{EXPOSED_URL}/webhook"
    resp = await http_client.get(f"{TELEGRAM_API}/setWebhook", params={"url": webhook_url, "allowed_updates": '["message","callback_query"]'})
    return resp.json()


# Graceful shutdown: close http client
@app.on_event("shutdown")
async def shutdown_event():
    try:
        await http_client.aclose()
    except Exception:
        log.exception("Error closing http client")
