from urllib.parse import quote
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
import auth
from pydantic import BaseModel, Field
from typing import Optional, List, Literal
from math import isfinite
from pathlib import Path
from contextlib import asynccontextmanager
from collections import OrderedDict
import threading
import time
import requests
import backtest as backtest_module
import metrics
from trading_rules import BotConfigRequest, INTERVAL_SECONDS, validate_closed_candles

from config import settings
from binance_client import binance_client
from indicators import analyze_market_signals
from bot_manager import bot_manager

Market = Literal["SPOT", "FUTURES"]

@asynccontextmanager
async def lifespan(app):
    bot_manager.startup()
    try:
        yield
    finally:
        bot_manager.shutdown()


app = FastAPI(title="Binance Demo Trading & Academy Bot", version="3.0.0", lifespan=lifespan)

@app.middleware("http")
async def require_login(request, call_next):
    path = request.url.path
    config = auth.settings
    if path == "/healthz":
        return await call_next(request)
    if config.misconfigured:
        # Public deployment without credentials: refuse rather than expose trading controls.
        return JSONResponse(status_code=503, content={
            "detail": "Chưa cấu hình APP_USERNAME và APP_PASSWORD. Ứng dụng đã khóa để bảo vệ tài khoản."})
    if request.method not in ("GET", "HEAD", "OPTIONS") and not auth.same_origin(request):
        return JSONResponse(status_code=403, content={"detail": "Yêu cầu khác nguồn bị từ chối."})
    if not config.enabled or path in auth.PUBLIC_PATHS:
        return await call_next(request)
    if config.verify(request.cookies.get(auth.COOKIE_NAME)):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse(status_code=401, content={"detail": "Cần đăng nhập."})
    target = "/login" if path == "/" else f"/login?next={quote(path, safe='/')}"
    return RedirectResponse(target, status_code=303)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# ----------------- LOGIN -----------------

class LoginRequest(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=500)


@app.get("/login")
def login_page():
    return FileResponse(STATIC_DIR / "login.html", headers={"Cache-Control": "no-store"})


@app.get("/api/auth/status")
def auth_status(request: Request):
    config = auth.settings
    return {"enabled": config.enabled,
            "authenticated": config.enabled and config.verify(request.cookies.get(auth.COOKIE_NAME))}


@app.post("/api/auth/login")
def login(req: LoginRequest, request: Request):
    config = auth.settings
    if not config.enabled:
        raise HTTPException(status_code=400, detail="Chưa bật đăng nhập (thiếu APP_USERNAME/APP_PASSWORD).")
    ip = auth.client_ip(request)
    wait = config.locked_for(ip)
    if wait:
        raise HTTPException(status_code=429, detail=f"Sai quá nhiều lần. Thử lại sau {wait} giây.")
    if not config.check_credentials(req.username, req.password):
        config.record_failure(ip)
        time.sleep(0.5)   # slows guessing; the lockout is the real limit
        raise HTTPException(status_code=401, detail="Sai tên đăng nhập hoặc mật khẩu.")
    config.record_success(ip)
    response = JSONResponse({"success": True})
    response.set_cookie(auth.COOKIE_NAME, config.issue(), max_age=auth.SESSION_TTL_SEC, httponly=True,
                        samesite="strict", secure=auth.is_https(request), path="/")
    return response


@app.post("/api/auth/logout")
def logout(request: Request):
    auth.settings.revoke(request.cookies.get(auth.COOKIE_NAME))
    response = JSONResponse({"success": True})
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return response


@app.exception_handler(ValueError)
async def invalid_operation(request, exc):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(RuntimeError)
async def unavailable_operation(request, exc):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


# Request Models
class ManualOrderRequest(BaseModel):
    market_type: Literal["SPOT", "FUTURES"] = "SPOT" # "SPOT" or "FUTURES"
    symbol: str
    side: Literal["BUY", "SELL"]
    order_type: Literal["MARKET", "LIMIT"] = "MARKET" # MARKET or LIMIT
    quote_order_qty: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    quantity: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    price: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)
    leverage: Optional[int] = Field(default=10, ge=1, le=50)
    reduce_only: Optional[bool] = False

class LeverageRequest(BaseModel):
    symbol: str
    leverage: int = Field(ge=1, le=50)

class ClosePositionRequest(BaseModel):
    symbol: Optional[str] = None

STATIC_DIR = Path(__file__).resolve().parent / "static"

# ----------------- SYSTEM & ACCOUNT ENDPOINTS -----------------

