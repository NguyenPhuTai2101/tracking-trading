// backtest.js - fee-aware strategy backtests (calls POST /api/backtest)

let backtestResults = [];
let backtestChart = null;
let backtestSeries = null;

function btEscape(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

function btMoney(value) {
  const n = Number(value) || 0;
  return `${n >= 0 ? "+" : "-"}$${Math.abs(n).toFixed(2)}`;
}

function btTone(value) {
  return Number(value) >= 0 ? "text-emerald-400" : "text-red-400";
}

function btTime(seconds) {
  return new Date(seconds * 1000).toLocaleString("vi-VN", { hour12: false, day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function backtestUseBotConfig() {
  const bot = (typeof botsStatus !== "undefined" && botsStatus) ? botsStatus[selectedBotMarket] : null;
  if (!bot) {
    document.getElementById("bt-status").textContent = "Chưa tải được cấu hình bot.";
    return;
  }
  const set = (id, value) => { if (value !== undefined && value !== null) document.getElementById(id).value = value; };
  set("bt-symbols", (bot.watchlist_symbols || []).join(", "));
  set("bt-market", bot.market_type);
  const interval = document.getElementById("bt-interval");
  if (![...interval.options].some(o => o.value === bot.interval)) interval.add(new Option(bot.interval, bot.interval));
  set("bt-interval", bot.interval);
  set("bt-leverage", bot.market_type === "FUTURES" ? bot.leverage : 1);
  set("bt-amount", bot.trade_amount_usdt);
  set("bt-tp", bot.take_profit_pct);
  set("bt-sl", bot.stop_loss_pct);
  set("bt-max-positions", bot.max_positions);
  document.querySelectorAll("input[name=bt-strategy]").forEach(box => { box.checked = box.value === bot.strategy; });
  set("bt-exit-mode", bot.exit_mode || "ROE");
  set("bt-atr-sl", bot.atr_sl_mult);
  set("bt-atr-tp", bot.atr_tp_mult);
  set("bt-cooldown", bot.cooldown_candles);
  set("bt-breakeven", bot.breakeven_at_r);
  set("bt-risk-per-trade", bot.risk_per_trade_usdt);
  set("bt-daily-loss", bot.max_daily_loss_usdt);
  set("bt-max-losses", bot.max_consecutive_losses);
  document.getElementById("bt-trend").checked = Boolean(bot.trend_filter);
  btLimits = { max_total_margin_usdt: bot.max_total_margin_usdt, max_open_risk_usdt: bot.max_open_risk_usdt };
  document.getElementById("bt-status").textContent = `Đã lấy cấu hình Bot ${bot.market_type} (gồm trần ký quỹ và trần rủi ro).`;
}

// Bot caps are only sent after "use bot config"; manual edits fall back to server defaults.
let btLimits = {};
["bt-amount", "bt-sl", "bt-max-positions"].forEach(id => {
  document.addEventListener("DOMContentLoaded", () => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("input", () => { btLimits = {}; });
  });
});

async function runBacktest(event) {
  event.preventDefault();
  const status = document.getElementById("bt-status");
  const button = document.getElementById("bt-run");
  const num = id => Number(document.getElementById(id).value);
  const strategies = [...document.querySelectorAll("input[name=bt-strategy]:checked")].map(b => b.value);
  if (!strategies.length) {
    status.textContent = "Chọn ít nhất một chiến lược.";
    return;
  }
  const feeRaw = document.getElementById("bt-fee").value.trim();
  const body = {
    symbols: document.getElementById("bt-symbols").value.split(/[\s,]+/).filter(Boolean).map(s => s.toUpperCase()),
    market: document.getElementById("bt-market").value,
    strategies,
    interval: document.getElementById("bt-interval").value,
    days: num("bt-days"),
    leverage: document.getElementById("bt-market").value === "SPOT" ? 1 : num("bt-leverage"),
    trade_amount_usdt: num("bt-amount"),
    take_profit_pct: num("bt-tp"),
    stop_loss_pct: num("bt-sl"),
    max_positions: num("bt-max-positions"),
    fee_pct: feeRaw === "" ? null : Number(feeRaw),
    slippage_bps: num("bt-slippage"),
    cooldown_candles: num("bt-cooldown"),
    exit_mode: document.getElementById("bt-exit-mode").value,
    atr_sl_mult: num("bt-atr-sl"),
    atr_tp_mult: num("bt-atr-tp"),
    breakeven_at_r: num("bt-breakeven"),
    risk_per_trade_usdt: num("bt-risk-per-trade"),
    max_daily_loss_usdt: num("bt-daily-loss"),
    max_consecutive_losses: num("bt-max-losses"),
    trend_filter: document.getElementById("bt-trend").checked,
    initial_balance: num("bt-balance"),
    ...btLimits,
  };
  button.disabled = true;
  status.innerHTML = '<i class="fa-solid fa-spinner fa-spin mr-1"></i> Đang tải nến lịch sử và chạy mô phỏng... (có thể mất 10–40 giây)';
  try {
    const res = await fetch("/api/backtest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) {
      const detail = Array.isArray(data.detail) ? data.detail.map(d => d.msg).join("; ") : data.detail;
      throw new Error(detail || `Lỗi ${res.status}`);
    }
    backtestResults = data.results;
    status.textContent = `Xong: ${body.symbols.length} mã, ${body.days} ngày, khung ${body.interval}.`;
    renderBacktestResults();
  } catch (err) {
    status.textContent = "Lỗi backtest: " + err.message;
  } finally {
    button.disabled = false;
  }
}

function renderBacktestResults() {
  document.getElementById("bt-results").classList.remove("hidden");
  const body = document.getElementById("bt-compare-body");
  body.innerHTML = backtestResults.map(r => {
    const s = r.summary;
    const ok = s.winrate_pct >= s.breakeven_winrate_pct;
    return `<tr class="border-b border-[#232a3d]">
      <td class="p-2 font-bold text-white">${btEscape(r.config.strategy)}</td>
      <td class="p-2 text-right font-mono">${s.trades}</td>
      <td class="p-2 text-right font-mono ${ok ? "text-emerald-400" : "text-red-400"}">${s.winrate_pct}%</td>
      <td class="p-2 text-right font-mono text-gray-400">${s.breakeven_winrate_pct}%</td>
      <td class="p-2 text-right font-mono ${btTone(s.gross_pnl)}">${btMoney(s.gross_pnl)}</td>
      <td class="p-2 text-right font-mono text-amber-300">-$${Number(s.fees).toFixed(2)}</td>
      <td class="p-2 text-right font-mono font-bold ${btTone(s.net_pnl)}">${btMoney(s.net_pnl)}</td>
      <td class="p-2 text-right font-mono ${btTone(s.return_pct)}">${s.return_pct >= 0 ? "+" : ""}${s.return_pct}%</td>
      <td class="p-2 text-right font-mono">${s.profit_factor ?? "-"}</td>
      <td class="p-2 text-right font-mono text-red-300">${s.max_drawdown_pct}%</td>
    </tr>`;
  }).join("");
  const part = p => p
    ? `<td class="p-2 text-right font-mono">${p.trades}</td>
       <td class="p-2 text-right font-mono font-bold ${btTone(p.net_pnl)}">${btMoney(p.net_pnl)}</td>
       <td class="p-2 text-right font-mono">${p.profit_factor ?? "-"}</td>`
    : '<td class="p-2 text-right text-gray-500" colspan="3">-</td>';
  document.getElementById("bt-robust-body").innerHTML = backtestResults.map(r => {
    const s = r.summary;
    const hold = s.buy_hold_return_pct;
    return `<tr class="border-b border-[#232a3d]">
      <td class="p-2 font-bold text-white">${btEscape(r.config.strategy)}</td>
      ${part(s.first_part)}${part(s.last_part)}
      <td class="p-2 text-right font-mono ${hold == null ? "text-gray-500" : btTone(hold)}">${hold == null ? "-" : `${hold >= 0 ? "+" : ""}${hold}%`}</td>
    </tr>`;
  }).join("");
  const warnings = [...new Set(backtestResults.flatMap(r => r.warnings))];
  document.getElementById("bt-warnings").innerHTML = warnings.map(w => `<li>⚠ ${btEscape(w)}</li>`).join("");
  const view = document.getElementById("bt-view");
  view.innerHTML = backtestResults.map((r, i) => `<option value="${i}">${btEscape(r.config.strategy)}</option>`).join("");
  renderBacktestDetail();
}

function renderBacktestDetail() {
  if (!backtestResults.length) return;
  const result = backtestResults[Number(document.getElementById("bt-view").value) || 0];

  document.getElementById("bt-symbol-body").innerHTML = Object.entries(result.per_symbol).map(([sym, row]) => `
    <tr class="border-b border-[#232a3d]">
      <td class="p-1.5 text-white">${btEscape(sym)}</td>
      <td class="p-1.5 text-right font-mono">${row.trades}</td>
      <td class="p-1.5 text-right font-mono">${row.winrate_pct}%</td>
      <td class="p-1.5 text-right font-mono ${btTone(row.net_pnl)}">${btMoney(row.net_pnl)}</td>
    </tr>`).join("");
  document.getElementById("bt-exit-reasons").textContent = "Lý do thoát: " +
    (Object.entries(result.exit_reasons).map(([k, v]) => `${k} ${v}`).join(" · ") || "chưa có lệnh");

  const trades = result.trades.slice().reverse();
  document.getElementById("bt-trades-note").textContent = result.total_trades > trades.length
    ? `(${trades.length} / ${result.total_trades} lệnh)` : `(${result.total_trades} lệnh)`;
  document.getElementById("bt-trades-body").innerHTML = trades.map(t => `
    <tr class="border-b border-[#232a3d]">
      <td class="p-2 font-mono text-gray-400">${btTime(t.entry_time)}</td>
      <td class="p-2 text-white">${btEscape(t.symbol)}</td>
      <td class="p-2 ${t.direction === "LONG" ? "text-emerald-400" : "text-red-400"}">${t.direction}</td>
      <td class="p-2 text-right font-mono">${Number(t.entry_price).toPrecision(6)}</td>
      <td class="p-2 text-right font-mono">${Number(t.exit_price).toPrecision(6)}</td>
      <td class="p-2">${btEscape(t.exit_reason)}</td>
      <td class="p-2 text-right font-mono ${btTone(t.gross_pnl)}">${btMoney(t.gross_pnl)}</td>
      <td class="p-2 text-right font-mono text-amber-300">-$${Number(t.fees).toFixed(3)}</td>
      <td class="p-2 text-right font-mono font-bold ${btTone(t.net_pnl)}">${btMoney(t.net_pnl)}</td>
    </tr>`).join("") || '<tr><td colspan="9" class="p-3 text-center text-gray-500">Không có lệnh nào trong giai đoạn này.</td></tr>';

  renderBacktestEquity(result);
}

function renderBacktestEquity(result) {
  const container = document.getElementById("bt-equity");
  if (!container || container.clientWidth === 0 || typeof LightweightCharts === "undefined") return;
  if (!backtestChart) {
    backtestChart = LightweightCharts.createChart(container, {
      width: container.clientWidth, height: 280,
      layout: { background: { color: "#131722" }, textColor: "#848e9c" },
      grid: { vertLines: { color: "#1c2230" }, horzLines: { color: "#1c2230" } },
      rightPriceScale: { borderColor: "#262c3d" },
      timeScale: { borderColor: "#262c3d", timeVisible: true, secondsVisible: false },
    });
    backtestSeries = backtestChart.addLineSeries({ color: "#22d3ee", lineWidth: 2 });
    window.addEventListener("resize", () => backtestChart && backtestChart.applyOptions({ width: container.clientWidth }));
  } else {
    backtestChart.applyOptions({ width: container.clientWidth });
  }
  // Lightweight Charts needs strictly increasing times: keep the last equity per second.
  const points = new Map();
  points.set(result.summary.period_start || (result.equity_curve[0]?.time ?? 0) - 1, result.config.initial_balance);
  result.equity_curve.forEach(p => points.set(p.time, p.equity));
  const data = [...points.entries()].filter(([t]) => t > 0).sort((a, b) => a[0] - b[0]).map(([time, value]) => ({ time, value }));
  backtestSeries.setData(data);
  backtestChart.timeScale().fitContent();
}
