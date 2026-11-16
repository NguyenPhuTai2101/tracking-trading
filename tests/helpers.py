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


def recent_candles(count, step=HOUR, price=100.0, wick=0.5):
    """Closed candles whose last one closed at the most recent `step` boundary, as a live feed would give."""
    last_open = int(time.time()) // step * step - step
    return candles_from_closes([price] * count, wick=wick, start=last_open - (count - 1) * step, step=step)


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
        self.fee_rate = 0.0004    # what this exchange really charges (the bot assumes 0.05%)
        self.fee_asset = "USDT"
        self.entry_price = {}     # symbol -> average entry of the open position
        self.fills = {}           # exchange order id -> executions, as /fapi/v1/userTrades returns them
        self.funding = []         # {"symbol", "income", "time"} rows, as /fapi/v1/income returns them
        self.history_ready = True
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
        order_id = len(self.sent)
        self._book(symbol, side, quantity, self.price[symbol], order_id)
        record = {"status": "FILLED", "executedQty": str(quantity), "cumQuote": str(quantity * self.price[symbol]),
                  "orderId": order_id}
        self.orders[client_order_id] = record
        if self.order_result is not None:
            result, self.order_result = self.order_result, None
            return result
        return {"success": True, "data": record, "client_order_id": client_order_id}

    def _book(self, symbol, side, quantity, price, order_id):
        """Apply a fill to the position and record it the way the exchange's trade history does."""
        signed = quantity if side == "BUY" else -quantity
        before = self.position.get(symbol, 0.0)
        realized = 0.0
        if before and before * signed < 0:                      # reducing: PnL against the entry price
            realized = (price - self.entry_price.get(symbol, price)) * quantity * (1 if before > 0 else -1)
        else:
            self.entry_price[symbol] = price
        self.position[symbol] = round(before + signed, 6)
        self.fills.setdefault(order_id, []).append({
            "symbol": symbol, "orderId": order_id, "side": side, "price": str(price), "qty": str(quantity),
            "realizedPnl": str(realized), "commission": str(quantity * price * self.fee_rate),
            "commissionAsset": self.fee_asset, "time": int(time.time() * 1000)})

    def get_futures_order_fills(self, symbol, order_id):
        return list(self.fills.get(order_id, [])) if self.history_ready else None

    def get_futures_funding(self, symbol, start_ms, end_ms):
        if not self.history_ready:
            return None
        return [row for row in self.funding if row["symbol"] == symbol and start_ms <= row["time"] <= end_ms]

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
        self._book(stop["symbol"], stop["side"], quantity, fill_price, 777)
        stop.update(algoStatus="FINISHED", actualOrderId="777")
        self.orders["exchange:777"] = {"status": "FILLED", "executedQty": str(quantity),
                                       "cumQuote": str(quantity * fill_price), "orderId": 777}

    def live_stops(self, symbol):
        return [s for s in self.stops.values() if s["symbol"] == symbol and s["algoStatus"] == "NEW"]
