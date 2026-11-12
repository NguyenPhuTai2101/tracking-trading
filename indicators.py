import math
from typing import List, Dict, Any, Optional

def calculate_sma(data: List[float], period: int) -> List[Optional[float]]:
    if len(data) < period:
        return [None] * len(data)
    result = [None] * (period - 1)
    current_sum = sum(data[:period])
    result.append(current_sum / period)
    for i in range(period, len(data)):
        current_sum += data[i] - data[i - period]
        result.append(current_sum / period)
    return result

def calculate_ema(data: List[float], period: int) -> List[Optional[float]]:
    if len(data) < period:
        return [None] * len(data)
    result = [None] * (period - 1)
    sma = sum(data[:period]) / period
    result.append(sma)
    multiplier = 2.0 / (period + 1)
    for i in range(period, len(data)):
        ema = (data[i] - result[-1]) * multiplier + result[-1]
        result.append(ema)
    return result

def calculate_rsi(closes: List[float], period: int = 14) -> List[Optional[float]]:
    if len(closes) <= period:
        return [None] * len(closes)
    
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    
    rsi_list = [None] * period
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    if avg_loss == 0:
        rsi_list.append(100.0 if avg_gain > 0 else 50.0)
    else:
        rs = avg_gain / avg_loss
        rsi_list.append(100.0 - (100.0 / (1.0 + rs)))
        
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi_list.append(100.0 if avg_gain > 0 else 50.0)
        else:
            rs = avg_gain / avg_loss
            rsi_list.append(100.0 - (100.0 / (1.0 + rs)))
            
    return rsi_list

def calculate_macd(closes: List[float], fast: int = 12, slow: int = 26, signal_period: int = 9) -> Dict[str, Any]:
    if len(closes) < slow + signal_period:
        return {"macd": None, "signal": None, "hist": None}
    
    fast_ema = calculate_ema(closes, fast)
    slow_ema = calculate_ema(closes, slow)
    
    macd_line = []
    for f, s in zip(fast_ema, slow_ema):
        if f is not None and s is not None:
            macd_line.append(f - s)
        else:
            macd_line.append(None)
            
    valid_macd = [m for m in macd_line if m is not None]
    if len(valid_macd) < signal_period:
        return {"macd": None, "signal": None, "hist": None}
        
    signal_line_valid = calculate_ema(valid_macd, signal_period)
    
    # Pad None to match length
    padding = len(macd_line) - len(valid_macd)
    signal_line = [None] * padding + signal_line_valid
    
    last_macd = macd_line[-1]
    last_signal = signal_line[-1]
    last_hist = (last_macd - last_signal) if (last_macd is not None and last_signal is not None) else 0.0
    
    prev_macd = macd_line[-2] if len(macd_line) >= 2 else None
    prev_signal = signal_line[-2] if len(signal_line) >= 2 else None
    
    crossover = "NONE"
    if last_macd is not None and last_signal is not None and prev_macd is not None and prev_signal is not None:
        if prev_macd <= prev_signal and last_macd > last_signal:
            crossover = "BULLISH_CROSS"
        elif prev_macd >= prev_signal and last_macd < last_signal:
            crossover = "BEARISH_CROSS"
            
    return {
        "macd": last_macd if last_macd is not None else 0.0,
        "signal": last_signal if last_signal is not None else 0.0,
        "hist": last_hist,
        "crossover": crossover
    }

def calculate_bollinger_bands(closes: List[float], period: int = 20, num_std: float = 2.0) -> Dict[str, Any]:
    if len(closes) < period:
        return {"upper": None, "middle": None, "lower": None, "percent_b": 0.5}
    
    recent = closes[-period:]
    sma = sum(recent) / period
    variance = sum((x - sma) ** 2 for x in recent) / period
    std = math.sqrt(variance)
    
    upper = sma + (num_std * std)
    lower = sma - (num_std * std)
    current_price = closes[-1]
    
    percent_b = (current_price - lower) / (upper - lower) if (upper - lower) > 0 else 0.5
    
    return {
        "upper": upper,
        "middle": sma,
        "lower": lower,
        "bandwidth": round((upper - lower) / sma * 100, 2) if sma > 0 else 0,
        "percent_b": percent_b
    }

