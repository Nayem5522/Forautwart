import os
import threading
import asyncio
import logging
from flask import Flask
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram.enums import ParseMode
from pyrogram.errors import (
    UserNotParticipant, ChatAdminRequired, PeerIdInvalid, RPCError,
    FloodWait, UserIsBot, SessionPasswordNeeded, PhoneCodeInvalid,
    PasswordRequired, PhoneNumberInvalid, AuthKeyUnregistered
)

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ---------- Flask healthcheck server ----------
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"Flask server starting on port {port}")
    flask_app.run(host="0.0.0.0", port=port)

# Run Flask in a separate thread
threading.Thread(target=run_flask).start()
# ---------- end Flask ----------

# ---------- MongoDB Client ----------
MONGO_DB_URL = os.environ.get("MONGO_DB_URL")
if not MONGO_DB_URL:
    logger.error("Error: MONGO_DB_URL environment variable is not set. Exiting.")
    exit(1)

db_client = AsyncIOMotorClient(MONGO_DB_URL)
db = db_client.autoforward_db
users_collection = db.users  # Stores user-specific data (sources, destinations, session strings)

# ---------- Pyrogram client ----------
# Ensure these environment variables are set
try:
    API_ID = int(os.environ["API_ID"])
    API_HASH = os.environ["API_HASH"]
    BOT_TOKEN = os.environ["BOT_TOKEN"]
    OWNER_ID = int(os.environ.get("OWNER_ID", "5926160191")) # Default owner ID if not set
except KeyError as e:
    logger.error(f"Missing environment variable: {e}. Exiting.")
    exit(1)

