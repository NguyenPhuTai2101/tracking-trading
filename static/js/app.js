// Session expired or logged out elsewhere: any 401 from the API goes back to the login page.
(() => {
  const nativeFetch = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const response = await nativeFetch(...args);
    const url = typeof args[0] === "string" ? args[0] : args[0]?.url || "";
    if (response.status === 401 && url.startsWith("/api/") && !url.startsWith("/api/auth/")) {
      location.replace("/login?next=" + encodeURIComponent(location.pathname));
    }
    return response;
  };
})();

async function initAuthControls() {
  try {
    const status = await (await fetch("/api/auth/status")).json();
    const button = document.getElementById("btn-logout");
    if (button && status.enabled) button.classList.remove("hidden");
  } catch (err) {
    console.error("auth status error:", err);
  }
}

async function logout() {
  try {
    await fetch("/api/auth/logout", { method: "POST" });
  } finally {
    location.replace("/login");
  }
}

let botConfig = null;
let botConfigDirty = false;
let botSymbols = [];
let botLeverage = 10;
let botSaveInFlight = false;
let selectedBotMarket = "FUTURES";
let botsStatus = {};
const botDrafts = {};
const botActionsInFlight = new Set();
let botStatusInFlight = false;
let marketDataRequest;
// app.js - Binance Multi-Coin Spot & Futures Trading Controller

let state = {
  marketMode: "FUTURES", // "SPOT" or "FUTURES"
  symbol: "BTCUSDT",
  interval: "15m",
  strategy: "HYBRID_SMART",
  leverage: 10,
  maxPositions: 3,
  watchlistSymbols: ["BTCUSDT", "ETHUSDT", "SOLUSDT", "SUIUSDT", "NEARUSDT", "PEPEUSDT"],
  isBotRunning: false,
  activeTab: "terminal",
  availableBalanceUsdt: 5000.0,
  activePositions: [],
};

const POPULAR_WATCHLIST_CANDIDATES = [
  "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "SUIUSDT", 
  "NEARUSDT", "PEPEUSDT", "DOGEUSDT", "XRPUSDT", "AVAXUSDT", 
  "LINKUSDT", "ADAUSDT", "APTUSDT", "SHIBUSDT"
];

// ==================== INITIALIZATION ====================
document.addEventListener("DOMContentLoaded", () => {
  initAuthControls();
  initChart();
  fetchSystemStatus();
  fetchBotStatus();
  fetchAvailableSymbols();
  fetchTickers();
  fetchKlinesAndSignals();
  fetchAccountBalances();
  fetchOpenOrders();
  fetchTradeHistory();

  // Apply default Futures mode UI
  setMarketMode("FUTURES", false);

  // Polling loops
  setInterval(fetchTickers, 3000);
  setInterval(fetchBotStatus, 2500);
  setInterval(fetchKlinesAndSignals, 6000);
  setInterval(fetchAccountBalances, 6000);

  // Manual order type switch
  const orderTypeSelect = document.getElementById("manual-order-type");
  if (orderTypeSelect) {
    orderTypeSelect.addEventListener("change", (e) => {
      const limitGroup = document.getElementById("limit-price-group");
      if (e.target.value === "LIMIT") {
        limitGroup.classList.remove("hidden");
      } else {
        limitGroup.classList.add("hidden");
      }
    });
  }
});

// ==================== SYSTEM STATUS & LATENCY ====================
async function fetchSystemStatus() {
  const start = Date.now();
  try {
    const res = await fetch("/api/status");
    const ping = Date.now() - start;
    const latencyEl = document.getElementById("server-latency");
    if (latencyEl) latencyEl.textContent = `${ping} ms`;
    
    if (res.ok) {
      const data = await res.json();
      const statusText = document.getElementById("server-status-text");
      if (data.binance_demo?.connected && data.binance_demo?.futures_connected) {
        statusText.textContent = "Demo Sẵn Sàng";
      } else {
        statusText.textContent = "Kết nối Binance chưa đầy đủ";
      }
    }
  } catch (err) {
    const statusText = document.getElementById("server-status-text");
    if (statusText) statusText.textContent = "Mất kết nối";
  }
}

// ==================== MARKET MODE TOGGLE (SPOT vs FUTURES) ====================
function setMarketMode(mode, saveConfig = true) {
  state.marketMode = mode.toUpperCase();

  const spotBtn = document.getElementById("mode-btn-spot");
  const futuresBtn = document.getElementById("mode-btn-futures");
  const brandTitle = document.getElementById("brand-title");
  const marketBadge = document.getElementById("market-badge");
  const walletLabel = document.getElementById("wallet-label");
  const walletIcon = document.getElementById("wallet-icon");
  const manualMarketTag = document.getElementById("manual-market-tag");
  const levTag = document.getElementById("futures-leverage-tag");
  const spotBtns = document.getElementById("spot-order-buttons");
  const futuresBtns = document.getElementById("futures-order-buttons");
  const statMode = document.getElementById("stat-current-mode");

  if (state.marketMode === "FUTURES") {
    futuresBtn.className = "px-3 py-1 rounded-lg text-xs font-bold transition-all bg-purple-600 text-white shadow-lg shadow-purple-600/30";
    spotBtn.className = "px-3 py-1 rounded-lg text-xs font-bold text-gray-400 hover:text-white transition-all";

    brandTitle.textContent = "BINANCE FUTURES";
    marketBadge.className = "px-2 py-0.5 text-[11px] font-bold rounded-md bg-purple-500/20 text-purple-400 border border-purple-500/30";
    marketBadge.textContent = "FUTURES DEMO";

    walletLabel.textContent = "Ví Ký Quỹ Futures";
    walletIcon.className = "fa-solid fa-bolt text-purple-400 text-sm";


    manualMarketTag.textContent = "FUTURES";
    manualMarketTag.className = "px-2 py-0.5 text-[10px] font-bold rounded bg-purple-500/20 text-purple-400";

    levTag.classList.remove("hidden");
    spotBtns.classList.add("hidden");
    futuresBtns.classList.remove("hidden");



  } else { // SPOT
    spotBtn.className = "px-3 py-1 rounded-lg text-xs font-bold transition-all bg-yellow-500 text-black shadow";
    futuresBtn.className = "px-3 py-1 rounded-lg text-xs font-bold text-gray-400 hover:text-white transition-all";

    brandTitle.textContent = "BINANCE DEMO";
    marketBadge.className = "px-2 py-0.5 text-[11px] font-bold rounded-md bg-yellow-500/20 text-yellow-400 border border-yellow-500/30";
    marketBadge.textContent = "SPOT MODE";

    walletLabel.textContent = "Ví Spot Demo";
    walletIcon.className = "fa-solid fa-wallet text-yellow-400 text-sm";


    manualMarketTag.textContent = "SPOT";
    manualMarketTag.className = "px-2 py-0.5 text-[10px] font-bold rounded bg-yellow-500/20 text-yellow-400";

    levTag.classList.add("hidden");
    spotBtns.classList.remove("hidden");
    futuresBtns.classList.add("hidden");


  }

  fetchAccountBalances();
  fetchOpenOrders();
  fetchTradeHistory();
  fetchTickers();
  fetchKlinesAndSignals();

  document.getElementById("manual-leverage-row").classList.toggle("hidden", state.marketMode !== "FUTURES");
  allSymbols = [];
  fetchAvailableSymbols();
}

// ==================== LEVERAGE CONTROL ====================
function setLeverage(lev) {
  state.leverage = lev;
  document.getElementById("futures-leverage-tag").textContent = `${lev}x lệnh tay`;
}

function setBotLeverage(lev) {
  if (selectedBotMarket === "SPOT") return;
  botLeverage = lev;
  document.getElementById("leverage-display-val").textContent = `${lev}x`;
  markBotDirty();
}

