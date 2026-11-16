"""Capital model. The strategy proposes a trade; this module decides whether and how big.

All limits are percentages of account equity (wallet balance + unrealised PnL), so the
bot scales itself down while losing and up while winning. None of them can be switched off.

- Size: a stop-out loses `risk_pct` of equity, fees and slippage included. Leverage is a
  consequence of the stop distance, never an input to size.
- Portfolio: caps on open risk, positions, same-direction positions (crypto is highly
  correlated) and gross exposure.
- Circuit breakers: daily and weekly loss limits and a losing-streak limit pause new
  entries until the next UTC day/week. Open positions keep their stops.
- Drawdown ladder: below `dd_reduce_pct` from the equity peak the bot risks half; at
  `dd_halt_pct` it stops and waits for the user to acknowledge.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

MIN_NOTIONAL_USDT = 5.0
MARGIN_BUFFER = 0.95        # never plan to use the last 5% of available margin
LIQUIDATION_BUFFER = 0.8    # the stop must sit well inside the ~1/leverage liquidation distance


class RiskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    risk_pct: float = Field(0.75, ge=0.1, le=2.0)                 # equity lost when one stop is hit
    max_open_risk_pct: float = Field(3.0, ge=0.5, le=8.0)         # all open stops hit together
    max_positions: int = Field(4, ge=1, le=10)
    max_same_direction: int = Field(3, ge=1, le=10)
    leverage: int = Field(5, ge=1, le=10)                         # exchange setting = margin efficiency only
    max_position_notional_pct: float = Field(100.0, ge=10, le=500)
    max_gross_exposure_pct: float = Field(300.0, ge=50, le=900)
    daily_loss_pct: float = Field(2.0, ge=0.5, le=5.0)
    weekly_loss_pct: float = Field(5.0, ge=1.0, le=15.0)
    max_consecutive_losses: int = Field(4, ge=2, le=10)
    dd_reduce_pct: float = Field(10.0, ge=3, le=30)
    dd_halt_pct: float = Field(20.0, ge=5, le=50)
    max_cost_r: float = Field(0.15, ge=0.05, le=0.5)              # round-trip costs as a share of 1R
    cooldown_candles: int = Field(3, ge=0, le=48)                 # 1h candles to skip a symbol after an exit
    fee_rate: float = Field(0.0005, ge=0, le=0.002)               # taker, per side
    slippage_rate: float = Field(0.0002, ge=0, le=0.002)          # per fill

    @model_validator(mode="after")
    def _check(self):
        if self.max_open_risk_pct < self.risk_pct:
            raise ValueError("Tổng rủi ro đang mở phải ≥ rủi ro mỗi lệnh")
        if self.weekly_loss_pct < self.daily_loss_pct:
            raise ValueError("Giới hạn lỗ tuần phải ≥ giới hạn lỗ ngày")
        if self.dd_halt_pct <= self.dd_reduce_pct:
            raise ValueError("Mức drawdown dừng bot phải lớn hơn mức giảm rủi ro")
        return self

    @property
    def round_trip_cost(self) -> float:
        return 2 * (self.fee_rate + self.slippage_rate)


@dataclass
class Decision:
    ok: bool
    reason: str = ""
    code: str = ""              # stable key for grouping rejections (reason carries the numbers)
    notional: float = 0.0
    risk_usdt: float = 0.0
    scale: float = 1.0


# ----------------------------- state -----------------------------

def new_state() -> Dict:
    return {"peak_equity": None, "day": "", "day_start_equity": None, "day_block": None,
            "week": "", "week_start_equity": None, "week_block": None,
            "loss_streak": 0, "halted": False, "halt_reason": None}


def _loss_pct(start, equity):
    return (start - equity) / start * 100 if start and start > 0 else 0.0


def roll(state: Dict, equity: float, now: datetime, cfg: RiskConfig) -> List[str]:
    """Feed the latest equity. Rolls the UTC day/week, tracks the peak and trips the
    breakers. Returns messages for anything that tripped just now."""
    now = now.astimezone(timezone.utc)
    events = []
    if equity <= 0:
        return events
    day = now.strftime("%Y-%m-%d")
    iso = now.isocalendar()
    week = f"{iso[0]}-W{iso[1]:02d}"
    if state["peak_equity"] is None or equity > state["peak_equity"]:
        state["peak_equity"] = equity
    if state["day"] != day:
        state.update(day=day, day_start_equity=equity, day_block=None, loss_streak=0)
    if state["week"] != week:
        state.update(week=week, week_start_equity=equity, week_block=None)
    day_loss = _loss_pct(state["day_start_equity"], equity)
    if not state["day_block"] and day_loss >= cfg.daily_loss_pct:
        state["day_block"] = f"Lỗ trong ngày {day_loss:.2f}% chạm giới hạn {cfg.daily_loss_pct:g}%"
        events.append(state["day_block"] + ". Nghỉ mở lệnh đến 00:00 UTC.")
    week_loss = _loss_pct(state["week_start_equity"], equity)
    if not state["week_block"] and week_loss >= cfg.weekly_loss_pct:
        state["week_block"] = f"Lỗ trong tuần {week_loss:.2f}% chạm giới hạn {cfg.weekly_loss_pct:g}%"
        events.append(state["week_block"] + ". Nghỉ mở lệnh đến thứ Hai (UTC).")
    drawdown = _loss_pct(state["peak_equity"], equity)
    if not state["halted"] and drawdown >= cfg.dd_halt_pct:
        state["halted"] = True
        state["halt_reason"] = (f"Drawdown {drawdown:.1f}% từ đỉnh vốn {state['peak_equity']:.2f} USDT "
                                f"chạm mức dừng {cfg.dd_halt_pct:g}%")
        events.append(state["halt_reason"] + ". Bot dừng mở lệnh cho tới khi bạn xác nhận.")
    return events


def record_trade(state: Dict, net_pnl: float, cfg: RiskConfig) -> Optional[str]:
    """Count a closed trade towards the losing streak. Returns a message when it trips."""
    if net_pnl > 0:
        state["loss_streak"] = 0
    elif net_pnl < 0:
        state["loss_streak"] += 1
        if state["loss_streak"] >= cfg.max_consecutive_losses and not state["day_block"]:
            state["day_block"] = f"Thua {state['loss_streak']} lệnh liên tiếp (giới hạn {cfg.max_consecutive_losses})"
            return state["day_block"] + ". Nghỉ mở lệnh đến 00:00 UTC."
    return None


def acknowledge_halt(state: Dict, equity: float):
    """The user has reviewed the drawdown: restart the high-water mark from today's equity."""
    state.update(halted=False, halt_reason=None, peak_equity=equity)


