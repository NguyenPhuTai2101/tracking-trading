"""Synthetic candles and an in-memory exchange for tests. Nothing here touches the network."""
import math
import time

HOUR = 3600


def candles_from_closes(closes, wick=0.2, start=1_700_000_000 // HOUR * HOUR, step=HOUR):
    out, previous = [], closes[0]
    for i, close in enumerate(closes):
        out.append({"time": start + i * step, "open": previous, "close": close,
                    "high": max(previous, close) + wick, "low": min(previous, close) - wick, "volume": 1.0,
                    "close_time_ms": (start + (i + 1) * step) * 1000 - 1, "closed": True})
        previous = close
    return out


def pullback_closes(sign=1):
    """A steady trend, a five-candle pullback to the EMA20, then a confirmation candle."""
    closes = [100 + 0.3 * i for i in range(80)]
    for _ in range(5):
        closes.append(closes[-1] - 0.9)
    closes.append(closes[-1] + 1.7)
    return closes if sign > 0 else [200 - c for c in closes]


class FakeExchange:
    """Just enough of BinanceDemoClient for the engine: one-way futures, market fills at self.price."""
    has_credentials = True

    def __init__(self, equity=1000.0):
        self.equity = equity
        self.price = {}
        self.position = {}        # symbol -> signed quantity
        self.stops = {}           # client algo id -> exchange record
        self.orders = {}          # client order id -> exchange record
        self.klines = {}          # (symbol, interval) -> candles
        self.sent = []            # every order submission, in order
        self.order_result = None  # set to override the next create_futures_order response
        self.on_order = None      # hook called just before an order is accepted

    def _get_timestamp(self):
        return int(time.time() * 1000)

    def validate_symbol(self, market, symbol):
        pass

    def get_futures_account_info(self):
        return {"success": True, "totalMarginBalance": self.equity, "availableBalance": self.equity,
                "totalWalletBalance": self.equity, "totalUnrealizedProfit": 0.0}

    def get_futures_positions(self, symbol=None, strict=False):
        return [{"symbol": s, "positionAmt": q, "positionSide": "BOTH"} for s, q in self.position.items()
                if q and (symbol is None or s == symbol)]

    def set_leverage(self, symbol, leverage):
        return {"success": True, "leverage": leverage}

    def get_ticker_price(self, symbol, market="FUTURES", mark=False):
        return {"symbol": symbol, "price": self.price[symbol]}

    def get_klines(self, symbol, interval="1h", limit=100, market="FUTURES"):
        return self.klines.get((symbol, interval), [])

    def load_futures_symbol_filters(self, symbol):
        return {"minNotional": "5"}

    def format_futures_quantity(self, symbol, quantity, market_order=False):
        rounded = math.floor(quantity * 1000) / 1000
        if rounded <= 0:
            raise ValueError("Quantity outside exchange limits")
        return f"{rounded:.3f}"

    def prepare_entry(self, market, symbol, budget, leverage, expected_price):
        price = self.price[symbol]
        return float(self.format_futures_quantity(symbol, budget * leverage / price)), price

    def create_futures_order(self, symbol, side, order_type="MARKET", quantity=None, price=None,
                             reduce_only=False, client_order_id=None):
        self.sent.append({"symbol": symbol, "side": side, "quantity": quantity, "reduce_only": reduce_only,
                          "id": client_order_id})
        if self.on_order:
            self.on_order()
        signed = quantity if side == "BUY" else -quantity
        self.position[symbol] = round(self.position.get(symbol, 0.0) + signed, 6)
        record = {"status": "FILLED", "executedQty": str(quantity), "cumQuote": str(quantity * self.price[symbol]),
                  "orderId": len(self.sent)}
        self.orders[client_order_id] = record
        if self.order_result is not None:
            result, self.order_result = self.order_result, None
            return result
        return {"success": True, "data": record, "client_order_id": client_order_id}

    def query_order(self, market, symbol, client_order_id=None, order_id=None):
        record = self.orders.get(client_order_id) or self.orders.get(f"exchange:{order_id}")
        if record:
            return {"success": True, "data": record}
        return {"success": False, "unknown": True, "client_order_id": client_order_id}

    def cancel_by_client_id(self, market, symbol, client_order_id):
        return self.query_order(market, symbol, client_order_id)

    def create_futures_stop(self, symbol, side, quantity, trigger_price, client_algo_id):
        self.stops[client_algo_id] = {
            "algoStatus": "NEW", "algoId": len(self.stops) + 1, "symbol": symbol, "clientAlgoId": client_algo_id,
            "reduceOnly": True, "workingType": "MARK_PRICE", "orderType": "STOP_MARKET", "side": side,
            "quantity": str(quantity), "triggerPrice": str(trigger_price), "actualOrderId": "0"}
        return {"success": True, "data": self.stops[client_algo_id]}

    def query_futures_stop(self, client_algo_id):
        return {"success": True, "data": self.stops[client_algo_id]}

    def cancel_futures_stop(self, client_algo_id):
        if self.stops[client_algo_id]["algoStatus"] == "NEW":
            self.stops[client_algo_id]["algoStatus"] = "CANCELED"
        return self.query_futures_stop(client_algo_id)

    def trigger_stop(self, client_algo_id, fill_price):
        """The exchange fires a stop while the bot is not looking."""
        stop = self.stops[client_algo_id]
        quantity = float(stop["quantity"])
        self.position[stop["symbol"]] = round(
            self.position[stop["symbol"]] + (-quantity if stop["side"] == "SELL" else quantity), 6)
        stop.update(algoStatus="FINISHED", actualOrderId="777")
        self.orders["exchange:777"] = {"status": "FILLED", "executedQty": str(quantity),
                                       "cumQuote": str(quantity * fill_price), "orderId": 777}

    def live_stops(self, symbol):
        return [s for s in self.stops.values() if s["symbol"] == symbol and s["algoStatus"] == "NEW"]
