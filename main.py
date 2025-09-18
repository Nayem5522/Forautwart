import os
import threading
import asyncio
import logging
from flask import Flask
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram.enums import ParseMode, ChatMemberStatus, ChatType # Import ChatType
from pyrogram.errors import UserNotParticipant, ChatAdminRequired, PeerIdInvalid, RPCError, FloodWait, UserIsBot # Removed BotBlocked


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
            except UserIsBot as e: # Now only catching UserIsBot
                logger.info(f"Cannot send message to {chat_id}: {e} (User blocked bot or is a bot)")
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
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("ℹ️ About", callback_data="about_cmd")],
        [InlineKeyboardButton("📖 Help", callback_data="help_cmd")]
    ])
    await message.reply_text(
        "👋 Welcome!\n\nThis bot can automatically forward posts from one channel/group to another.",
        reply_markup=buttons,
        parse_mode=ParseMode.HTML
    )

@app.on_callback_query()
async def cb_handler(client, query):
    user_id = query.from_user.id
    if query.data == "about_cmd":
        me = await client.get_me()
        about_message = f"""<b><blockquote>⍟───[  <a href='https://t.me/PrimeXBots'>MY ᴅᴇᴛᴀɪʟꜱ ʙy ᴘʀɪᴍᴇXʙᴏᴛs</a ]───⍟</blockquote>

‣ ᴍʏ ɴᴀᴍᴇ : <a href=https://t.me/{me.username}>{me.first_name}</a>
‣ ᴍʏ ʙᴇsᴛ ғʀɪᴇɴᴅ : <a href='tg://settings'>ᴛʜɪs ᴘᴇʀsᴏɴ</a> 
‣ ᴅᴇᴠᴇʟᴏᴘᴇʀ : <a href='https://t.me/Prime_Nayem'>ᴍʀ.ᴘʀɪᴍᴇ</a> 
‣ ᴜᴘᴅᴀᴛᴇꜱ ᴄʜᴀɴɴᴇʟ : <a href='https://t.me/PrimeXBots'>ᴘʀɪᴍᴇXʙᴏᴛꜱ</a> 
‣ ᴍᴀɪɴ ᴄʜᴀɴɴᴇʟ : <a href='https://t.me/PrimeCineZone'>Pʀɪᴍᴇ Cɪɴᴇᴢᴏɴᴇ</a> 
‣ ѕᴜᴘᴘᴏʀᴛ ɢʀᴏᴜᴘ : <a href='https://t.me/Prime_Support_group'>ᴘʀɪᴍᴇ X ѕᴜᴘᴘᴏʀᴛ</a> 
‣ ᴅᴀᴛᴀ ʙᴀsᴇ : <a href='https://www.mongodb.com/'>ᴍᴏɴɢᴏ ᴅʙ</a> 
‣ ʙᴏᴛ sᴇʀᴠᴇʀ : <a href='https://heroku.com'>ʜᴇʀᴏᴋᴜ</a> 
‣ ʙᴜɪʟᴅ sᴛᴀᴛᴜs : ᴠ2.7.1 [sᴛᴀʙʟᴇ]></b>"""
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
                [InlineKeyboardButton("❌ Remove this destination", callback_data=f"del_dest_confirm_{chat_id}")],
                [InlineKeyboardButton("🔙 Back to Destinations", callback_data="show_dest_list")]
            ])
            await query.message.edit_text(text, reply_markup=buttons, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception as e:
            # If chat info can't be fetched, maybe it's gone or bot was removed. Offer to remove it.
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Remove inaccessible destination", callback_data=f"del_dest_confirm_{chat_id}")],
                [InlineKeyboardButton("🔙 Back to Destinations", callback_data="show_dest_list")]
            ])
            await query.message.edit_text(f"⚠️ Error fetching chat info for <code>{chat_id}</code>: {e}\nIt might be inaccessible. Do you want to remove it?", reply_markup=buttons, parse_mode=ParseMode.HTML)


    elif query.data == "show_dest_list":
        # Simply call show_destiny_list, it will handle editing the message appropriately
        await show_destiny_list(client, query.message, edit_message=True)

    elif query.data.startswith("del_dest_confirm_"):
        chat_id = int(query.data.split("_")[-1])
        removed_chat_name = f"<code>{chat_id}</code>" # Default name if we can't get chat info
        try:
            chat = await client.get_chat(chat_id)
            removed_chat_name = chat.title
        except Exception:
            pass # Ignore error, we can still remove by ID

        await remove_destination(user_id, chat_id)
        await query.answer(f"Destination {removed_chat_name} removed!", show_alert=True)
        # After removal, re-display the destination list with an appropriate message
        await query.message.edit_text(f"✅ Destination removed: <b>{removed_chat_name}</b>", parse_mode=ParseMode.HTML)
        # Then, show the updated list
        await show_destiny_list(client, query.message, edit_message=True)


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
            chat_member = await client.get_chat_member(chat.id, client.me.id)

            # Check bot's status and permissions
            if chat_member.status == ChatMemberStatus.ADMINISTRATOR:
                if not chat_member.can_post_messages:
                    return await message.reply_text(f"⚠️ Bot is an admin in {chat.title} (ID: <code>{chat.id}</code>) but does not have 'Post Messages' permission. Please enable it.", parse_mode=ParseMode.HTML)
            elif chat_member.status == ChatMemberStatus.MEMBER:
                # Members can post in groups by default, but not in channels.
                # If it's a channel, members can't post.
                if chat.type == ChatType.CHANNEL:
                    return await message.reply_text(f"⚠️ Bot is a member of {chat.title} (ID: <code>{chat.id}</code>) but cannot post in a channel. Please promote me to admin.", parse_mode=ParseMode.HTML)
            elif chat_member.status == ChatMemberStatus.RESTRICTED:
                if not chat_member.can_send_messages: # For restricted, check can_send_messages
                    return await message.reply_text(f"⚠️ Bot is restricted in {chat.title} (ID: <code>{chat.id}</code>) and cannot send messages. Please unrestrict or promote me.", parse_mode=ParseMode.HTML)
            else: # Banned, Left, Kicked, etc.
                return await message.reply_text(f"⚠️ Bot's status in {chat.title} (ID: <code>{chat.id}</code>) is {chat_member.status.value}. I need to be an admin or a member with posting rights.", parse_mode=ParseMode.HTML)


        except UserNotParticipant:
            return await message.reply_text(f"⚠️ Bot is not a member of {chat.title} (ID: <code>{chat.id}</code>). Please add me first.", parse_mode=ParseMode.HTML)
        except ChatAdminRequired:
            # This error typically means the bot needs to be admin to even *see* members in private groups/channels
            return await message.reply_text(f"⚠️ Bot needs to be admin in {chat.title} (ID: <code>{chat.id}</code>) to check its permissions. Please promote me.", parse_mode=ParseMode.HTML)
        except PeerIdInvalid:
            return await message.reply_text(f"⚠️ Invalid chat ID for {chat.title}.", parse_mode=ParseMode.HTML)
        except RPCError as e:
            return await message.reply_text(f"⚠️ Telegram API error: {e}", parse_mode=ParseMode.HTML)

        chat_info = await client.get_chat(chat.id)

        if user_id in waiting_for_destiny:
            user_data = await get_user_data(user_id)
            if chat.id not in user_data["destination_chats"]:
                await add_destination(user_id, chat.id)
                await message.reply_text(f"✅ Destination set: <b>{chat_info.title}</b>", parse_mode=ParseMode.HTML)
            else:
                await message.reply_text(f"ℹ️ This destination is already added: <b>{chat_info.title}</b>", parse_mode=ParseMode.HTML)
            waiting_for_destiny.discard(user_id)
        else:
            await update_user_data(user_id, "source_chat", chat.id)
            await message.reply_text(f"✅ Source channel set: <b>{chat_info.title}</b>", parse_mode=ParseMode.HTML)

    except Exception as e:
        waiting_for_destiny.discard(user_id)
        await message.reply_text(f"⚠️ Error: {e}", parse_mode=ParseMode.HTML)