function markBotDirty() {
  botConfigDirty = true;
  botDrafts[selectedBotMarket] = readBotDraft();
  renderBotOverview();
  document.getElementById("bot-config-status").textContent = "Có thay đổi chưa lưu. Tạm dừng mở lệnh trước khi lưu.";
}

async function changeBotMarket() {
  selectBot(document.getElementById("bot-market").value);
}

function readBotDraft() {
  const value = id => document.getElementById(id).value;
  return {
    market_type: selectedBotMarket, leverage: selectedBotMarket === "SPOT" ? 1 : botLeverage,
    watchlist_symbols: [...state.watchlistSymbols], max_positions: state.maxPositions,
    strategy: state.strategy, interval: value("bot-timeframe"),
    trade_amount_usdt: value("bot-amount"), scan_interval_sec: value("bot-interval"),
    take_profit_pct: value("bot-tp"), stop_loss_pct: value("bot-sl"),
    max_total_margin_usdt: value("bot-total-budget"), max_open_risk_usdt: value("bot-risk-budget"),
  };
}

function selectBot(market) {
  if (botSaveInFlight || !botsStatus[market]) {
    document.getElementById("bot-market").value = selectedBotMarket;
    return;
  }
  if (botConfigDirty) botDrafts[selectedBotMarket] = readBotDraft();
  selectedBotMarket = market;
  botSymbols = [];
  applyBotConfig(botDrafts[market] || botsStatus[market], Boolean(botDrafts[market]));
  renderSelectedBot(botsStatus[market]);
  renderBotOverview();
}

function renderBotOverview() {
  for (const market of ["SPOT", "FUTURES"]) {
    const bot = botsStatus[market];
    if (!bot) continue;
    const card = document.getElementById(`bot-summary-${market}`);
    card.classList.toggle("border-yellow-500", market === selectedBotMarket);
    const status = document.getElementById(`bot-summary-status-${market}`);
    status.textContent = bot.is_running ? (bot.entries_enabled ? "Đang mở lệnh" : "Đang chặn mở mới") : "Dừng mở lệnh";
    document.getElementById(`bot-summary-config-${market}`).textContent = `${bot.strategy} · ${bot.interval} · ${bot.trade_amount_usdt} USDT/lệnh`;
    document.getElementById(`bot-summary-positions-${market}`).textContent = `${bot.active_positions_count}/${bot.max_positions} vị thế · ${bot.leverage}x`;
    const button = document.getElementById(`bot-power-${market}`);
    button.textContent = bot.is_running ? `Dừng ${market}` : `Chạy ${market}`;
    button.disabled = botActionsInFlight.has(market);
    document.getElementById(`bot-edit-${market}`).textContent = `Thiết lập${botDrafts[market] ? " • chưa lưu" : ""}`;
  }
}

async function loadBotSymbols() {
  const market = selectedBotMarket;
  document.getElementById("futures-leverage-row").classList.toggle("hidden", market !== "FUTURES");
  try {
    const res = await fetch(`/api/market/symbols?market=${market}`);
    if (!res.ok) throw new Error("Không tải được danh sách coin của bot.");
    const data = await res.json();
    if (market !== selectedBotMarket) return;
    botSymbols = data.symbols;
    const invalid = state.watchlistSymbols.filter(sym => !botSymbols.includes(sym));
    if (invalid.length) document.getElementById("bot-config-status").textContent = `Bỏ các mã không thuộc ${market}: ${invalid.join(", ")} rồi chọn mã phù hợp.`;
    renderWatchlistChips();
  } catch (err) {
    if (market === selectedBotMarket) document.getElementById("bot-config-status").textContent = err.message;
  }
}

function applyBotConfig(bot, draft = false) {
  botConfig = botsStatus[selectedBotMarket] || bot;
  state.watchlistSymbols = [...bot.watchlist_symbols];
  state.maxPositions = bot.max_positions;
  state.strategy = bot.strategy;
  botLeverage = bot.leverage;
  const fields = {
    "bot-market": bot.market_type, "bot-timeframe": bot.interval,
    "bot-amount": bot.trade_amount_usdt, "bot-interval": bot.scan_interval_sec,
    "bot-tp": bot.take_profit_pct, "bot-sl": bot.stop_loss_pct,
    "bot-total-budget": bot.max_total_margin_usdt, "bot-risk-budget": bot.max_open_risk_usdt,
  };
  for (const [id, value] of Object.entries(fields)) document.getElementById(id).value = value;
  document.getElementById("leverage-display-val").textContent = `${bot.leverage}x`;
  document.getElementById("max-pos-display").textContent = `${bot.max_positions} lệnh`;
  document.querySelectorAll(".strategy-card").forEach(el => el.classList.toggle("selected", el.id === `strat-${bot.strategy}`));
  document.querySelectorAll(".lev-btn").forEach(el => el.classList.toggle("selected", Number(el.dataset.lev) === bot.leverage));
  document.getElementById("bot-config-status").textContent = draft ? `Bot ${selectedBotMarket} có thay đổi chưa lưu.` : `Cấu hình riêng của bot ${selectedBotMarket} đã lưu trên máy chủ.`;
  botConfigDirty = draft;
  loadBotSymbols();
}

function switchTab(tabId) {
  state.activeTab = tabId;

  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.classList.remove("active");
    btn.classList.add("text-gray-400");
  });

  const activeBtn = document.getElementById(`tab-btn-${tabId}`);
  if (activeBtn) {
    activeBtn.classList.add("active");
    activeBtn.classList.remove("text-gray-400");
  }

  const tabTerminal = document.getElementById("tab-content-terminal");
  const tabPositions = document.getElementById("tab-content-positions");
  const tabAcademy = document.getElementById("tab-content-academy");
  const tabHistory = document.getElementById("tab-content-history");
  const tabBacktest = document.getElementById("tab-content-backtest");

  tabBacktest.classList.add("hidden");
  tabTerminal.classList.add("hidden");
  tabPositions.classList.add("hidden");
  tabAcademy.classList.add("hidden");
  tabHistory.classList.add("hidden");

  if (tabId === "terminal") {
    tabTerminal.classList.remove("hidden");
    setTimeout(() => { if (window.chart) window.chart.timeScale().fitContent(); }, 50);
  } else if (tabId === "positions") {
    tabPositions.classList.remove("hidden");
    fetchBotStatus();
  } else if (tabId === "academy") {
    tabAcademy.classList.remove("hidden");
  } else if (tabId === "history") {
    tabHistory.classList.remove("hidden");
    fetchOpenOrders();
    fetchTradeHistory();
  } else if (tabId === "backtest") {
    tabBacktest.classList.remove("hidden");
    if (typeof renderBacktestDetail === "function") setTimeout(renderBacktestDetail, 50);
  }
}

// ==================== ACCOUNT BALANCES ====================
async function fetchAccountBalances() {
  try {
    const res = await fetch(`/api/account?market=${state.marketMode}`);
    if (!res.ok) return;
    const data = await res.json();

    const usdtEl = document.getElementById("usdt-balance");
    const statUsdt = document.getElementById("stat-usdt");

    if (state.marketMode === "FUTURES") {
      const total = data.totalWalletBalance || 0;
      state.availableBalanceUsdt = total;
      const formatted = total.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      if (usdtEl) usdtEl.textContent = formatted;
      if (statUsdt) statUsdt.textContent = formatted;
    } else {
      if (data.balances) {
        const usdt = data.balances.find((b) => b.asset === "USDT");
        const freeVal = usdt ? parseFloat(usdt.free) : 0.0;
        state.availableBalanceUsdt = freeVal;
        const formatted = freeVal.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        if (usdtEl) usdtEl.textContent = formatted;
        if (statUsdt) statUsdt.textContent = formatted;
      }
    }
  } catch (err) {
    console.error("fetchAccountBalances error:", err);
  }
}