def entry_block(state: Dict) -> Optional[str]:
    return state["halt_reason"] if state["halted"] else state["day_block"] or state["week_block"]


def risk_scale(state: Dict, equity: float, cfg: RiskConfig) -> float:
    return 0.5 if _loss_pct(state["peak_equity"], equity) >= cfg.dd_reduce_pct else 1.0


# ----------------------------- portfolio -----------------------------
# A position here is {"symbol", "direction", "quantity", "entry_price", "stop_price"}.

def position_risk(position: Dict) -> float:
    """What the position still loses if its stop is hit; zero once the stop locks in a profit."""
    s = 1 if position["direction"] == "LONG" else -1
    return max(0.0, s * (position["entry_price"] - position["stop_price"])) * position["quantity"]


def open_risk(positions: List[Dict]) -> float:
    return sum(position_risk(p) for p in positions)


def gross_exposure(positions: List[Dict]) -> float:
    return sum(p["quantity"] * p["entry_price"] for p in positions)


def plan_entry(*, symbol: str, direction: str, entry: float, stop: float, equity: float, available: float,
               positions: List[Dict], state: Dict, cfg: RiskConfig) -> Decision:
    """Approve and size one proposed trade, or say why not."""
    def no(code, reason):
        return Decision(False, reason, code)

    if equity <= 0:
        return no("NO_EQUITY", "Không xác định được vốn tài khoản")
    block = entry_block(state)
    if block:
        return no("CIRCUIT_BREAKER", block)
    s = 1 if direction == "LONG" else -1
    distance = s * (entry - stop) / entry if entry > 0 else 0
    if distance <= 0:
        return no("BAD_STOP", "SL nằm sai phía so với giá vào lệnh")
    if any(p["symbol"] == symbol for p in positions):
        return no("DUPLICATE", "Mã này đã có vị thế")
    if len(positions) >= cfg.max_positions:
        return no("MAX_POSITIONS", f"Đã đủ {cfg.max_positions} vị thế")
    if sum(p["direction"] == direction for p in positions) >= cfg.max_same_direction:
        return no("SAME_DIRECTION", f"Đã đủ {cfg.max_same_direction} vị thế {direction} (các coin đi cùng nhau)")
    if distance >= LIQUIDATION_BUFFER / cfg.leverage:
        return no("LIQUIDATION", f"SL cách {distance * 100:.1f}% quá gần giá thanh lý ở đòn bẩy {cfg.leverage}x")
    cost = cfg.round_trip_cost
    if cost > cfg.max_cost_r * distance:
        return no("COST", f"Phí + trượt giá ≈ {cost / distance:.2f}R, vượt {cfg.max_cost_r:g}R: SL quá sát so với chi phí")

    scale = risk_scale(state, equity, cfg)
    risk_usdt = equity * cfg.risk_pct / 100 * scale
    room = equity * cfg.max_open_risk_pct / 100 - open_risk(positions)
    if risk_usdt > room + 1e-9:
        return no("OPEN_RISK", f"Tổng rủi ro đang mở sẽ vượt {cfg.max_open_risk_pct:g}% vốn")
    notional = risk_usdt / (distance + cost)
    # Caps below only ever shrink the trade, so the risk actually taken can be less than risk_pct.
    notional = min(notional, equity * cfg.max_position_notional_pct / 100)
    gross_cap = equity * min(cfg.max_gross_exposure_pct, cfg.leverage * 90.0) / 100
    notional = min(notional, gross_cap - gross_exposure(positions))
    notional = min(notional, max(0.0, available) * MARGIN_BUFFER * cfg.leverage)
    if notional < MIN_NOTIONAL_USDT:
        return no("TOO_SMALL", "Khối lượng sau các trần vốn/ký quỹ nhỏ hơn mức tối thiểu của sàn")
    return Decision(True, "", "", notional, notional * (distance + cost), scale)


def snapshot(state: Dict, equity: Optional[float], positions: List[Dict], cfg: RiskConfig) -> Dict:
    """Limit usage for the dashboard."""
    if not equity or equity <= 0:
        return {"equity": None, "block_reason": entry_block(state), "halted": state["halted"],
                "loss_streak": state["loss_streak"], "peak_equity": state["peak_equity"]}
    return {
        "equity": equity,
        "peak_equity": state["peak_equity"],
        "drawdown_pct": round(_loss_pct(state["peak_equity"], equity), 3),
        "day_pnl_pct": round(-_loss_pct(state["day_start_equity"], equity), 3),
        "week_pnl_pct": round(-_loss_pct(state["week_start_equity"], equity), 3),
        "open_risk_pct": round(open_risk(positions) / equity * 100, 3),
        "gross_exposure_pct": round(gross_exposure(positions) / equity * 100, 2),
        "risk_scale": risk_scale(state, equity, cfg),
        "risk_per_trade_usdt": round(equity * cfg.risk_pct / 100 * risk_scale(state, equity, cfg), 4),
        "loss_streak": state["loss_streak"],
        "halted": state["halted"],
        "block_reason": entry_block(state),
        "day": state["day"],
        "week": state["week"],
    }
