import os
import re
import time
import logging
import random
import shlex
import secrets
import pytz
import shutil
import asyncio
import traceback
import httpx
from typing import Tuple
from os.path import join
from verify import send_verification_message, is_user_verified
from verify import complete_verification
from datetime import datetime, timedelta
from hachoir.metadata import extractMetadata
from hachoir.parser import createParser
from pyrogram import Client, filters
from verify_api import tokens
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
import config
from config import (
    ENABLE_SHORTLINK,
    WORKING_GROUP,
    AUTH_USERS,
    SHORTLINK_URL,
    SHORTLINK_API,
    VERIFICATION_EXPIRY_SECONDS,
)

logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger(__name__)

rvbot = Client("recorder", bot_token=config.BOT_TOKEN, api_id=config.API_ID, api_hash=config.API_HASH)

user_status = {}
user_tasks = {}

STATUS_PAGE_SIZE = 5

async def unauthorized_access(message: Message):
    await message.reply_text(
        f"❌ You cannot access the bot.\n"
        f"Access is allowed only in this group: {config.GROUP_LINK}"
    )

async def cancel_single_task(task_id: int):
    task = None
    user_id = user_tasks.get(task_id)
    if not user_id:
        return

    # Remove task from user_status
    if user_id in user_status:
        tasks = user_status[user_id]
        for t in tasks:
            if t["id"] == task_id:
                task = t
                break
        user_status[user_id] = [t for t in tasks if t["id"] != task_id]
        if not user_status[user_id]:
            user_status.pop(user_id)
    user_tasks.pop(task_id, None)

    if not task:
        return

    # Stop ffmpeg process
    process = task.get("process")
    if process and process.returncode is None:
        try:
            process.terminate()
            await asyncio.sleep(1)
            if process.returncode is None:
                process.kill()
        except Exception as e:
            LOG.warning(f"[Cancel] Error terminating process: {e}")

    file_path = task.get("output")
    if file_path and os.path.exists(file_path):
        try:
            chat_id = task.get("chat_id") or user_id
            username = task.get("username") or "anonymous"
            date = task.get("Date", "Unknown Date")
            start = task.get("start_time", "Unknown Start")
            end = task.get("end_time", "Unknown End")

            caption = (
                f"🛑 Recording cancelled by admin.\n"
                f"👤 User: @{username}\n"
                f"📁 File: `{os.path.basename(file_path)}`\n"
                f"📅 Date: {date}\n"
                f"⏱ Time: {start} to {end}"
        )

            await rvbot.send_video(
                chat_id=chat_id,
                video=file_path,
                caption=caption
            )
        except Exception as e:
            LOG.warning(f"[Cancel] Failed to send video: {e}")

    folder = task.get("folder")
    if folder and os.path.exists(folder):
        try:
            shutil.rmtree(folder)
        except Exception as e:
            LOG.warning(f"[Cancel] Failed to cleanup: {e}")

def authorized_only(func):
    async def wrapper(client, message):
        if message.from_user.id not in config.AUTH_USERS and message.chat.id != config.WORKING_GROUP:
            await unauthorized_access(message)
            return
        await func(client, message)
    return wrapper

def authorized_only_cb(func):
    async def wrapper(client, callback_query):
        user_id = callback_query.from_user.id
        if user_id not in config.AUTH_USERS:
            await callback_query.answer("❌ Unauthorized", show_alert=True)
            return
        await func(client, callback_query)
    return wrapper

def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:"*?<>|]+', "", name).strip()