def analyze_market_signals(candles: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Generates comprehensive trading recommendation with detailed explanations"""
    if len(candles) < 35:
        return {
            "action": "WAIT",
            "net_score": 0,
            "ema_cross": "NONE",
            "reasons": ["Chưa đủ dữ liệu nến để phân tích kỹ thuật (cần tối thiểu 35 nến)."],
            "rsi": 50.0,
            "ema9": 0.0,
            "ema21": 0.0,
            "macd": {},
            "bb": {}
        }
        
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    current_price = closes[-1]
    
    rsi_series = calculate_rsi(closes, period=14)
    current_rsi = rsi_series[-1] if rsi_series[-1] is not None else 50.0
    
    ema9_series = calculate_ema(closes, 9)
    ema21_series = calculate_ema(closes, 21)
    current_ema9 = ema9_series[-1] or current_price
    current_ema21 = ema21_series[-1] or current_price
    prev_ema9 = ema9_series[-2] or current_price
    prev_ema21 = ema21_series[-2] or current_price
    
    macd_res = calculate_macd(closes)
    bb_res = calculate_bollinger_bands(closes)
    
    reasons: List[str] = []
    bullish_score = 0
    bearish_score = 0
    
    # 1. RSI Analysis
    if current_rsi < 30:
        bullish_score += 35
        reasons.append(f"🟢 **RSI = {current_rsi:.1f}** đang nằm sâu ở vùng Quá Bán (Oversold < 30) - Xác suất hồi phục kỹ thuật cao.")
    elif current_rsi < 40:
        bullish_score += 15
        reasons.append(f"🟢 **RSI = {current_rsi:.1f}** đang tiệm cận vùng hỗ trợ mua (30-40).")
    elif current_rsi > 70:
        bearish_score += 35
        reasons.append(f"🔴 **RSI = {current_rsi:.1f}** đang ở vùng Quá Mua (Overbought > 70) - Cảnh báo rủi ro chốt lời / đảo chiều giảm.")
    elif current_rsi > 60:
        bearish_score += 15
        reasons.append(f"🟡 **RSI = {current_rsi:.1f}** ở vùng tích cực nhưng cần chú ý kháng cự 70.")
    else:
        reasons.append(f"⚪ **RSI = {current_rsi:.1f}** ở mức trung tính cân bằng (40-60).")
        
    # 2. EMA Cross Analysis
    ema_cross = "NONE"
    if prev_ema9 <= prev_ema21 and current_ema9 > current_ema21:
        ema_cross = "BULLISH_CROSS"
        bullish_score += 35
        reasons.append(f"🟢 **Golden Cross**: Đường EMA(9) vừa cắt LÊN đường EMA(21) - Tín hiệu kích hoạt xu hướng tăng mạnh.")
    elif prev_ema9 >= prev_ema21 and current_ema9 < current_ema21:
        ema_cross = "BEARISH_CROSS"
        bearish_score += 35
        reasons.append(f"🔴 **Death Cross**: Đường EMA(9) vừa cắt XUỐNG đường EMA(21) - Tín hiệu kích hoạt xu hướng giảm giá.")
    elif current_ema9 > current_ema21:
        bullish_score += 15
        reasons.append(f"🟢 Giá đang duy trì trên EMA(21) với EMA(9) > EMA(21) - Xu hướng ngắn hạn tiếp tục tăng.")
    elif current_ema9 < current_ema21:
        bearish_score += 15
        reasons.append(f"🔴 Giá đang nằm dưới EMA(21) với EMA(9) < EMA(21) - Xu hướng ngắn hạn tiếp tục giảm.")
        
    # 3. MACD Analysis
    if macd_res.get("crossover") == "BULLISH_CROSS":
        bullish_score += 20
        reasons.append("🟢 **MACD Bullish Crossover**: Đường MACD cắt lên đường Signal.")
    elif macd_res.get("crossover") == "BEARISH_CROSS":
        bearish_score += 20
        reasons.append("🔴 **MACD Bearish Crossover**: Đường MACD cắt xuống đường Signal.")
    elif macd_res.get("hist", 0) > 0:
        bullish_score += 10
        reasons.append("🟢 MACD Histogram dương, xung lượng mua chiếm ưu thế.")
    elif macd_res["hist"] < 0:
        bearish_score += 10
        reasons.append("🔴 MACD Histogram âm, áp lực bán đang chi phối.")
        
    # 4. Bollinger Bands
    if bb_res.get("percent_b", 0.5) <= 0.05:
        bullish_score += 15
        reasons.append(f"🟢 Giá chạm dải dưới Bollinger Bands (${bb_res['lower']}) - Vùng hỗ trợ động.")
    elif bb_res.get("percent_b", 0.5) >= 0.95:
        bearish_score += 15
        reasons.append(f"🔴 Giá chạm dải trên Bollinger Bands (${bb_res['upper']}) - Vùng kháng cự động.")
        
    # Calculate Action
    net_score = bullish_score - bearish_score
    if net_score >= 45:
        action = "STRONG_BUY"
        action_text = "MUA MẠNH (STRONG BUY)"
    elif net_score >= 20:
        action = "BUY"
        action_text = "MUA (BUY)"
    elif net_score <= -45:
        action = "STRONG_SELL"
        action_text = "BÁN MẠNH (STRONG SELL)"
    elif net_score <= -20:
        action = "SELL"
        action_text = "BÁN / HẠ TỶ TRỌNG (SELL)"
    else:
        action = "NEUTRAL"
        action_text = "CHỜ ĐỢI (NEUTRAL)"
        
    # Suggested Risk Management (1:2 R:R Ratio)
    true_ranges = [max(h - l, abs(h - prev), abs(l - prev))
                   for h, l, prev in zip(highs[1:], lows[1:], closes[:-1])]
    atr = sum(true_ranges[:14]) / 14
    for true_range in true_ranges[14:]:
        atr = (atr * 13 + true_range) / 14
    direction = 1 if "BUY" in action else -1 if "SELL" in action else 0
    stop_loss = current_price - direction * atr * 1.5 if direction and atr else None
    take_profit = current_price + direction * atr * 3 if direction and atr else None
    stop_loss = stop_loss if stop_loss and stop_loss > 0 else None
    take_profit = take_profit if take_profit and take_profit > 0 else None
    
    return {
        "symbol": candles[0].get("symbol", "BTCUSDT") if candles else "BTCUSDT",
        "action": action,
        "action_text": action_text,
        "net_score": net_score,
        "ema_cross": ema_cross,
        "atr": atr,
        "bullish_score": bullish_score,
        "bearish_score": bearish_score,
        "reasons": reasons,
        "current_price": current_price,
        "suggested_entry": current_price,
        "suggested_sl": stop_loss,
        "suggested_tp": take_profit,
        "risk_reward_ratio": "1 : 2.0",
        "rsi": current_rsi,
        "ema9": current_ema9,
        "ema21": current_ema21,
        "macd": macd_res,
        "bb": bb_res
    }
