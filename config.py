import os
from pathlib import Path

from dotenv import load_dotenv

# Resolve configuration relative to this file, independent of the launch directory.
# Explicit process environment values take precedence over .env.
load_dotenv(Path(__file__).resolve().with_name(".env"), override=False, encoding="utf-8-sig")

class Settings:
    API_KEY: str = os.getenv("BINANCE_API_KEY", "")
    SECRET_KEY: str = os.getenv("BINANCE_SECRET_KEY", "")
    
    # Binance Demo Endpoints
    SPOT_BASE_URL: str = "https://demo-api.binance.com"
    FUTURES_BASE_URL: str = "https://demo-fapi.binance.com"
    
    # Web server settings
    HOST: str = os.getenv("HOST", "127.0.0.1")
    PORT: int = int(os.getenv("PORT", "8000"))
    
    # Bot default configs
    DEFAULT_SYMBOL: str = "BTCUSDT"
    DEFAULT_INTERVAL: str = "15m"
    DEFAULT_TRADE_AMOUNT_USDT: float = 20.0
    DEFAULT_STOP_LOSS_PCT: float = 1.5
    DEFAULT_TAKE_PROFIT_PCT: float = 3.0
    DEFAULT_SCAN_INTERVAL_SEC: int = 10

settings = Settings()