def build_status_page(page: int, bot: Client):
    users = list(user_status.items())
    total_pages = (len(users) + STATUS_PAGE_SIZE - 1) // STATUS_PAGE_SIZE
    page = max(0, min(page, total_pages - 1))

    start = page * STATUS_PAGE_SIZE
    end = start + STATUS_PAGE_SIZE

    lines = [f"**📊 Active Recording Tasks — Page {page+1}/{total_pages}:**\n"]

    for user_id, tasks in users[start:end]:
        # Get username once per user from first task, or fallback
        username = tasks[0].get("username", f"User ID: {user_id}") if tasks else f"User ID: {user_id}"
        lines.append(f" 👤 Username: {username}")

        for st in tasks:
            lines.append(
                f"  🆔 Task ID: {st['id']}\n"
                f"  📁 Filename: {st['filename']}\n"
                f"  ⏱ Duration: {st['target']}\n"
                f"  🕒 Start: {st['start_time']}\n"
                f"  🕔 Expected End: {st['end_time']}\n"
                "  —"
            )
        lines.append("")  # Blank line after each user block

    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("⬅️ Back", callback_data=f"status_page_{page-1}"))
    if page < total_pages - 1:
        buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"status_page_{page+1}"))

    markup = InlineKeyboardMarkup([buttons]) if buttons else None

    return "\n".join(lines), markup

def build_user_list_kb():
    users = list(user_status.keys())
    buttons = []

    for uid in users:
        username = user_status[uid][0].get("username", f"User ID: {uid}")
        buttons.append(
            [InlineKeyboardButton(text=username, callback_data=f"cancel_user_{uid}")]
        )

    if not buttons:
        buttons = [[InlineKeyboardButton("No active recording users", callback_data="noop")]]

    buttons.append([InlineKeyboardButton("Exit", callback_data="cancel_exit")])
    return InlineKeyboardMarkup(buttons)

def build_task_list_kb(user_id, page=0):
    tasks = user_status.get(user_id, [])
    if not tasks:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("No tasks found", callback_data="noop")],
             [InlineKeyboardButton("Back", callback_data="cancel_back")]]
        )

    page = max(0, min(page, len(tasks) - 1))
    task = tasks[page]

    buttons = [
        [InlineKeyboardButton(f"Cancel Task {page+1}", callback_data=f"cancel_task_{task['id']}")],
        [InlineKeyboardButton("Cancel All Tasks", callback_data=f"cancel_all_{user_id}")],
        [
            InlineKeyboardButton("⬅️ Prev", callback_data=f"cancel_task_nav_{user_id}_{page-1}" if page > 0 else "noop"),
            InlineKeyboardButton(f"Task {page+1}/{len(tasks)}", callback_data="noop"),
            InlineKeyboardButton("Next ➡️", callback_data=f"cancel_task_nav_{user_id}_{page+1}" if page < len(tasks) -1 else "noop"),
        ],
        [InlineKeyboardButton("Back to Users", callback_data="cancel_back")]
    ]

    # Remove noop buttons from navigation if disabled
    if page <= 0:
        buttons[2][0] = InlineKeyboardButton(" ", callback_data="noop")
    if page >= len(tasks) - 1:
        buttons[2][2] = InlineKeyboardButton(" ", callback_data="noop")

    return InlineKeyboardMarkup(buttons)

def build_cancel_all_confirm_kb(user_id):
    buttons = [
        [InlineKeyboardButton("✅ Confirm Cancel All", callback_data=f"cancel_all_confirm_{user_id}")],
        [InlineKeyboardButton("Back", callback_data=f"cancel_user_{user_id}")]
    ]
    return InlineKeyboardMarkup(buttons)

def build_statusme_page(user_id: int):
    tasks = user_status.get(user_id, [])

    if not tasks:
        return "You have no active recording tasks."

    # Get username from first task or fallback to user ID
    username = tasks[0].get("username", f"User ID: {user_id}")

    lines = [f"**📊 Your Active Recording Tasks:**\n"]
    lines.append(f" 👤 Username: {username}\n")

    for st in tasks:
        lines.append(
            f"  🆔 Task ID: {st['id']}\n"
            f"  📁 Filename: {st['filename']}\n"
            f"  ⏱ Duration: {st['target']}\n"
            f"  🕒 Start: {st['start_time']}\n"
            f"  🕔 Expected End: {st['end_time']}\n"
            "  —"
        )

    return "\n".join(lines)

