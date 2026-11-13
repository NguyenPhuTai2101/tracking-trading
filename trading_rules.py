import math
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


INTERVAL_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
                    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600,
                    "8h": 28800, "12h": 43200, "1d": 86400}


class BotConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    market_type: Optional[Literal["SPOT", "FUTURES"]] = None
    leverage: Optional[int] = Field(default=None, ge=1, le=50)
    symbol: Optional[str] = Field(default=None, pattern=r"^[A-Z0-9]{2,30}USDT$")
    watchlist_symbols: Optional[list[str]] = Field(default=None, min_length=1, max_length=30)
    max_positions: Optional[int] = Field(default=None, ge=1, le=10)
    strategy: Optional[Literal["RSI_SCALPING", "EMA_CROSSOVER", "HYBRID_SMART"]] = None
    trade_amount_usdt: Optional[float] = Field(default=None, ge=5)
    stop_loss_pct: Optional[float] = Field(default=None, gt=0, lt=100)
    take_profit_pct: Optional[float] = Field(default=None, gt=0, lt=100)
    scan_interval_sec: Optional[int] = Field(default=None, ge=3, le=3600)
    interval: Optional[str] = None
    max_total_margin_usdt: Optional[float] = Field(default=None, ge=5)
    max_open_risk_usdt: Optional[float] = Field(default=None, gt=0)
    # Entry/exit refinements (see strategy.py). Defaults keep the original behaviour.
    exit_mode: Optional[Literal["ROE", "ATR"]] = None
    atr_sl_mult: Optional[float] = Field(default=None, ge=0.3, le=10)
    atr_tp_mult: Optional[float] = Field(default=None, ge=0.3, le=20)
    trend_filter: Optional[bool] = None
    cooldown_candles: Optional[int] = Field(default=None, ge=0, le=100)
    breakeven_at_r: Optional[float] = Field(default=None, ge=0, le=10)
    # Risk controls; 0 = off (original behaviour: fixed margin per trade, no daily limit).
    risk_per_trade_usdt: Optional[float] = Field(default=None, ge=0, le=1_000_000)
    max_daily_loss_usdt: Optional[float] = Field(default=None, ge=0, le=1_000_000)
    max_consecutive_losses: Optional[int] = Field(default=None, ge=0, le=50)

    @field_validator("watchlist_symbols")
    @classmethod
    def validate_symbols(cls, symbols):
        if symbols is None:
            return symbols
        import re
        normalized = list(dict.fromkeys(s.strip().upper() for s in symbols))
        if any(not re.fullmatch(r"[A-Z0-9]{2,30}USDT", s) for s in normalized):
            raise ValueError("Watchlist chứa mã không hợp lệ")
        return normalized

    @field_validator("breakeven_at_r")
    @classmethod
    def validate_breakeven(cls, value):
        if value is not None and 0 < value < 0.5:
            raise ValueError("Dời SL về hòa vốn: đặt 0 (tắt) hoặc từ 0.5R trở lên")
        return value

    @field_validator("interval")
    @classmethod
    def validate_interval(cls, interval):
        if interval is not None and interval not in INTERVAL_SECONDS:
            raise ValueError("Khung nến không được hỗ trợ")
        return interval


def validate_closed_candles(candles, interval, now_ms):
    """Reject stale, incomplete, unordered or malformed input before making a signal."""
    duration_ms = INTERVAL_SECONDS[interval] * 1000
    closed = [c for c in candles if c.get("closed")]
    if len(closed) < 35:
        raise ValueError("Chưa đủ 35 nến đã đóng")
    age = now_ms - closed[-1]["close_time_ms"]
    if age < 0 or age > duration_ms + 30_000:
        raise ValueError("Dữ liệu nến đã cũ hoặc lệch thời gian")
    previous = None
    for candle in closed:
        values = [candle[k] for k in ("open", "high", "low", "close")]
        if any(not math.isfinite(v) or v <= 0 for v in values):
            raise ValueError("Giá nến không hợp lệ")
        if candle["low"] > min(candle["open"], candle["close"]) or candle["high"] < max(candle["open"], candle["close"]):
            raise ValueError("Biên độ nến không hợp lệ")
        if previous is not None and (candle["time"] - previous) * 1000 != duration_ms:
            raise ValueError("Dữ liệu nến bị thiếu hoặc sai thứ tự")
        previous = candle["time"]
    return closed