@app.get("/api/status")
def get_system_status():
    server_stat = binance_client.get_server_status()
    masked_key = "Configured" if binance_client.has_credentials else "Not configured"
    return {
        "status": "online",
        "binance_demo": server_stat,
        "api_key_masked": masked_key,
        "spot_url": settings.SPOT_BASE_URL,
        "futures_url": settings.FUTURES_BASE_URL
    }

@app.get("/api/account")
def get_account_balances(market: str = Query("SPOT")):
    if market.upper() == "FUTURES":
        res = binance_client.get_futures_account_info()
        if not res.get("success"):
            raise HTTPException(status_code=400, detail=res.get("error", "Failed to fetch Futures account"))
        return res
    else:
        res = binance_client.get_account_info()
        if not res.get("success"):
            raise HTTPException(status_code=400, detail=res.get("error", "Failed to fetch Spot account"))
        return res

@app.get("/api/futures/positions")
def get_futures_positions(symbol: Optional[str] = None):
    """Retrieves live positions on Binance Futures Demo directly"""
    positions = binance_client.get_futures_positions(symbol=symbol)
    return {"positions": positions}

@app.post("/api/futures/leverage")
def set_leverage(req: LeverageRequest):
    with bot_manager.engines["FUTURES"].manual_action("FUTURES", req.symbol.upper()):
        res = binance_client.set_leverage(req.symbol, req.leverage)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "Failed to set leverage"))
    return res

# ----------------- MARKET DATA ENDPOINTS -----------------

@app.get("/api/market/symbols")
def get_available_symbols(market: Literal["SPOT", "FUTURES"] = "SPOT"):
    return {"market": market, "symbols": sorted(binance_client.get_exchange_symbols(market))}

@app.get("/api/market/ticker")
def get_tickers(symbol: Optional[str] = None, market: Literal["SPOT", "FUTURES"] = "SPOT"):
    watchlist = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "SUIUSDT", "NEARUSDT", "PEPEUSDT", "AVAXUSDT"]
    
    if symbol:
        binance_client.validate_symbol(market, symbol.upper())
        data = binance_client.get_ticker_price(symbol.upper(), market=market)
        if not data:
            raise HTTPException(status_code=503, detail="Market price unavailable")
        try:
            import requests
            base = f"{settings.FUTURES_BASE_URL}/fapi/v1" if market == "FUTURES" else f"{settings.SPOT_BASE_URL}/api/v3"
            r = requests.get(f"{base}/ticker/24hr", params={"symbol": symbol.upper()}, timeout=4)
            if r.status_code == 200:
                data["change24h"] = float(r.json().get("priceChangePercent", 0.0))
        except Exception:
            pass
        return {"data": data}
    
    all_tickers = binance_client.get_ticker_price(market=market)
    if isinstance(all_tickers, list):
        lookup = {t["symbol"]: float(t["price"]) for t in all_tickers if t["symbol"] in watchlist}
        return {"watchlist": lookup}
    return {"watchlist": {}}

def closed_candle_signals(symbol, candles, interval="15m"):
    closed = validate_closed_candles(candles, interval, binance_client._get_timestamp())
    analysis = analyze_market_signals(closed)
    analysis["symbol"] = symbol
    return analysis


@app.get("/api/market/klines")
def get_klines(symbol: str = "BTCUSDT", interval: str = "15m", limit: int = Query(100, ge=1, le=1000),
               market: Literal["SPOT", "FUTURES"] = "SPOT", include_signals: bool = False):
    symbol = symbol.upper()
    if interval not in INTERVAL_SECONDS:
        raise HTTPException(status_code=422, detail="Khung nến không được hỗ trợ")
    binance_client.validate_symbol(market, symbol)
    candles = binance_client.get_klines(symbol, interval=interval, limit=max(limit, 61) if include_signals else limit,
                                       market=market)
    if not candles:
        raise HTTPException(status_code=503, detail="Không lấy được nến từ Binance")
    result = {"symbol": symbol, "market": market, "interval": interval, "candles": candles[-limit:]}
    if include_signals and candles:
        result["signals"] = closed_candle_signals(symbol, candles[-61:], interval)
    return result