def get_user_tasks_status(user_id):
    tasks = user_status.get(user_id, [])
    if not tasks:
        return "⚠️ No active tasks found."

    username = tasks[0].get("username", f"User ID: {user_id}")
    lines = [f"**📊 Active Recording Tasks:**\n"]
    lines.append(f"👤 Username: {username}\n")

    for st in tasks:
        lines.append(
            f"🆔 Task ID: {st['id']}\n"
            f"📁 Filename: {st['filename']}\n"
            f"⏱ Duration: {st['target']}\n"
            f"🕒 Start: {st['start_time']}\n"
            f"🕔 Expected End: {st['end_time']}\n"
            "—"
        )

    return "\n".join(lines)

def is_user_verified(user_id: int) -> bool:
    """Check if user is verified and verification hasn't expired."""
    now = int(time.time())
    record = tokens.find_one({"_id": user_id})
    if record and record.get("verified") and record.get("expires_at", 0) > now:
        return True
    return False


async def send_verification_message(bot: Client, message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name
    now = int(time.time())

    existing = tokens.find_one({"_id": user_id})
    if existing:
        expires_at = existing.get("expires_at", 0)
        if existing.get("verified") and now < expires_at:
            remaining = expires_at - now
            return await message.reply(
                f"✅ You are already verified.\n"
                f"⏳ Remaining time: {remaining // 3600}h {(remaining % 3600) // 60}m",
                quote=True
            )

    token = secrets.token_urlsafe(12)
    tokens.update_one(
        {"_id": user_id},
        {"$set": {
            "token": token,
            "username": username,
            "verified": False,
            "expires_at": now + VERIFICATION_EXPIRY_SECONDS
        }},
        upsert=True
    )

    bot_username = (await bot.get_me()).username
    verify_url = f"https://t.me/{bot_username}?start=verify_{token}"

    api_url = f"{SHORTLINK_URL}/api?api={SHORTLINK_API}&url={verify_url}"

    async with httpx.AsyncClient() as client:
        resp = await client.get(api_url)
        data = resp.json()
        if data.get("status") == "success":
            shortlink = data.get("shortenedUrl")
        else:
            shortlink = verify_url

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Verify Here", url=shortlink)]
    ])

    await message.reply(
        f"🔐 **Verification Required**\n\n"
        f"Click the button below to verify and unlock recording for {VERIFICATION_EXPIRY_SECONDS // 3600} hours.",
        reply_markup=markup
    )


async def complete_verification(bot: Client, user_id: int, token: str) -> bool:
    """Mark user as verified if token matches and is not expired."""
    now = int(time.time())
    record = tokens.find_one({"_id": user_id})
    if record and record.get("token") == token and record.get("expires_at", 0) > now:
        tokens.update_one(
            {"_id": user_id},
            {"$set": {"verified": True}}
        )
        return True
    return False

@rvbot.on_message(filters.command("start"))
async def start(bot: Client, message: Message):
    text = message.text.strip()
    user_id = message.from_user.id

    # Handle /start verify_<token>
    match = re.match(r"/start verify_([\w-]+)", text)
    if match:
        token = match.group(1)
        success = await complete_verification(bot, user_id, token)
        if success:
            await message.reply("✅ You are now verified and can use all features.")
        else:
            await message.reply("❌ Invalid or expired verification token.")
        return

    if message.chat.type != "private":
        return await message.reply("👋 I'm ready here! Use /verify or /help to get started.")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📖 Help", callback_data="help")],
        [InlineKeyboardButton("💠 Plans", callback_data="plan")],
        [InlineKeyboardButton("📢 Channel", url="https://t.me/ToonEncodesIndia")]
    ])

    user_mention = (
        f"@{message.from_user.username}" if message.from_user.username
        else message.from_user.first_name
    )

    welcome_text = (
        f"👋 Hi {user_mention}, welcome to [](https://t.me/)!\n\n"
        "▶️ *How to use:*\n"
        "Send a message in this format:\n"
        "`http://video_link 00:00:00 Your Filename`\n\n"
        "⏰ *Timestamp* must be in `HH:MM:SS` format.\n"
        "📁 *Filename* is the name for your recorded clip.\n\n"
        "Use /help to see all commands and instructions.\n\n"
        "_Powered by @TEMohanish_"
    )

    await message.reply(
        welcome_text,
        reply_markup=kb,
        disable_web_page_preview=True,
        parse_mode="markdown"
    )