app = Client(
    "autoforward_bot", # Changed session name for clarity
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# ---------- Global Semaphores for concurrency control ----------
SEND_SEMAPHORE = asyncio.Semaphore(10) # Limit concurrent send_message operations
COPY_SEMAPHORE = asyncio.Semaphore(5)  # Limit concurrent copy_message operations

# In-memory store for user states for sequential operations (e.g., adding session)
# user_id: state_string (e.g., "waiting_for_destiny", "waiting_for_phone", "waiting_for_code", "waiting_for_2fa")
user_states = {}
# user_id: temporary_data (e.g., phone number, UserbotClient instance)
user_temp_data = {}

# Dictionary to hold active userbot clients {user_id: UserbotClient instance}
active_userbots = {}

# ---------- Helper Functions ----------
async def get_user_data(user_id):
    user_data = await users_collection.find_one({"_id": user_id})
    if not user_data:
        user_data = {"_id": user_id, "source_chat": None, "destination_chats": [], "private_sources": [], "session_string": None}
        await users_collection.insert_one(user_data)
    return user_data

async def update_user_data(user_id, field, value):
    await users_collection.update_one({"_id": user_id}, {"$set": {field: value}}, upsert=True)

async def add_destination(user_id, chat_id):
    await users_collection.update_one({"_id": user_id}, {"$addToSet": {"destination_chats": chat_id}}, upsert=True)

async def remove_destination(user_id, chat_id):
    await users_collection.update_one({"_id": user_id}, {"$pull": {"destination_chats": chat_id}})

async def save_session_string(user_id, session_string):
    await users_collection.update_one({"_id": user_id}, {"$set": {"session_string": session_string}}, upsert=True)

async def get_session_string(user_id):
    user_data = await users_collection.find_one({"_id": user_id})
    return user_data.get("session_string")

async def add_private_source_to_db(user_id, chat_id):
    await users_collection.update_one({"_id": user_id}, {"$addToSet": {"private_sources": chat_id}}, upsert=True)

async def get_user_destinations(user_id):
    user_data = await get_user_data(user_id)
    return user_data.get("destination_chats", [])

async def send_with_retry(client, chat_id, text, parse_mode=ParseMode.HTML, retries=3):
    async with SEND_SEMAPHORE:
        for attempt in range(retries):
            try:
                return await client.send_message(chat_id, text, parse_mode=parse_mode)
            except FloodWait as e:
                wait = e.value if hasattr(e, 'value') else 5
                logger.warning(f"FloodWait: sleeping for {wait} seconds before retrying send to {chat_id}")
                await asyncio.sleep(wait + 1)
            except (BotBlocked, UserIsBot):
                logger.info(f"Cannot send message to {chat_id}: Bot blocked or user is a bot.")
                return None
            except Exception as e:
                logger.exception(f"Failed to send message to {chat_id} on attempt {attempt+1}: {e}")
                await asyncio.sleep(1)
        return None

async def copy_with_retry(client, chat_id, from_chat_id, message_id, retries=3):
    async with COPY_SEMAPHORE:
        for attempt in range(retries):
            try:
                return await client.copy_message(chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id, disable_notification=True)
            except FloodWait as e:
                wait = e.value if hasattr(e, 'value') else 5
                logger.warning(f"FloodWait: sleeping for {wait} seconds before retrying copy to {chat_id}")
                await asyncio.sleep(wait + 1)
            except (BotBlocked, UserIsBot):
                logger.info(f"Cannot copy message to {chat_id}: Bot blocked or user is a bot.")
                return None
            except Exception as e:
                logger.exception(f"Failed to copy message to {chat_id} on attempt {attempt+1}: {e}")
                await asyncio.sleep(1)
        return None

# ---------- START Command ----------
@app.on_message(filters.command("start") & filters.private)
async def start(client, message):
    buttons = [
        [
            InlineKeyboardButton("✪ ꜱᴜᴘᴘᴏʀᴛ ɢʀᴏᴜᴘ ✪", url="https://t.me/Prime_Support_group"),
            InlineKeyboardButton("〄 ᴍᴏᴠɪᴇ ᴄʜᴀɴɴᴇʟ 〄", url="https://t.me/PrimeCineZone")
        ],
        [InlineKeyboardButton("〄 ᴜᴘᴅᴀᴛᴇs ᴄʜᴀɴɴᴇʟ 〄", url="https://t.me/PrimeXBots")],
        [
            InlineKeyboardButton("〆 ʜᴇʟᴘ 〆", callback_data="help_cmd"),
            InlineKeyboardButton("〆 ᴀʙᴏᴜᴛ 〆", callback_data="about_cmd")
        ],
        [InlineKeyboardButton("✧ ᴄʀᴇᴀᴛᴏʀ ✧", url="https://t.me/Prime_Nayem")]
    ]

    await message.reply_photo(
        photo="https://i.postimg.cc/fLkdDgs2/file-00000000346461fab560bc2d21951e7f.png",
        caption=(
            f"👋 Hello {message.from_user.mention},\n\n"
            "Welcome! To This Bot\nThis bot can automatically forward New posts from one channel to another Channel/group\n\n"
            "⊰•─•─✦✗✦─•◈•─✦✗✦─•─•⊱\n"
            "⚡ Use the buttons below to navigate and get started!"
        ),
        reply_markup=InlineKeyboardMarkup(buttons)
        )

# ---------- Callback Query Handler ----------
@app.on_callback_query()
async def cb_handler(client, query):
    user_id = query.from_user.id
    data = query.data

    if data == "close":
        try:
            await query.message.delete()
        except Exception:
            await query.answer("⚠️ Cannot delete message.", show_alert=True)
        return

    elif data == "about_cmd":
        me = await client.get_me()
        about_text = (
            "<b><blockquote>⍟───[  <a href='https://t.me/PrimeXBots'>ᴍʏ ᴅᴇᴛᴀɪʟꜱ ʙʏ ᴘʀɪᴍᴇXʙᴏᴛꜱ</a> ]───⍟</blockquote></b>\n\n"
            f"‣ ᴍʏ ɴᴀᴍᴇ : <a href='https://t.me/{me.username}'>{me.first_name}</a>\n"
            "‣ ʙᴇꜱᴛ ꜰʀɪᴇɴᴅ : <a href='tg://settings'>ᴛʜɪꜱ ᴘᴇʀꜱᴏɴ</a>\n"
            "‣ ᴅᴇᴠᴇʟᴏᴘᴇʀ : <a href='https://t.me/Prime_Nayem'>ᴍʀ.ᴘʀɪᴍᴇ</a>\n"
            "‣ ᴜᴘᴅᴀᴛᴇꜱ ᴄʜᴀɴɴᴇʟ : <a href='https://t.me/PrimeXBots'>ᴘʀɪᴍᴇXʙᴏᴛꜱ</a>\n"
            "‣ ᴍᴀɪɴ ᴄʜᴀɴɴᴇʟ : <a href='https://t.me/PrimeCineZone'>ᴘʀɪᴍᴇ ᴄɪɴᴇᴢᴏɴᴇ</a>\n"
            "‣ ꜱᴜᴘᴘᴏʀᴛ ɢʀᴏᴜᴘ : <a href='https://t.me/Prime_Support_group'>ᴘʀɪᴍᴇX ꜱᴜᴘᴘᴏʀᴛ</a>\n"
            "‣ ᴅᴀᴛᴀʙᴀꜱᴇ : <a href='https://www.mongodb.com/'>ᴍᴏɴɢᴏᴅʙ</a>\n"
            "‣ ʙᴏᴛ ꜱᴇʀᴠᴇʀ : <a href='https://heroku.com'>ʜᴇʀᴏᴋᴜ</a>\n"
            "‣ ʙᴜɪʟᴅ ꜱᴛᴀᴛᴜꜱ : v2.7.1 [ꜱᴛᴀʙʟᴇ]\n"
        )

        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Close", callback_data="close")]
        ])

        await query.message.edit_text(
            about_text,
            disable_web_page_preview=True,
            parse_mode=ParseMode.HTML,
            reply_markup=buttons
            )

    elif data == "help_cmd":
        help_text = (
            "📝 <b>How to use this bot:</b>\n\n"
            "➊ <code>/set_source</code> – Set your source channel (bot must be admin there)\n"
            "➋ <code>/set_destiny</code> – Set your destination channel/group (bot must be admin there)\n"
            "➌ <code>/show_source</code> – View or remove the current source\n"
            "➍ <code>/show_destiny</code> – View/manage all your destinations\n"
            "➎ <code>/add_session</code> - Add your user account session for private sources\n"
            "➏ <code>/add_private_source</code> - Set a private channel as source (requires /add_session)\n"
            "➐ <code>/stats</code> – View total users, sources & destinations (Owner only)\n"
            "➑ <code>/broadcast</code> <i>your message</i> – Send a broadcast to all users (Owner only)\n\n"
            "⚡ After setting a source, new posts from it will automatically be forwarded to your destinations."
        )

        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Close", callback_data="close")]
        ])

        await query.message.edit_text(
            help_text,
            parse_mode=ParseMode.HTML,
            reply_markup=buttons
        )

    elif data.startswith("show_dest_info_"):
        chat_id = int(data.split("_")[-1])
        try:
            chat = await client.get_chat(chat_id)
            invite_link = getattr(chat, 'invite_link', None) or "No invite link available."
            chat_type_str = chat.type.value.capitalize()
            text = f"🎯 <b>Destination Details:</b>\n" \
                   f"• <b>Name:</b> {chat.title}\n" \
                   f"• <b>ID:</b> <code>{chat.id}</code>\n" \
                   f"• <b>Type:</b> {chat_type_str}\n" \
                   f"• <b>Invite Link:</b> {invite_link}\n\n" \
                   f"<i>Are you sure you want to remove this destination?</i>"
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Remove this destination ❗", callback_data=f"del_dest_confirm_{chat_id}")],
                [InlineKeyboardButton("❌ Close ⭕", callback_data="close")]
            ])
            await query.message.edit_text(text, reply_markup=buttons, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception as e:
            await query.message.edit_text(f"⚠️ Error fetching chat info for {chat_id}: {e}", parse_mode=ParseMode.HTML)

    elif data == "show_dest_list":
        await show_destiny_list(client, query.message, edit_message=True)

    elif data.startswith("del_dest_confirm_"):
        chat_id = int(data.split("_")[-1])
        await remove_destination(user_id, chat_id)

        try:
            chat_info = await client.get_chat(chat_id)
            chat_name = chat_info.title
        except Exception:
            chat_name = str(chat_id) # fallback if channel info fetch fails

        await query.answer(f"Destination {chat_name} removed!", show_alert=True)

        custom_text = f"✅ Destination removed: <b>{chat_name}</b>"
        await show_destiny_list(client, query.message, edit_message=True, custom_text=custom_text)

    elif data == "del_source_confirm":
        await update_user_data(user_id, "source_chat", None)
        await query.message.edit_text("✅ Source removed.", parse_mode=ParseMode.HTML)

# ---------- SET SOURCE / DESTINY ----------
@app.on_message(filters.command("set_source") & filters.private)
async def set_source_command(client, message):
    user_states[message.from_user.id] = "waiting_for_source_forward"
    await message.reply_text(
        "📢 Please forward a message from your source channel here.\n\n⚠️ Bot must be admin in that channel.",
        parse_mode=ParseMode.HTML
    )

@app.on_message(filters.command("set_destiny") & filters.private)
async def set_destiny_command(client, message):
    user_states[message.from_user.id] = "waiting_for_destiny_forward"
    await message.reply_text(
        "🎯 Please forward a message from your destination channel/group.\n\n⚠️ Bot must be admin there.",
        parse_mode=ParseMode.HTML
    )

# ---------- CATCH FORWARDED messages and handle state-based input ----------
@app.on_message(filters.forwarded & filters.private)
async def catch_forwarded(client, message):
    user_id = message.from_user.id
    current_state = user_states.get(user_id)

    if not message.forward_from_chat:
        if current_state in ["waiting_for_source_forward", "waiting_for_destiny_forward", "waiting_for_private_source_forward"]:
            await message.reply_text("⚠️ Forwarded message must be from a channel/group. Please try again.", parse_mode=ParseMode.HTML)
            # Do not clear state, let user try again
        return

    chat = message.forward_from_chat
    try:
        if current_state in ["waiting_for_source_forward", "waiting_for_destiny_forward"]:
            # Check bot's permissions
            try:
                chat_member = await client.get_chat_member(chat.id, client.me.id)
                if not (chat_member.can_post_messages or chat_member.can_be_edited): # simplified check for channel posting/admin
                    return await message.reply_text(f"⚠️ Bot needs to be an admin in {chat.title} (ID: <code>{chat.id}</code>) with 'Post Messages' permission. Please promote me.", parse_mode=ParseMode.HTML)
            except UserNotParticipant:
                return await message.reply_text(f"⚠️ Bot is not a member of {chat.title} (ID: <code>{chat.id}</code>). Please add me first.", parse_mode=ParseMode.HTML)
            except ChatAdminRequired:
                return await message.reply_text(f"⚠️ Bot needs to be admin in {chat.title} (ID: <code>{chat.id}</code>). Please promote me.", parse_mode=ParseMode.HTML)
            except PeerIdInvalid:
                return await message.reply_text(f"⚠️ Invalid chat ID for {chat.title}.", parse_mode=ParseMode.HTML)
            except RPCError as e:
                return await message.reply_text(f"⚠️ Telegram API error: {e}", parse_mode=ParseMode.HTML)

            chat_info = await client.get_chat(chat.id)

            if current_state == "waiting_for_destiny_forward":
                user_data = await get_user_data(user_id)
                if chat.id not in user_data["destination_chats"]:
                    await add_destination(user_id, chat.id)
                    await message.reply_text(f"✅ Destination set: {chat_info.title}", parse_mode=ParseMode.HTML)
                else:
                    await message.reply_text(f"ℹ️ This destination is already added: {chat_info.title}", parse_mode=ParseMode.HTML)
            elif current_state == "waiting_for_source_forward":
                await update_user_data(user_id, "source_chat", chat.id)
                await message.reply_text(f"✅ Source channel set: {chat_info.title}", parse_mode=ParseMode.HTML)

            user_states.pop(user_id, None) # Clear state after successful operation

        elif current_state == "waiting_for_private_source_forward":
            # For private sources, no bot permissions check needed, as userbot handles it
            source_chat_id = chat.id
            await add_private_source_to_db(user_id, source_chat_id)
            await message.reply_text(f"✅ Private source {chat.title} (<code>{source_chat_id}</code>) saved. New posts will be forwarded to your destinations automatically.", parse_mode=ParseMode.HTML)

            # Re-initialize or update the userbot to listen to this new source
            session_string = await get_session_string(user_id)
            if session_string:
                await initialize_userbot_for_user(user_id, session_string)
            else:
                await message.reply_text("⚠️ No session found for your account. Please use /add_session first to enable private source forwarding.", parse_mode=ParseMode.HTML)

            user_states.pop(user_id, None) # Clear state

    except Exception as e:
        logger.exception(f"Error handling forwarded message for user {user_id}: {e}")
        await message.reply_text(f"⚠️ An error occurred: {e}", parse_mode=ParseMode.HTML)
        user_states.pop(user_id, None) # Clear state on error

# ---------- SHOW DESTINY LIST ----------
async def show_destiny_list(client, message, edit_message=False, custom_text=None):
    user_data = await get_user_data(message.from_user.id)
    dests = user_data.get("destination_chats", [])

    if dests:
        text = custom_text or "🎯 Select a destination to manage:\n"
        buttons = []
        for d_chat_id in dests:
            try:
                chat = await client.get_chat(d_chat_id)
                buttons.append([InlineKeyboardButton(chat.title, callback_data=f"show_dest_info_{d_chat_id}")])
            except Exception:
                buttons.append([InlineKeyboardButton(f"Unknown Chat ({d_chat_id})", callback_data=f"show_dest_info_{d_chat_id}")])
        reply_markup = InlineKeyboardMarkup(buttons)
        if edit_message:
            await message.edit_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        else:
            await message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    else:
        text = custom_text or "⚠️ No destinations set. Use /set_destiny to add one."
        if edit_message:
            await message.edit_text(text, parse_mode=ParseMode.HTML)
        else:
            await message.reply_text(text, parse_mode=ParseMode.HTML)

@app.on_message(filters.command("show_destiny") & filters.private)
async def show_destiny_command_handler(client, message):
    await show_destiny_list(client, message)

@app.on_message(filters.command("show_source") & filters.private)
async def show_source(client, message):
    user_data = await get_user_data(message.from_user.id)
    src = user_data.get("source_chat")
    private_sources = user_data.get("private_sources", [])

    response_text = ""
    buttons = []

    if src:
        try:
            chat = await client.get_chat(src)
            response_text += f"📢 Current public source: {chat.title} (<code>{chat.id}</code>)\n"
            buttons.append([InlineKeyboardButton("❌ Remove Public Source", callback_data="del_source_confirm")])
        except Exception as e:
            response_text += f"⚠️ Current public source ({src}) is inaccessible. Error: {e}\n"
    else:
        response_text += "⚠️ No public source set. Use /set_source to add one.\n"

    if private_sources:
        response_text += "\n🔒 Your private sources:\n"
        for p_src_id in private_sources:
            try:
                chat = await client.get_chat(p_src_id) # Bot can get public chat info
                response_text += f"• {chat.title} (<code>{p_src_id}</code>)\n"
            except Exception:
                response_text += f"• Unknown Private Chat (<code>{p_src_id}</code>)\n"
        # Add a button to manage private sources if needed
        # buttons.append([InlineKeyboardButton("Manage Private Sources", callback_data="manage_private_sources")])
    else:
        response_text += "\n⚠️ No private sources set. Use /add_private_source to add one."

    await message.reply_text(response_text, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None, parse_mode=ParseMode.HTML)


# ---------- STATUS & BROADCAST ----------
@app.on_message(filters.command("stats") & filters.user(OWNER_ID))
async def status_cmd(client, message):
    total_users = await users_collection.count_documents({})
    total_sources = await users_collection.count_documents({"source_chat": {"$ne": None}})
    total_private_sources_count = await users_collection.count_documents({"private_sources": {"$ne": []}})

    pipeline = [{"$unwind": "$destination_chats"},
                {"$group": {"_id": None, "total": {"$sum": 1}}}]
    dest_agg = await users_collection.aggregate(pipeline).to_list(None)
    total_destinations = dest_agg[0]["total"] if dest_agg else 0

    await message.reply_text(
        f"👤 Total Users: <b>{total_users}</b>\n"
        f"📢 Public Sources Set: <b>{total_sources}</b>\n"
        f"🔒 Users with Private Sources: <b>{total_private_sources_count}</b>\n"
        f"🎯 Destinations Added: <b>{total_destinations}</b>",
        parse_mode=ParseMode.HTML
    )

@app.on_message(filters.command("broadcast") & filters.user(OWNER_ID))
async def broadcast_cmd(client, message):
    if len(message.command) < 2:
        return await message.reply_text("Usage: /broadcast your message")
    text = message.text.split(" ", 1)[1]

    count = 0
    failed = 0
    tasks = []

    async for user_data in users_collection.find({}):
        uid = user_data.get("_id")
        if not isinstance(uid, int):
            try:
                uid = int(uid)
            except (ValueError, TypeError):
                logger.warning(f"Invalid user ID found in DB: {uid}")
                continue
        tasks.append(asyncio.create_task(send_with_retry(client, uid, text)))

    if not tasks:
        return await message.reply_text("ℹ️ No users found in database to broadcast.")

    results = await asyncio.gather(*tasks)
    for r in results:
        if r is None:
            failed += 1
        else:
            count += 1

    await message.reply_text(f"✅ Broadcast sent to {count} users. Failed: {failed}")

# ---------- PUBLIC CHANNEL FORWARDER ----------
@app.on_message(filters.channel)
async def forward_public_channel_message(client, message):
    # This will only trigger for channels where the bot is a member.
    # It will forward from sources set by users.
    async for user_data in users_collection.find({"source_chat": message.chat.id}):
        user_id = user_data["_id"]
        destinations = user_data.get("destination_chats", [])
        for dest_chat_id in destinations:
            try:
                await copy_with_retry(client, dest_chat_id, message.chat.id, message.id)
            except Exception as e:
                logger.error(f"Failed to copy message {message.id} from {message.chat.id} to {dest_chat_id} for user {user_id}: {e}")
                # Try to inform the user about the failure
                await send_with_retry(client, user_id,
                                      f"⚠️ Failed to forward a message from <b>{message.chat.title}</b> "
                                      f"to your destination (ID: <code>{dest_chat_id}</code>). Error: {e}")

# ---------- USERBOT SESSION & PRIVATE SOURCE ----------

# This is a custom handler for messages that come from users when a state is active
@app.on_message(filters.private & filters.text)
async def state_handler(client, message):
    user_id = message.from_user.id
    current_state = user_states.get(user_id)

    if current_state == "waiting_for_phone":
        phone_number = message.text.strip()
        if not phone_number.startswith("+"):
            return await message.reply_text("Please include the country code (e.g., +8801XXXXXXXXX).")

        userbot_client = Client(
            name=f"userbot_{user_id}",
            api_id=API_ID,
            api_hash=API_HASH,
            in_memory=True # Do not save to disk, will be saved to DB
        )
        try:
            await userbot_client.connect()
            sent_code_info = await userbot_client.send_code(phone_number)
            user_temp_data[user_id] = {"phone_number": phone_number, "sent_code_info": sent_code_info, "userbot_client": userbot_client}
            user_states[user_id] = "waiting_for_code"
            await message.reply_text(f"A login code has been sent to {phone_number}. Please enter the code:")
        except PhoneNumberInvalid:
            await userbot_client.disconnect()
            await message.reply_text("⚠️ Invalid phone number. Please try again with a valid number.")
            user_states.pop(user_id, None)
            user_temp_data.pop(user_id, None)
        except RPCError as e:
            await userbot_client.disconnect()
            await message.reply_text(f"⚠️ Telegram error when sending code: {e}")
            user_states.pop(user_id, None)
            user_temp_data.pop(user_id, None)
        except Exception as e:
            logger.exception(f"Error in waiting_for_phone state for user {user_id}: {e}")
            await userbot_client.disconnect()
            await message.reply_text(f"⚠️ An unexpected error occurred: {e}")
            user_states.pop(user_id, None)
            user_temp_data.pop(user_id, None)

    elif current_state == "waiting_for_code":
        code = message.text.strip()
        temp_data = user_temp_data.get(user_id)
        if not temp_data:
            return await message.reply_text("Error: Missing session data. Please restart /add_session.")

        userbot_client = temp_data["userbot_client"]
        phone_number = temp_data["phone_number"]
        sent_code_info = temp_data["sent_code_info"]

        try:
            await userbot_client.sign_in(phone_number, sent_code_info.phone_code_hash, code)
            session_string = await userbot_client.export_session_string()
            await save_session_string(user_id, session_string)
            await userbot_client.disconnect() # Disconnect the temp client

            await message.reply_text("✅ Session added successfully. You can now use /add_private_source.")
            user_states.pop(user_id, None)
            user_temp_data.pop(user_id, None)

            # Re-initialize userbot for background listening
            await initialize_userbot_for_user(user_id, session_string)

        except PhoneCodeInvalid:
            await message.reply_text("⚠️ Invalid code. Please try again or restart /add_session.")
        except SessionPasswordNeeded:
            user_states[user_id] = "waiting_for_2fa"
            await message.reply_text("Two-factor authentication (2FA) password required. Please send it:")
        except RPCError as e:
            await userbot_client.disconnect()
            await message.reply_text(f"⚠️ Telegram error: {e}")
            user_states.pop(user_id, None)
            user_temp_data.pop(user_id, None)
        except Exception as e:
            logger.exception(f"Error in waiting_for_code state for user {user_id}: {e}")
            await userbot_client.disconnect()
            await message.reply_text(f"⚠️ An unexpected error occurred: {e}")
            user_states.pop(user_id, None)
            user_temp_data.pop(user_id, None)

    elif current_state == "waiting_for_2fa":
        password = message.text.strip()
        temp_data = user_temp_data.get(user_id)
        if not temp_data:
            return await message.reply_text("Error: Missing session data. Please restart /add_session.")

        userbot_client = temp_data["userbot_client"]
        try:
            await userbot_client.check_password(password=password)
            session_string = await userbot_client.export_session_string()
            await save_session_string(user_id, session_string)
            await userbot_client.disconnect()

            await message.reply_text("✅ Session added successfully. You can now use /add_private_source.")
            user_states.pop(user_id, None)
            user_temp_data.pop(user_id, None)

            await initialize_userbot_for_user(user_id, session_string)

        except PasswordRequired:
            await userbot_client.disconnect()
            await message.reply_text("⚠️ Two-factor authentication (2FA) is enabled. Please restart /add_session and provide your password when prompted.")
            user_states.pop(user_id, None)
            user_temp_data.pop(user_id, None)
        except AuthKeyUnregistered:
            await userbot_client.disconnect()
            await message.reply_text("⚠️ This session is no longer valid. Please restart /add_session to generate a new one.")
            user_states.pop(user_id, None)
            user_temp_data.pop(user_id, None)
        except Exception as e:
            logger.exception(f"Error in waiting_for_2fa state for user {user_id}: {e}")
            await userbot_client.disconnect()
            await message.reply_text(f"⚠️ An unexpected error occurred during 2FA: {e}")
            user_states.pop(user_id, None)
            user_temp_data.pop(user_id, None)

    # If the message is not a forwarded message and no state is active,
    # it might be a regular text message not handled by other filters.
    # In a real bot, you might want to add a default response here.
    # For now, we'll just log it or ignore.
    else:
        # If no specific state, check if it's a command not caught, or just ignore.
        if not message.text.startswith('/') and current_state not in ["waiting_for_source_forward", "waiting_for_destiny_forward", "waiting_for_private_source_forward"]:
            logger.debug(f"Unhandled private message from {user_id}: {message.text}")
            # await message.reply_text("I'm not sure how to handle that. Use /help to see available commands.")
        elif current_state:
            # If there's a state but it's not handled by the specific state handlers above,
            # it means the input was unexpected for that state.
            await message.reply_text("⚠️ Unexpected input for the current operation. Please try again or use /cancel to reset.")
            # For simplicity, we'll pop the state, but you might want a /cancel command
            user_states.pop(user_id, None)
            user_temp_data.pop(user_id, None)


# ---------- USERBOT CLIENT MANAGEMENT & PRIVATE SOURCE FORWARDER ----------
async def initialize_userbot_for_user(user_id, session_string):
    """
    Initializes a userbot client for a given user_id using their session string
    and sets up a message handler to forward from private sources.
    """
    logger.info(f"Initializing userbot for user {user_id}")

    # If an old userbot client exists for this user, stop it first
    if user_id in active_userbots and active_userbots[user_id].is_connected:
        try:
            await active_userbots[user_id].stop()
            logger.info(f"Stopped existing userbot for user {user_id}")
        except Exception as e:
            logger.warning(f"Error stopping existing userbot for {user_id}: {e}")

    try:
        # Create a new client instance for the userbot
        user_client = Client(
            name=f"userbot_session_{user_id}",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=session_string,
            no_updates=False # We want to receive updates
        )

        # Define a message handler specific to this userbot
        @user_client.on_message(filters.channel)
        async def userbot_private_forwarder(ub_client, message):
            user_data = await get_user_data(user_id)
            private_sources = user_data.get("private_sources", [])
            destinations = user_data.get("destination_chats", [])

            if message.chat.id in private_sources:
                logger.info(f"Userbot {user_id} detected message in private source {message.chat.id}")
                for dest_chat_id in destinations:
                    try:
                        # Use the main bot client (app) to copy the message to destinations
                        # as the userbot might not be admin in public destination channels.
                        # The main bot 'app' is guaranteed to be admin there if set via /set_destiny.
                        await copy_with_retry(app, dest_chat_id, message.chat.id, message.id)
                        logger.debug(f"Forwarded message {message.id} from private source {message.chat.id} to {dest_chat_id} for user {user_id}")
                    except Exception as e:
                        logger.error(f"Userbot failed to copy message {message.id} from {message.chat.id} to {dest_chat_id} for user {user_id}: {e}")
                        # Inform the user via the main bot if a destination fails
                        await send_with_retry(app, user_id,
                                              f"⚠️ Failed to forward a message from your private source <b>{message.chat.title}</b> "
                                              f"to your destination (ID: <code>{dest_chat_id}</code>). Error: {e}")

        await user_client.start()
        active_userbots[user_id] = user_client
        logger.info(f"Userbot for user {user_id} started successfully.")

    except Exception as e:
        logger.exception(f"Failed to start userbot for user {user_id}: {e}")
        # Clear session string if it's invalid
        await save_session_string(user_id, None)
        if user_id in active_userbots:
            try:
                await active_userbots[user_id].stop()
            except Exception:
                pass
            del active_userbots[user_id]
        await send_with_retry(app, user_id,
                              f"⚠️ Failed to initialize your userbot session. Please try /add_session again. Error: {e}")


# ---------- ON STARTUP: LOAD ALL EXISTING USERBOT SESSIONS ----------
@app.on_raw_update()
async def initial_userbot_loader(client, update, users, chats):
    # This raw update handler runs once when the bot first connects and receives an update.
    # We use it to load all existing userbot sessions from the database.
    if not hasattr(client, "_userbots_loaded"): # Ensure it runs only once
        client._userbots_loaded = True
        logger.info("Loading existing userbot sessions from database...")
        async for user_data in users_collection.find({"session_string": {"$ne": None}}):
            user_id = user_data["_id"]
            session_string = user_data["session_string"]
            # Start the userbot in a background task
            asyncio.create_task(initialize_userbot_for_user(user_id, session_string))
        logger.info("Finished initiating existing userbot sessions.")


# ---------- MAIN FUNCTION TO RUN THE BOT ----------
async def main():
    logger.info("Starting AutoForward Bot...")
    await app.start()
    logger.info("Bot started successfully!")

    # Keep the bot running
    await idle()

    logger.info("Stopping bot...")
    # Stop all active userbots before the main bot stops
    for user_id, ub_client in list(active_userbots.items()):
        if ub_client.is_connected:
            try:
                await ub_client.stop()
                logger.info(f"Stopped userbot for user {user_id}")
            except Exception as e:
                logger.warning(f"Error stopping userbot for {user_id}: {e}")
    await app.stop()
    logger.info("Bot stopped.")

if __name__ == "__main__":
    app.run(main()) # Changed to app.run(main()) for Pyrogram v2
