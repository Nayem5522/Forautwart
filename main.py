import os
import threading
from flask import Flask
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from motor.motor_asyncio import AsyncIOMotorClient

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

# In-memory store for states (to avoid frequent DB calls for temporary states)
waiting_for_destiny = set()
# waiting_for_source = set() # Not strictly needed as set_source immediately expects a forward

# ---------- Helper Functions for DB Operations ----------
async def get_user_data(user_id):
    user_data = await users_collection.find_one({"_id": user_id})
    if not user_data:
        # Initialize if not found
        user_data = {"_id": user_id, "source_chat": None, "destination_chats": []}
        await users_collection.insert_one(user_data)
    return user_data

async def update_user_data(user_id, field, value):
    await users_collection.update_one({"_id": user_id}, {"$set": {field: value}}, upsert=True)

async def add_destination(user_id, chat_id):
    await users_collection.update_one({"_id": user_id}, {"$addToSet": {"destination_chats": chat_id}})

async def remove_destination(user_id, chat_id):
    await users_collection.update_one({"_id": user_id}, {"$pull": {"destination_chats": chat_id}})

# ---------- START ----------
@app.on_message(filters.command("start") & filters.private)
async def start(client, message):
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("ℹ️ About", callback_data="about_cmd")],
        [InlineKeyboardButton("📖 Help", callback_data="help_cmd")]
    ])
    await message.reply_text(
        "👋 Welcome!\n\nThis bot can automatically forward posts from one channel/group to another.",
        reply_markup=buttons
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
        await query.message.edit_text(about_message, disable_web_page_preview=True)
    elif query.data == "help_cmd":
        await query.message.edit_text(
            "📝 How to use:\n"
            "1️⃣ /set_source → Set source channel\n"
            "2️⃣ /set_destiny → Set destination channel/group\n"
            "3️⃣ /show_destiny → Show and manage destinations\n" # Updated for new functionality
            "4️⃣ /show_source → Show current source\n"
            "5️⃣ /del_source → Delete source\n\n"
            "After setup, any post in source will be forwarded automatically to destinations."
        )
    elif query.data.startswith("show_dest_info_"):
        chat_id = int(query.data.split("_")[-1])
        try:
            chat = await client.get_chat(chat_id)
            invite_link = chat.invite_link if chat.invite_link else "No invite link available."
            
            text = f"🎯 <b>Destination Details:</b>\n" \
                   f"• <b>Name:</b> {chat.title}\n" \
                   f"• <b>ID:</b> <code>{chat.id}</code>\n" \
                   f"• <b>Type:</b> {chat.type.capitalize()}\n" \
                   f"• <b>Invite Link:</b> {invite_link}\n\n" \
                   f"<i>Are you sure you want to remove this destination?</i>"
            
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel this destination", callback_data=f"del_dest_confirm_{chat_id}")],
                [InlineKeyboardButton("🔙 Back to Destinations", callback_data="show_dest_list")]
            ])
            await query.message.edit_text(text, reply_markup=buttons, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            await query.message.edit_text(f"⚠️ Error fetching chat info: {e}\nPerhaps the bot was removed from this chat?")
    
    elif query.data == "show_dest_list":
        await show_destiny_list(client, query.message, edit_message=True)

    elif query.data.startswith("del_dest_confirm_"):
        chat_id = int(query.data.split("_")[-1])
        await remove_destination(user_id, chat_id)
        await query.answer(f"Destination {chat_id} removed!", show_alert=True)
        await show_destiny_list(client, query.message, edit_message=True) # Refresh the list

# ---------- SET SOURCE ----------
@app.on_message(filters.command("set_source") & filters.private)
async def set_source(client, message):
    await message.reply_text(
        "📢 Please forward a message from your source channel here.\n\n"
        "⚠️ Bot must be admin in that channel."
    )

# ---------- SET DESTINY ----------
@app.on_message(filters.command("set_destiny") & filters.private)
async def set_destiny(client, message):
    waiting_for_destiny.add(message.from_user.id)
    await message.reply_text(
        "🎯 Please forward a message from your destination channel/group.\n\n"
        "⚠️ Bot must be admin there."
    )

# ---------- CATCH FORWARDED (source or destination) ----------
@app.on_message(filters.forwarded & filters.private)
async def catch_forwarded(client, message):
    user_id = message.from_user.id
    if not message.forward_from_chat:
        return await message.reply_text("⚠️ Forwarded message must be from a channel/group.")
    chat = message.forward_from_chat

    try:
        # Check if bot has access and is admin (optional, but good practice for destinations)
        member = await client.get_chat_member(chat.id, client.me.id)
        if member.status not in ["administrator", "creator"]:
             # For private channels, even if not admin, get_chat might work but forwarding will fail.
             # For public channels, if not admin, get_chat might work. Best to check for actual forwarding capability.
             # Let's assume get_chat itself is enough to determine basic access for now.
             pass 

        chat_info = await client.get_chat(chat.id) # This confirms bot has access to fetch info

        if user_id in waiting_for_destiny:
            # Destination mode
            user_data = await get_user_data(user_id)
            if chat.id not in user_data["destination_chats"]:
                await add_destination(user_id, chat.id)
                await message.reply_text(f"✅ Destination set: {chat_info.title}")
            else:
                await message.reply_text(f"ℹ️ This destination is already added: {chat_info.title}")
            waiting_for_destiny.discard(user_id)
        else:
            # Source mode
            await update_user_data(user_id, "source_chat", chat.id)
            await message.reply_text(f"✅ Source channel set: {chat_info.title}")

    except Exception as e:
        if user_id in waiting_for_destiny:
            waiting_for_destiny.discard(user_id)
        await message.reply_text(f"⚠️ Bot is not admin or cannot access that chat.\nError: {e}")

# ---------- SHOW / DELETE (Updated) ----------
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
                # If bot lost access to chat, it will show as unknown
                buttons.append([InlineKeyboardButton(f"Unknown Chat ({d_chat_id})", callback_data=f"show_dest_info_{d_chat_id}")])
        
        reply_markup = InlineKeyboardMarkup(buttons)
        if edit_message:
            await message.edit_text(text, reply_markup=reply_markup)
        else:
            await message.reply_text(text, reply_markup=reply_markup)
    else:
        if edit_message:
            await message.edit_text("⚠️ No destinations set.")
        else:
            await message.reply_text("⚠️ No destinations set. Use /set_destiny to add one.")

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
            await message.reply_text(f"📢 Current source: {chat.title}")
        except Exception:
            await message.reply_text(f"⚠️ Current source ({src}) is inaccessible. Please /del_source and /set_source again.")
    else:
        await message.reply_text("⚠️ No source set. Use /set_source to add one.")

@app.on_message(filters.command("del_source") & filters.private)
async def del_source(client, message):
    user_data = await get_user_data(message.from_user.id)
    if user_data.get("source_chat"):
        await update_user_data(message.from_user.id, "source_chat", None)
        await message.reply_text("✅ Source removed.")
    else:
        await message.reply_text("⚠️ No source to remove.")

# Removed @app.on_message(filters.command("del_destiny"))
# Removed @app.on_callback_query(filters.regex(r"del_(\-?\d+)")) - integrated into show_destiny_list logic

# ---------- FORWARDER ----------
@app.on_message(filters.channel)
async def forward_message(client, message):
    # Iterate through all users who have set this channel as source
    async for user_data in users_collection.find({"source_chat": message.chat.id}):
        user_id = user_data["_id"]
        destinations = user_data.get("destination_chats", [])

        for dest_chat_id in destinations:
            try:
                # Using copy_message to avoid "Forwarded from" tag
                await client.copy_message(
                    chat_id=dest_chat_id,
                    from_chat_id=message.chat.id,
                    message_id=message.id,
                    disable_notification=True # Optional: Send silently
                )
            except Exception as e:
                # If error, try to notify the user
                try:
                    await client.send_message(user_id, f"⚠️ Could not forward message from {message.chat.title} to {dest_chat_id} (ID: <code>{dest_chat_id}</code>). Error: {e}", parse_mode="HTML")
                except Exception as notify_e:
                    # If even notification fails, log it or print to console
                    print(f"Failed to notify user {user_id} about forwarding error to {dest_chat_id}. Error: {notify_e}")

app.run()
