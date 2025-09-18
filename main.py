import os
import threading
import asyncio
import logging
from flask import Flask
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram.enums import ParseMode
#from pyrogram.errors import UserNotParticipant, ChatAdminRequired, PeerIdInvalid, RPCError, FloodWait, BotBlocked, UserIsBot
from pyrogram.errors import UserNotParticipant, ChatAdminRequired, PeerIdInvalid, RPCError, FloodWait, UserIsBot


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- Flask healthcheck server ----------
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_flask).start()
# ---------- end Flask ----------

# ---------- MongoDB Client ----------
MONGO_DB_URL = os.environ.get("MONGO_DB_URL")
if not MONGO_DB_URL:
    print("Error: MONGO_DB_URL environment variable is not set. Exiting.")
    exit(1)

db_client = AsyncIOMotorClient(MONGO_DB_URL)
db = db_client.autoforward_db
users_collection = db.users  # Stores user-specific data (sources, destinations)

# ---------- Pyrogram client ----------
app = Client(
    "autoforward",
    api_id=int(os.environ["API_ID"]),
    api_hash=os.environ["API_HASH"],
    bot_token=os.environ["BOT_TOKEN"]
)

OWNER_ID = 5926160191  # আপনার Owner আইডি

# In-memory store for states
waiting_for_destiny = set()

# ---------- Helper Functions ----------
async def get_user_data(user_id):
    user_data = await users_collection.find_one({"_id": user_id})
    if not user_data:
        user_data = {"_id": user_id, "source_chat": None, "destination_chats": []}
        await users_collection.insert_one(user_data)
    return user_data

async def update_user_data(user_id, field, value):
    await users_collection.update_one({"_id": user_id}, {"$set": {field: value}}, upsert=True)

async def add_destination(user_id, chat_id):
    await users_collection.update_one({"_id": user_id}, {"$addToSet": {"destination_chats": chat_id}})

async def remove_destination(user_id, chat_id):
    await users_collection.update_one({"_id": user_id}, {"$pull": {"destination_chats": chat_id}})

# safe send with floodwait handling and limited concurrency
async def send_with_retry(client, chat_id, text, parse_mode="html", semaphore=None, retries=3):
    # semaphore to limit concurrent sends (avoid floods)
    if semaphore is None:
        semaphore = asyncio.Semaphore(10)
    async with semaphore:
        for attempt in range(retries):
            try:
                return await client.send_message(chat_id, text, parse_mode=parse_mode)
            except FloodWait as e:
                wait = e.x if hasattr(e, 'x') else getattr(e, 'value', 5)
                logger.warning(f"FloodWait: sleeping for {wait} seconds before retrying send to {chat_id}")
                await asyncio.sleep(wait + 1)
            except (BotBlocked, UserIsBot) as e:
                logger.info(f"Cannot send message to {chat_id}: {e}")
                return None
            except Exception as e:
                logger.exception(f"Failed to send message to {chat_id} on attempt {attempt+1}: {e}")
                await asyncio.sleep(1)
        return None

# safe copy_message with floodwait handling
async def copy_with_retry(client, chat_id, from_chat_id, message_id, semaphore=None, retries=3):
    if semaphore is None:
        semaphore = asyncio.Semaphore(5)
    async with semaphore:
        for attempt in range(retries):
            try:
                return await client.copy_message(chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id, disable_notification=True)
            except FloodWait as e:
                wait = e.x if hasattr(e, 'x') else getattr(e, 'value', 5)
                logger.warning(f"FloodWait: sleeping for {wait} seconds before retrying copy to {chat_id}")
                await asyncio.sleep(wait + 1)
            except Exception as e:
                logger.exception(f"Failed to copy message to {chat_id} on attempt {attempt+1}: {e}")
                await asyncio.sleep(1)
        return None
        
