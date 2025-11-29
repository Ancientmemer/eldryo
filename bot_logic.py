# bot_logic.py
# Manglish: main bot handlers and helpers

import os
import asyncio
import httpx
from typing import Dict, Any
from db import DB

ADMIN_ID = int(os.getenv("ADMIN_ID")) if os.getenv("ADMIN_ID") else None

# helper low-level telegram calls
async def tg_post(api_base: str, method: str, data: Dict[str, Any] = None, files=None):
    url = f"{api_base}/{method}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        if files:
            return await client.post(url, data=data, files=files)
        return await client.post(url, data=data)

async def send_message(api_base: str, chat_id: int, text: str, reply_markup=None):
    data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = reply_markup
    await tg_post(api_base, "sendMessage", data=data)

async def delete_message(api_base: str, chat_id: int, message_id: int):
    await tg_post(api_base, "deleteMessage", data={"chat_id": chat_id, "message_id": message_id})

async def forward_message(api_base: str, from_chat_id: int, to_chat_id: int, message_id: int):
    await tg_post(api_base, "forwardMessage", data={
        "chat_id": to_chat_id,
        "from_chat_id": from_chat_id,
        "message_id": message_id
    })

# use in admin broadcast route
async def set_webhook_telegram(text: str, db: DB, api_base: str):
    # iterate users collection and send message
    cur = db.users.find({}, {"user_id":1})
    count = 0
    async for u in cur:
        uid = u.get("user_id")
        try:
            await send_message(api_base, uid, text)
            count += 1
            await asyncio.sleep(0.05)  # small rate limit
        except Exception:
            continue
    return count

async def handle_update(update: dict, db: DB, api_base: str):
    # Parse incoming update and route to handlers
    try:
        # message or edited_message
        msg = update.get("message") or update.get("edited_message") or {}
        if not msg:
            return
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        from_user = msg.get("from", {})
        user_id = from_user.get("id")

        # store user info
        await db.add_user(user_id, {"username": from_user.get("username"), "name": from_user.get("first_name")})
        if chat.get("type") in ("group", "supergroup"):
            await db.add_group(chat_id, {"title": chat.get("title")})

        text = msg.get("text") or msg.get("caption") or ""
        # commands
        if text and text.startswith("/"):
            parts = text.split(" ", 1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""

            if cmd == "/start":
                await cmd_start(api_base, chat_id, from_user)
                return
            if cmd == "/help":
                await cmd_help(api_base, chat_id)
                return
            if cmd == "/stats":
                await cmd_stats(api_base, chat_id, db)
                return
            if cmd == "/broadcast":
                # admin only
                if str(user_id) != str(ADMIN_ID):
                    await send_message(api_base, chat_id, "Only admin can use /broadcast")
                    return
                if not arg:
                    await send_message(api_base, chat_id, "Usage: /broadcast Your message here")
                    return
                # background broadcast
                asyncio.create_task(set_webhook_telegram(arg, db, api_base))
                await send_message(api_base, chat_id, "Broadcast started.")
                return
            if cmd == "/set_upload_channel":
                if str(user_id) != str(ADMIN_ID):
                    await send_message(api_base, chat_id, "Only admin can set upload channel")
                    return
                # arg should be chat id (admin can forward a message from target and use its chat id)
                try:
                    target = int(arg.strip())
                    await db.set_upload_channel(target)
                    await send_message(api_base, chat_id, f"Upload channel set to {target}")
                except Exception:
                    await send_message(api_base, chat_id, "Provide numeric chat id: /set_upload_channel <chat_id>")
                return
            if cmd == "/banned_add":
                if str(user_id) != str(ADMIN_ID):
                    await send_message(api_base, chat_id, "Admin only")
                    return
                word = arg.strip()
                if not word:
                    await send_message(api_base, chat_id, "Usage: /banned_add word")
                    return
                await db.add_banned_word(word)
                await send_message(api_base, chat_id, f"Added banned word: {word}")
                return
            if cmd == "/banned_list":
                bl = await db.list_banned()
                await send_message(api_base, chat_id, "Banned words:\n" + "\n".join(bl or ["<empty>"]))
                return
            if cmd == "/banned_remove":
                if str(user_id) != str(ADMIN_ID):
                    await send_message(api_base, chat_id, "Admin only")
                    return
                word = arg.strip()
                await db.remove_banned_word(word)
                await send_message(api_base, chat_id, f"Removed banned word: {word}")
                return

        # non-command text: auto-filter
        if text:
            is_bad = await db.is_banned(text)
            if is_bad:
                # attempt to delete message
                msg_id = msg.get("message_id")
                try:
                    await delete_message(api_base, chat_id, msg_id)
                except Exception:
                    pass
                await send_message(api_base, chat_id, f"Message removed for policy violation.")
                return

        # file handling: document / photo / audio / video
        if msg.get("document") or msg.get("photo") or msg.get("video") or msg.get("audio"):
            file_obj = msg.get("document") or (msg.get("photo")[-1] if msg.get("photo") else None) or msg.get("video") or msg.get("audio")
            if file_obj:
                file_id = file_obj.get("file_id")
                file_unique_id = file_obj.get("file_unique_id")
                file_name = file_obj.get("file_name") or ""
                mime = file_obj.get("mime_type") or ""
                size = file_obj.get("file_size") or 0
                # save metadata
                await db.save_file({
                    "file_id": file_id,
                    "file_unique_id": file_unique_id,
                    "file_name": file_name,
                    "mime": mime,
                    "size": size,
                    "from_id": user_id,
                    "chat_id": chat_id,
                    "tg_message_id": msg.get("message_id")
                })
                # forward to upload channel if set
                ch = await db.get_upload_channel()
                if ch:
                    try:
                        await forward_message(api_base, chat_id, ch, msg.get("message_id"))
                    except Exception:
                        pass
                await send_message(api_base, chat_id, "File saved. Thanks!")

    except Exception as e:
        # log error
        print("handle_update error:", repr(e))
        # don't raise