// ==================== MARKET TICKERS & CHART ====================
async function fetchTickers() {
  try {
    const market = state.marketMode;
    const res = await fetch(`/api/market/ticker?market=${market}`);
    const data = await res.json();
    if (market !== state.marketMode) return;
    if (data.watchlist) {
      for (const [sym, price] of Object.entries(data.watchlist)) {
        const el = document.getElementById(`ticker-${sym}`);
        if (el) {
          el.textContent = price > 10 ? price.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : price;
        }
      }

      if (data.watchlist[state.symbol]) {
        const p = data.watchlist[state.symbol];
        const formatted = p > 10 ? p.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : p;
        document.getElementById("current-price").textContent = `$${formatted}`;
      }
    }
  } catch (err) {
    console.error("fetchTickers error:", err);
  }
}

async function fetchKlinesAndSignals() {
  const { symbol, interval, marketMode } = state;
  const query = new URLSearchParams({ symbol, interval, market: marketMode, limit: 100, include_signals: true });
  const key = query.toString();
  if (marketDataRequest?.key === key) return;
  marketDataRequest?.controller.abort();
  const controller = new AbortController();
  marketDataRequest = { key, controller };
  const timeout = setTimeout(() => controller.abort("timeout"), 20000);
  try {
    const res = await fetch(`/api/market/klines?${query}`, { signal: controller.signal });
    if (!res.ok) {
      const error = await res.json();
      throw new Error(error.detail || "Không tải được dữ liệu thị trường. Hệ thống sẽ tự thử lại.");
    }
    const data = await res.json();
    if (controller.signal.aborted || symbol !== state.symbol || interval !== state.interval || marketMode !== state.marketMode) return;
    if (!data.candles?.length) throw new Error("Chưa lấy được nến cho mã này trên thị trường đã chọn.");
    if (!data.signals) throw new Error("API chưa trả tín hiệu phân tích. Cần khởi động lại backend để nạp bản cập nhật.");
    renderSignals(data.signals);
    try {
      updateChartData(data.candles);
    } catch (chartError) {
      console.error("Chart update failed:", chartError);
    }
  } catch (err) {
    if (marketDataRequest?.controller === controller && (!controller.signal.aborted || controller.signal.reason === "timeout")) {
      document.getElementById("signal-action-text").textContent = "CHƯA TẢI ĐƯỢC";
      document.getElementById("signal-reasons-list").textContent = controller.signal.reason === "timeout"
        ? "Kết nối quá thời gian chờ. Hệ thống sẽ tự thử lại."
        : err.message;
    }
  } finally {
    clearTimeout(timeout);
    if (marketDataRequest?.controller === controller) marketDataRequest = null;
  }
}

function renderSignals(sig) {
  const badge = document.getElementById("signal-badge");
  const actionText = document.getElementById("signal-action-text");
  
  if (sig.action.includes("BUY")) {
    badge.className = "px-3.5 py-1.5 rounded-lg font-bold text-xs badge-buy flex items-center space-x-2";
  } else if (sig.action.includes("SELL")) {
    badge.className = "px-3.5 py-1.5 rounded-lg font-bold text-xs badge-sell flex items-center space-x-2";
  } else {
    badge.className = "px-3.5 py-1.5 rounded-lg font-bold text-xs badge-gold flex items-center space-x-2";
  }
  actionText.textContent = sig.action_text || sig.action;

  // RSI
  document.getElementById("rsi-val").textContent = Number(sig.rsi).toFixed(2);
  const rsiBar = document.getElementById("rsi-bar");
  rsiBar.style.width = `${Math.min(Math.max(sig.rsi, 0), 100)}%`;
  const rsiBadge = document.getElementById("rsi-status-badge");
  if (sig.rsi < 30) {
    rsiBadge.textContent = "Quá Bán (Oversold)";
    rsiBadge.className = "font-semibold text-emerald-400";
    rsiBar.className = "bg-emerald-400 h-full rounded-full transition-all";
  } else if (sig.rsi > 70) {
    rsiBadge.textContent = "Quá Mua (Overbought)";
    rsiBadge.className = "font-semibold text-red-400";
    rsiBar.className = "bg-red-400 h-full rounded-full transition-all";
  } else {
    rsiBadge.textContent = "Trung tính";
    rsiBadge.className = "font-semibold text-gray-400";
    rsiBar.className = "bg-yellow-400 h-full rounded-full transition-all";
  }

  // EMA
  document.getElementById("ema9-val").textContent = `$${sig.ema9.toLocaleString("en-US", { maximumSignificantDigits: 8 })}`;
  document.getElementById("ema21-val").textContent = `$${sig.ema21.toLocaleString("en-US", { maximumSignificantDigits: 8 })}`;
  const emaTrend = document.getElementById("ema-trend-badge");
  if (sig.ema9 >= sig.ema21) {
    emaTrend.textContent = "Tăng (Bullish)";
    emaTrend.className = "font-semibold text-emerald-400";
  } else {
    emaTrend.textContent = "Giảm (Bearish)";
    emaTrend.className = "font-semibold text-red-400";
  }

  // MACD
  if (sig.macd) {
    document.getElementById("macd-line-val").textContent = Number(sig.macd.macd).toPrecision(6);
    document.getElementById("macd-sig-val").textContent = Number(sig.macd.signal).toPrecision(6);
    const macdHistBadge = document.getElementById("macd-hist-badge");
    macdHistBadge.textContent = sig.macd.hist >= 0 ? `+${Number(sig.macd.hist).toPrecision(6)}` : Number(sig.macd.hist).toPrecision(6);
    macdHistBadge.className = sig.macd.hist >= 0 ? "font-semibold text-emerald-400" : "font-semibold text-red-400";
  }

  // Bollinger Bands
  if (sig.bb?.upper != null) {
    document.getElementById("bb-upper").textContent = `$${sig.bb.upper.toLocaleString("en-US", { maximumSignificantDigits: 8 })}`;
    document.getElementById("bb-lower").textContent = `$${sig.bb.lower.toLocaleString("en-US", { maximumSignificantDigits: 8 })}`;
    const bbStatus = document.getElementById("bb-status");
    if (sig.bb.percent_b <= 0.1) {
      bbStatus.textContent = "Chạm dải dưới";
      bbStatus.className = "font-semibold text-emerald-400";
    } else if (sig.bb.percent_b >= 0.9) {
      bbStatus.textContent = "Chạm dải trên";
      bbStatus.className = "font-semibold text-red-400";
    } else {
      bbStatus.textContent = "Trong kênh";
      bbStatus.className = "font-semibold text-gray-400";
    }
  }

  // Reasons list
  const reasonsList = document.getElementById("signal-reasons-list");
  if (sig.reasons && sig.reasons.length > 0) {
    reasonsList.innerHTML = sig.reasons
      .map((r) => `<li class="flex items-start"><span class="text-yellow-400 mr-2">•</span><span>${r}</span></li>`)
      .join("");
  }

  // Setup Plan
  document.getElementById("suggested-entry").textContent = sig.suggested_entry == null ? "—" : `$${sig.suggested_entry.toLocaleString("en-US", { maximumSignificantDigits: 8 })}`;
  document.getElementById("suggested-tp").textContent = sig.suggested_tp == null ? "—" : `$${sig.suggested_tp.toLocaleString("en-US", { maximumSignificantDigits: 8 })}`;
  document.getElementById("suggested-sl").textContent = sig.suggested_sl == null ? "—" : `$${sig.suggested_sl.toLocaleString("en-US", { maximumSignificantDigits: 8 })}`;
}

