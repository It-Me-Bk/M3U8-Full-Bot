from os import environ

# 🔐 Telegram API credentials (get from https://my.telegram.org)
API_ID = int(environ.get("API_ID", 27623611))  # Replace default with your real API_ID
API_HASH = environ.get("API_HASH", "4a326a928083750585c7f96408ced199")  # Your API_HASH

# 🤖 Telegram bot token (get from https://t.me/BotFather)
BOT_TOKEN = environ.get("BOT_TOKEN", "8081507527:AAHVRI2z4Ygx8ja9qLEz5RohPkqkC1jBma8")  # Your bot token

# 👤 Bot owner user ID (get via https://t.me/userinfobot)
OWNER_ID = int(environ.get("OWNER_ID", "5180174682"))  # Owner's Telegram user ID

# ✅ Authorized users (comma-separated user IDs)
AUTH_USERS = list(map(int, environ.get("AUTH_USERS", "5180174682,1856011609").replace(",", " ").split()))

# 📂 Directory to store downloads
DOWNLOAD_DIRECTORY = environ.get("DOWNLOAD_DIRECTORY", "./downloads")

# 🌍 Timezone string for your region (e.g., "Asia/Kolkata", "UTC")
TIMEZONE = environ.get("TIMEZONE", "Asia/Kolkata")

# 👥 Group where the bot works
WORKING_GROUP = int(environ.get("WORKING_GROUP", "-1002641866665"))

# 🔐 Shortlink verification system
ENABLE_SHORTLINK = environ.get("ENABLE_SHORTLINK", "true").lower() == "true"
VERIFICATION_EXPIRY_SECONDS = 4 * 60 * 60  # 4 hours
SHORTLINK_URL = environ.get("SHORTLINK_URL", "https://gplinks.com")
SHORTLINK_API = environ.get("SHORTLINK_API", "e7bd4be5488779c6a552398e4373bb909475cf65")

# 🔗 Group invite link
GROUP_LINK = "https://t.me/YourGroupLink"

# ☁️ MongoDB connection string
MONGO_URI = environ.get("MONGO_URI", "mongodb+srv://toonixofficial:Mh6cBlxmjdyytClh@cluster0.5u82bdo.mongodb.net/?retryWrites=true&w=majority")

# ⏱️ Max duration allowed per recording (in seconds) for non-auth users
MAX_DURATION_SEC = int(environ.get("MAX_DURATION", "1800"))  # 10 minutes

# 🗃️ Telegram channel ID where all recordings will be stored (bot must be admin)
STORE_CHANNEL_ID = int(environ.get("STORE_CHANNEL_ID", "-1002212854973"))

# 🔗 Max number of active recording links allowed per user globally (0 = unlimited)
LIMIT_LINK = int(environ.get("LIMIT_LINK", "20"))

# 👤 Max number of simultaneous recordings per individual user (0 = unlimited)
USER_LIMIT_LINK = int(environ.get("USER_LIMIT_LINK", "3"))
