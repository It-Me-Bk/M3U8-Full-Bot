import time
import secrets
import httpx
from pymongo import MongoClient
from pyrogram import Client
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import (
    MONGO_URI,
    SHORTLINK_URL,
    SHORTLINK_API,
    VERIFICATION_EXPIRY_SECONDS,
    WORKING_GROUP
)

# -----------------------
# 🔌 MongoDB Setup
# -----------------------
mongo = MongoClient(MONGO_URI)
db = mongo['verifyDB']
tokens = db['tokens']

# -----------------------
# ✅ Check if user is verified
# -----------------------
def is_user_verified(user_id: int) -> bool:
    doc = tokens.find_one({"_id": user_id})
    if not doc:
        return False
    return doc.get("verified", False) and doc.get("expires_at", 0) > int(time.time())

# -----------------------
# ✅ Send verification message with shortlink button
# -----------------------
async def send_verification_message(bot: Client, message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name
    now = int(time.time())

    # Check existing verified and unexpired
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

    # Generate new token and save
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

    # Prepare verification URL and get shortlink via API
    bot_username = (await bot.get_me()).username
    verify_url = f"https://t.me/{bot_username}?start=verify_{token}"

    api_url = f"{SHORTLINK_URL}/api?api={SHORTLINK_API}&url={verify_url}"

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(api_url, timeout=10)
            data = resp.json()
            if data.get("status") == "success":
                shortlink = data.get("shortenedUrl")
            else:
                shortlink = verify_url
        except Exception:
            shortlink = verify_url

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Verify Here", url=shortlink)]
    ])

    await message.reply(
        f"🔐 **Verification Required**\n\n"
        f"Click the button below to verify and unlock recording for {VERIFICATION_EXPIRY_SECONDS // 3600} hours.",
        reply_markup=markup
    )


# -----------------------
# ✅ Complete verification (called by webhook or /start handler)
# -----------------------
async def complete_verification(bot: Client, user_id: int, token: str) -> bool:
    doc = tokens.find_one({"_id": user_id})
    if not doc or doc.get("token") != token:
        return False

    now = int(time.time())

    # Update verified status and expiry
    tokens.update_one(
        {"_id": user_id},
        {"$set": {
            "verified": True,
            "expires_at": now + VERIFICATION_EXPIRY_SECONDS
        }}
    )

    # Notify group (optional)
    try:
        username = doc.get("username", "User")
        await bot.send_message(
            WORKING_GROUP,
            f"✅ **{username}** has successfully verified and can now access recording features."
        )
    except Exception:
        pass

    return True
