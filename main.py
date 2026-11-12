import os
import base64
import binascii
import secrets
from fastapi import FastAPI, HTTPException, Query, Body
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from typing import Optional, List, Literal
from math import isfinite
from pathlib import Path
from contextlib import asynccontextmanager
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

# Optional HTTP Basic Auth: enabled only when both APP_USERNAME and APP_PASSWORD are set.
_AUTH_USER = os.getenv("APP_USERNAME", "")
_AUTH_PASS = os.getenv("APP_PASSWORD", "")
_AUTH_EXEMPT = {"/healthz"}


@app.middleware("http")
async def basic_auth(request, call_next):
    if not (_AUTH_USER and _AUTH_PASS) or request.url.path in _AUTH_EXEMPT:
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (binascii.Error, UnicodeDecodeError):
            user, password = "", ""
        if secrets.compare_digest(user.encode(), _AUTH_USER.encode()) and \
                secrets.compare_digest(password.encode(), _AUTH_PASS.encode()):
            return await call_next(request)
    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Tracking-Trading"'})


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


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

# Serve static files and root
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/")
def read_root():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.HOST, port=settings.PORT)