# ---------- START ----------
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
    await msg.reply_photo(
        photo="https://i.postimg.cc/fyrXmg6S/file-000000004e7461faaef2bd964cbbd408.png",
        caption=(
            f"👋 Hello {msg.from_user.mention},\n\n"
            "👋 Welcome!\n\nThis bot can automatically forward posts from one channel/group to another Channel/group\n\n"
            "⊰•─•─✦✗✦─•◈•─✦✗✦─•─•⊱\n"
            "⚡ Use the buttons below to navigate and get started!"
        ),
        reply_markup=InlineKeyboardMarkup(buttons)#,parse_mode=ParseMode.HTML
    )

@app.on_callback_query()
async def cb_handler(client, query):
    user_id = query.from_user.id
    if query.data == "close":
        try:
            await query.message.delete()
        except Exception:
            await query.answer("⚠️ Cannot delete message.", show_alert=True)
        return  # exit early
        
    elif query.data == "about_cmd":
        me = await client.get_me()
        about_message = f"""<b>⍟───[  <a href='https://t.me/PrimeXBots'>ᴍy ᴅᴇᴛᴀɪʟꜱ ʙy ᴘʀɪᴍᴇXʙᴏᴛs</a ]───⍟</b>

‣ ᴍʏ ɴᴀᴍᴇ : <a href=https://t.me/{me.username}>{me.first_name}</a>
‣ ᴍʏ ʙᴇsᴛ ғʀɪᴇɴᴅ : <a href='tg://settings'>ᴛʜɪs ᴘᴇʀsᴏɴ</a> 
‣ ᴅᴇᴠᴇʟᴏᴘᴇʀ : <a href='https://t.me/Prime_Nayem'>ᴍʀ.ᴘʀɪᴍᴇ</a> 
‣ ᴜᴘᴅᴀᴛᴇꜱ ᴄʜᴀɴɴᴇʟ : <a href='https://t.me/PrimeXBots'>ᴘʀɪᴍᴇXʙᴏᴛꜱ</a> 
‣ ᴍᴀɪɴ ᴄʜᴀɴɴᴇʟ : <a href='https://t.me/PrimeCineZone'>Pʀɪᴍᴇ Cɪɴᴇᴢᴏɴᴇ</a> 
‣ ѕᴜᴘᴘᴏʀᴛ ɢʀᴏᴜᴘ : <a href='https://t.me/Prime_Support_group'>ᴘʀɪᴍᴇ X ѕᴜᴘᴘᴏʀᴛ</a> 
‣ ᴅᴀᴛᴀ ʙᴀsᴇ : <a href='https://www.mongodb.com/'>ᴍᴏɴɢᴏ ᴅʙ</a> 
‣ ʙᴏᴛ sᴇʀᴠᴇʀ : <a href='https://heroku.com'>ʜᴇʀᴏᴋᴜ</a> 
‣ ʙᴜɪʟᴅ sᴛᴀᴛᴜs : ᴠ2.7.1 [sᴛᴀʙʟᴇ]>"""
        await query.message.edit_text(about_message, disable_web_page_preview=True, parse_mode=ParseMode.HTML)

    elif query.data == "help_cmd":
        await query.message.edit_text(
            "📝 How to use:\n"
            "1️⃣ /set_source → Set source channel\n"
            "2️⃣ /set_destiny → Set destination channel/group\n"
            "3️⃣ /show_destiny → Show and manage destinations\n"
            "4️⃣ /show_source → Show current source & remove\n\n"
            "After setup, any post in source will be forwarded automatically to destinations.",
            parse_mode=ParseMode.HTML
        )

    elif query.data.startswith("show_dest_info_"):
        chat_id = int(query.data.split("_")[-1])
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

    elif query.data == "show_dest_list":
        await show_destiny_list(client, query.message, edit_message=True)
        
    elif query.data.startswith("del_dest_confirm_"):
        chat_id = int(query.data.split("_")[-1])
        await remove_destination(user_id, chat_id)

    # chat_info নিয়ে আসা
        try:
            chat_info = await client.get_chat(chat_id)
            chat_name = chat_info.title
        except Exception:
            chat_name = str(chat_id)  # fallback যদি চ্যানেল info fetch না হয়

        await query.answer(f"Destination {chat_name} removed!", show_alert=True)

    # custom_text দিয়ে লিস্ট দেখানো হবে
        custom_text = f"✅ Destination removed: <b>{chat_name}</b>"
        await show_destiny_list(client, query.message, edit_message=True, custom_text=custom_text)
    
    
    elif query.data == "del_source_confirm":
        await update_user_data(user_id, "source_chat", None)
        await query.message.edit_text("✅ Source removed.", parse_mode=ParseMode.HTML)

