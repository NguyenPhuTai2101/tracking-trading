import time
import hmac
import hashlib
from decimal import Decimal, ROUND_DOWN, ROUND_UP, ROUND_HALF_UP
from collections import OrderedDict
from threading import Lock, local
import requests
from urllib.parse import urlencode
from uuid import uuid4
from typing import Dict, Any, Optional, List
from config import settings

class BinanceDemoClient:
    def __init__(self, api_key: str = None, secret_key: str = None, spot_url: str = None, futures_url: str = None):
        self.api_key = api_key or settings.API_KEY
        self.secret_key = secret_key or settings.SECRET_KEY
        self.spot_url = spot_url or settings.SPOT_BASE_URL
        self.futures_url = futures_url or settings.FUTURES_BASE_URL
        self.time_offset = 0
        self.http_local = local()
        self.request_lock = Lock()
        self.retry_after = 0.0
        self.exchange_cache = {}
        self.exchange_lock = Lock()
        self.clock_lock = Lock()
        self.clock_synced_at = None
        self.kline_cache = OrderedDict()
        self.kline_cache_lock = Lock()
        self.kline_locks = [Lock() for _ in range(32)]
        self.spot_symbol_filters: Dict[str, Dict[str, Any]] = {}
        self.futures_symbol_filters: Dict[str, Dict[str, Any]] = {}


    def _request(self, method, url, **kwargs):
        # Sessions are thread-local because FastAPI and the bot use different workers.
        with self.request_lock:
            if time.monotonic() < self.retry_after:
                raise RuntimeError("Binance đang giới hạn yêu cầu. Đợi trước khi thử lại.")
        if not hasattr(self.http_local, "session"):
            self.http_local.session = requests.Session()
        response = self.http_local.session.request(method, url, **kwargs)
        if response.status_code in (418, 429):
            try:
                delay = max(1, float(response.headers.get("Retry-After", 60)))
            except ValueError:
                delay = 60
            with self.request_lock:
                self.retry_after = time.monotonic() + delay
        return response

    def get_exchange_symbols(self, market):
        with self.exchange_lock:
            cached = self.exchange_cache.get(market)
            if cached and time.monotonic() - cached[0] < 300:
                return cached[1]
            base = self.futures_url if market == "FUTURES" else self.spot_url
            prefix = "/fapi/v1" if market == "FUTURES" else "/api/v3"
            response = self._request("GET", base + prefix + "/exchangeInfo", timeout=6)
            if not response.ok:
                raise RuntimeError("Không tải được quy tắc thị trường từ Binance")
            symbols = {s["symbol"]: s for s in response.json()["symbols"]
                       if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT"}
            self.exchange_cache[market] = (time.monotonic(), symbols)
            return symbols

    def validate_symbol(self, market, symbol):
        if symbol not in self.get_exchange_symbols(market):
            raise ValueError(f"{symbol} không giao dịch trên {market}. Hãy chọn mã trong danh sách của thị trường.")

    def _load_filters(self, market, symbol):
        self.validate_symbol(market, symbol)
        info = self.get_exchange_symbols(market)[symbol]
        filters = {"baseAsset": info["baseAsset"], "quoteAsset": info["quoteAsset"]}
        for rule in info["filters"]:
            kind = rule["filterType"]
            if kind == "LOT_SIZE":
                filters.update({key: rule[key] for key in ("stepSize", "minQty", "maxQty")})
            elif kind == "MARKET_LOT_SIZE":
                filters["marketLot"] = rule
            elif kind == "PRICE_FILTER":
                filters.update({key: rule[key] for key in ("tickSize", "minPrice", "maxPrice")})
            elif kind in ("MIN_NOTIONAL", "NOTIONAL"):
                filters["minNotional"] = rule.get("minNotional", rule.get("notional", "0"))
                filters["maxNotional"] = rule.get("maxNotional", "0")
                filters["applyMinToMarket"] = rule.get("applyMinToMarket", rule.get("applyToMarket", True))
                filters["applyMaxToMarket"] = rule.get("applyMaxToMarket", False)
        return filters

    def prepare_entry(self, market, symbol, budget, leverage, expected_price):
        """Size down to the user's budget, never raise it to an exchange minimum."""
        filters = self._load_filters(market, symbol)
        ticker = self.get_ticker_price(symbol, market=market)
        if not ticker:
            raise RuntimeError("Không lấy được giá hiện tại")
        price = Decimal(str(ticker["price"]))
        expected = Decimal(str(expected_price))
        if not price.is_finite() or price <= 0 or expected <= 0:
            raise ValueError("Giá không hợp lệ")
        if abs(price / expected - 1) > Decimal("0.01"):
            raise ValueError("Giá đã lệch quá 1% so với tín hiệu. Bỏ qua lệnh.")
        notional = Decimal(str(budget)) * (leverage if market == "FUTURES" else 1)
        qty = self._format_order_quantity(notional / price, filters, True)
        actual = Decimal(qty) * price if market == "FUTURES" else notional
        minimum = Decimal(str(filters.get("minNotional", 0)))
        if filters.get("applyMinToMarket", True) and actual < minimum:
            raise ValueError(f"Ngân sách hiện tại dưới mức tối thiểu {minimum} USDT của {symbol}.")
        maximum = Decimal(str(filters.get("maxNotional", 0)))
        if filters.get("applyMaxToMarket") and maximum > 0 and actual > maximum:
            raise ValueError(f"Giá trị lệnh vượt giới hạn của {symbol}.")
        return float(qty), float(price)

    def cancel_by_client_id(self, market, symbol, client_order_id):
        try:
            query = self._sign_params({"symbol": symbol, "origClientOrderId": client_order_id})
            response = self._request("DELETE", self._order_url(market) + "?" + query,
                                     headers=self._get_headers(), timeout=6)
            if response.ok:
                return {"success": True, "data": response.json()}
        except (requests.RequestException, RuntimeError, ValueError):
            pass
        # A cancel timeout does not prove cancellation; always query the original id.
        return self.query_order(market, symbol, client_order_id)

    def sync_time(self):
        """Calculates difference between local clock and Binance server clock"""
        try:
            url = f"{self.spot_url}/api/v3/time"
            sent_at = time.time()
            res = self._request("GET", url, timeout=5)
            if res.status_code == 200:
                server_time = res.json()["serverTime"]
                local_time = int((sent_at + time.time()) * 500)
                self.time_offset = server_time - local_time
                return True
        except Exception as e:
            print(f"[BinanceClient] Warning: Failed to sync server time: {e}")
        return False

    def _get_timestamp(self) -> int:
        return int(time.time() * 1000) + self.time_offset

    def _sign_params(self, params: Dict[str, Any]) -> str:
        if not self.has_credentials:
            raise ValueError("Configure BINANCE_API_KEY and BINANCE_SECRET_KEY")
        with self.clock_lock:
            now = time.monotonic()
            if self.clock_synced_at is None or now - self.clock_synced_at >= 300:
                if self.sync_time():
                    self.clock_synced_at = now
        params["timestamp"] = self._get_timestamp()
        if "recvWindow" not in params:
            params["recvWindow"] = 10000
        
        query_string = urlencode({k: v for k, v in params.items() if v is not None})
        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        return f"{query_string}&signature={signature}"

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.secret_key)

    def query_order(self, market: str, symbol: str, client_order_id: Optional[str] = None,
                    order_id: Optional[str] = None) -> Dict[str, Any]:
        url = self._order_url(market)
        try:
            query = self._sign_params({"symbol": symbol, "origClientOrderId": client_order_id,
                                       "orderId": order_id})
            response = self._request("GET", f"{url}?{query}", headers=self._get_headers(), timeout=5)
            if response.ok:
                return {"success": True, "data": response.json()}
        except (requests.RequestException, RuntimeError, ValueError):
            pass
        return {"success": False, "unknown": True, "client_order_id": client_order_id,
                "error": "Không xác nhận được trạng thái lệnh"}

    def _order_url(self, market: str) -> str:
        if market == "FUTURES":
            return f"{self.futures_url}/fapi/v1/order"
        return f"{self.spot_url}/api/v3/order"

    def query_futures_stop(self, client_algo_id):
        try:
            query = self._sign_params({"clientAlgoId": client_algo_id})
            response = self._request("GET", f"{self.futures_url}/fapi/v1/algoOrder?{query}",
                                     headers=self._get_headers(), timeout=6)
            if response.ok:
                return {"success": True, "data": response.json()}
        except (requests.RequestException, RuntimeError, ValueError):
            pass
        return {"success": False, "unknown": True, "error": "Chưa xác nhận được lệnh cắt lỗ trên sàn"}

    def cancel_futures_stop(self, client_algo_id):
        try:
            query = self._sign_params({"clientAlgoId": client_algo_id})
            self._request("DELETE", f"{self.futures_url}/fapi/v1/algoOrder?{query}",
                          headers=self._get_headers(), timeout=6)
        except (requests.RequestException, RuntimeError, ValueError):
            pass
        # DELETE only acknowledges the request. Query catches a concurrent trigger/fill.
        return self.query_futures_stop(client_algo_id)

    def create_futures_stop(self, symbol, side, quantity, trigger_price, client_algo_id):
        try:
            filters = self.load_futures_symbol_filters(symbol)
            params = {
                "symbol": symbol, "side": side, "type": "STOP_MARKET",
                "algoType": "CONDITIONAL", "workingType": "MARK_PRICE",
                "reduceOnly": "true", "clientAlgoId": client_algo_id,
                "quantity": self._format_order_quantity(quantity, filters, True),
                # Round toward entry so exchange precision cannot widen the stop.
                "triggerPrice": self._format_increment(trigger_price, filters.get("tickSize"),
                                                        ROUND_UP if side == "SELL" else ROUND_DOWN),
            }
            query = self._sign_params(params)
        except (ValueError, RuntimeError):
            return {"success": False, "error": "Không chuẩn bị được lệnh cắt lỗ theo quy tắc sàn"}
        try:
            response = self._request("POST", f"{self.futures_url}/fapi/v1/algoOrder", data=query,
                                     headers=self._get_headers(), timeout=10)
            data = response.json()
            if response.ok:
                return {"success": True, "data": data}
            if response.status_code < 500 and data.get("code") not in (-1006, -1007):
                return {"success": False, "error": data.get("msg", "Lệnh cắt lỗ bị từ chối")}
        except (requests.RequestException, RuntimeError, ValueError):
            pass
        # Never repost an ambiguous submission, even with the same client id.
        return self.query_futures_stop(client_algo_id)

    def _submit_order(self, market: str, params: Dict[str, Any], client_order_id: Optional[str]):
        client_order_id = client_order_id or uuid4().hex
        params["newClientOrderId"] = client_order_id
        query = self._sign_params(params)
        try:
            response = self._request("POST", self._order_url(market), data=query,
                                     headers=self._get_headers(), timeout=10)
            data = response.json()
            if response.ok:
                return {"success": True, "data": data, "client_order_id": client_order_id}
            if response.status_code < 500 and data.get("code") not in (-1006, -1007):
                return {"success": False, "error": data.get("msg", str(data))}
        except (requests.RequestException, RuntimeError, ValueError):
            pass
        return self.query_order(market, params["symbol"], client_order_id)

    def _get_headers(self) -> Dict[str, str]:
        return {
            "X-MBX-APIKEY": self.api_key,
            "Content-Type": "application/x-www-form-urlencoded"
        }

    def get_server_status(self) -> Dict[str, Any]:
        try:
            t0 = time.time()
            res_spot = self._request("GET", f"{self.spot_url}/api/v3/ping", timeout=5)
            latency_ms = int((time.time() - t0) * 1000)
            
            res_futures = self._request("GET", f"{self.futures_url}/fapi/v1/ping", timeout=5)
            futures_ok = res_futures.status_code == 200
            
            return {
                "connected": res_spot.status_code == 200,
                "futures_connected": futures_ok,
                "latency_ms": latency_ms
            }
        except Exception as e:
            return {"connected": False, "futures_connected": False, "error": str(e), "latency_ms": -1}

    # ==================== SPOT METHODS ====================

    def get_account_info(self) -> Dict[str, Any]:
        """Fetches spot demo account balances and status"""
        try:
            query = self._sign_params({})
            url = f"{self.spot_url}/api/v3/account?{query}"
            res = self._request("GET", url, headers=self._get_headers(), timeout=10)
            data = res.json()
            if res.status_code != 200 and data.get("code") == -1021:
                self.sync_time()
                query = self._sign_params({})
                url = f"{self.spot_url}/api/v3/account?{query}"
                res = self._request("GET", url, headers=self._get_headers(), timeout=10)
                data = res.json()
            
            if res.status_code == 200:
                balances = []
                for b in data.get("balances", []):
                    free = float(b["free"])
                    locked = float(b["locked"])
                    if free > 0 or locked > 0 or b["asset"] in ["USDT", "BTC", "ETH", "USDC"]:
                        balances.append({
                            "asset": b["asset"],
                            "free": round(free, 6),
                            "locked": round(locked, 6),
                            "total": round(free + locked, 6)
                        })
                return {"success": True, "balances": balances, "canTrade": data.get("canTrade", True)}
            return {"success": False, "error": data.get("msg", "Unknown error")}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_asset_balance(self, asset: str) -> float:
        """Returns exact unrounded free balance for a spot asset"""
        try:
            query = self._sign_params({})
            url = f"{self.spot_url}/api/v3/account?{query}"
            res = self._request("GET", url, headers=self._get_headers(), timeout=10)
            if res.status_code == 200:
                data = res.json()
                for b in data.get("balances", []):
                    if b["asset"].upper() == asset.upper():
                        return float(b["free"])
        except Exception as e:
            print(f"[BinanceClient] Error get_asset_balance: {e}")
        return 0.0

    def get_ticker_price(self, symbol: Optional[str] = None, market: str = "SPOT", mark: bool = False) -> Any:
        try:
            url = f"{self.spot_url}/api/v3/ticker/price"
            if market == "FUTURES":
                path = "premiumIndex" if mark else "ticker/price"
                url = f"{self.futures_url}/fapi/v1/{path}"
            params = {"symbol": symbol} if symbol else {}
            res = self._request("GET", url, params=params, timeout=5)
            if res.status_code == 200:
                data = res.json()
                if mark and market == "FUTURES" and symbol:
                    return {"symbol": symbol, "price": data["markPrice"]}
                return data
            return None
        except Exception as e:
            print(f"[BinanceClient] Error get_ticker_price: {e}")
            return None

    def get_klines(self, symbol: str, interval: str = "15m", limit: int = 100, market: str = "SPOT") -> List[Dict[str, Any]]:
        # Dashboard and bot share one baseline window and one in-flight request per key.
        size = max(100, limit)
        key = (market, symbol, interval, size)
        with self.kline_locks[hash(key) % len(self.kline_locks)]:
            with self.kline_cache_lock:
                cached = self.kline_cache.get(key)
                if cached and time.monotonic() - cached[0] < 3 and cached[2] > time.time() * 1000:
                    # Do not promote a cached open candle to a closed candle.
                    self.kline_cache.move_to_end(key)
                    return cached[1][-limit:]
            candles = self._fetch_klines(symbol, interval, size, market)
            if candles:
                with self.kline_cache_lock:
                    self.kline_cache[key] = (time.monotonic(), candles, candles[-1]["close_time_ms"])
                    self.kline_cache.move_to_end(key)
                    if len(self.kline_cache) > 256:
                        self.kline_cache.popitem(last=False)
            return candles[-limit:]

    def _fetch_klines(self, symbol: str, interval: str = "15m", limit: int = 100, market: str = "SPOT") -> List[Dict[str, Any]]:
        try:
            url = f"{self.spot_url}/api/v3/klines"
            if market == "FUTURES":
                url = f"{self.futures_url}/fapi/v1/klines"
            params = {"symbol": symbol, "interval": interval, "limit": limit}
            requested_at_ms = self._get_timestamp()
            res = self._request("GET", url, params=params, timeout=10)
            if res.status_code == 200:
                candles = []
                for row in res.json():
                    candles.append({
                        "time": int(row[0] / 1000),
                        "open": float(row[1]),
                        "high": float(row[2]),
                        "low": float(row[3]),
                        "close": float(row[4]),
                        "volume": float(row[5]),
                        "close_time_ms": int(row[6]),
                        "closed": int(row[6]) < requested_at_ms
                    })
                return candles
            return []
        except Exception as e:
            print(f"[BinanceClient] Error get_klines: {e}")
            return []

    def load_symbol_filters(self, symbol: str) -> Dict[str, Any]:
        return self._load_filters("SPOT", symbol)

    @staticmethod
    def _format_increment(value, increment, rounding):
        if not increment:
            raise ValueError("Exchange trading increment unavailable")
        number, step = Decimal(str(value)), Decimal(str(increment))
        if not number.is_finite() or number <= 0 or step <= 0:
            raise ValueError("Price and quantity must be positive")
        rounded = (number / step).to_integral_value(rounding=rounding) * step
        if rounded <= 0:
            raise ValueError("Value below exchange increment")
        return format(rounded, "f")

    @classmethod
    def _format_order_quantity(cls, quantity, filters, market_order):
        lot = filters.get("marketLot", {}) if market_order else {}
        step = lot.get("stepSize")
        if not step or Decimal(step) == 0:
            step = filters.get("stepSize")
        formatted = cls._format_increment(quantity, step, ROUND_DOWN)
        qty = Decimal(formatted)
        minimum = Decimal(str(lot.get("minQty", filters.get("minQty", 0))))
        maximum = Decimal(str(lot.get("maxQty", filters.get("maxQty", 0))))
        if qty < minimum or (maximum > 0 and qty > maximum):
            raise ValueError("Quantity outside exchange limits")
        base_step = Decimal(str(filters.get("stepSize", 0)))
        if base_step > 0 and qty % base_step:
            raise ValueError("Quantity does not match LOT_SIZE")
        if qty < Decimal(str(filters.get("minQty", 0))):
            raise ValueError("Quantity below LOT_SIZE minimum")
        base_max = Decimal(str(filters.get("maxQty", 0)))
        if base_max > 0 and qty > base_max:
            raise ValueError("Quantity exceeds LOT_SIZE maximum")
        return formatted

    def format_quantity(self, symbol: str, quantity: float, market_order: bool = False) -> str:
        filters = self.load_symbol_filters(symbol)
        return self._format_order_quantity(quantity, filters, market_order)

    def format_price(self, symbol: str, price: float) -> str:
        filters = self.load_symbol_filters(symbol)
        return self._format_increment(price, filters.get("tickSize"), ROUND_HALF_UP)

    def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str = "MARKET",
        quantity: Optional[float] = None,
        quote_order_qty: Optional[float] = None,
        price: Optional[float] = None,
        time_in_force: str = "GTC",
        client_order_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Places a real demo order on Binance Spot Demo"""
        if not self.has_credentials:
            return {"success": False, "error": "Configure Binance credentials"}
        try:
            params: Dict[str, Any] = {
                "symbol": symbol.upper(),
                "side": side.upper(),
                "type": order_type.upper(),
            }
            if order_type.upper() == "MARKET":
                if quote_order_qty is not None and side.upper() == "BUY":
                    params["quoteOrderQty"] = f"{quote_order_qty:.2f}"
                elif quantity is not None:
                    params["quantity"] = self.format_quantity(symbol, quantity, market_order=True)
                elif quote_order_qty is not None:
                    ticker = self.get_ticker_price(symbol)
                    if not ticker or float(ticker["price"]) <= 0:
                        return {"success": False, "error": "Spot price unavailable"}
                    curr_price = float(ticker["price"])
                    params["quantity"] = self.format_quantity(symbol, quote_order_qty / curr_price, market_order=True)
            elif order_type.upper() == "LIMIT":
                if quantity is None or price is None:
                    return {"success": False, "error": "LIMIT order requires quantity and price"}
                params["quantity"] = self.format_quantity(symbol, quantity)
                params["price"] = self.format_price(symbol, price)
                params["timeInForce"] = time_in_force

            if "quantity" not in params and "quoteOrderQty" not in params:
                return {"success": False, "error": "Order quantity is required"}
            params["newOrderRespType"] = "FULL"
            return self._submit_order("SPOT", params, client_order_id)
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        try:
            params = {"symbol": symbol.upper()} if symbol else {}
            query = self._sign_params(params)
            url = f"{self.spot_url}/api/v3/openOrders?{query}"
            res = self._request("GET", url, headers=self._get_headers(), timeout=10)
            if res.status_code == 200:
                return res.json()
            return []
        except Exception as e:
            print(f"[BinanceClient] Error get_open_orders: {e}")
            return []

    def cancel_order(self, symbol: str, order_id: int) -> Dict[str, Any]:
        try:
            params = {"symbol": symbol.upper(), "orderId": order_id}
            query = self._sign_params(params)
            url = f"{self.spot_url}/api/v3/order?{query}"
            res = self._request("DELETE", url, headers=self._get_headers(), timeout=10)
            data = res.json()
            if res.status_code == 200:
                return {"success": True, "data": data}
            return {"success": False, "error": data.get("msg", str(data))}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_my_trades(self, symbol: str = "BTCUSDT", limit: int = 15) -> List[Dict[str, Any]]:
        try:
            params = {"symbol": symbol.upper(), "limit": limit}
            query = self._sign_params(params)
            url = f"{self.spot_url}/api/v3/myTrades?{query}"
            res = self._request("GET", url, headers=self._get_headers(), timeout=10)
            if res.status_code == 200:
                return res.json()
            return []
        except Exception as e:
            print(f"[BinanceClient] Error get_my_trades: {e}")
            return []

    # ==================== FUTURES METHODS ====================

    def get_futures_account_info(self) -> Dict[str, Any]:
        """Fetches Binance Futures Demo balance and margin information"""
        try:
            query = self._sign_params({})
            url = f"{self.futures_url}/fapi/v2/account?{query}"
            res = self._request("GET", url, headers=self._get_headers(), timeout=10)
            data = res.json()
            if res.status_code == 200:
                total_wallet = float(data.get("totalWalletBalance", 0.0))
                total_unrealized = float(data.get("totalUnrealizedProfit", 0.0))
                total_margin = float(data.get("totalMarginBalance", 0.0))
                available_balance = float(data.get("availableBalance", 0.0))
                
                # Active non-zero positions
                positions = []
                for p in data.get("positions", []):
                    amt = float(p.get("positionAmt", 0))
                    if amt != 0:
                        positions.append({
                            "symbol": p["symbol"],
                            "positionAmt": amt,
                            "entryPrice": float(p.get("entryPrice", 0)),
                            "unrealizedProfit": float(p.get("unrealizedProfit", 0)),
                            "leverage": int(p.get("leverage", 1)),
                            "liquidationPrice": float(p.get("liquidationPrice", 0)),
                            "marginType": p.get("marginType", "cross"),
                            "side": "LONG" if amt > 0 else "SHORT"
                        })
                
                return {
                    "success": True,
                    "totalWalletBalance": round(total_wallet, 2),
                    "totalUnrealizedProfit": round(total_unrealized, 2),
                    "totalMarginBalance": round(total_margin, 2),
                    "availableBalance": round(available_balance, 2),
                    "positions": positions
                }
            return {"success": False, "error": data.get("msg", str(data))}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_futures_positions(self, symbol: Optional[str] = None, strict: bool = False) -> List[Dict[str, Any]]:
        """Retrieves risk and positions for symbols"""
        try:
            params = {"symbol": symbol.upper()} if symbol else {}
            query = self._sign_params(params)
            url = f"{self.futures_url}/fapi/v2/positionRisk?{query}"
            res = self._request("GET", url, headers=self._get_headers(), timeout=10)
            if res.status_code == 200:
                active = []
                for p in res.json():
                    amt = float(p.get("positionAmt", 0))
                    if amt != 0 or (symbol and p["symbol"] == symbol.upper()):
                        active.append({
                            "symbol": p["symbol"],
                            "positionAmt": amt,
                            "positionSide": p.get("positionSide", "BOTH"),
                            "entryPrice": float(p.get("entryPrice", 0)),
                            "markPrice": float(p.get("markPrice", 0)),
                            "unrealizedProfit": float(p.get("unRealizedProfit", 0)),
                            "liquidationPrice": float(p.get("liquidationPrice", 0)),
                            "leverage": int(p.get("leverage", 1)),
                            "side": "LONG" if amt > 0 else ("SHORT" if amt < 0 else "NONE")
                        })
                return active
            if strict:
                raise RuntimeError(f"Futures position lookup failed ({res.status_code})")
            return []
        except Exception as e:
            if strict:
                raise RuntimeError("Futures positions unavailable") from e
            print(f"[BinanceClient] Error get_futures_positions: {e}")
            return []

    def set_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        """Sets leverage for a futures symbol (e.g. 5, 10, 20)"""
        try:
            params = {
                "symbol": symbol.upper(),
                "leverage": leverage
            }
            query = self._sign_params(params)
            url = f"{self.futures_url}/fapi/v1/leverage"
            res = self._request("POST", url, data=query, headers=self._get_headers(), timeout=10)
            data = res.json()
            if res.status_code == 200:
                return {"success": True, "leverage": data.get("leverage", leverage), "symbol": symbol}
            return {"success": False, "error": data.get("msg", str(data))}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def load_futures_symbol_filters(self, symbol: str) -> Dict[str, Any]:
        return self._load_filters("FUTURES", symbol)

    def format_futures_quantity(self, symbol: str, quantity: float, market_order: bool = False) -> str:
        filters = self.load_futures_symbol_filters(symbol)
        return self._format_order_quantity(quantity, filters, market_order)

    def format_futures_price(self, symbol: str, price: float) -> str:
        filters = self.load_futures_symbol_filters(symbol)
        return self._format_increment(price, filters.get("tickSize"), ROUND_HALF_UP)

    def create_futures_order(
        self,
        symbol: str,
        side: str, # BUY or SELL
        order_type: str = "MARKET",
        quantity: Optional[float] = None,
        price: Optional[float] = None,
        reduce_only: bool = False,
        client_order_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Places real demo order on Binance Futures Demo"""
        if not self.has_credentials:
            return {"success": False, "error": "Configure Binance credentials"}
        try:
            params: Dict[str, Any] = {
                "symbol": symbol.upper(),
                "side": side.upper(),
                "type": order_type.upper(),
            }
            if quantity is not None:
                params["quantity"] = self.format_futures_quantity(symbol, quantity, market_order=order_type.upper() == "MARKET")
            if order_type.upper() == "LIMIT":
                if price is None:
                    return {"success": False, "error": "LIMIT requires price"}
                params["price"] = self.format_futures_price(symbol, price)
                params["timeInForce"] = "GTC"
            if reduce_only:
                params["reduceOnly"] = "true"

            if "quantity" not in params:
                return {"success": False, "error": "Order quantity is required"}
            params["newOrderRespType"] = "RESULT"
            return self._submit_order("FUTURES", params, client_order_id)
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_futures_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        try:
            params = {"symbol": symbol.upper()} if symbol else {}
            query = self._sign_params(params)
            url = f"{self.futures_url}/fapi/v1/openOrders?{query}"
            res = self._request("GET", url, headers=self._get_headers(), timeout=10)
            if res.status_code == 200:
                return res.json()
            return []
        except Exception as e:
            print(f"[BinanceClient] Error get_futures_open_orders: {e}")
            return []

    def cancel_futures_order(self, symbol: str, order_id: int) -> Dict[str, Any]:
        try:
            params = {"symbol": symbol.upper(), "orderId": order_id}
            query = self._sign_params(params)
            url = f"{self.futures_url}/fapi/v1/order?{query}"
            res = self._request("DELETE", url, headers=self._get_headers(), timeout=10)
            data = res.json()
            if res.status_code == 200:
                return {"success": True, "data": data}
            return {"success": False, "error": data.get("msg", str(data))}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_futures_trades(self, symbol: str = "BTCUSDT", limit: int = 15) -> List[Dict[str, Any]]:
        try:
            params = {"symbol": symbol.upper(), "limit": limit}
            query = self._sign_params(params)
            url = f"{self.futures_url}/fapi/v1/userTrades?{query}"
            res = self._request("GET", url, headers=self._get_headers(), timeout=10)
            if res.status_code == 200:
                return res.json()
            return []
        except Exception as e:
            print(f"[BinanceClient] Error get_futures_trades: {e}")
            return []

binance_client = BinanceDemoClient()
