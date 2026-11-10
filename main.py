import os
import logging
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import asyncpg
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application

# Load environment variables from .env file
load_dotenv()

# All handlers, DB schema and logic live in features.py
import features

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)


def clean_dsn(url: str) -> str:
    """Neon URLs often carry libpq-only options (channel_binding) that asyncpg does not understand."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "channel_binding"]
    return urlunsplit(parts._replace(query=urlencode(query)))


async def post_init(application: Application):
    """Connects to Neon PostgreSQL, then lets features.py create its tables."""
    logger.info("Connecting to Neon PostgreSQL...")
    try:
        pool = await asyncpg.create_pool(dsn=clean_dsn(os.getenv("DATABASE_URL")), min_size=2, max_size=10)
        logger.info("✅ Connected to Database successfully.")
        # Store pool in bot_data so handlers can access it
        application.bot_data['db_pool'] = pool
        await features.setup(application, pool)
    except Exception as e:
        logger.error(f"❌ Startup failed: {e}")
        raise


async def post_shutdown(application: Application):
    """Closes the database connection when the bot stops."""
    logger.info("Shutting down database connection...")
    pool = application.bot_data.get('db_pool')
    if pool:
        await pool.close()


def main():
    # Verify required environment variables
    required_vars = ["BOT_TOKEN", "DATABASE_URL", "OWNER_ID", "HELPER_CHAT_ID", "MASTERLOG_CHAT_ID"]
    for var in required_vars:
        if not os.getenv(var):
            raise ValueError(f"Missing required environment variable: {var}. Check your .env file.")

    application = (
        Application.builder()
        .token(os.getenv("BOT_TOKEN"))
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    features.register_handlers(application)

    logger.info("🚀 Bot is starting polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