# ---------- SET SOURCE / DESTINY ----------
@app.on_message(filters.command("set_source") & filters.private)
async def set_source(client, message):
    await message.reply_text(
        "📢 Please forward a message from your source channel here.\n\n⚠️ Bot must be admin in that channel.",
        parse_mode=ParseMode.HTML
    )

@app.on_message(filters.command("set_destiny") & filters.private)
async def set_destiny(client, message):
    waiting_for_destiny.add(message.from_user.id)
    await message.reply_text(
        "🎯 Please forward a message from your destination channel/group.\n\n⚠️ Bot must be admin there.",
        parse_mode=ParseMode.HTML
    )

# ---------- CATCH FORWARDED ----------
@app.on_message(filters.forwarded & filters.private)
async def catch_forwarded(client, message):
    user_id = message.from_user.id
    if not message.forward_from_chat:
        return await message.reply_text("⚠️ Forwarded message must be from a channel/group.", parse_mode=ParseMode.HTML)
    chat = message.forward_from_chat
    try:
        try:
            await client.get_chat_member(chat.id, client.me.id)
        except UserNotParticipant:
            return await message.reply_text(f"⚠️ Bot is not a member of {chat.title} (ID: <code>{chat.id}</code>). Please add me first.", parse_mode=ParseMode.HTML)
        except ChatAdminRequired:
            return await message.reply_text(f"⚠️ Bot needs to be admin in {chat.title} (ID: <code>{chat.id}</code>). Please promote me.", parse_mode=ParseMode.HTML)
        except PeerIdInvalid:
            return await message.reply_text(f"⚠️ Invalid chat ID for {chat.title}.", parse_mode=ParseMode.HTML)
        except RPCError as e:
            return await message.reply_text(f"⚠️ Telegram API error: {e}", parse_mode=ParseMode.HTML)

        chat_info = await client.get_chat(chat.id)

        if user_id in waiting_for_destiny:
            user_data = await get_user_data(user_id)
            if chat.id not in user_data["destination_chats"]:
                await add_destination(user_id, chat.id)
                await message.reply_text(f"✅ Destination set: {chat_info.title}", parse_mode=ParseMode.HTML)
            else:
                await message.reply_text(f"ℹ️ This destination is already added: {chat_info.title}", parse_mode=ParseMode.HTML)
            waiting_for_destiny.discard(user_id)
        else:
            await update_user_data(user_id, "source_chat", chat.id)
            await message.reply_text(f"✅ Source channel set: {chat_info.title}", parse_mode=ParseMode.HTML)

    except Exception as e:
        waiting_for_destiny.discard(user_id)
        await message.reply_text(f"⚠️ Error: {e}", parse_mode=ParseMode.HTML)

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
        # যদি custom_text থাকে, সেটি দেখাবে; না হলে default দেখাবে
        text = custom_text or "⚠️ No destinations set. Use /set_destiny to add one."
        if edit_message:
            await message.edit_text(text, parse_mode=ParseMode.HTML)
        else:
            await message.reply_text(text, parse_mode=ParseMode.HTML)

@app.on_message(filters.command("show_destiny") & filters.private)
async def show_destiny_command(client, message):
    await show_destiny_list(client, message)

@app.on_message(filters.command("show_source") & filters.private)
async def show_source(client, message):
    user_data = await get_user_data(message.from_user.id)
    src = user_data.get("source_chat")
    if src:
        try:
            chat = await client.get_chat(src)
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Remove Source", callback_data="del_source_confirm")]
            ])
            await message.reply_text(f"📢 Current source: {chat.title}", reply_markup=buttons, parse_mode=ParseMode.HTML)
        except Exception as e:
            await message.reply_text(f"⚠️ Current source ({src}) is inaccessible. Error: {e}", parse_mode=ParseMode.HTML)
    else:
        await message.reply_text("⚠️ No source set. Use /set_source to add one.", parse_mode=ParseMode.HTML)