# ---------- SHOW DESTINY LIST ----------
async def show_destiny_list(client, message, edit_message=False):
    user_data = await get_user_data(message.from_user.id)
    dests = user_data.get("destination_chats", [])
    if dests:
        text = "🎯 Select a destination to manage:\n"
        buttons = []
        for d_chat_id in dests:
            try:
                chat = await client.get_chat(d_chat_id)
                buttons.append([InlineKeyboardButton(chat.title, callback_data=f"show_dest_info_{d_chat_id}")])
            except Exception:
                # If we can't get chat info, show its ID
                buttons.append([InlineKeyboardButton(f"Unknown Chat (<code>{d_chat_id}</code>)", callback_data=f"show_dest_info_{d_chat_id}")])
        reply_markup = InlineKeyboardMarkup(buttons)
        if edit_message:
            await message.edit_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        else:
            await message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    else:
        text = "⚠️ No destinations set. Use /set_destiny to add one."
        # Always remove buttons if no destinations
        if edit_message:
            await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([])) # Clear buttons
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
            await message.reply_text(f"📢 Current source: <b>{chat.title}</b>", reply_markup=buttons, parse_mode=ParseMode.HTML)
        except Exception as e:
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Remove inaccessible source", callback_data="del_source_confirm")]
            ])
            await message.reply_text(f"⚠️ Current source (<code>{src}</code>) is inaccessible. Error: {e}\nDo you want to remove it?", reply_markup=buttons, parse_mode=ParseMode.HTML)
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

    # Iterate through users_collection and ensure _id is int
    async for user_data in users_collection.find({}):
        uid = user_data.get("_id")
        if isinstance(uid, str): # Try to convert string IDs to int
            try:
                uid = int(uid)
            except ValueError:
                logger.warning(f"Invalid user ID found in DB: {user_data.get('_id')}. Skipping.")
                continue # Skip this user if ID is not a valid integer

        if isinstance(uid, int): # Ensure it's an int before adding to tasks
            tasks.append(asyncio.create_task(send_with_retry(client, uid, text, semaphore=sem)))
        else:
            logger.warning(f"User ID {uid} is not an integer. Skipping broadcast to this user.")


    if not tasks:
        return await message.reply_text("ℹ️ No users found in database to broadcast.")

    # Show initial broadcast message
    broadcast_msg = await message.reply_text("🚀 Starting broadcast...")

    results = await asyncio.gather(*tasks)
    for r in results:
        if r is None:
            failed += 1
        else:
            count += 1

    await broadcast_msg.edit_text(f"✅ Broadcast sent to {count} users. Failed: {failed}")

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
                    # Log the error and notify the user (owner)
                    logger.error(f"Failed to forward message {message.id} from {message.chat.id} to {dest_chat_id} for user {user_id}. Error: {e}")
                    # Only send a specific notification if the error is significant and not just a normal failure
                    # For example, if the bot was removed from the destination chat
                    if "CHAT_WRITE_FORBIDDEN" in str(e) or "USER_BLOCKED" in str(e) or "USER_IS_BOT" in str(e): # Updated error checks for notification
                        await client.send_message(user_id, f"⚠️ Could not forward to destination (ID: <code>{dest_chat_id}</code> - Name: {dest_chat_id if isinstance(dest_chat_id, str) else 'Unknown'}). Bot might not have access or admin rights anymore, or bot was blocked. Error: {e}", parse_mode=ParseMode.HTML)
                except Exception as inner_e:
                    logger.exception(f"Failed to notify user {user_id} about forwarding error: {inner_e}")


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
                chat_member = await client.get_chat_member(chat_id, client.me.id)
                # Check bot's status and permissions
                if chat_member.status == ChatMemberStatus.ADMINISTRATOR:
                    if not chat_member.can_post_messages:
                        bad_chats.append((chat_id, "Bot is an admin but does not have 'Post Messages' permission."))
                elif chat_member.status == ChatMemberStatus.MEMBER:
                    # For channels, being a member is not enough to post
                    chat_info = await client.get_chat(chat_id) # Need chat info to check type
                    if chat_info.type == ChatType.CHANNEL:
                        bad_chats.append((chat_id, "Bot is a member but cannot post in a channel. Needs admin rights."))
                elif chat_member.status == ChatMemberStatus.RESTRICTED:
                    if not chat_member.can_send_messages: # For restricted, check can_send_messages
                        bad_chats.append((chat_id, "Bot is restricted and cannot send messages."))
                else: # Banned, Left, Kicked, etc.
                    bad_chats.append((chat_id, f"Bot's status is {chat_member.status.value}. Needs to be admin or a member with posting rights."))

            except UserNotParticipant:
                bad_chats.append((chat_id, "Bot is not a member of this chat."))
            except ChatAdminRequired:
                bad_chats.append((chat_id, "Bot needs to be admin to check membership in this private chat."))
            except Exception as e:
                logger.warning(f"Bot cannot access chat {chat_id}: {e}")
                bad_chats.append((chat_id, str(e)))

    if bad_chats:
        text = "⚠️ <b>Startup check found issues with stored chats:</b>\n"
        for cid, err in bad_chats[:50]: # Limit report size
            text += f"• ID <code>{cid}</code>: {err}\n"
        if len(bad_chats) > 50:
            text += f"• ...and {len(bad_chats) - 50} more issues.\n"
        text += "\nPlease review these chats and update your source/destinations."
        try:
            await client.send_message(OWNER_ID, text, parse_mode=ParseMode.HTML)
        except Exception:
            logger.exception("Could not send startup report to owner")
    else:
        logger.info("Startup checks passed: bot has access to stored chats.")

# ---------- RUN ----------
app.start()
                    