// ==================== DYNAMIC COIN SELECTION ====================
let allSymbols = [];
let activeCategory = "ALL";

const COIN_CATEGORIES = {
  L1: ["BTC", "ETH", "SOL", "BNB", "SUI", "NEAR", "AVAX", "ADA", "APT", "DOT", "TRX", "ATOM", "FTM", "ALGO", "SEI", "INJ", "LTC"],
  DEFI: ["UNI", "AAVE", "LINK", "MKR", "CRV", "SNX", "LDO", "COMP", "DYDX", "RUNE", "SUSHI", "JUP", "RAY", "PENDLE"],
  MEME: ["DOGE", "SHIB", "PEPE", "FLOKI", "BONK", "WIF", "BOME", "MEME", "1000PEPE", "1000SHIB", "1000FLOKI", "1000BONK"],
  AI: ["NEAR", "FET", "RENDER", "TAO", "GRT", "AGIX", "WLD", "OCEAN", "RNDR", "ARKM", "IO", "ATH"]
};

async function fetchAvailableSymbols() {
  try {
    const market = state.marketMode;
    const res = await fetch(`/api/market/symbols?market=${market}`);
    if (res.ok) {
      const data = await res.json();
      if (market !== state.marketMode) return;
      allSymbols = data.symbols || [];
      renderCoinList(allSymbols);
    }
  } catch (err) {
    console.error("fetchAvailableSymbols error:", err);
  }
}

function toggleCoinModal() {
  const modal = document.getElementById("coin-search-modal");
  if (!modal) return;
  const isHidden = modal.classList.contains("hidden");
  if (isHidden) {
    modal.classList.remove("hidden");
    if (allSymbols.length === 0) fetchAvailableSymbols();
    else renderCoinList(allSymbols);
    setTimeout(() => document.getElementById("coin-search-input")?.focus(), 100);
  } else {
    modal.classList.add("hidden");
  }
}

function filterCategory(cat) {
  activeCategory = cat;
  document.querySelectorAll(".cat-btn").forEach((btn) => {
    if (btn.getAttribute("data-cat") === cat) {
      btn.className = "cat-btn px-3 py-1 rounded-lg bg-yellow-500 text-black font-bold whitespace-nowrap";
    } else {
      btn.className = "cat-btn px-3 py-1 rounded-lg bg-[#080a0f] border border-[#232a3d] text-gray-300 hover:text-white whitespace-nowrap";
    }
  });
  filterCoinList();
}

function filterCoinList() {
  const input = document.getElementById("coin-search-input")?.value.toUpperCase() || "";
  let filtered = allSymbols;

  if (activeCategory !== "ALL" && COIN_CATEGORIES[activeCategory]) {
    const allowed = COIN_CATEGORIES[activeCategory];
    filtered = filtered.filter((s) => allowed.some((token) => s.startsWith(token)));
  }

  if (input) {
    filtered = filtered.filter((s) => s.includes(input));
  }

  renderCoinList(filtered, input);
}

function renderCoinList(symbols, searchInput = "") {
  const container = document.getElementById("coin-list-container");
  if (!container) return;

  if (!symbols || symbols.length === 0) {
    let customOption = "";
    if (searchInput && searchInput.length >= 3) {
      const customSym = searchInput.endsWith("USDT") ? searchInput : `${searchInput}USDT`;
      customOption = `
        <div class="mt-3 p-3 bg-yellow-500/10 border border-yellow-500/30 rounded-xl text-center">
          <div class="text-xs text-yellow-300 mb-2">Thử giao dịch cặp tùy chỉnh: <b>${customSym}</b></div>
          <button onclick="selectSymbol('${customSym}')" class="px-4 py-1.5 bg-yellow-500 hover:bg-yellow-400 text-black font-bold rounded-lg text-xs">
            Chọn ${customSym}
          </button>
        </div>
      `;
    }
    container.innerHTML = `
      <div class="p-6 text-center text-gray-400">
        <i class="fa-solid fa-circle-exclamation text-2xl mb-2 text-yellow-500"></i>
        <div>Không tìm thấy cặp coin khớp với từ khóa.</div>
        ${customOption}
      </div>
    `;
    return;
  }

  container.innerHTML = symbols
    .slice(0, 100)
    .map((s) => {
      const isSelected = (s === state.symbol);
      const base = s.replace("USDT", "");
      return `
      <div onclick="selectSymbol('${s}')" class="p-3 hover:bg-[#1c2233] flex items-center justify-between cursor-pointer rounded-xl transition-colors ${isSelected ? 'bg-yellow-500/10 border-l-4 border-l-yellow-500' : ''}">
        <div class="flex items-center space-x-3">
          <span class="w-7 h-7 rounded-full bg-[#151a26] border border-[#232a3d] text-yellow-400 font-bold text-xs flex items-center justify-center">${base.slice(0, 1)}</span>
          <div>
            <div class="font-bold text-white text-sm">${base} <span class="text-xs text-gray-400">/ USDT</span></div>
            <div class="text-[10px] text-gray-400 font-sans">${s}</div>
          </div>
        </div>
        <div class="text-right">
          ${isSelected ? '<span class="text-xs text-yellow-400 font-bold"><i class="fa-solid fa-check mr-1"></i> Đang chọn</span>' : '<span class="text-xs text-gray-400 hover:text-white">Chọn »</span>'}
        </div>
      </div>
    `;
    })
    .join("");
}

function selectSymbol(sym) {
  state.symbol = sym.toUpperCase();
  const base = state.symbol.replace("USDT", "");
  
  const symLabel = document.getElementById("selected-coin-symbol");
  if (symLabel) symLabel.textContent = `${base} / USDT`;
  const icon = document.getElementById("selected-coin-icon");
  if (icon) icon.textContent = base.slice(0, 1);

  // Update active pill
  document.querySelectorAll(".coin-pill").forEach((pill) => {
    if (pill.getAttribute("data-sym") === state.symbol) {
      pill.className = "coin-pill active px-2.5 py-1 rounded-lg bg-[#232a3d] text-yellow-400 font-bold font-mono";
    } else {
      pill.className = "coin-pill px-2.5 py-1 rounded-lg bg-[#080a0f] border border-[#232a3d] text-gray-300 hover:text-white font-mono";
    }
  });

  const modal = document.getElementById("coin-search-modal");
  if (modal) modal.classList.add("hidden");

  fetchKlinesAndSignals();
  fetchOpenOrders();
  fetchTradeHistory();
}

function changeInterval(tf) {
  state.interval = tf;
  document.querySelectorAll(".tf-btn").forEach((btn) => {
    if (btn.getAttribute("data-tf") === tf) {
      btn.className = "tf-btn px-2.5 py-1 rounded bg-[#232a3d] text-yellow-400 font-bold";
    } else {
      btn.className = "tf-btn px-2.5 py-1 rounded text-gray-400 hover:text-white";
    }
  });
  fetchKlinesAndSignals();
}

// ==================== MULTI-COIN WATCHLIST CONTROLLER ====================
function renderWatchlistChips() {
  const container = document.getElementById("bot-watchlist-chips");
  if (!container) return;

  const countBadge = document.getElementById("watchlist-count-badge");
  if (countBadge) countBadge.textContent = `${state.watchlistSymbols.length} Coin`;

  // Merge known candidates with current watchlist
  const suggested = POPULAR_WATCHLIST_CANDIDATES.flatMap(sym => [sym, "1000" + sym]);
  const allCandidates = Array.from(new Set([...state.watchlistSymbols, ...suggested.filter(sym => botSymbols.includes(sym))]));

  container.innerHTML = allCandidates
    .map((sym) => {
      const isChecked = state.watchlistSymbols.includes(sym);
      const base = sym.replace("USDT", "");
      return `
        <button type="button" onclick="toggleWatchlistCoin('${sym}')" 
          class="coin-chip px-2.5 py-1 rounded-lg text-xs font-mono font-semibold flex items-center space-x-1 ${isChecked ? 'active' : ''}">
          <i class="fa-solid ${isChecked ? 'fa-check text-yellow-400' : 'fa-plus text-gray-500'} text-[10px]"></i>
          <span>${base}</span>
        </button>
      `;
    })
    .join("");
}

