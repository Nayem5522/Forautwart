import os
import threading
import asyncio
import logging
from flask import Flask
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram.enums import ParseMode, ChatType
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
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ.get("OWNER_ID", "5926160191")) # Default owner ID if not set

app = Client(
    "autoforward_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

SEND_SEMAPHORE = asyncio.Semaphore(10)
COPY_SEMAPHORE = asyncio.Semaphore(5)

# states & temp data
user_states = {}
user_temp_data = {}
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

async def send_with_retry(client, chat_id, text, parse_mode=ParseMode.HTML, retries=3):
    async with SEND_SEMAPHORE:
        for attempt in range(retries):
            try:
                return await client.send_message(chat_id, text, parse_mode=parse_mode)
            except FloodWait as e:
                wait = getattr(e, 'value', 5)
                await asyncio.sleep(wait + 1)
            except (UserIsBot,):
                return None
            except Exception as e:
                logger.warning(f"send failed {attempt+1}: {e}")
                await asyncio.sleep(1)
        return None

async def copy_with_retry(client, chat_id, from_chat_id, message_id, retries=3):
    async with COPY_SEMAPHORE:
        for attempt in range(retries):
            try:
                return await client.copy_message(chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id, disable_notification=True)
            except FloodWait as e:
                wait = getattr(e, 'value', 5)
                await asyncio.sleep(wait + 1)
            except Exception as e:
                logger.warning(f"copy failed {attempt+1}: {e}")
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
        caption=f"👋 Hello {message.from_user.mention},\n\nWelcome!",
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

# ---------- ADD SESSION ----------
@app.on_message(filters.command("add_session") & filters.private)
async def add_session(client, message):
    user_states[message.from_user.id] = "waiting_for_phone"
    await message.reply_text("📞 Please send your phone number with +country code:")

# ---------- SET SOURCE / DESTINY ----------
@app.on_message(filters.command("set_source") & filters.private)
async def set_source_command(client, message):
    user_states[message.from_user.id] = "waiting_for_source_forward"
    await message.reply_text("📢 Please forward a message from your source channel.")

@app.on_message(filters.command("set_destiny") & filters.private)
async def set_destiny_command(client, message):
    user_states[message.from_user.id] = "waiting_for_destiny_forward"
    await message.reply_text("🎯 Please forward a message from your destination channel/group.")

# ---------- CATCH FORWARDED messages ----------
@app.on_message(filters.forwarded & filters.private)
async def catch_forwarded(client, message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    if not message.forward_from_chat:
        if state in ["waiting_for_source_forward", "waiting_for_destiny_forward", "waiting_for_private_source_forward"]:
            await message.reply_text("⚠️ Forwarded message must be from a channel/group.")
        return
    chat = message.forward_from_chat
    try:
        if state in ["waiting_for_source_forward", "waiting_for_destiny_forward"]:
            # fixed permission check
            try:
                chat_member = await client.get_chat_member(chat.id, client.me.id)
                if chat.type == ChatType.CHANNEL:
                    if not chat_member.privileges or not chat_member.privileges.can_post_messages:
                        return await message.reply_text(f"⚠️ Bot needs 'Post Messages' permission in {chat.title} (<code>{chat.id}</code>)",
                                                        parse_mode=ParseMode.HTML)
                else:
                    perms = chat.permissions
                    if not perms or not perms.can_send_messages:
                        return await message.reply_text(f"⚠️ Bot needs 'Send Messages' permission in {chat.title} (<code>{chat.id}</code>)",
                                                        parse_mode=ParseMode.HTML)
            except UserNotParticipant:
                return await message.reply_text(f"⚠️ Bot is not a member of {chat.title} (<code>{chat.id}</code>).")
            except ChatAdminRequired:
                return await message.reply_text(f"⚠️ Bot needs admin in {chat.title} (<code>{chat.id}</code>).")
            except PeerIdInvalid:
                return await message.reply_text(f"⚠️ Invalid chat ID for {chat.title}.")
            chat_info = await client.get_chat(chat.id)
            if state == "waiting_for_destiny_forward":
                user_data = await get_user_data(user_id)
                if chat.id not in user_data["destination_chats"]:
                    await add_destination(user_id, chat.id)
                    await message.reply_text(f"✅ Destination set: {chat_info.title}")
                else:
                    await message.reply_text(f"ℹ️ Already added: {chat_info.title}")
            elif state == "waiting_for_source_forward":
                await update_user_data(user_id, "source_chat", chat.id)
                await message.reply_text(f"✅ Source channel set: {chat_info.title}")
            user_states.pop(user_id, None)
        elif state == "waiting_for_private_source_forward":
            source_chat_id = chat.id
            await add_private_source_to_db(user_id, source_chat_id)
            await message.reply_text(f"✅ Private source {chat.title} (<code>{source_chat_id}</code>) saved.", parse_mode=ParseMode.HTML)
            session_string = await get_session_string(user_id)
            if session_string:
                await initialize_userbot_for_user(user_id, session_string)
            else:
                await message.reply_text("⚠️ No session found. Please use /add_session first.")
            user_states.pop(user_id, None)
    except Exception as e:
        logger.exception(e)
        await message.reply_text(f"⚠️ An error occurred: {e}")
        user_states.pop(user_id, None)

# ---------- STATE HANDLER ----------
@app.on_message(filters.private & filters.text)
async def state_handler(client, message):
    user_id = message.from_user.id
    state = user_states.get(user_id)
    from pyrogram import Client as UserbotClient
    if state == "waiting_for_phone":
        phone = message.text.strip()
        if not phone.startswith("+"):
            return await message.reply_text("Include country code (e.g., +8801…).")
        userbot_client = UserbotClient(name=f"userbot_{user_id}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
        try:
            await userbot_client.connect()
            sent_code_info = await userbot_client.send_code(phone)
            user_temp_data[user_id] = {"phone_number": phone, "sent_code_info": sent_code_info, "userbot_client": userbot_client}
            user_states[user_id] = "waiting_for_code"
            await message.reply_text(f"A login code has been sent to {phone}. Please enter the code:")
        except Exception as e:
            await userbot_client.disconnect()
            user_states.pop(user_id, None)
            user_temp_data.pop(user_id, None)
            await message.reply_text(f"⚠️ Error sending code: {e}")
    elif state == "waiting_for_code":
        code = message.text.strip()
        temp = user_temp_data.get(user_id)
        if not temp: return await message.reply_text("Missing session data. Restart /add_session.")
        userbot_client = temp["userbot_client"]
        phone = temp["phone_number"]
        sent_code_info = temp["sent_code_info"]
        try:
            await userbot_client.sign_in(phone, sent_code_info.phone_code_hash, code)
            session_string = await userbot_client.export_session_string()
            await save_session_string(user_id, session_string)
            await userbot_client.disconnect()
            await message.reply_text("✅ Session added successfully. You can now use /add_private_source.")
            user_states.pop(user_id, None)
            user_temp_data.pop(user_id, None)
            await initialize_userbot_for_user(user_id, session_string)
        except SessionPasswordNeeded:
            user_states[user_id] = "waiting_for_2fa"
            await message.reply_text("Two-factor password required. Please send it:")
        except Exception as e:
            await userbot_client.disconnect()
            await message.reply_text(f"⚠️ Error: {e}")
            user_states.pop(user_id, None)
            user_temp_data.pop(user_id, None)
    elif state == "waiting_for_2fa":
        password = message.text.strip()
        temp = user_temp_data.get(user_id)
        if not temp: return await message.reply_text("Missing session data. Restart /add_session.")
        userbot_client = temp["userbot_client"]
        try:
            await userbot_client.check_password(password=password)
            session_string = await userbot_client.export_session_string()
            await save_session_string(user_id, session_string)
            await userbot_client.disconnect()
            await message.reply_text("✅ Session added successfully. You can now use /add_private_source.")
            user_states.pop(user_id, None)
            user_temp_data.pop(user_id, None)
            await initialize_userbot_for_user(user_id, session_string)
        except Exception as e:
            await userbot_client.disconnect()
            user_states.pop(user_id, None)
            user_temp_data.pop(user_id, None)
            await message.reply_text(f"⚠️ Error during 2FA: {e}")

# ---------- USERBOT CLIENT MANAGEMENT ----------
async def initialize_userbot_for_user(user_id, session_string):
    logger.info(f"Initializing userbot for user {user_id}")
    if user_id in active_userbots:
        try:
            await active_userbots[user_id].stop()
        except Exception:
            pass
    from pyrogram import Client as UserbotClient
    try:
        user_client = UserbotClient(name=f"userbot_session_{user_id}", api_id=API_ID, api_hash=API_HASH,
                                    session_string=session_string, no_updates=False)

        @user_client.on_message(filters.channel)
        async def userbot_private_forwarder(ub_client, message):
            user_data = await get_user_data(user_id)
            if message.chat.id in user_data.get("private_sources", []):
                for dest_chat_id in user_data.get("destination_chats", []):
                    try:
                        await copy_with_retry(app, dest_chat_id, message.chat.id, message.id)
                    except Exception as e:
                        await send_with_retry(app, user_id,
                            f"⚠️ Failed to forward from <b>{message.chat.title}</b> to destination <code>{dest_chat_id}</code>. Error: {e}")
        await user_client.start()
        active_userbots[user_id] = user_client
        logger.info(f"Userbot started for {user_id}")
    except Exception as e:
        await save_session_string(user_id, None)
        await send_with_retry(app, user_id, f"⚠️ Failed to initialize your userbot session. Please try /add_session again. Error: {e}")

@app.on_raw_update()
async def initial_userbot_loader(client, update, users, chats):
    if not hasattr(client, "_userbots_loaded"):
        client._userbots_loaded = True
        async for user_data in users_collection.find({"session_string": {"$ne": None}}):
            asyncio.create_task(initialize_userbot_for_user(user_data["_id"], user_data["session_string"]))

# ---------- RUN ----------
app.run()
