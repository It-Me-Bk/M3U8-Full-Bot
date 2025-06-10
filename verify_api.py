import time
import logging
import asyncio
from fastapi import FastAPI, Request
from pymongo import MongoClient
from pyrogram import Client

from config import MONGO_URI, WORKING_GROUP, VERIFICATION_EXPIRY_SECONDS, API_ID, API_HASH, BOT_TOKEN

app = FastAPI()

# MongoDB setup
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["verifyDB"]
tokens = db["tokens"]

# Initialize Pyrogram client (bot)
rvbot = Client("rvbot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Start the Pyrogram bot on FastAPI startup
@app.on_event("startup")
async def startup_event():
    # Run the Pyrogram bot in background
    asyncio.create_task(rvbot.start())
    logging.info("Pyrogram bot started")

# Shutdown event to stop the bot cleanly
@app.on_event("shutdown")
async def shutdown_event():
    await rvbot.stop()
    logging.info("Pyrogram bot stopped")

# POST endpoint for shortlink verification callback
@app.post("/verify_callback")
async def verify_callback(req: Request):
    data = await req.json()
    token = data.get("token")
    if not token:
        return {"status": "error", "message": "Missing token."}

    entry = tokens.find_one({"token": token})
    if not entry:
        return {"status": "error", "message": "Invalid token."}

    user_id = entry.get("_id")
    username = entry.get("username", "User")

    now = int(time.time())
    tokens.update_one(
        {"token": token},
        {"$set": {
            "verified": True,
            "verified_at": now,
            "expires_at": now + VERIFICATION_EXPIRY_SECONDS
        }}
    )

    try:
        await rvbot.send_message(
            WORKING_GROUP,
            f"✅ **{username}** has successfully verified and can now access recording features!"
        )
    except Exception as e:
        logging.error(f"Failed to send verification message: {e}")

    return {"status": "success"}
