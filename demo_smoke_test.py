"""Demo integration checks. Stop the application before using --probe-stop.

Never places an entry order. The optional probe creates and cancels one
reduce-only conditional stop while the Demo Futures account is flat.
"""
import argparse
import json
import re
import time
from pathlib import Path
from uuid import uuid4

from binance_client import binance_client as client
from bot_manager import bot_manager


JOURNAL = Path(__file__).with_name("demo_stop_probe.json")


def cleanup_probe(key):
    for attempt in range(5):
        if attempt:
            time.sleep(1)
        result = client.cancel_futures_stop(key)
        data = result.get("data") or {}
        if result.get("success") and data.get("algoStatus") in {"CANCELED", "REJECTED", "EXPIRED"}:
            JOURNAL.unlink(missing_ok=True)
            return data["algoStatus"]
    raise RuntimeError("Chưa xác nhận hủy probe; giữ journal để đối soát lần chạy sau")


def check(probe_stop=False):
    if (client.spot_url != "https://demo-api.binance.com"
            or client.futures_url != "https://demo-fapi.binance.com"):
        raise RuntimeError("Chỉ chạy kiểm tra này trên Binance Demo")
    if not client.has_credentials:
        raise RuntimeError("Chưa cấu hình khóa Demo")
    report = {}
    for market in ("SPOT", "FUTURES"):
        report[market.lower() + "_symbols"] = len(client.get_exchange_symbols(market))
    for market, get_account in (("SPOT", client.get_account_info), ("FUTURES", client.get_futures_account_info)):
        if not get_account().get("success"):
            raise RuntimeError(f"Không xác thực được tài khoản {market}")
        report[market.lower() + "_authenticated"] = True
    if not probe_stop:
        return report

    bot_manager.acquire_locks()
    try:
        bot_manager.load()
        if JOURNAL.exists():
            key = json.loads(JOURNAL.read_text(encoding="utf-8"))["id"]
            if not re.fullmatch(r"probe_[a-f0-9]{28}", key):
                raise RuntimeError("Journal probe không hợp lệ")
            report["recovered_probe"] = cleanup_probe(key)
        if any(bot.state_error or bot.active_positions or bot.pending_orders or bot.protective_orders
               for bot in bot_manager.engines.values()):
            raise RuntimeError("Bot phải hết vị thế và lệnh chờ trước khi probe")
        if any(float(p["positionAmt"]) for p in client.get_futures_positions(strict=True)):
            raise RuntimeError("Tài khoản Demo Futures phải hết vị thế trước khi probe")
        for path in ("/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders"):
            signed = client._sign_params({})
            response = client._request("GET", client.futures_url + path + "?" + signed,
                                       headers=client._get_headers(), timeout=6)
            if not response.ok or response.json():
                raise RuntimeError("Tài khoản Demo Futures phải hết lệnh chờ trước khi probe")
        symbol = "BTCUSDT"
        filters = client.load_futures_symbol_filters(symbol)
        price = float(client.get_ticker_price(symbol, market="FUTURES", mark=True)["price"])
        key = "probe_" + uuid4().hex[:28]
        JOURNAL.write_text(json.dumps({"id": key, "symbol": symbol}), encoding="utf-8")
        try:
            result = client.create_futures_stop(symbol, "SELL", float(filters["minQty"]), price * 0.8, key)
            if not result.get("success"):
                if not result.get("unknown"):
                    JOURNAL.unlink(missing_ok=True)
                raise RuntimeError(result.get("error", "Probe chưa xác nhận"))
            report["stop_submission"] = result["data"].get("algoStatus")
            for attempt in range(5):
                if attempt:
                    time.sleep(1)
                queried = client.query_futures_stop(key)
                data = queried.get("data") or {}
                if queried.get("success"):
                    break
            if (not queried.get("success") or data.get("reduceOnly") not in (True, "true")
                    or data.get("workingType") != "MARK_PRICE" or data.get("algoStatus") != "NEW"):
                raise RuntimeError("Phản hồi stop probe không đúng yêu cầu")
            report["stop_query"] = data["algoStatus"]
        finally:
            if JOURNAL.exists():
                report["stop_cleanup"] = cleanup_probe(key)
        report["futures_positions_after"] = sum(
            float(p["positionAmt"]) != 0 for p in client.get_futures_positions(strict=True))
        if report["futures_positions_after"]:
            raise RuntimeError("Tài khoản đã thay đổi vị thế trong lúc probe")
        return report
    finally:
        bot_manager.release_locks()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-stop", action="store_true")
    args = parser.parse_args()
    print(json.dumps(check(args.probe_stop), ensure_ascii=False, indent=2))