function toggleWatchlistCoin(sym) {
  const idx = state.watchlistSymbols.indexOf(sym);
  if (idx > -1) {
    if (state.watchlistSymbols.length <= 1) {
      alert("Bot cần theo dõi tối thiểu ít nhất 1 đồng coin!");
      return;
    }
    state.watchlistSymbols.splice(idx, 1);
  } else {
    state.watchlistSymbols.push(sym);
  }
  renderWatchlistChips();
  markBotDirty();
}

function setMaxPositions(val) {
  state.maxPositions = val;
  const maxDisplay = document.getElementById("max-pos-display");
  if (maxDisplay) maxDisplay.textContent = `${val} lệnh`;

  document.querySelectorAll(".max-pos-btn").forEach((btn) => {
    if (parseInt(btn.getAttribute("data-val")) === val) {
      btn.className = "max-pos-btn py-1 rounded-lg bg-yellow-500/20 border border-yellow-500/50 text-yellow-400 font-mono font-bold";
    } else {
      btn.className = "max-pos-btn py-1 rounded-lg border border-[#232a3d] text-gray-300 font-mono font-bold";
    }
  });

  markBotDirty();
}

// ==================== BOT STATUS & MULTI-POSITION RENDERING ====================
async function fetchBotStatus() {
  if (botStatusInFlight) return;
  botStatusInFlight = true;
  try {
    const res = await fetch("/api/bots/status");
    if (!res.ok) throw new Error("Không tải được trạng thái hai bot");
    const data = await res.json();
    botsStatus = data.bots;
    renderBotOverview();
    renderSelectedBot(botsStatus[selectedBotMarket]);
  } catch (err) {
    document.getElementById("bot-monitor-status").textContent = err.message;
    console.error("fetchBotStatus error:", err);
  } finally {
    botStatusInFlight = false;
  }
}

function renderSelectedBot(bot) {
    state.isBotRunning = bot.is_running;
    document.getElementById("positions-bot-select").value = selectedBotMarket;
    document.querySelectorAll(".selected-bot-label").forEach(el => el.textContent = `Bot ${selectedBotMarket}`);

    const configKeys = ["market_type", "leverage", "interval", "watchlist_symbols", "max_positions", "strategy",
      "trade_amount_usdt", "scan_interval_sec", "take_profit_pct", "stop_loss_pct", "max_total_margin_usdt", "max_open_risk_usdt"];
    if (!botConfig || (!botConfigDirty && !botSaveInFlight && configKeys.some(key => JSON.stringify(botConfig[key]) !== JSON.stringify(bot[key])))) {
      applyBotConfig(bot);
    }
    document.getElementById("bot-market-tag").textContent = bot.market_type;
    const modeStat = document.getElementById("stat-current-mode");
    if (modeStat) modeStat.textContent = `${bot.market_type} ${bot.market_type === "FUTURES" ? bot.leverage : 1}x`;
    document.getElementById("bot-desc-text").textContent = `${bot.market_type} · ${bot.leverage}x · nến ${bot.interval} · TP ${bot.take_profit_pct}% / SL ${bot.stop_loss_pct}%`;
    document.getElementById("bot-monitor-status").textContent = (bot.state_error ? "Lỗi lưu trạng thái. Đã chặn gửi lệnh." : bot.last_error) || (
      Object.keys(bot.position_mismatches || {}).length ? "Có vị thế lệch với sàn. Đã chặn mở mới." :
      bot.close_all_requested ? "Đang đóng toàn bộ vị thế và xử lý lệnh chờ." :
      bot.futures_protection_ready === false ? "Đang xác nhận bảo vệ SL Futures. Đã chặn mở mới." :
      bot.monitoring ? "Giám sát vị thế đang hoạt động." : "Giám sát vị thế đang dừng."
    );

    // Exchange positions with no bot record (e.g. manual trades). Closing them is opt-in per symbol.
    untrackedPositions = Object.entries(bot.position_mismatches || {})
      .filter(([sym, d]) => sym !== "account" && !d.error && !d.bot_quantity && d.exchange_quantity)
      .map(([sym, d]) => ({ symbol: sym, quantity: d.exchange_quantity }));
    const untrackedBtn = document.getElementById("btn-close-untracked");
    if (untrackedBtn) {
      const show = bot.market_type === "FUTURES" && untrackedPositions.length > 0;
      untrackedBtn.classList.toggle("hidden", !show);
      untrackedBtn.textContent = show ? `Đóng vị thế ngoài bot: ${untrackedPositions.map(p => p.symbol).join(", ")}` : "";
      untrackedBtn.disabled = bot.is_running;
      untrackedBtn.title = bot.is_running ? "Dừng bot trước khi đóng" : "";
    }

    // Power Button UI
    const toggleBtn = document.getElementById("bot-toggle-btn");
    const toggleText = document.getElementById("bot-toggle-text");
    const icon = toggleBtn.querySelector("i");
    if (bot.is_running) {
      toggleBtn.className = "px-3.5 py-2 rounded-xl font-bold text-xs bg-red-500 hover:bg-red-600 text-white flex items-center space-x-1.5 transition-all shadow-lg shadow-red-500/20";
      toggleText.textContent = `DỪNG ${selectedBotMarket}`;
      icon.className = "fa-solid fa-pause";
    } else {
      toggleBtn.className = "px-3.5 py-2 rounded-xl font-bold text-xs bg-emerald-500 hover:bg-emerald-400 text-black flex items-center space-x-1.5 transition-all shadow-lg shadow-emerald-500/20";
      toggleText.textContent = `CHẠY ${selectedBotMarket}`;
      icon.className = "fa-solid fa-play";
    }
    toggleBtn.disabled = botActionsInFlight.has(selectedBotMarket);

    // Process Active Positions
    state.activePositions = bot.active_positions || (bot.active_position ? [bot.active_position] : []);
    renderActivePositions(state.activePositions);

    // Bot Metrics
    const statPnl = document.getElementById("stat-total-pnl");
    if (statPnl) {
      statPnl.textContent = `${bot.total_pnl_usdt >= 0 ? "+$" : "-$"}${Math.abs(bot.total_pnl_usdt).toFixed(2)}`;
      statPnl.className = bot.total_pnl_usdt >= 0 ? "text-2xl font-bold font-mono text-emerald-400 mt-2" : "text-2xl font-bold font-mono text-red-400 mt-2";
    }
    const statWinrate = document.getElementById("stat-winrate");
    if (statWinrate) statWinrate.textContent = `${bot.winrate}%`;
    const statWinLoss = document.getElementById("stat-win-loss-text");
    if (statWinLoss) statWinLoss.textContent = `${bot.win_count} thắng / ${bot.loss_count} thua (${bot.total_trades} lệnh)`;
    document.getElementById("dash-realized-pnl").textContent = `${bot.total_pnl_usdt >= 0 ? "+" : "-"}$${Math.abs(bot.total_pnl_usdt).toFixed(2)}`;
    document.getElementById("dash-winrate-text").textContent = `Bot ${selectedBotMarket} · Winrate ${bot.winrate}%${bot.history_scope === "legacy_combined" ? " · gồm lịch sử chung trước khi tách" : ""}`;

    // Live Logs
    const logContainer = document.getElementById("bot-console-logs");
    if (bot.recent_logs && bot.recent_logs.length > 0) {
      logContainer.replaceChildren(...bot.recent_logs.map((log) => {
        const row = document.createElement("div");
        row.className = {
          SUCCESS: "text-emerald-400 font-bold",
          TP: "text-emerald-400 font-bold",
          ERROR: "text-red-400 font-bold",
          SL: "text-red-400 font-bold",
          SIGNAL: "text-yellow-400 font-bold",
          START: "text-blue-400 font-semibold",
          STOP: "text-blue-400 font-semibold",
        }[log.level] || "text-gray-300";
        row.textContent = `[${log.time}] [${log.level}] ${log.message}`;
        return row;
      }));
    } else {
      logContainer.textContent = `Bot ${selectedBotMarket} chưa có nhật ký trong phiên này.`;
    }

    const pending = Object.keys(bot.pending_orders || {});
    const lastCheck = document.getElementById("bot-last-check");
    if (pending.length) {
      lastCheck.textContent = `Đang đối soát lệnh: ${pending.join(", ")}`;
    } else if (bot.last_check_time) {
      lastCheck.textContent = `Quét gần nhất: ${bot.last_check_time}`;
    } else {
      lastCheck.textContent = "Chờ quét…";
    }
}