@app.get("/api/market/signals")
def get_technical_signals(symbol: str = "BTCUSDT", interval: str = "15m",
                          market: Literal["SPOT", "FUTURES"] = "SPOT"):
    if interval not in INTERVAL_SECONDS:
        raise HTTPException(status_code=422, detail="Khung nến không được hỗ trợ")
    binance_client.validate_symbol(market, symbol.upper())
    candles = binance_client.get_klines(symbol.upper(), interval=interval, limit=61, market=market)
    if not candles:
        raise HTTPException(status_code=503, detail="Could not load candlestick data")
    return closed_candle_signals(symbol.upper(), candles, interval)


# ----------------- ORDER EXECUTION (SPOT & FUTURES) -----------------

def order_error(result, fallback):
    if result.get("unknown"):
        return f"Lệnh chưa rõ kết quả. Kiểm tra trên Binance trước khi gửi lại. Mã: {result['client_order_id']}"
    return result.get("error", fallback)


@app.post("/api/order/manual")
def place_manual_order(req: ManualOrderRequest):
    with bot_manager.engines[req.market_type].manual_action(req.market_type, req.symbol.upper()):
        binance_client.validate_symbol(req.market_type, req.symbol.upper())
        return _place_manual_order(req)


def _place_manual_order(req):
    if not binance_client.has_credentials:
        raise HTTPException(status_code=503, detail="Cần cấu hình khóa Binance Demo.")
    sym = req.symbol.upper()
    m_type = req.market_type.upper()

    if m_type == "FUTURES":
        qty = req.quantity
        if qty is None and req.quote_order_qty is not None:
            ticker = binance_client.get_ticker_price(sym, market="FUTURES") if req.order_type == "MARKET" else None
            curr_price = req.price if req.order_type == "LIMIT" else float(ticker["price"]) if ticker else None
            if not curr_price or not isfinite(curr_price) or curr_price <= 0:
                raise HTTPException(status_code=422, detail="Không có giá hợp lệ để tính khối lượng.")
            notional = req.quote_order_qty * (req.leverage or 10)
            qty = notional / curr_price

        if qty is None or (req.order_type == "LIMIT" and req.price is None):
            raise HTTPException(status_code=422, detail="Lệnh thiếu giá hoặc khối lượng.")
        if not req.reduce_only:
            leverage = binance_client.set_leverage(sym, req.leverage or 10)
            if not leverage.get("success"):
                raise HTTPException(status_code=400, detail=leverage.get("error", "Không đổi được đòn bẩy."))
        res = binance_client.create_futures_order(
            symbol=sym,
            side=req.side.upper(),
            order_type=req.order_type.upper(),
            quantity=qty,
            price=req.price,
            reduce_only=req.reduce_only or False
        )
        if not res.get("success"):
            raise HTTPException(status_code=400, detail=order_error(res, "Futures order failed"))
        bot_manager.engines[m_type].add_log("MANUAL", f"Lệnh FUTURES {req.side} {sym} ({req.order_type}) thành công #{res['data'].get('orderId')}")
        return res

    else: # SPOT
        res = binance_client.create_order(
            symbol=sym,
            side=req.side.upper(),
            order_type=req.order_type.upper(),
            quantity=req.quantity,
            quote_order_qty=req.quote_order_qty,
            price=req.price
        )
        if not res.get("success"):
            raise HTTPException(status_code=400, detail=order_error(res, "Spot order failed"))
        bot_manager.engines[m_type].add_log("MANUAL", f"Lệnh SPOT {req.side} {sym} ({req.order_type}) thành công #{res['data'].get('orderId')}")
        return res

@app.get("/api/orders/open")
def get_open_orders(symbol: Optional[str] = None, market: str = Query("SPOT")):
    if market.upper() == "FUTURES":
        orders = binance_client.get_futures_open_orders(symbol=symbol)
    else:
        orders = binance_client.get_open_orders(symbol=symbol)
    return {"orders": orders}

@app.delete("/api/orders/{symbol}/{order_id}")
def cancel_order(symbol: str, order_id: int, market: Literal["SPOT", "FUTURES"] = "SPOT"):
    with bot_manager.engines[market].manual_action(market, symbol.upper()):
        return _cancel_order(symbol, order_id, market)


def _cancel_order(symbol, order_id, market):
    if market.upper() == "FUTURES":
        res = binance_client.cancel_futures_order(symbol=symbol.upper(), order_id=order_id)
    else:
        res = binance_client.cancel_order(symbol=symbol.upper(), order_id=order_id)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "Cancel failed"))
    return res

