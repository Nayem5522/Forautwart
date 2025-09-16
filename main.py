import os
import threading
from flask import Flask

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

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

# ---------- Pyrogram client ----------
app = Client(
    "autoforward",
    api_id=int(os.environ["API_ID"]),
    api_hash=os.environ["API_HASH"],
    bot_token=os.environ["BOT_TOKEN"]
)

# Data stores
user_sources = {}       # {user_id: chat_id}
user_destinations = {}  # {user_id: [chat_ids]}
waiting_for_destiny = set()

# ---------- START ----------
@app.on_message(filters.command("start") & filters.private)
async def start(client, message):
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("ℹ️ About", callback_data="about")],
        [InlineKeyboardButton("📖 Help", callback_data="help")]
    ])
    await message.reply_text(
        "👋 Welcome!\n\nThis bot can automatically forward posts from one channel/group to another.",
        reply_markup=buttons
    )

@app.on_callback_query()
async def cb_handler(client, query):
    if query.data == "about":
        await query.message.edit_text("ℹ️ This bot forwards messages automatically from your source to destination channels/groups.")
    elif query.data == "help":
        await query.message.edit_text(
            "📝 How to use:\n"
            "1️⃣ /set_source → Set source channel\n"
            "2️⃣ /set_destiny → Set destination channel/group\n"
            "3️⃣ /show_source → Show current source\n"
            "4️⃣ /show_destiny → Show destinations\n"
            "5️⃣ /del_destiny → Delete a destination\n"
            "6️⃣ /del_source → Delete source\n\n"
            "After setup, any post in source will be forwarded automatically to destinations."
        )

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
        # If we can get chat info, bot has access
        chat_info = await client.get_chat(chat.id)

        if user_id in waiting_for_destiny:
            # destination mode
            user_destinations.setdefault(user_id, [])
            if chat.id not in user_destinations[user_id]:
                user_destinations[user_id].append(chat.id)
            await message.reply_text(f"✅ Destination set: {chat_info.title}")
            waiting_for_destiny.discard(user_id)
        else:
            # source mode
            user_sources[user_id] = chat.id
            await message.reply_text(f"✅ Source channel set: {chat_info.title}")

    except Exception:
        if user_id in waiting_for_destiny:
            waiting_for_destiny.discard(user_id)
        await message.reply_text("⚠️ Bot is not admin or cannot access that chat.")

# ---------- SHOW / DELETE ----------
@app.on_message(filters.command("show_source") & filters.private)
async def show_source(client, message):
    src = user_sources.get(message.from_user.id)
    if src:
        chat = await client.get_chat(src)
        await message.reply_text(f"📢 Current source: {chat.title}")
    else:
        await message.reply_text("⚠️ No source set.")

@app.on_message(filters.command("del_source") & filters.private)
async def del_source(client, message):
    if message.from_user.id in user_sources:
        user_sources.pop(message.from_user.id)
        await message.reply_text("✅ Source removed.")
    else:
        await message.reply_text("⚠️ No source to remove.")

@app.on_message(filters.command("show_destiny") & filters.private)
async def show_destiny(client, message):
    dests = user_destinations.get(message.from_user.id, [])
    if dests:
        text = "🎯 Destinations:\n"
        for d in dests:
            chat = await client.get_chat(d)
            text += f"• {chat.title}\n"
        await message.reply_text(text)
    else:
        await message.reply_text("⚠️ No destinations set.")

@app.on_message(filters.command("del_destiny") & filters.private)
async def del_destiny(client, message):
    dests = user_destinations.get(message.from_user.id, [])
    if not dests:
        return await message.reply_text("⚠️ No destinations to delete.")
    buttons = []
    for d in dests:
        chat = await client.get_chat(d)
        buttons.append([InlineKeyboardButton(chat.title, callback_data=f"del_{d}")])
    await message.reply_text(
        "Select a destination to remove:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

@app.on_callback_query(filters.regex(r"del_(\-?\d+)"))
async def del_destiny_cb(client, query):
    user_id = query.from_user.id
    chat_id = int(query.data.split("_", 1)[1])
    if chat_id in user_destinations.get(user_id, []):
        user_destinations[user_id].remove(chat_id)
        await query.message.edit_text("✅ Destination removed.")
    else:
        await query.message.edit_text("⚠️ Destination not found.")

# ---------- FORWARDER ----------
@app.on_message(filters.channel)
async def forward_message(client, message):
    for user_id, source in user_sources.items():
        if source == message.chat.id:
            destinations = user_destinations.get(user_id, [])
            for dest in destinations:
                try:
                    await message.forward(dest)
                except Exception as e:
                    try:
                        await client.send_message(user_id, f"⚠️ Could not forward to {dest}: {e}")
                    except:
                        pass

app.run()