function renderActivePositions(positions) {
  const count = positions ? positions.length : 0;
  
  // Update badge counters
  const termBadge = document.getElementById("terminal-pos-count-badge");
  const navBadge = document.getElementById("active-pos-counter");
  const dashCount = document.getElementById("dash-positions-count");
  const dashCap = document.getElementById("dash-positions-capacity");
  const closeAllBtn = document.getElementById("btn-close-all-terminal");

  if (termBadge) termBadge.textContent = count;
  if (navBadge) navBadge.textContent = count;
  const capacity = botsStatus[selectedBotMarket]?.max_positions || state.maxPositions;
  if (dashCount) dashCount.textContent = `${count} / ${capacity}`;
  if (dashCap) {
    const remain = capacity - count;
    dashCap.textContent = remain > 0 ? `Còn trống ${remain} vị thế` : "Đã đạt giới hạn tối đa";
  }

  if (closeAllBtn) {
    if (count > 0) closeAllBtn.classList.remove("hidden");
    else closeAllBtn.classList.add("hidden");
  }

  // Calculate sum PnL & Margin
  let totalUnrealized = 0;
  let totalMargin = 0;
  positions.forEach((p) => {
    totalUnrealized += (p.current_pnl_usdt || 0);
    totalMargin += (p.cost_usdt || 0);
  });

  const dashUnreal = document.getElementById("dash-unrealized-pnl");
  if (dashUnreal) {
    dashUnreal.textContent = `${totalUnrealized >= 0 ? "+$" : "-$"}${Math.abs(totalUnrealized).toFixed(2)}`;
    dashUnreal.className = totalUnrealized >= 0 ? "text-2xl font-bold font-mono text-emerald-400 mt-1" : "text-2xl font-bold font-mono text-red-400 mt-1";
  }

  const dashMarg = document.getElementById("dash-margin-used");
  if (dashMarg) {
    dashMarg.textContent = `$${totalMargin.toFixed(2)}`;
  }

  // 1. Render in Terminal Card below chart
  const termContainer = document.getElementById("terminal-positions-container");
  if (termContainer) {
    if (count === 0) {
      termContainer.innerHTML = `
        <div class="py-8 text-center text-gray-500 text-xs font-sans">
          <i class="fa-solid fa-inbox text-2xl mb-1 text-gray-600 block"></i>
          Bot ${selectedBotMarket} chưa có vị thế đang mở.
        </div>
      `;
    } else {
      termContainer.innerHTML = `
        <table class="w-full text-left text-xs text-gray-300">
          <thead class="bg-[#080a0f] text-gray-400 font-mono">
            <tr>
              <th class="p-2.5">Cặp Coin</th>
              <th class="p-2.5">Phe & Đòn Bẩy</th>
              <th class="p-2.5">Giá Vào / Hiện Tại</th>
              <th class="p-2.5">Khối Lượng</th>
              <th class="p-2.5">Lãi/Lỗ (ROE)</th>
              <th class="p-2.5 text-right">Đóng Lệnh</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-[#232a3d] font-mono">
            ${positions.map((p) => {
              const isLong = p.position_side === "LONG";
              const pnlPct = p.current_pnl_pct || 0;
              const pnlUsdt = p.current_pnl_usdt || 0;
              const pnlColor = pnlPct >= 0 ? "text-emerald-400 font-bold" : "text-red-400 font-bold";
              return `
                <tr class="hover:bg-[#151a26] transition-colors pos-row-enter">
                  <td class="p-2.5 font-bold text-white flex items-center space-x-1.5">
                    <span class="w-2 h-2 rounded-full ${pnlPct >= 0 ? 'bg-emerald-400 pulse-green' : 'bg-red-400 pulse-red'}"></span>
                    <span>${p.symbol}</span>
                    <span class="text-[10px] text-gray-500">[${p.market_type}]</span>
                  </td>
                  <td class="p-2.5">
                    <span class="px-2 py-0.5 rounded text-[11px] font-bold ${isLong ? 'bg-emerald-500/20 text-emerald-400' : 'bg-red-500/20 text-red-400'}">
                      ${p.position_side} ${p.leverage ? p.leverage + "x" : ""}
                    </span>
                  </td>
                  <td class="p-2.5 font-mono text-gray-300">
                    <div>$${p.entry_price ? p.entry_price.toLocaleString() : "--"}</div>
                    <div class="text-white font-bold">$${p.current_price ? p.current_price.toLocaleString() : "--"}</div>
                  </td>
                  <td class="p-2.5 font-mono text-gray-300">
                    <div>${p.quantity}</div>
                    <div class="text-[10px] text-yellow-400/80">$${p.cost_usdt ? p.cost_usdt.toFixed(1) : "--"}</div>
                  </td>
                  <td class="p-2.5 font-mono ${pnlColor}">
                    ${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(2)}%
                    <div class="text-[11px] font-normal">${pnlUsdt >= 0 ? "+$" : "-$"}${Math.abs(pnlUsdt).toFixed(2)}</div>
                  </td>
                  <td class="p-2.5 text-right">
                    <button onclick="closePosition('${p.symbol}', '${p.market_type}')" class="px-2.5 py-1 rounded-lg bg-red-500/20 hover:bg-red-500 text-red-300 hover:text-white font-bold text-xs transition-colors">
                      Đóng
                    </button>
                  </td>
                </tr>
              `;
            }).join("")}
          </tbody>
        </table>
      `;
    }
  }

  // 2. Render in Full Positions Dashboard (Tab 2)
  const fullTbody = document.getElementById("full-positions-table-body");
  if (fullTbody) {
    if (count === 0) {
      fullTbody.innerHTML = `
        <tr>
          <td colspan="8" class="text-center py-12 text-gray-500 font-sans">
            <i class="fa-solid fa-inbox text-3xl mb-2 text-gray-600 block"></i>
            Không có vị thế nào đang mở. Bot sẽ tự động mở lệnh khi quét thấy tín hiệu đẹp.
          </td>
        </tr>
      `;
    } else {
      fullTbody.innerHTML = positions.map((p) => {
        const isLong = p.position_side === "LONG";
        const pnlPct = p.current_pnl_pct || 0;
        const pnlUsdt = p.current_pnl_usdt || 0;
        const pnlColor = pnlPct >= 0 ? "text-emerald-400 font-bold" : "text-red-400 font-bold";
        return `
          <tr class="hover:bg-[#151a26] transition-colors pos-row-enter">
            <td class="p-3 font-bold text-white flex items-center space-x-2">
              <span class="w-2.5 h-2.5 rounded-full ${pnlPct >= 0 ? 'bg-emerald-400 pulse-green' : 'bg-red-400 pulse-red'}"></span>
              <span class="text-sm">${p.symbol}</span>
            </td>
            <td class="p-3">
              <span class="px-2.5 py-1 rounded-md text-xs font-bold ${isLong ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30' : 'bg-red-500/20 text-red-400 border border-red-500/30'}">
                ${p.market_type} · ${p.position_side} ${p.leverage ? p.leverage + "x" : ""}
              </span>
            </td>
            <td class="p-3 font-mono text-gray-300">$${p.entry_price ? p.entry_price.toLocaleString() : "--"}</td>
            <td class="p-3 font-mono font-bold text-white">$${p.current_price ? p.current_price.toLocaleString() : "--"}</td>
            <td class="p-3 font-mono text-gray-300">
              <div>${p.quantity} ${p.symbol.replace("USDT","")}</div>
              <div class="text-[11px] text-yellow-400">Ký quỹ: $${p.cost_usdt ? p.cost_usdt.toFixed(2) : "--"}</div>
            </td>
            <td class="p-3 font-mono text-xs">
              <div class="text-emerald-400">TP: $${p.target_tp_price ? p.target_tp_price.toLocaleString() : "--"}</div>
              <div class="text-red-400">SL: $${p.target_sl_price ? p.target_sl_price.toLocaleString() : "--"}</div>
              <div class="text-[10px] ${p.stop_protection === 'exchange' ? 'text-emerald-400' : 'text-yellow-400'}">${p.stop_protection === 'exchange' ? 'SL trên Binance đã xác nhận' : p.stop_protection === 'local' ? 'SL do ứng dụng giám sát' : 'SL trên sàn đang xử lý'}</div>
            </td>
            <td class="p-3 font-mono ${pnlColor} text-sm">
              ${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(2)}%
              <div class="text-xs font-semibold">${pnlUsdt >= 0 ? "+$" : "-$"}${Math.abs(pnlUsdt).toFixed(2)}</div>
            </td>
            <td class="p-3 text-right">
              <button onclick="closePosition('${p.symbol}', '${p.market_type}')" class="px-3 py-1.5 rounded-lg bg-red-500/20 hover:bg-red-500 text-red-300 hover:text-white font-bold text-xs transition-all shadow">
                Đóng Vị Thế
              </button>
            </td>
          </tr>
        `;
      }).join("");
    }
  }
}