@app.get("/api/orders/history")
def get_trade_history(symbol: str = "BTCUSDT", limit: int = 20, market: str = Query("SPOT")):
    if market.upper() == "FUTURES":
        trades = binance_client.get_futures_trades(symbol=symbol.upper(), limit=limit)
    else:
        trades = binance_client.get_my_trades(symbol=symbol.upper(), limit=limit)
    return {"trades": list(reversed(trades))}

# ----------------- BOT CONTROLS -----------------

@app.get("/api/bot/status")
def get_bot_status(market: Market):
    return bot_manager.engines[market].get_status()

@app.get("/api/bots/status")
def get_bots_status():
    return {"bots": {market: engine.get_status() for market, engine in bot_manager.engines.items()}}

@app.get("/api/dashboard")
def get_dashboard(market: Literal["ALL", "SPOT", "FUTURES"] = "ALL", days: int = Query(30, ge=0, le=3650)):
    """Performance after estimated fees and current risk usage. days=0 means all recorded history."""
    engines = [e for m, e in bot_manager.engines.items() if market in ("ALL", m)]
    fills, open_keys = [], []
    # Older state files can hold fills of the other market, so filter by the fill, not the bot.
    for engine in bot_manager.engines.values():
        engine_fills, engine_open = engine.get_history()
        fills += [f for f in engine_fills if market in ("ALL", f.get("market_type"))]
        open_keys += engine_open
    result = metrics.summarise_fills(fills, days=days, open_keys=open_keys)
    result["market"] = market
    result["bots"] = [metrics.live_snapshot(engine.get_status()) for engine in engines]
    return result


@app.post("/api/bot/start")
def start_bot(market: Market):
    return bot_manager.engines[market].start()

@app.post("/api/bot/stop")
def stop_bot(market: Market):
    return bot_manager.engines[market].stop()

@app.post("/api/bot/config")
def update_bot_config(req: BotConfigRequest, market: Market):
    bot_engine = bot_manager.engines[market]
    saved = bot_engine.update_config(
        market_type=req.market_type,
        leverage=req.leverage,
        symbol=req.symbol,
        watchlist_symbols=req.watchlist_symbols,
        max_positions=req.max_positions,
        strategy=req.strategy,
        amount=req.trade_amount_usdt,
        sl=req.stop_loss_pct,
        tp=req.take_profit_pct,
        interval_sec=req.scan_interval_sec,
        interval=req.interval,
        max_total_margin_usdt=req.max_total_margin_usdt,
        max_open_risk_usdt=req.max_open_risk_usdt,
        exit_mode=req.exit_mode,
        atr_sl_mult=req.atr_sl_mult,
        atr_tp_mult=req.atr_tp_mult,
        trend_filter=req.trend_filter,
        cooldown_candles=req.cooldown_candles,
        breakeven_at_r=req.breakeven_at_r,
        risk_per_trade_usdt=req.risk_per_trade_usdt,
        max_daily_loss_usdt=req.max_daily_loss_usdt,
        max_consecutive_losses=req.max_consecutive_losses,
    )
    if not saved:
        raise HTTPException(status_code=503, detail="Không lưu được cấu hình bot.")
    return {"success": True, "message": "Cấu hình bot đã được lưu!", "config": bot_engine.get_status()}

@app.post("/api/bot/close-position")
def close_bot_position(market: Market, req: Optional[ClosePositionRequest] = None, symbol: Optional[str] = None):
    bot_engine = bot_manager.engines[market]
    target_symbol = symbol or (req.symbol if req else None)
    
    with bot_engine.lock:
        if not bot_engine.active_positions:
            raise HTTPException(status_code=400, detail="Không có vị thế nào đang mở để đóng!")
        if not target_symbol:
            target_symbol = next(iter(bot_engine.active_positions.keys()))
        elif target_symbol not in bot_engine.active_positions:
            raise HTTPException(status_code=400, detail=f"Không tìm thấy vị thế đang mở cho {target_symbol}")
        pos = bot_engine.active_positions[target_symbol]

    res = bot_engine._close_position(target_symbol, "MANUAL_FORCE_CLOSE", expected_position=pos)
    if not res.get("success"):
        raise HTTPException(status_code=409 if res.get("pending") else 400,
                            detail=res.get("error", "Lỗi đóng vị thế"))
    remaining = res["remaining_quantity"]
    message = f"Đã khớp lệnh đóng {target_symbol}. Còn lại: {remaining:g}."
    return {"success": True, "message": message, "trade": res.get("trade"), "remaining_quantity": remaining}


