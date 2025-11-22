# config.py
"""
Configuration loader for the HH Telegram Bot project.
Uses .env file via python-dotenv, provides typed settings,
and ensures required variables are present.
"""

from dotenv import load_dotenv
import os
from typing import Optional

# Load .env
load_dotenv()


# =========================================================
# Helpers
# =========================================================
def env(key: str, default: Optional[str] = None, required: bool = False) -> str:
    """
    Read an environment variable with optional default value
    and strict 'required' mode.
    """
    value = os.getenv(key, default)
    if required and (value is None or value == ""):
        raise RuntimeError(f"Environment variable '{key}' is required but not set.")
    return value


# =========================================================
# Telegram Bot
# =========================================================
BOT_TOKEN: str = env("BOT_TOKEN", required=True)


# =========================================================
# HH OAuth / API
# =========================================================
HH_CLIENT_ID: str = env("HH_CLIENT_ID", required=True)
HH_CLIENT_SECRET: str = env("HH_CLIENT_SECRET", required=True)

# Base domain where FastAPI is deployed
PUBLIC_BASE_URL: str = env("PUBLIC_BASE_URL", required=True).rstrip("/")

# User-Agent according to HH API requirements
HH_USER_AGENT: str = env(
    "HH_USER_AGENT",
    default="hh-telegram-bot/1.0 (contact@example.com)",
)


# =========================================================
# URLs for HH OAuth + Webhooks
# =========================================================
HH_REDIRECT_PATH: str = "/hh/oauth/callback"
HH_WEBHOOK_PATH: str = "/hh/webhook"

HH_REDIRECT_URI: str = f"{PUBLIC_BASE_URL}{HH_REDIRECT_PATH}"
HH_WEBHOOK_URL: str = f"{PUBLIC_BASE_URL}{HH_WEBHOOK_PATH}"


# =========================================================
# Database
# =========================================================
DATABASE_URL_ASYNC: str = env(
    "DATABASE_URL_ASYNC",
    default="sqlite+aiosqlite:///./hh_bot.db",
)

DATABASE_URL_SYNC: str = env(
    "DATABASE_URL_SYNC",
    default="sqlite:///./hh_bot.db",
)


# =========================================================
# Debug settings
# =========================================================
DEBUG: bool = env("DEBUG", "false").lower() in ("true", "1", "yes")


# =========================================================
# Optional settings for message filtering
# =========================================================
# Use your own list of phrases if needed, but these can also be redefined via env
REJECTION_PATTERNS = [
    "к сожалению",
    "к сожелению",
    "мы не готовы вас принять",
    "вы нам не подходите",
    "вынуждены отказать",
    "не можем продолжить",
    "отказ",
]