@rvbot.on_message(filters.command("status"))
@authorized_only
async def status_cmd(bot, message):
    if message.from_user.id not in config.AUTH_USERS:
        return await message.reply("⛔ You are not authorized to use this command.")
    if not user_status:
        return await message.reply("ℹ️ No active tasks for any user.")

    # Build first page (page 0)
    text, markup = build_status_page(0, bot)
    await message.reply_text(text, reply_markup=markup)

@rvbot.on_callback_query(filters.regex(r"^status_page_\d+$"))
@authorized_only_cb
async def status_pagination_cb(bot, callback_query: CallbackQuery):
    page = int(callback_query.data.split("_")[-1])
    text, markup = build_status_page(page, bot)
    try:
        await callback_query.edit_message_text(text, reply_markup=markup)
        await callback_query.answer()
    except Exception:
        pass

@rvbot.on_message(filters.command("statusme"))
@authorized_only
async def statusme_handler(client, message):
    user_id = message.from_user.id
    status_text = build_statusme_page(user_id)
    await message.reply(status_text)

@rvbot.on_message(filters.command("cancel"))
async def cancel_by_admin(bot, message):
    if message.from_user.id not in config.AUTH_USERS:
        return await message.reply("⛔ You are not authorized to use this command.")
    if not user_status:
        return await message.reply("⚠️ No active recording users.")

    buttons = [
        [InlineKeyboardButton(tasks[0].get("username", f"User ID: {uid}"), callback_data=f"cancel_user_{uid}")]
        for uid, tasks in user_status.items()
    ]
    buttons.append([InlineKeyboardButton("❌ Exit", callback_data="cancel_exit")])

    markup = InlineKeyboardMarkup(buttons)
    await message.reply("👥 Select user to cancel recording:", reply_markup=markup)

@rvbot.on_callback_query(filters.regex(r"^cancel_user_(\d+)$"))
async def confirm_cancel_user(bot, query):
    user_id = int(query.matches[0].group(1))
    tasks = user_status.get(user_id, [])
    if not tasks:
        await query.answer("No active tasks for this user.", show_alert=True)
        return await query.message.edit_text("⚠️ No active tasks for this user.")

    username = tasks[0].get("username", f"User ID: {user_id}")
    buttons = [
        [InlineKeyboardButton(f"🆔 Task {i+1}", callback_data=f"cancel_task_{task['id']}")]
        for i, task in enumerate(tasks)
    ]
    buttons.append([
        InlineKeyboardButton("🗑 Cancel All", callback_data=f"cancel_all_{user_id}"),
        InlineKeyboardButton("🔙 Back", callback_data="cancel_back")
    ])

    await query.message.edit_text(f"📋 Tasks for {username}:", reply_markup=InlineKeyboardMarkup(buttons))
    await query.answer()

@rvbot.on_callback_query(filters.regex(r"^cancel_task_(\d+)$"))
@authorized_only
async def on_cancel_task(bot, query):
    task_id = int(query.matches[0].group(1))
    await cancel_single_task(task_id)
    await query.answer("Task cancelled.")
    await query.edit_message_text("✅ Task cancelled and file sent to user.")

@rvbot.on_callback_query(filters.regex(r"^cancel_all_(\d+)$"))
@authorized_only
async def confirm_cancel_all(bot, query):
    user_id = int(query.matches[0].group(1))
    await query.answer()

    tasks = list(user_tasks.keys())
    count = 0
    for task_id in tasks:
        if user_tasks.get(task_id) == user_id:
            await cancel_single_task(task_id)
            count += 1

    if count == 0:
        await query.message.edit_text("⚠️ No active tasks found for this user.")
    else:
        await query.message.edit_text(f"✅ All ({count}) tasks cancelled for user.")

