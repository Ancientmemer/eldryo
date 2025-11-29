# main.py
# FastAPI webhook bot for Telegram: auto-filter + stats + broadcast + file DB
# Manglish comments where helpful

import os
import json
import httpx
import traceback
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from typing import Optional, List
from db import upsert_chat, add_file, stats, get_all_user_chat_ids, broadcasts_col
from db import db  # only if you want to access directly
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
EXPOSED_URL = os.getenv("EXPOSED_URL")
PORT = int(os.getenv("PORT", "8080"))
ADMIN_IDS = os.getenv("ADMIN_IDS", "")  # comma separated telegram numeric ids
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS.split(",") if x.strip()]

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Set TELEGRAM_BOT_TOKEN env var first")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
app = FastAPI()

# -----------------------
# Utility helpers
# -----------------------
async def telegram_request(method: str, payload: dict):
    url = f"{TELEGRAM_API}/{method}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=payload)
        try:
            return resp.json()
        except Exception:
            return {"ok": False, "status": resp.status_code, "text": resp.text}

async def send_message(chat_id: int, text: str, reply_markup: Optional[dict] = None, parse_mode: Optional[str] = None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return await telegram_request("sendMessage", payload)


def make_start_buttons():
    # Manglish: simple inline buttons for /help and contact
    kb = {
        "inline_keyboard": [
            [{"text": "Help / Commands", "callback_data": "help_cb"}],
            [{"text": "Add to Channel/Group", "callback_data": "add_group_cb"}]
        ]
    }
    return kb

# -----------------------
# Filtering - placeholder
# -----------------------
def apply_filters(message: dict) -> bool:
    """
    Manglish: put your filter rules here.
    If returns True -> message should be flagged/removed (bot can instruct admins).
    Right now it's a stub. Add rules like banned words, file type restrictions, spam heuristics.
    """
    text = (message.get("text") or "").lower()
    # example rule: block message containing 'badword'
    banned_words = ["badword1", "badword2"]
    for w in banned_words:
        if w in text:
            return True
    return False

# -----------------------
# Background tasks
# -----------------------
async def broadcast_to_all(text: str):
    # Manglish: broadcast to all private users saved in DB
    chat_ids = get_all_user_chat_ids()
    success = 0
    fail = 0
    for cid in chat_ids:
        try:
            await send_message(cid, text)
            success += 1
        except Exception:
            fail += 1
    # store broadcast history
    broadcasts_col.insert_one({"text": text, "success": success, "fail": fail})
    return {"success": success, "fail": fail}


# -----------------------
# Routes
# -----------------------
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/webhook")
async def webhook(req: Request, background_tasks: BackgroundTasks):
    update = await req.json()

    # 1) handle callback_query (buttons)
    if "callback_query" in update:
        cb = update["callback_query"]
        data = cb.get("data")
        from_user = cb.get("from", {})
        chat_id = cb.get("message", {}).get("chat", {}).get("id")
        if data == "help_cb":
            background_tasks.add_task(send_message, from_user["id"], HELP_TEXT)
        elif data == "add_group_cb":
            # Instructions for adding to groups
            txt = ("To add this bot to a channel or group: add the bot as admin and then send any message "
                   "from that group to register it in DB. Files posted in that group will be saved to DB.")
            background_tasks.add_task(send_message, from_user["id"], txt)
        return {"ok": True}

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return {"ok": True}

    chat = msg.get("chat", {})
    from_user = msg.get("from", {})
    chat_obj = {
        "id": chat.get("id"),
        "type": chat.get("type"),
        "title": chat.get("title"),
        "username": chat.get("username"),
        "first_name": from_user.get("first_name"),
        "last_name": from_user.get("last_name")
    }
    # upsert chat into DB
    try:
        upsert_chat(chat_obj)
    except Exception:
        traceback.print_exc()

    # if command
    text = msg.get("text", "") or ""
    if text.startswith("/"):
        parts = text.split(" ", 1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        # /start
        if cmd == "/start":
            welcome = ("Hello! I'm your Advanced Auto-Filter Bot.\n\n"
                       "Use /help to see commands.")
            background_tasks.add_task(send_message, chat["id"], welcome, make_start_buttons())
            return {"ok": True}

        # /help
        if cmd == "/help":
            background_tasks.add_task(send_message, chat["id"], HELP_TEXT)
            return {"ok": True}

        # /stats
        if cmd == "/stats":
            s = stats()
            stats_text = (
                f"📊 Stats:\n"
                f"- Total users (private): {s['total_users']}\n"
                f"- Total groups/channels: {s['total_groups']}\n"
                f"- Total files collected: {s['total_files']}\n"
            )
            background_tasks.add_task(send_message, chat["id"], stats_text)
            return {"ok": True}

        # /broadcast (admin only)
        if cmd == "/broadcast":
            if from_user.get("id") not in ADMIN_IDS:
                background_tasks.add_task(send_message, chat["id"], "❌ You are not allowed to broadcast.")
                return {"ok": True}
            if not args.strip():
                background_tasks.add_task(send_message, chat["id"], "Usage: /broadcast <message text>")
                return {"ok": True}
            background_tasks.add_task(broadcast_to_all, args.strip())
            background_tasks.add_task(send_message, chat["id"], "Broadcast started. Check history later.")
            return {"ok": True}

    # Non-command message: check filters and file handling
    try:
        # apply filters
        if apply_filters(msg):
            # simple behavior: notify user and admins (not deleting for now)
            note = "Your message matched the filter rules. Please check rules."
            background_tasks.add_task(send_message, chat["id"], note)
            # optionally notify admins
            for admin in ADMIN_IDS:
                background_tasks.add_task(send_message, admin, f"Filter matched in chat {chat.get('id')}: {text[:200]}")
            # you can also call deleteMessage via Telegram API if you want:
            # await telegram_request("deleteMessage", {"chat_id": chat["id"], "message_id": msg["message_id"]})
            return {"ok": True}
    except Exception:
        traceback.print_exc()

    # Files detection - save metadata
    try:
        file_meta = None
        if "document" in msg:
            d = msg["document"]
            file_meta = {
                "file_type": "document",
                "file_id": d.get("file_id"),
                "file_unique_id": d.get("file_unique_id"),
                "file_name": d.get("file_name"),
                "mime_type": d.get("mime_type")
            }
        elif "photo" in msg:
            # photo is a list, take largest
            ph = msg["photo"][-1]
            file_meta = {
                "file_type": "photo",
                "file_id": ph.get("file_id"),
                "file_unique_id": ph.get("file_unique_id"),
                "width": ph.get("width"),
                "height": ph.get("height")
            }
        elif "video" in msg:
            v = msg["video"]
            file_meta = {
                "file_type": "video",
                "file_id": v.get("file_id"),
                "file_unique_id": v.get("file_unique_id"),
                "duration": v.get("duration")
            }
        elif "voice" in msg:
            v = msg["voice"]
            file_meta = {
                "file_type": "voice",
                "file_id": v.get("file_id"),
                "file_unique_id": v.get("file_unique_id"),
                "duration": v.get("duration")
            }
        elif "audio" in msg:
            a = msg["audio"]
            file_meta = {
                "file_type": "audio",
                "file_id": a.get("file_id"),
                "file_unique_id": a.get("file_unique_id"),
                "duration": a.get("duration")
            }

        if file_meta:
            file_meta.update({
                "chat_id": chat.get("id"),
                "from_id": from_user.get("id"),
                "date": msg.get("date"),
                "caption": msg.get("caption")
            })
            add_file(file_meta)
            # optional: reply to user that file is saved
            background_tasks.add_task(send_message, chat["id"], "File saved to DB ✅")
    except Exception:
        traceback.print_exc()

    return {"ok": True}

# -----------------------
# Webhook setter
# -----------------------
@app.get("/set_webhook")
async def set_webhook():
    if not EXPOSED_URL:
        raise HTTPException(status_code=400, detail="Set EXPOSED_URL env var first")
    webhook_url = f"{EXPOSED_URL}/webhook"
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{TELEGRAM_API}/setWebhook", params={"url": webhook_url})
        return resp.json()

# -----------------------
# Constants
# -----------------------
HELP_TEXT = (
    "🤖 Advanced Auto-Filter Bot — Commands:\n\n"
    "/start - start and show buttons\n"
    "/help - this help text\n"
    "/stats - show total users/groups/files\n"
    "/broadcast <text> - admin only, send text to all saved private users\n\n"
    "How files work: Any file (photo/document/video/audio/voice) posted in a chat where the bot is present will be recorded into the DB. Use /stats to view totals.\n\n"
    "You can add filtering rules in the bot code (apply_filters function)."
            )