async function toggleBotPower(market = selectedBotMarket) {
  const bot = botsStatus[market];
  if (!bot || botActionsInFlight.has(market)) return;
  if (!bot.is_running && (botDrafts[market] || (botSaveInFlight && market === selectedBotMarket))) {
    alert("Hãy lưu cấu hình bot trước khi khởi động.");
    return;
  }
  const endpoint = `/api/bot/${bot.is_running ? "stop" : "start"}?market=${market}`;
  botActionsInFlight.add(market);
  renderBotOverview();
  try {
    const res = await fetch(endpoint, { method: "POST" });
    const data = await res.json();
    if (!res.ok || !data.success) throw new Error(data.message || data.detail || "Không điều khiển được bot");
    alert(`Bot ${market}: ${data.message}`);
  } catch (err) {
    alert("Lỗi khi điều khiển Bot: " + err);
  } finally {
    botActionsInFlight.delete(market);
    await fetchBotStatus();
  }
}

function selectStrategy(stratName) {
  state.strategy = stratName;
  document.querySelectorAll(".strategy-card").forEach((c) => c.classList.remove("selected"));
  const target = document.getElementById(`strat-${stratName}`);
  if (target) target.classList.add("selected");
  markBotDirty();
}

async function saveBotConfig() {
  if (!botConfig || botSaveInFlight) return;
  if (state.isBotRunning) {
    alert("Tạm dừng mở lệnh mới trước khi lưu cấu hình.");
    return;
  }
  const market = selectedBotMarket;
  const payload = readBotDraft();
  for (const key of ["trade_amount_usdt", "scan_interval_sec", "take_profit_pct", "stop_loss_pct", "max_total_margin_usdt", "max_open_risk_usdt"]) payload[key] = Number(payload[key]);
  botSaveInFlight = true;
  document.getElementById("bot-settings-fields").disabled = true;
  try {
    const res = await fetch(`/api/bot/config?market=${market}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Cấu hình không hợp lệ. Kiểm tra lại các giá trị.");
    botsStatus[market] = data.config;
    delete botDrafts[market];
    applyBotConfig(data.config);
    renderBotOverview();
    await fetchBotStatus();
  } catch (err) {
    document.getElementById("bot-config-status").textContent = err.message;
  } finally {
    botSaveInFlight = false;
    document.getElementById("bot-settings-fields").disabled = false;
  }
}

async function closePosition(symbol, market = selectedBotMarket) {
  if (!confirm(`Đóng vị thế ${symbol} của bot ${market} theo giá thị trường Binance Demo?`)) return;
  try {
    const res = await fetch(`/api/bot/close-position?market=${market}&symbol=${symbol}`, { method: "POST" });
    const data = await res.json();
    if (res.ok && data.success) {
      alert(data.message || `Đã đóng vị thế ${symbol}!`);
    } else {
      alert("Lỗi đóng vị thế: " + (data.detail || data.error));
    }
    fetchBotStatus();
    fetchAccountBalances();
  } catch (err) {
    alert("Lỗi đóng vị thế: " + err);
  }
}

async function closeAllPositions() {
  const market = selectedBotMarket;
  if (!confirm(`Đóng tất cả vị thế và dừng mở mới của bot ${market}? Bot còn lại tiếp tục hoạt động.`)) return;
  try {
    const res = await fetch(`/api/bot/close-all?market=${market}`, { method: "POST" });
    const data = await res.json();
    if (res.ok && (data.success || data.pending)) {
      alert(data.message);
    } else {
      alert("Lỗi khi đóng tất cả: " + (data.detail || data.error));
    }
    fetchBotStatus();
    fetchAccountBalances();
  } catch (err) {
    alert("Lỗi kết nối: " + err);
  }
}

let untrackedPositions = [];

async function closeUntrackedPositions() {
  const market = selectedBotMarket;
  if (market !== "FUTURES" || !untrackedPositions.length) return;
  const detail = untrackedPositions.map(p => `${p.symbol}: ${p.quantity}`).join("\n");
  if (!confirm(`Các vị thế sau KHÔNG do bot mở (có thể là lệnh tay của bạn):\n${detail}\n\nĐóng bằng lệnh MARKET reduce-only?`)) return;
  try {
    const res = await fetch(`/api/bot/close-untracked?market=${market}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbols: untrackedPositions.map(p => p.symbol) }),
    });
    const data = await res.json();
    if (!res.ok) {
      alert("Lỗi: " + (data.detail || data.error));
    } else {
      alert(data.results.map(r => `${r.symbol}: ${r.success ? "đã đóng" : r.error}`).join("\n"));
    }
    fetchBotStatus();
    fetchAccountBalances();
  } catch (err) {
    alert("Lỗi kết nối: " + err);
  }
}

// ==================== MANUAL ORDER EXECUTION ====================
function setManualPercent(factor) {
  const amountInput = document.getElementById("manual-amount");
  if (!amountInput) return;
  const calculated = Math.max(5, Math.floor(state.availableBalanceUsdt * factor));
  amountInput.value = calculated;
}