@app.post("/api/bot/close-all")
def close_all_bot_positions(market: Market):
    res = bot_manager.engines[market].close_all_positions("MANUAL_FORCE_CLOSE_ALL")
    return res


class CloseUntrackedRequest(BaseModel):
    symbols: List[str] = Field(min_length=1, max_length=30)


@app.post("/api/bot/close-untracked")
def close_untracked_positions(market: Market, req: CloseUntrackedRequest):
    """Explicit, per-symbol close of exchange positions the bot does not manage."""
    return bot_manager.engines[market].close_untracked_positions(req.symbols)


# ----------------- BACKTEST -----------------

class BacktestRequest(BaseModel):
    model_config = {"extra": "forbid"}
    symbols: List[str] = Field(min_length=1, max_length=backtest_module.MAX_SYMBOLS)
    market: Market = "FUTURES"
    strategies: List[Literal["RSI_SCALPING", "EMA_CROSSOVER", "HYBRID_SMART"]] = Field(min_length=1, max_length=3)
    interval: str = "15m"
    days: int = Field(30, ge=1, le=backtest_module.MAX_DAYS)
    leverage: int = Field(10, ge=1, le=50)
    trade_amount_usdt: float = Field(20, gt=0, allow_inf_nan=False)
    take_profit_pct: float = Field(3, gt=0, lt=100, allow_inf_nan=False)
    stop_loss_pct: float = Field(1.5, gt=0, lt=100, allow_inf_nan=False)
    max_positions: int = Field(3, ge=1, le=10)
    max_total_margin_usdt: Optional[float] = Field(None, gt=0, allow_inf_nan=False)
    max_open_risk_usdt: Optional[float] = Field(None, gt=0, allow_inf_nan=False)
    fee_pct: Optional[float] = Field(None, ge=0, lt=1, allow_inf_nan=False)
    slippage_bps: float = Field(2, ge=0, le=100, allow_inf_nan=False)
    cooldown_candles: int = Field(0, ge=0, le=100)
    exit_mode: Literal["ROE", "ATR"] = "ROE"
    atr_sl_mult: float = Field(1.5, ge=0.3, le=10, allow_inf_nan=False)
    atr_tp_mult: float = Field(3.0, ge=0.3, le=20, allow_inf_nan=False)
    trend_filter: bool = False
    breakeven_at_r: float = Field(0, ge=0, le=10, allow_inf_nan=False)
    risk_per_trade_usdt: float = Field(0, ge=0, le=1_000_000, allow_inf_nan=False)
    max_daily_loss_usdt: float = Field(0, ge=0, le=1_000_000, allow_inf_nan=False)
    max_consecutive_losses: int = Field(0, ge=0, le=50)
    initial_balance: float = Field(1000, gt=0, allow_inf_nan=False)
    source: Literal["live", "demo"] = "live"


_backtest_lock = threading.Lock()
_backtest_cache = OrderedDict()   # small LRU of downloaded candles for quick parameter tweaks


@app.post("/api/backtest")
def run_backtest_endpoint(req: BacktestRequest):
    params = req.model_dump(exclude={"strategies", "fee_pct"})
    params["fee_rate"] = None if req.fee_pct is None else req.fee_pct / 100
    configs = [backtest_module.BacktestConfig(strategy=s, **params) for s in dict.fromkeys(req.strategies)]
    if not _backtest_lock.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="Đang chạy một backtest khác. Đợi xong rồi thử lại.")
    try:
        base = configs[0]
        step = INTERVAL_SECONDS[base.interval] * 1000
        end_ms = int(time.time() * 1000) // step * step
        key = (base.source, base.market, tuple(base.symbols), base.interval, base.days, end_ms)
        data = _backtest_cache.get(key)
        if data is None:
            for config in configs:
                config.end_ms = end_ms
            data = backtest_module.load_data(base)
            _backtest_cache[key] = data
            while len(_backtest_cache) > 4:
                _backtest_cache.popitem(last=False)
        _backtest_cache.move_to_end(key)
        results = []
        for config in configs:
            result = backtest_module.run_backtest(config, data).to_dict()
            result["total_trades"] = len(result["trades"])
            result["trades"] = result["trades"][-200:]
            results.append(result)
        return {"results": results}
    except requests.RequestException as exc:
        raise HTTPException(status_code=503, detail=f"Không tải được dữ liệu nến: {exc}")
    finally:
        _backtest_lock.release()

# Serve static files and root
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/")
def read_root():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.HOST, port=settings.PORT)