@rvbot.on_callback_query(filters.regex(r"^cancel_exit$"))
async def cancel_exit(bot, query):
    await query.message.delete()
    await query.answer()

@rvbot.on_callback_query(filters.regex(r"^cancel_back$"))
async def cancel_back(bot, query):
    if not user_status:
        await query.message.edit_text("⚠️ No active recording users.")
        return await query.answer()

    buttons = [
        [InlineKeyboardButton(tasks[0].get("username", f"User ID: {uid}"), callback_data=f"cancel_user_{uid}")]
        for uid, tasks in user_status.items()
    ]
    buttons.append([InlineKeyboardButton("❌ Exit", callback_data="cancel_exit")])

    markup = InlineKeyboardMarkup(buttons)
    await query.message.edit_text("👥 Select user to cancel recording:", reply_markup=markup)
    await query.answer()

@rvbot.on_message(filters.command("help"))
@authorized_only
async def help_cmd(bot, message):
    await message.reply_text(
        "**🛠 Help Menu**\n\n"
        "**To start a recording:**\n"
        "`http://link 00:00:00 My Filename`\n\n"
        "**Commands:**\n"
        "• /status – Check your current recording\n"
        "• /start – Welcome screen\n"
        "• /plan – View plans\n"
        "**Notes:**\n"
        "- Link must not be DRM-protected.\n"
        "- Timestamp must be in hh:mm:ss format.\n"
        "- Bot sends file with auto thumbnail and duration.\n"
        "- Make sure filename doesn't use `/\\:*?\"<>|`\n\n"
        "_Bot by @TEMohanish_",
        disable_web_page_preview=True
    )

@rvbot.on_message(filters.command("verify"))
async def verify_handler(client: Client, message: Message):
    user_id = message.from_user.id

    if not ENABLE_SHORTLINK:
        return await message.reply("⚠️ Verification system is currently disabled.")

    if message.chat.id != WORKING_GROUP:
        return await message.reply("❌ You can only use /verify in the main group.")

    if user_id in AUTH_USERS:
        return await message.reply("✅ You are already authorized. No verification needed.")

    if is_user_verified(user_id):
        return await message.reply("✅ You are already verified. You can start recording now.")

    await send_verification_message(client, message)

@rvbot.on_message(filters.command("cancelme"))
@authorized_only
async def cancelme_handler(bot, message):
    user_id = message.from_user.id
    tasks = user_status.get(user_id, [])

    if not tasks:
        return await message.reply("❌ You don't have any active recording tasks.")

    buttons = [
        [InlineKeyboardButton(f"🆔 Task {i+1}", callback_data=f"cancelme_task_{task['id']}")]
        for i, task in enumerate(tasks)
    ]
    buttons.append([InlineKeyboardButton("❌ Exit", callback_data="cancelme_exit")])

    markup = InlineKeyboardMarkup(buttons)
    await message.reply("📋 Your active tasks:", reply_markup=markup)

@rvbot.on_callback_query(filters.regex(r"^cancelme_task_(\d+)$"))
async def cancelme_task_selected(bot, query):
    user_id = query.from_user.id
    task_id = int(query.matches[0].group(1))

    task = None
    for t in user_status.get(user_id, []):
        if t["id"] == task_id:
            task = t
            break

    if not task:
        return await query.answer("❌ Task not found or already completed.", show_alert=True)

    caption = (
        f"🆔 **Task ID**: {task['id']}\n"
        f"📁 **Filename**: {task['filename']}\n"
        f"⏱ **Duration**: {task['target']}\n"
        f"🕒 **Started at**: {task['start_time']}\n"
        f"🕔 **Expected End**: {task['end_time']}"
    )

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm Cancel", callback_data=f"cancelme_confirm_{task_id}")],
        [InlineKeyboardButton("🔙 Back", callback_data="cancelme_back")]
    ])

    await query.edit_message_text(caption, reply_markup=kb)

