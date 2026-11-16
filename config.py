import os
from pathlib import Path

from dotenv import load_dotenv

# Resolve configuration relative to this file, independent of the launch directory.
# Explicit process environment values take precedence over .env.
load_dotenv(Path(__file__).resolve().with_name(".env"), override=False, encoding="utf-8-sig")


class Settings:
    API_KEY: str = os.getenv("BINANCE_API_KEY", "")
    SECRET_KEY: str = os.getenv("BINANCE_SECRET_KEY", "")

    # Binance Demo endpoints. The bot trades Futures only; the Spot host is used for clock sync.
    SPOT_BASE_URL: str = "https://demo-api.binance.com"
    FUTURES_BASE_URL: str = "https://demo-fapi.binance.com"

    # Web server settings
    HOST: str = os.getenv("HOST", "127.0.0.1")
    PORT: int = int(os.getenv("PORT", "8000"))


settings = Settings()