# ---------- STATUS & BROADCAST ----------
@app.on_message(filters.command("stats") & filters.user(OWNER_ID))
async def status_cmd(client, message):
    total_users = await users_collection.count_documents({})
    total_sources = await users_collection.count_documents({"source_chat": {"$ne": None}})
    pipeline = [{"$unwind": "$destination_chats"},
                {"$group": {"_id": None, "total": {"$sum": 1}}}]
    dest_agg = await users_collection.aggregate(pipeline).to_list(None)
    total_destinations = dest_agg[0]["total"] if dest_agg else 0
    await message.reply_text(
        f"👤 Total Users: <b>{total_users}</b>\n"
        f"📢 Sources Set: <b>{total_sources}</b>\n"
        f"🎯 Destinations Added: <b>{total_destinations}</b>",
        parse_mode=ParseMode.HTML
    )

@app.on_message(filters.command("broadcast") & filters.user(OWNER_ID))
async def broadcast_cmd(client, message):
    # robust broadcast with concurrency limit and flood-wait handling
    if len(message.command) < 2:
        return await message.reply_text("Usage: /broadcast your message")
    text = message.text.split(" ", 1)[1]

    sem = asyncio.Semaphore(10)  # max concurrent sends
    count = 0
    failed = 0
    tasks = []

    async for user_data in users_collection.find({}):
        uid = user_data.get("_id")
        if not isinstance(uid, int):
            # try to coerce
            try:
                uid = int(uid)
            except:
                continue
        tasks.append(asyncio.create_task(send_with_retry(client, uid, text, semaphore=sem)))

    if not tasks:
        return await message.reply_text("ℹ️ No users found in database to broadcast.")

    results = await asyncio.gather(*tasks)
    for r in results:
        if r is None:
            failed += 1
        else:
            count += 1

    await message.reply_text(f"✅ Broadcast sent to {count} users. Failed: {failed}")

# ---------- FORWARDER ----------
@app.on_message(filters.channel)
async def forward_message(client, message):
    # limit concurrency for copies
    sem = asyncio.Semaphore(5)
    async for user_data in users_collection.find({"source_chat": message.chat.id}):
        user_id = user_data["_id"]
        destinations = user_data.get("destination_chats", [])
        for dest_chat_id in destinations:
            try:
                await copy_with_retry(client, dest_chat_id, message.chat.id, message.id, semaphore=sem)
            except Exception as e:
                try:
                    await client.send_message(user_id, f"⚠️ Could not forward to destination (ID: <code>{dest_chat_id}</code>). Error: {e}", parse_mode=ParseMode.HTML)
                except Exception:
                    pass

# ---------- STARTUP CHECKS ----------
async def startup_checks(client):
    # verify bot is still member/admin in stored chats and report to owner if issues
    logger.info("Running startup checks for stored chats...")
    bad_chats = []
    checked = set()
    async for user_data in users_collection.find({}):
        src = user_data.get("source_chat")
        dests = user_data.get("destination_chats", [])
        candidates = []
        if src:
            candidates.append(src)
        candidates.extend(dests)
        for chat_id in candidates:
            if chat_id in checked:
                continue
            checked.add(chat_id)
            try:
                await client.get_chat_member(chat_id, client.me.id)
            except Exception as e:
                logger.warning(f"Bot not member/admin or cannot access chat {chat_id}: {e}")
                bad_chats.append((chat_id, str(e)))

    if bad_chats:
        text = "⚠️ Startup check found chats where bot may not have access or admin rights:\n"
        for cid, err in bad_chats[:50]:
            text += f"• ID {cid}: {err}\n"
        try:
            await client.send_message(OWNER_ID, text)
        except Exception:
            logger.exception("Could not send startup report to owner")
    else:
        logger.info("Startup checks passed: bot has access to stored chats.")

# ---------- RUN ----------
app.run()