@rvbot.on_callback_query(filters.regex(r"^cancelme_confirm_(\d+)$"))
async def cancelme_confirm(bot, query):
    user_id = query.from_user.id
    task_id = int(query.matches[0].group(1))

    # Double-check ownership
    if user_tasks.get(task_id) != user_id:
        return await query.answer("❌ You cannot cancel this task.", show_alert=True)

    await cancel_single_task(task_id)
    await query.edit_message_text("✅ Task cancelled and file sent (if available).")
    await query.answer()

@rvbot.on_callback_query(filters.regex("cancelme_back"))
async def cancelme_back(bot, query):
    user_id = query.from_user.id
    tasks = user_status.get(user_id, [])

    if not tasks:
        return await query.edit_message_text("❌ No active tasks found.")

    buttons = [
        [InlineKeyboardButton(f"🆔 Task {i+1}", callback_data=f"cancelme_task_{task['id']}")]
        for i, task in enumerate(tasks)
    ]
    buttons.append([InlineKeyboardButton("❌ Exit", callback_data="cancelme_exit")])

    markup = InlineKeyboardMarkup(buttons)
    await query.edit_message_text("📋 Your active tasks:", reply_markup=markup)

@rvbot.on_callback_query(filters.regex("cancelme_exit"))
async def cancelme_exit(bot, query):
    await query.message.delete()
    await query.answer()

@rvbot.on_message(filters.command("plan"))
@authorized_only
async def plan_cmd(bot, message):
    text = (
        "**💠 Subscription Plans**\n\n"
        "**Free Plan:**\n"
        "• ⏳ Time gap between recordings\n"
        "• ⏱ Limited recording length\n\n"
        "**Premium Benefits:**\n"
        "• 🚫 No time gaps\n"
        "• ⏰ Record up to 3–5 hours per task\n"
        "• 🎧 Multi-audio support\n"
        "• ⚡ Faster processing\n\n"
        "**💳 Pricing:**\n"
        "• 🪙 1 Month — ₹40\n"
        "• 💫 3 Months — ₹140\n"
        "• 💎 6 Months — ₹270\n\n"
        "To upgrade, contact the owner below:"
    )

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Contact Owner", url="https://t.me/")],
        [InlineKeyboardButton("📢 Updates", url="https://t.me/")]
    ])

    await message.reply_text(text, reply_markup=markup)