async function submitManualOrder(side) {
  const orderType = document.getElementById("manual-order-type").value;
  const amountUsdt = parseFloat(document.getElementById("manual-amount").value);
  const limitPrice = parseFloat(document.getElementById("manual-limit-price").value);

  if (amountUsdt < 5.0) {
    alert("Số tiền tối thiểu cho 1 lệnh Spot là 5 USDT!");
    return;
  }

  const payload = {
    market_type: "SPOT",
    symbol: state.symbol,
    side: side,
    order_type: orderType,
    quote_order_qty: amountUsdt,
  };

  if (orderType === "LIMIT") {
    if (!limitPrice || limitPrice <= 0) {
      alert("Vui lòng nhập mức giá Limit hợp lệ!");
      return;
    }
    payload.price = limitPrice;
    payload.quantity = amountUsdt / limitPrice;
    delete payload.quote_order_qty;
  }

  if (!confirm(`Xác nhận đặt lệnh SPOT: ${side} ${state.symbol} với giá trị $${amountUsdt} USDT?`)) return;

  try {
    const res = await fetch("/api/order/manual", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (res.ok && data.success) {
      alert(`✅ Đặt lệnh SPOT ${side} thành công! Order ID: #${data.data.orderId}`);
      fetchAccountBalances();
      fetchBotStatus();
      fetchOpenOrders();
    } else {
      alert(`❌ Đặt lệnh thất bại: ${data.detail || data.error}`);
    }
  } catch (err) {
    alert("Lỗi kết nối đặt lệnh: " + err);
  }
}

async function submitFuturesOrder(side) {
  const orderType = document.getElementById("manual-order-type").value;
  const amountUsdt = parseFloat(document.getElementById("manual-amount").value);
  const limitPrice = parseFloat(document.getElementById("manual-limit-price").value);

  const directionText = side === "BUY" ? "LONG" : "SHORT";

  if (amountUsdt < 5.0) {
    alert("Số tiền ký quỹ tối thiểu là 5 USDT!");
    return;
  }

  const payload = {
    market_type: "FUTURES",
    symbol: state.symbol,
    side: side,
    order_type: orderType,
    quote_order_qty: amountUsdt,
    leverage: state.leverage,
  };

  if (orderType === "LIMIT") {
    if (!limitPrice || limitPrice <= 0) {
      alert("Vui lòng nhập mức giá Limit hợp lệ!");
      return;
    }
    payload.price = limitPrice;
  }

  const confirmMsg = `Xác nhận mở vị thế FUTURES ${directionText} (${state.leverage}x) cho cặp ${state.symbol} với vốn ký quỹ $${amountUsdt} USDT?`;
  if (!confirm(confirmMsg)) return;

  try {
    const res = await fetch("/api/order/manual", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (res.ok && data.success) {
      alert(`✅ Mở vị thế FUTURES ${directionText} (${state.leverage}x) thành công! Order ID: #${data.data.orderId}`);
      fetchAccountBalances();
      fetchBotStatus();
      fetchOpenOrders();
    } else {
      alert(`❌ Mở vị thế Futures thất bại: ${data.detail || data.error}`);
    }
  } catch (err) {
    alert("Lỗi đặt lệnh Futures: " + err);
  }
}

// ==================== OPEN ORDERS & TRADE HISTORY ====================
async function fetchOpenOrders() {
  try {
    const res = await fetch(`/api/orders/open?symbol=${state.symbol}&market=${state.marketMode}`);
    if (!res.ok) return;
    const data = await res.json();
    const tbody = document.getElementById("open-orders-table-body");
    if (!tbody) return;

    if (!data.orders || data.orders.length === 0) {
      tbody.innerHTML = '<tr><td colspan="8" class="text-center py-6 text-gray-500 font-sans">Không có lệnh nào đang chờ khớp.</td></tr>';
      return;
    }

    tbody.innerHTML = data.orders
      .map((o) => {
        const sideClass = o.side === "BUY" ? "text-emerald-400 font-bold" : "text-red-400 font-bold";
        return `
        <tr class="hover:bg-[#151a26]">
          <td class="p-3 text-gray-400">#${o.orderId}</td>
          <td class="p-3 text-purple-400 font-bold">${state.marketMode}</td>
          <td class="p-3 text-white font-bold">${o.symbol}</td>
          <td class="p-3 ${sideClass}">${o.side}</td>
          <td class="p-3 text-white font-mono">$${parseFloat(o.price).toLocaleString()}</td>
          <td class="p-3 text-white font-mono">${parseFloat(o.origQty)}</td>
          <td class="p-3 text-gray-400 font-mono">${parseFloat(o.executedQty)}</td>
          <td class="p-3 text-right">
            <button onclick="cancelOrder('${o.symbol}', ${o.orderId})" class="text-xs bg-red-500/20 hover:bg-red-500 text-red-400 hover:text-white px-2.5 py-1 rounded transition-colors">
              Hủy lệnh
            </button>
          </td>
        </tr>
      `;
      })
      .join("");
  } catch (err) {
    console.error("fetchOpenOrders error:", err);
  }
}

async function cancelOrder(symbol, orderId) {
  if (!confirm(`Xác nhận hủy lệnh #${orderId}?`)) return;
  try {
    const res = await fetch(`/api/orders/${symbol}/${orderId}?market=${state.marketMode}`, { method: "DELETE" });
    const data = await res.json();
    if (res.ok && data.success) {
      alert("Đã hủy lệnh thành công!");
      fetchOpenOrders();
      fetchAccountBalances();
    } else {
      alert("Lỗi khi hủy lệnh: " + (data.detail || data.error));
    }
  } catch (err) {
    alert("Lỗi hủy lệnh: " + err);
  }
}

async function fetchTradeHistory() {
  try {
    const res = await fetch(`/api/orders/history?symbol=${state.symbol}&limit=15&market=${state.marketMode}`);
    if (!res.ok) return;
    const data = await res.json();
    const tbody = document.getElementById("trades-table-body");
    if (!tbody) return;

    if (!data.trades || data.trades.length === 0) {
      tbody.innerHTML = '<tr><td colspan="8" class="text-center py-6 text-gray-500 font-sans">Chưa có giao dịch khớp lệnh nào trong danh sách.</td></tr>';
      return;
    }

    tbody.innerHTML = data.trades
      .map((t) => {
        const isBuyer = t.isBuyer || t.side === "BUY";
        const sideText = isBuyer ? "MUA / LONG" : "BÁN / SHORT";
        const sideColor = isBuyer ? "text-emerald-400" : "text-red-400";
        const d = new Date(t.time).toLocaleTimeString("vi-VN");
        const price = parseFloat(t.price);
        const qty = parseFloat(t.qty);
        const cost = (price * qty).toFixed(2);
        const realizedPnl = t.realizedPnl ? `$${parseFloat(t.realizedPnl).toFixed(2)}` : "--";

        return `
        <tr class="hover:bg-[#151a26]">
          <td class="p-3 text-gray-400 font-mono">${d}</td>
          <td class="p-3 text-purple-400 font-bold">${state.marketMode}</td>
          <td class="p-3 text-white font-bold">${t.symbol}</td>
          <td class="p-3 font-mono text-white">$${price.toLocaleString()}</td>
          <td class="p-3 font-mono text-gray-300">${qty}</td>
          <td class="p-3 font-mono text-yellow-400">$${cost}</td>
          <td class="p-3 font-mono ${parseFloat(t.realizedPnl) >= 0 ? 'text-emerald-400' : 'text-red-400'}">${realizedPnl}</td>
          <td class="p-3 font-bold ${sideColor}">${sideText}</td>
        </tr>
      `;
      })
      .join("");
  } catch (err) {
    console.error("fetchTradeHistory error:", err);
  }
}
