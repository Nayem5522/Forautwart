#!/usr/bin/env python3
"""
AutoForward Bot - Complete runnable version
Features:
 - Flask healthcheck
 - MongoDB (motor) persistence
 - Bot commands: /start, /set_source, /set_destiny, /show_source, /show_destiny, /stats, /broadcast
 - Add session flow for userbots (phone -> code -> 2FA)
 - Add private source forwarding via userbot sessions
 - Public channel forwarding via bot (copy_message)
 - Background userbot listeners for private sources

Environment variables required:
 - API_ID, API_HASH, BOT_TOKEN, MONGO_DB_URL
 Optional:
 - OWNER_ID

Notes:
 - This file is a single-file example. In production split responsibilities and handle secrets safely.
 - For userbot sessions this uses Pyrogram Client string sessions.
"""

import os
import logging
import asyncio
import threading
from typing import Dict, Any, Optional, List

from flask import Flask
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message
from pyrogram.enums import ParseMode
from pyrogram.errors import (UserNotParticipant, ChatAdminRequired, PeerIdInvalid, RPCError,
                             FloodWait, BotBlocked, UserIsBot, SessionPasswordNeeded, PhoneCodeInvalid,
                             PasswordRequired, PhoneNumberInvalid)
from motor.motor_asyncio import AsyncIOMotorClient

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ---------- Env vars ----------
try:
    API_ID = int(os.environ["API_ID"])
    API_HASH = os.environ["API_HASH"]
    BOT_TOKEN = os.environ["BOT_TOKEN"]
except KeyError as e:
    logger.error(f"Missing environment variable: {e}. Exiting.")
    raise SystemExit(1)

MONGO_DB_URL = os.environ.get("MONGO_DB_URL")
if not MONGO_DB_URL:
    logger.error("MONGO_DB_URL environment variable is not set. Exiting.")
    raise SystemExit(1)

OWNER_ID = int(os.environ.get("OWNER_ID", "5926160191"))

# ---------- Flask healthcheck server ----------
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"Flask server starting on port {port}")
    flask_app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_flask, daemon=True).start()

# ---------- Database ----------
db_client = AsyncIOMotorClient(MONGO_DB_URL)
db = db_client.autoforward_db
users_collection = db.users

