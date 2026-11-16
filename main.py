import os
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Literal, Optional
from urllib.parse import quote

import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import auth
import backtest as backtest_module
import metrics
from binance_client import binance_client
from config import settings
from engine import BotConfig, TradingEngine
from journal import Journal
from risk import RiskConfig
from strategy import SETUPS, StrategyParams

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATE_DIR = Path(os.getenv("STATE_DIR") or BASE_DIR)
STATE_DIR.mkdir(parents=True, exist_ok=True)

engine = TradingEngine(binance_client, Journal(STATE_DIR / "trading.db"))


@asynccontextmanager
async def lifespan(app):
    engine.acquire_process_lock()
    try:
        if engine.state_error:
            raise RuntimeError("Trạng thái bot đã lưu không hợp lệ")
        engine.supervise()
        yield
    finally:
        engine.shutdown()


app = FastAPI(title="Trading Bot - Quản lý vốn & giao dịch tự động", version="4.0.0", lifespan=lifespan)


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


@app.exception_handler(ValueError)
async def invalid_operation(request, exc):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(RuntimeError)
async def unavailable_operation(request, exc):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# ----------------- login -----------------

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


# ----------------- bot -----------------

class SymbolRequest(BaseModel):
    symbol: str = Field(pattern=r"^[A-Za-z0-9]{2,30}$")


class SymbolsRequest(BaseModel):
    symbols: List[str] = Field(min_length=1, max_length=30)


def _result(result, fallback):
    """Engine results that are still being reconciled are 409, refusals are 400."""
    if result.get("success"):
        return result
    raise HTTPException(status_code=409 if result.get("pending") else 400,
                        detail=result.get("message") or result.get("error") or fallback)


@app.get("/api/status")
def get_status():
    status = engine.get_status()
    status["logs"] = engine.journal.events(80)
    status["exchange"] = {"futures_url": settings.FUTURES_BASE_URL}
    return status


@app.post("/api/bot/start")
def start_bot():
    return _result(engine.start(), "Không chạy được bot")


@app.post("/api/bot/stop")
def stop_bot():
    return engine.stop()


@app.post("/api/bot/acknowledge-drawdown")
def acknowledge_drawdown():
    return engine.acknowledge_drawdown()


@app.post("/api/bot/close-position")
def close_position(req: SymbolRequest):
    result = _result(engine.close_position(req.symbol.upper()), "Lỗi đóng vị thế")
    return {"success": True, "message": f"Đã gửi lệnh đóng {req.symbol.upper()}.", **result}


@app.post("/api/bot/close-all")
def close_all():
    result = engine.close_all_positions()
    if not result["success"] and not result.get("pending"):
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@app.post("/api/bot/close-untracked")
def close_untracked(req: SymbolsRequest):
    """Explicit, per-symbol close of exchange positions the bot does not manage."""
    return engine.close_untracked_positions(req.symbols)


@app.get("/api/config")
def get_config():
    return {"config": engine.config.model_dump(), "defaults": BotConfig().model_dump(),
            "setups": [vars(setup) for setup in SETUPS.values()]}


@app.post("/api/config")
def update_config(req: BotConfig):
    engine.update_config(req)
    return {"success": True, "message": "Đã lưu cấu hình.", "config": engine.config.model_dump()}


@app.get("/api/market/symbols")
def market_symbols():
    return {"symbols": sorted(binance_client.get_exchange_symbols("FUTURES"))}


# ----------------- dashboard -----------------

@app.get("/api/dashboard")
def dashboard(days: int = Query(30, ge=0, le=3650)):
    """Closed trades net of estimated fees, plus the account equity curve. days=0 = everything."""
    since = int((time.time() - days * 86_400) * 1000) if days else 0
    trades = engine.journal.trades(since)
    curve = engine.journal.equity_curve(since)
    return {
        "days": days,
        "summary": metrics.summarise(trades),
        "per_symbol": metrics.group(trades, "symbol"),
        "by_reason": metrics.group(trades, "reason"),
        "by_setup": metrics.group(trades, "setup"),
        "equity_curve": curve,
        "max_drawdown_pct": metrics.max_drawdown_pct([p["equity"] for p in curve]),
        "trades": list(reversed(trades))[:200],
    }


# ----------------- backtest -----------------

class BacktestRequest(BaseModel):
    model_config = {"extra": "forbid"}
    symbols: List[str] = Field(min_length=1, max_length=backtest_module.MAX_SYMBOLS)
    days: int = Field(365, ge=7, le=backtest_module.MAX_DAYS)
    initial_balance: float = Field(1000, gt=0, le=1e9, allow_inf_nan=False)
    risk: Optional[RiskConfig] = None           # default: the bot's saved configuration
    strategy: Optional[StrategyParams] = None
    robustness: bool = False
    source: Literal["live", "demo"] = "live"


_backtest_lock = threading.Lock()
_backtest_cache = OrderedDict()   # small LRU of downloaded candles for quick parameter tweaks


@app.post("/api/backtest")
def run_backtest(req: BacktestRequest):
    config = backtest_module.BacktestConfig(
        req.symbols, req.days, req.initial_balance, req.risk or engine.config.risk,
        req.strategy or engine.config.strategy, req.source)
    if not _backtest_lock.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="Đang chạy một backtest khác. Đợi xong rồi thử lại.")
    try:
        config.end_ms = int(time.time() * 1000) // 3_600_000 * 3_600_000
        # The candles to download depend on which setups are traded (their timeframes).
        key = (config.source, tuple(config.symbols), config.days, config.end_ms, tuple(config.strategy.setups))
        data = _backtest_cache.get(key)
        if data is None:
            data = backtest_module.load_data(config)
            _backtest_cache[key] = data
            while len(_backtest_cache) > 3:
                _backtest_cache.popitem(last=False)
        _backtest_cache.move_to_end(key)
        result = backtest_module.run_backtest(config, data)
        result["total_trades"] = len(result["trades"])
        result["trades"] = result["trades"][-200:]
        if req.robustness:
            result["robustness"] = backtest_module.robustness(config, data)
        return result
    except requests.RequestException as exc:
        raise HTTPException(status_code=503, detail=f"Không tải được dữ liệu nến: {exc}")
    finally:
        _backtest_lock.release()


# ----------------- static -----------------

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def read_root():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.HOST, port=settings.PORT)