@rvbot.on_message(filters.regex(r"^http.*? \d{2}:\d{2}:\d{2}( .+)?$"))
@authorized_only
async def handle_record(bot, message):
    user_id = message.from_user.id

    # ⛔ Per-user task limit (if enabled)
    if user_id not in config.AUTH_USERS and config.USER_LIMIT_LINK > 0:
        user_tasks_list = user_status.get(user_id, [])
        if len(user_tasks_list) >= config.USER_LIMIT_LINK:
            try:
                soonest_task = min(user_tasks_list, key=lambda t: t.get("end_time"))
                end_time_str = soonest_task.get("end_time", "Unknown")
                return await message.reply_text(
                    f"❌ Your task is already running.\n"
                    f"⏳ Expected completion: {end_time_str}"
                )
            except Exception:
                return await message.reply_text("❌ You already have a running task. Please wait.")

    # ⛔ Group-wide task limit for non-auth users
    if user_id not in config.AUTH_USERS and config.LIMIT_LINK > 0:
        active_tasks = user_status.get(user_id, [])
        if len(active_tasks) >= config.LIMIT_LINK:
            try:
                soonest_task = min(active_tasks, key=lambda t: t.get("end_time"))
                end_time_str = soonest_task.get("end_time", "Unknown")
                return await message.reply_text(
                    f"❌ Group Limit Reached. Please wait until a current task finishes.\n"
                    f"⏳ Expected time: {end_time_str}"
                )
            except Exception:
                return await message.reply_text("❌ Group Limit Reached. Please wait for a task to complete.")

    # ✅ Verification for regular users
    if config.ENABLE_SHORTLINK and user_id not in config.AUTH_USERS:
        if not is_user_verified(user_id):
            return await message.reply_text(
                "❌ You are not a verified user.\n"
                f"Please use /verify to continue recording. Verification lasts for {config.VERIFICATION_EXPIRY_SECONDS // 3600} hours."
            )

    task_id = int(time.time() * 1000) + random.randint(1, 999)
    msg = await message.reply_text("⏳ Processing...")

    save_dir = None
    try:
        parts = message.text.strip().split(" ", 2)
        url = parts[0]
        timestamp = parts[1]
        raw_filename = parts[2].strip() if len(parts) > 2 and parts[2].strip() else "@Toonix_India"
        raw_filename = sanitize_filename(raw_filename)
        filename = f"{raw_filename}.mkv"

        save_dir = os.path.join(config.DOWNLOAD_DIRECTORY, str(int(time.time())))
        os.makedirs(save_dir, exist_ok=True)
        video_path = os.path.join(save_dir, filename)

        try:
            parts = timestamp.split(":")
            if len(parts) != 3:
                raise ValueError("Timestamp must be in hh:mm:ss format.")
            duration_parts = list(map(int, parts))
            total_seconds = duration_parts[0]*3600 + duration_parts[1]*60 + duration_parts[2]

            # Enforce max duration for non-auth users
            if user_id not in config.AUTH_USERS and total_seconds > config.MAX_DURATION_SEC:
                max_h = config.MAX_DURATION_SEC // 3600
                max_m = (config.MAX_DURATION_SEC % 3600) // 60
                max_s = config.MAX_DURATION_SEC % 60
                await msg.edit(
                    f"❌ This plan supports only up to {max_h:02}:{max_m:02}:{max_s:02} per recording.\n"
                    "Upgrade to premium to unlock longer durations."
                )
                return
        except Exception:
            await msg.edit("❌ Invalid timestamp format. Use hh:mm:ss (e.g., 00:45:00).")
            return

        tz = pytz.timezone(config.TIMEZONE)
        now = datetime.now(tz)
        formatted_date = now.strftime("%d-%m-%Y")
        start_time = datetime.now(tz)
        end_time = start_time + timedelta(seconds=total_seconds)

        user_tasks[task_id] = user_id
        if user_id not in user_status:
            user_status[user_id] = []
        user_status[user_id].append({
            "id": task_id,
            "filename": raw_filename,
            "target": timestamp,
            "progress": "00:00:00",
            "Date": formatted_date,
            "start_time": start_time.strftime("%I:%M:%S %p"),
            "end_time": end_time.strftime("%I:%M:%S %p"),
            "username": message.from_user.username or message.from_user.first_name or "anonymous",
            "output": video_path,
            "folder": save_dir,
            "chat_id": message.chat.id,
            "process": None  # Will be set after starting ffmpeg
        })

        ffmpeg_cmd = (
            f'ffmpeg -y -probesize 10000000 -analyzeduration 15000000 '
            f'-i "{url}" -map 0:v -map 0:a -c:v copy -c:a aac -t {timestamp} "{video_path}"'
        )
        process = await asyncio.create_subprocess_exec(
            *shlex.split(ffmpeg_cmd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        # 🔗 Save the process object in user_status for later cancellation
        user_status[user_id][-1]["process"] = process

        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise Exception(f"FFmpeg error:\n{stderr.decode()}")

        meta_cmd = (
            f'ffmpeg -y -i "{video_path}" -map 0 -metadata title="ToonEncodes" '
            f'-c copy "{video_path}.tmp.mkv"'
        )

        retcode, out, err = await runcmd(meta_cmd)
        if retcode != 0:
            raise Exception(f"FFmpeg metadata error:\n{err}")
        os.replace(f"{video_path}.tmp.mkv", video_path)

        dur = await get_video_duration(video_path)
        if dur > 10:
            rand_sec = random.randint(5, dur - 5)
        else:
            rand_sec = 1
        thumb_path = os.path.join(save_dir, "thumb.jpg")
        thumb_cmd = f'ffmpeg -y -ss {rand_sec} -i "{video_path}" -vframes 1 -q:v 2 "{thumb_path}"'
        retcode, out, err = await runcmd(thumb_cmd)
        if retcode != 0:
            LOG.warning(f"Thumbnail generation failed: {err}")

        display_name = raw_filename.strip() if raw_filename.strip() else "@Toonix_India"

        caption = (
            f"File Name : {display_name}\n"
            f"Size : {os.path.getsize(video_path) / (1024 * 1024):.2f} MB\n"
            f"Duration : {TimeFormatter(dur * 1000)}\n"
            f"Date : {formatted_date}\n"
            f"Time : {start_time.strftime('%I:%M:%S %p')} to {end_time.strftime('%I:%M:%S %p')}\n\n"
            "Credits By @Toonix_India"
        )

        start_unix = time.time()
        await message.reply_video(
            video=video_path,
            caption=caption,
            thumb=thumb_path if os.path.exists(thumb_path) else None,
            progress=progress_for_pyrogram,
            progress_args=(message, start_unix)
        )

        # ✅ Also store the video in STORE_CHANNEL
        try:
            store_caption = (
                f"📥 **Stored Recording**\n"
                f"👤 User: @{message.from_user.username or message.from_user.first_name}\n"
                f"📁 File: `{display_name}`\n"
                f"📅 Date: {formatted_date}\n"
                f"⏱ Time: {start_time.strftime('%I:%M:%S %p')} to {end_time.strftime('%I:%M:%S %p')}"
            )
            await rvbot.send_video(
                chat_id=config.STORE_CHANNEL_ID,
                video=video_path,
                caption=store_caption,
                thumb=thumb_path if os.path.exists(thumb_path) else None
            )
        except Exception as e:
            LOG.warning(f"[Store] Failed to send to store channel: {e}")

        await msg.delete()

    except Exception as e:
        LOG.error("Error in handle_record:\n" + traceback.format_exc())
        try:
            err_text = str(e)
            if len(err_text) > 4000:
                err_text = err_text[:4000] + "... [truncated]"
            await msg.edit(f"❌ This URL is not supported for recording or has expired. Please try a different URL.")
        except Exception as exc:
            LOG.error(f"Failed to edit error message: {exc}")

    finally:
        if user_id in user_status:
            user_status[user_id] = [t for t in user_status[user_id] if t["id"] != task_id]
            if not user_status[user_id]:
                user_status.pop(user_id)

        user_tasks.pop(task_id, None)

         # 🔥 Optional cleanup based on config
        if save_dir:
            try:
                shutil.rmtree(save_dir)
            except Exception as cleanup_err:
                LOG.warning(f"Cleanup failed: {cleanup_err}")

async def runcmd(cmd: str) -> Tuple[int, str, str]:
    args = shlex.split(cmd)
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    return process.returncode, stdout.decode(), stderr.decode()

async def get_video_duration(input_file: str) -> int:
    parser = createParser(input_file)
    if not parser:
        return 0
    metadata = extractMetadata(parser)
    if not metadata or not metadata.has("duration"):
        return 0
    duration = metadata.get("duration")
    return int(duration.seconds)

last_update = 0

async def progress_for_pyrogram(current, total, message, start):
    global last_update
    now = time.time()
    if now - last_update < 5:
        return
    last_update = now
    diff = now - start
    if diff == 0:
        diff = 1
    percentage = current * 100 / total
    speed = current / diff
    elapsed = TimeFormatter(int(diff * 1000))
    eta = TimeFormatter(int((total - current) / speed * 1000))
    text = (
        f"Progress: {percentage:.2f}%\n"
        f"Speed: {speed / 1024:.2f} KB/s\n"
        f"Elapsed: {elapsed}\n"
        f"ETA: {eta}"
    )
    try:
        await message.edit(text)
    except Exception:
        pass

def TimeFormatter(milliseconds: int) -> str:
    seconds, ms = divmod(milliseconds, 1000)
    minutes, seconds = divmod(milliseconds // 1000, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"

async def start_bot():
    await rvbot.start()
    LOG.info("rvbot started")


if __name__ == "__main__":
    LOG.info("🚀 Starting RV Paid Recorder Bot...")
    rvbot.run()