# ---------- Bot Client ----------
bot = Client("autoforward_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ---------- Concurrency Controls ----------
SEND_SEMAPHORE = asyncio.Semaphore(10)
COPY_SEMAPHORE = asyncio.Semaphore(5)

# ---------- In-memory states ----------
user_states: Dict[int, str] = {}          # user_id -> state string
user_temp_data: Dict[int, Dict[str, Any]] = {}  # temporary session data while adding session
active_userbots: Dict[int, Dict[str, Any]] = {}  # user_id -> {"client": Client, "task": asyncio.Task}

# ---------- Helper DB functions ----------
async def get_user_data(user_id: int) -> Dict[str, Any]:
    user_data = await users_collection.find_one({"_id": user_id})
    if not user_data:
        user_data = {"_id": user_id, "source_chat": None, "destination_chats": [], "private_sources": [], "session_string": None}
        await users_collection.insert_one(user_data)
    return user_data

async def update_user_data(user_id: int, field: str, value: Any):
    await users_collection.update_one({"_id": user_id}, {"$set": {field: value}}, upsert=True)

async def add_destination(user_id: int, chat_id: int):
    await users_collection.update_one({"_id": user_id}, {"$addToSet": {"destination_chats": chat_id}}, upsert=True)

async def remove_destination(user_id: int, chat_id: int):
    await users_collection.update_one({"_id": user_id}, {"$pull": {"destination_chats": chat_id}})

async def save_session_string(user_id: int, session_string: str):
    await users_collection.update_one({"_id": user_id}, {"$set": {"session_string": session_string}}, upsert=True)

async def get_session_string(user_id: int) -> Optional[str]:
    user_data = await users_collection.find_one({"_id": user_id})
    return user_data.get("session_string") if user_data else None

async def add_private_source_to_db(user_id: int, chat_id: int):
    await users_collection.update_one({"_id": user_id}, {"$addToSet": {"private_sources": chat_id}}, upsert=True)

async def remove_private_source_from_db(user_id: int, chat_id: int):
    await users_collection.update_one({"_id": user_id}, {"$pull": {"private_sources": chat_id}})

async def get_user_destinations(user_id: int) -> List[int]:
    user_data = await get_user_data(user_id)
    return user_data.get("destination_chats", [])

# ---------- Robust send/copy helpers ----------
async def send_with_retry(client: Client, chat_id: int, text: str, parse_mode=ParseMode.HTML, retries=3):
    async with SEND_SEMAPHORE:
        for attempt in range(retries):
            try:
                return await client.send_message(chat_id, text, parse_mode=parse_mode)
            except FloodWait as e:
                wait = getattr(e, 'value', 5)
                logger.warning(f"FloodWait: sleeping for {wait} seconds before retrying send to {chat_id}")
                await asyncio.sleep(wait + 1)
            except (BotBlocked, UserIsBot):
                logger.info(f"Cannot send message to {chat_id}: Bot blocked or user is a bot.")
                return None
            except Exception as e:
                logger.exception(f"Failed to send message to {chat_id} on attempt {attempt+1}: {e}")
                await asyncio.sleep(1)
        return None

async def copy_with_retry(client: Client, chat_id: int, from_chat_id: int, message_id: int, retries=3):
    async with COPY_SEMAPHORE:
        for attempt in range(retries):
            try:
                return await client.copy_message(chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id, disable_notification=True)
            except FloodWait as e:
                wait = getattr(e, 'value', 5)
                logger.warning(f"FloodWait: sleeping for {wait} seconds before retrying copy to {chat_id}")
                await asyncio.sleep(wait + 1)
            except (BotBlocked, UserIsBot):
                logger.info(f"Cannot copy message to {chat_id}: Bot blocked or user is a bot.")
                return None
            except Exception as e:
                logger.exception(f"Failed to copy message to {chat_id} on attempt {attempt+1}: {e}")
                await asyncio.sleep(1)
        return None

# ---------- Bot Commands ----------
@bot.on_message(filters.command("start") & filters.private)
async def start(client: Client, message: Message):
    buttons = [
        [InlineKeyboardButton("✪ Support", url="https://t.me/Prime_Support_group"), InlineKeyboardButton("Channel", url="https://t.me/PrimeCineZone")],
        [InlineKeyboardButton("Updates", url="https://t.me/PrimeXBots")],
        [InlineKeyboardButton("Help", callback_data="help_cmd"), InlineKeyboardButton("About", callback_data="about_cmd")]
    ]

    await message.reply_photo(
        photo="https://i.postimg.cc/fLkdDgs2/file-00000000346461fab560bc2d21951e7f.png",
        caption=(f"👋 Hello {message.from_user.mention},\n\n"
                 "This bot forwards new posts from a source channel to destinations you set.\n"
                 "Use /set_source and /set_destiny to configure."),
        reply_markup=InlineKeyboardMarkup(buttons)
    )

@bot.on_callback_query()
async def cb_handler(client: Client, query):
    data = query.data
    if data == "help_cmd":
        await query.message.edit_text(
            "Help:\n/set_source - forward a message from source channel\n/set_destiny - forward a message from destiny\n/add_session - add user session for private sources\n/add_private_source - forward a message from private channel",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Close", callback_data="close")]]),
            parse_mode=ParseMode.HTML
        )
    elif data == "about_cmd":
        me = await client.get_me()
        text = f"Bot: {me.first_name}\nDeveloper: @Prime_Nayem"
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Close", callback_data="close")]]))
    elif data == "close":
        try:
            await query.message.delete()
        except Exception:
            await query.answer("Cannot delete message", show_alert=True)
    elif data.startswith("show_dest_info_"):
        # handled elsewhere
        await query.answer()

# ---------- Set / Show commands ----------
@bot.on_message(filters.command("set_source") & filters.private)
async def set_source_command(client: Client, message: Message):
    user_states[message.from_user.id] = "waiting_for_source_forward"
    await message.reply_text("Please forward a message from your source channel here. The bot must be admin in that channel.")

@bot.on_message(filters.command("set_destiny") & filters.private)
async def set_destiny_command(client: Client, message: Message):
    user_states[message.from_user.id] = "waiting_for_destiny_forward"
    await message.reply_text("Please forward a message from the destination channel/group here. The bot must be admin there.")

@bot.on_message(filters.command("show_destiny") & filters.private)
async def show_destiny_command_handler(client: Client, message: Message):
    await show_destiny_list(client, message)

@bot.on_message(filters.command("show_source") & filters.private)
async def show_source(client: Client, message: Message):
    user_data = await get_user_data(message.from_user.id)
    src = user_data.get("source_chat")
    private_sources = user_data.get("private_sources", [])

    response_text = ""
    buttons = []

    if src:
        try:
            chat = await client.get_chat(src)
            response_text += f"Public source: {chat.title} (<code>{chat.id}</code>)\n"
            buttons.append([InlineKeyboardButton("Remove Public Source", callback_data="del_source_confirm")])
        except Exception as e:
            response_text += f"Public source set but inaccessible: {src}\nError: {e}\n"
    else:
        response_text += "No public source set. Use /set_source to add one.\n"

    if private_sources:
        response_text += "\nPrivate sources:\n"
        for p in private_sources:
            try:
                chat = await client.get_chat(p)
                response_text += f"• {chat.title} (<code>{p}</code>)\n"
            except Exception:
                response_text += f"• Unknown (<code>{p}</code>)\n"
    else:
        response_text += "\nNo private sources. Use /add_private_source after adding a session.\n"

    await message.reply_text(response_text, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None, parse_mode=ParseMode.HTML)

# ---------- Utility to show destiny list ----------
async def show_destiny_list(client: Client, message: Message, edit_message: bool = False, custom_text: Optional[str] = None):
    user_data = await get_user_data(message.from_user.id)
    dests = user_data.get("destination_chats", [])

    if dests:
        text = custom_text or "Select a destination to manage:\n"
        buttons = []
        for d in dests:
            try:
                chat = await client.get_chat(d)
                buttons.append([InlineKeyboardButton(chat.title, callback_data=f"show_dest_info_{d}")])
            except Exception:
                buttons.append([InlineKeyboardButton(str(d), callback_data=f"show_dest_info_{d}")])
        reply_markup = InlineKeyboardMarkup(buttons)
        if edit_message:
            await message.edit_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        else:
            await message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    else:
        text = custom_text or "No destinations set. Use /set_destiny to add one."
        if edit_message:
            await message.edit_text(text, parse_mode=ParseMode.HTML)
        else:
            await message.reply_text(text, parse_mode=ParseMode.HTML)

# ---------- Status and broadcast (owner only) ----------
@bot.on_message(filters.command("stats") & filters.user(OWNER_ID))
async def status_cmd(client: Client, message: Message):
    total_users = await users_collection.count_documents({})
    total_sources = await users_collection.count_documents({"source_chat": {"$ne": None}})
    total_private_sources_count = await users_collection.count_documents({"private_sources": {"$ne": []}})

    pipeline = [{"$unwind": "$destination_chats"}, {"$group": {"_id": None, "total": {"$sum": 1}}}]
    dest_agg = await users_collection.aggregate(pipeline).to_list(None)
    total_destinations = dest_agg[0]["total"] if dest_agg else 0

    await message.reply_text(
        f"Users: <b>{total_users}</b>\nPublic Sources Set: <b>{total_sources}</b>\nUsers with Private Sources: <b>{total_private_sources_count}</b>\nDestinations Added: <b>{total_destinations}</b>",
        parse_mode=ParseMode.HTML
    )

@bot.on_message(filters.command("broadcast") & filters.user(OWNER_ID))
async def broadcast_cmd(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("Usage: /broadcast your message")
    text = message.text.split(" ", 1)[1]

    tasks = []
    async for u in users_collection.find({}):
        uid = u.get("_id")
        try:
            uid = int(uid)
        except Exception:
            continue
        tasks.append(asyncio.create_task(send_with_retry(client, uid, text)))

    if not tasks:
        return await message.reply_text("No users to broadcast.")

    results = await asyncio.gather(*tasks)
    sent = sum(1 for r in results if r is not None)
    failed = len(results) - sent
    await message.reply_text(f"Broadcast sent to {sent}. Failed: {failed}")

# ---------- Public channel forwarder ----------
@bot.on_message(filters.channel)
async def forward_public_channel_message(client: Client, message: Message):
    # For each user that has this channel as source, copy message to their destinations
    async for user_data in users_collection.find({"source_chat": message.chat.id}):
        user_id = user_data["_id"]
        destinations = user_data.get("destination_chats", [])
        for dest in destinations:
            try:
                await copy_with_retry(client, dest, message.chat.id, message.id)
            except Exception as e:
                logger.error(f"Failed to forward message {message.id} from {message.chat.id} to {dest}: {e}")
                await send_with_retry(client, user_id, f"Failed to forward a message to destination {dest}. Error: {e}")

# ---------- Userbot management for private sources ----------
async def initialize_userbot_for_user(user_id: int, session_string: str):
    """Start a userbot for the given user (if not already started) and attach handlers for that user's private sources."""
    # If already running, stop and restart (to refresh source list)
    existing = active_userbots.get(user_id)
    if existing:
        try:
            client: Client = existing.get("client")
            await client.stop()
        except Exception:
            logger.exception("Error stopping existing userbot")
        task = existing.get("task")
        if task and not task.done():
            task.cancel()

    # Create a new userbot client using the session string
    userbot = Client(name=f"userbot_{user_id}", session_string=session_string, api_id=API_ID, api_hash=API_HASH)

    async def on_userbot_message(c: Client, m: Message):
        # This will be triggered for messages from chats the userbot is a member of. We forward messages from private_sources only.
        try:
            # Determine if channel is in user's private_sources
            udata = await get_user_data(user_id)
            private_sources = udata.get("private_sources", [])
            if m.chat and m.chat.id in private_sources:
                destinations = udata.get("destination_chats", [])
                # For each destination, instruct the main bot to copy the message using bot.copy_message
                for dest in destinations:
                    try:
                        await copy_with_retry(bot, dest, m.chat.id, m.id)
                    except Exception as e:
                        logger.error(f"Userbot forwarding failed from {m.chat.id} to {dest}: {e}")
                        await send_with_retry(bot, user_id, f"Failed to forward a private-source message to {dest}. Error: {e}")
        except Exception:
            logger.exception("Error in userbot message handler")

    # Register a handler that listens to all messages; inside we filter by private_sources to allow dynamic lists
    userbot.add_handler(filters=filters.all & filters.private == False, callback=on_userbot_message)  # register wildcard handler

    async def run_userbot():
        try:
            await userbot.start()
            logger.info(f"Userbot for {user_id} started")
            # keep it running until cancelled
            while True:
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            logger.info(f"Userbot task for {user_id} cancelled")
        except Exception:
            logger.exception(f"Userbot for {user_id} encountered an exception")
        finally:
            try:
                await userbot.stop()
            except Exception:
                pass

    task = asyncio.create_task(run_userbot())
    active_userbots[user_id] = {"client": userbot, "task": task}

# ---------- Commands to add session and private source ----------
@bot.on_message(filters.command("add_session") & filters.private)
async def add_session_command(client: Client, message: Message):
    user_states[message.from_user.id] = "waiting_for_phone"
    await message.reply_text("Please send your phone number with country code (e.g., +8801XXXXXXXXX). This will be used to create a user session for private sources.")

@bot.on_message(filters.command("add_private_source") & filters.private)
async def add_private_source_command(client: Client, message: Message):
    # user must have a session
    session = await get_session_string(message.from_user.id)
    if not session:
        return await message.reply_text("No session found. Use /add_session to add your account first.")
    user_states[message.from_user.id] = "waiting_for_private_source_forward"
    await message.reply_text("Please forward a message from the private channel you want to use as source. The user account (session) must be a member of that channel.")

# ---------- Catch forwarded messages for stateful operations ----------
@bot.on_message(filters.forwarded & filters.private)
async def catch_forwarded(client: Client, message: Message):
    user_id = message.from_user.id
    current_state = user_states.get(user_id)

    if not message.forward_from_chat:
        if current_state in ["waiting_for_source_forward", "waiting_for_destiny_forward", "waiting_for_private_source_forward"]:
            return await message.reply_text("Forwarded message must be from a channel or group. Please try again.")
        return

    chat = message.forward_from_chat

    try:
        if current_state in ["waiting_for_source_forward", "waiting_for_destiny_forward"]:
            # Check bot's permissions in that channel (best effort)
            try:
                chat_member = await client.get_chat_member(chat.id, client.me.id)
            except Exception:
                chat_member = None

            if current_state == "waiting_for_destiny_forward":
                user_data = await get_user_data(user_id)
                if chat.id not in user_data.get("destination_chats", []):
                    await add_destination(user_id, chat.id)
                    await message.reply_text(f"Destination set: {chat.title}")
                else:
                    await message.reply_text("This destination is already added.")
            elif current_state == "waiting_for_source_forward":
                await update_user_data(user_id, "source_chat", chat.id)
                await message.reply_text(f"Source set: {chat.title}")

            user_states.pop(user_id, None)

        elif current_state == "waiting_for_private_source_forward":
            # Save private source and ensure userbot is running
            await add_private_source_to_db(user_id, chat.id)
            await message.reply_text(f"Private source saved: {chat.title} (<code>{chat.id}</code>)", parse_mode=ParseMode.HTML)

            session_string = await get_session_string(user_id)
            if session_string:
                # initialize or restart userbot
                await initialize_userbot_for_user(user_id, session_string)
            else:
                await message.reply_text("No session found. Use /add_session to add your account first.")

            user_states.pop(user_id, None)

    except Exception as e:
        logger.exception(f"Error handling forwarded message: {e}")
        await message.reply_text(f"Error: {e}")
        user_states.pop(user_id, None)

# ---------- Stateful private-session creation (phone -> code -> 2fa) ----------
@bot.on_message(filters.private & filters.text)
async def state_handler(client: Client, message: Message):
    user_id = message.from_user.id
    current_state = user_states.get(user_id)

    if current_state == "waiting_for_phone":
        phone_number = message.text.strip()
        if not phone_number.startswith("+"):
            return await message.reply_text("Please include the country code, e.g., +8801XXXXXXXXX")

        # create an in-memory Pyrogram client to send code
        temp_client = Client(name=f"temp_{user_id}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
        try:
            await temp_client.connect()
            sent = await temp_client.send_code(phone_number)
            user_temp_data[user_id] = {"phone_number": phone_number, "sent_code_info": sent, "temp_client": temp_client}
            user_states[user_id] = "waiting_for_code"
            await message.reply_text(f"Code sent to {phone_number}. Please enter the code you received.")
        except PhoneNumberInvalid:
            await temp_client.disconnect()
            await message.reply_text("Invalid phone number. Please try again.")
            user_states.pop(user_id, None)
            user_temp_data.pop(user_id, None)
        except Exception as e:
            await temp_client.disconnect()
            logger.exception("Error sending code")
            await message.reply_text(f"Error sending code: {e}")
            user_states.pop(user_id, None)
            user_temp_data.pop(user_id, None)

    elif current_state == "waiting_for_code":
        code = message.text.strip()
        temp = user_temp_data.get(user_id)
        if not temp:
            return await message.reply_text("Session expired. Please /add_session again.")

        temp_client: Client = temp["temp_client"]
        phone_number = temp["phone_number"]
        sent_info = temp["sent_code_info"]
        try:
            await temp_client.sign_in(phone_number, sent_info.phone_code_hash, code)
            session_string = await temp_client.export_session_string()
            await save_session_string(user_id, session_string)
            await temp_client.disconnect()
            await message.reply_text("Session created and saved. You can now use /add_private_source")
            user_states.pop(user_id, None)
            user_temp_data.pop(user_id, None)
            # Start userbot in background
            await initialize_userbot_for_user(user_id, session_string)
        except SessionPasswordNeeded:
            user_states[user_id] = "waiting_for_2fa"
            await message.reply_text("Two-step verification enabled. Please send your 2FA password.")
        except PhoneCodeInvalid:
            await message.reply_text("Invalid code. Please try /add_session again.")
            await temp_client.disconnect()
            user_states.pop(user_id, None)
            user_temp_data.pop(user_id, None)
        except Exception as e:
            logger.exception("Error verifying code")
            await temp_client.disconnect()
            await message.reply_text(f"Error: {e}")
            user_states.pop(user_id, None)
            user_temp_data.pop(user_id, None)

    elif current_state == "waiting_for_2fa":
        password = message.text.strip()
        temp = user_temp_data.get(user_id)
        if not temp:
            return await message.reply_text("Session expired. Please /add_session again.")
        temp_client: Client = temp.get("temp_client")
        phone_number = temp.get("phone_number")
        sent_info = temp.get("sent_code_info")
        try:
            await temp_client.check_password(password=password)
            session_string = await temp_client.export_session_string()
            await save_session_string(user_id, session_string)
            await temp_client.disconnect()
            await message.reply_text("Session created and saved. You can now use /add_private_source")
            user_states.pop(user_id, None)
            user_temp_data.pop(user_id, None)
            await initialize_userbot_for_user(user_id, session_string)
        except Exception as e:
            logger.exception("2FA error")
            await temp_client.disconnect()
            await message.reply_text(f"2FA error: {e}")
            user_states.pop(user_id, None)
            user_temp_data.pop(user_id, None)

# ---------- Graceful cleanup on shutdown ----------
async def shutdown_all_userbots():
    for uid, data in list(active_userbots.items()):
        client = data.get("client")
        task = data.get("task")
        try:
            if task:
                task.cancel()
        except Exception:
            pass
        try:
            await client.stop()
        except Exception:
            pass
    active_userbots.clear()

# ---------- Start bot ----------
async def main():
    # Start main bot
    await bot.start()
    logger.info("Main bot started")

    # For any users with session strings already in DB, start their userbots
    async for u in users_collection.find({"session_string": {"$ne": None}}):
        uid = u.get("_id")
        ss = u.get("session_string")
        try:
            # try to start userbot but don't crash on errors
            await initialize_userbot_for_user(uid, ss)
        except Exception:
            logger.exception(f"Failed to start userbot for {uid}")

    # idle
    logger.info("Bot is up and running. Press Ctrl+C to stop.")
    try:
        await asyncio.get_event_loop().create_future()  # run forever
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutting down...")
    finally:
        await shutdown_all_userbots()
        await bot.stop()

if __name__ == '__main__':
    asyncio.run(main())
