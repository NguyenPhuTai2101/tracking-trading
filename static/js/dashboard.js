// dashboard.js - performance after estimated fees and current risk usage (GET /api/dashboard)

const DASH_GAIN = "#34d399";
const DASH_LOSS = "#f87171";
const DASH_MAX_DAILY_COLUMNS = 120;
const DASH_REASONS = {
  TAKE_PROFIT: "Chốt lời", STOP_LOSS: "Cắt lỗ", BREAKEVEN_STOP: "Dời SL hòa vốn",
  MANUAL_CLOSE: "Đóng tay", MANUAL_FORCE_CLOSE: "Đóng tay", MANUAL_CLOSE_ALL: "Đóng tất cả",
  MANUAL_FORCE_CLOSE_ALL: "Đóng tất cả", CANCEL_ENTRY: "Hủy lệnh vào", UNKNOWN: "Không rõ",
};

let dashFilter = { market: "ALL", days: 30 };
let dashData = null;
let dashInFlight = false;
let dashEquityChart = null;
let dashEquitySeries = null;

function dashEsc(value) {
  return String(value ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

// Signed money. Gains and losses differ by sign as well as colour.
function dashMoney(value, digits = 2) {
  const n = Number(value) || 0;
  const sign = n > 0 ? "+" : n < 0 ? "−" : "";
  return `${sign}$${Math.abs(n).toFixed(digits)}`;
}

function dashTone(value) {
  const n = Number(value) || 0;
  return n > 0 ? "text-emerald-400" : n < 0 ? "text-red-400" : "text-white";
}

function dashSetFigure(id, text, value) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.classList.remove("text-emerald-400", "text-red-400", "text-white");
  el.classList.add(value === undefined ? "text-white" : dashTone(value));
}

async function loadDashboard() {
  if (dashInFlight) return;
  dashInFlight = true;
  const error = document.getElementById("dash-error");
  try {
    const res = await fetch(`/api/dashboard?market=${dashFilter.market}&days=${dashFilter.days}`);
    if (!res.ok) throw new Error(`Máy chủ trả lỗi ${res.status}`);
    dashData = await res.json();
    error.classList.add("hidden");
    document.getElementById("dash-updated").textContent =
      `Cập nhật ${new Date().toLocaleTimeString("vi-VN", { hour12: false })}.`;
    renderDashboard();
  } catch (err) {
    // Keep the previous figures on screen; only say that the refresh failed.
    error.textContent = `Không tải được số liệu dashboard: ${err.message}`;
    error.classList.remove("hidden");
  } finally {
    dashInFlight = false;
  }
}

function renderDashboard() {
  if (!dashData) return;
  document.querySelectorAll("[data-dash-market]").forEach(b => b.classList.toggle("active", b.dataset.dashMarket === dashFilter.market));
  document.querySelectorAll("[data-dash-days]").forEach(b => b.classList.toggle("active", Number(b.dataset.dashDays) === dashFilter.days));
  renderDashKpis(dashData.kpis);
  renderDashLive(dashData.bots);
  renderDashEquity(dashData.equity);
  renderDashDaily(dashData.daily);
  renderDashBreakdown("dash-by-symbol", dashData.by_symbol, "symbol", "Mã", v => v);
  renderDashBreakdown("dash-by-side", dashData.by_side, "side", "Chiều", v => v);
  renderDashBreakdown("dash-by-reason", dashData.by_reason, "reason", "Lý do", v => DASH_REASONS[v] || v);
}

function renderDashKpis(k) {
  const period = dashFilter.days ? `${dashFilter.days} ngày qua` : "toàn bộ lịch sử đã lưu";
  dashSetFigure("dash-net", dashMoney(k.net_pnl), k.net_pnl);
  document.getElementById("dash-net-sub").textContent = k.trades || k.gross_pnl
    ? `Trước phí ${dashMoney(k.gross_pnl)} · phí ước tính −$${Number(k.fees_est).toFixed(2)} · ${period}`
    : `Chưa có lệnh đã đóng trong ${period}`;

  dashSetFigure("dash-winrate", k.winrate_pct == null ? "–" : `${k.winrate_pct}%`);
  document.getElementById("dash-winrate-sub").textContent = k.trades
    ? `${k.wins} thắng / ${k.losses} thua` + (k.breakeven_winrate_pct == null ? "" : ` · cần ≥ ${k.breakeven_winrate_pct}% để hòa vốn`)
    : "";

  dashSetFigure("dash-pf", k.profit_factor == null ? "–" : k.profit_factor.toFixed(2));
  document.getElementById("dash-pf-sub").textContent = k.trades
    ? `TB thắng ${dashMoney(k.avg_win)} · TB thua ${dashMoney(-k.avg_loss)}` : "Lãi gộp ÷ lỗ gộp, trên 1 là có lãi";

  dashSetFigure("dash-exp", k.expectancy == null ? "–" : dashMoney(k.expectancy, 3), k.expectancy ?? undefined);
  const streak = k.streak > 0 ? `đang thắng ${k.streak} lệnh liền` : k.streak < 0 ? `đang thua ${-k.streak} lệnh liền` : "";
  document.getElementById("dash-exp-sub").textContent = k.trades ? `${k.trades} lệnh đã đóng${streak ? " · " + streak : ""}` : "";

  dashSetFigure("dash-dd", k.trades || k.max_drawdown ? dashMoney(-k.max_drawdown) : "–", k.max_drawdown ? -k.max_drawdown : undefined);
  document.getElementById("dash-dd-sub").textContent = k.trades
    ? `Tệ nhất ${dashMoney(k.worst_trade)} · tốt nhất ${dashMoney(k.best_trade)}` : "";

  const note = document.getElementById("dash-note");
  const small = k.trades > 0 && !k.enough_trades;
  note.textContent = small
    ? `Mới có ${k.trades} lệnh trong giai đoạn này. Dưới 30 lệnh thì tỷ lệ thắng và profit factor chưa nói lên điều gì; xem thêm khi có nhiều lệnh hơn.` : "";
  note.classList.toggle("hidden", !small);
}

// One row per limit: name, bar against the limit, figure. No bar when the limit is not set.
function dashMeter(name, used, limit, figure) {
  const ratio = limit > 0 ? Math.min(1, Math.max(0, used / limit)) : 0;
  const color = ratio >= 0.9 ? DASH_LOSS : ratio >= 0.6 ? "#fbbf24" : "#22d3ee";
  const bar = limit > 0
    ? `<div class="h-2 rounded-full mt-1.5" style="background:${color}2e" role="meter" aria-label="${dashEsc(name)}" aria-valuemin="0" aria-valuemax="${limit}" aria-valuenow="${used}">
         <div class="h-2 rounded-full" style="width:${(ratio * 100).toFixed(1)}%;background:${color}"></div></div>`
    : "";
  return `<div>
    <div class="flex items-baseline justify-between gap-2 text-xs">
      <span class="text-gray-400">${dashEsc(name)}</span><span class="font-mono text-gray-200">${figure}</span>
    </div>${bar}</div>`;
}

function renderDashLive(bots) {
  const usd = v => `$${Number(v).toFixed(2)}`;
  document.getElementById("dash-live").innerHTML = bots.map(b => {
    const state = !b.is_running
      ? { icon: "fa-circle-stop", label: "Đã dừng mở lệnh", tone: "text-gray-400" }
      : b.entry_block_reason
        ? { icon: "fa-circle-pause", label: "Tạm nghỉ đến ngày mai", tone: "text-amber-300" }
        : b.entries_enabled
          ? { icon: "fa-circle-play", label: "Đang mở lệnh", tone: "text-emerald-400" }
          : { icon: "fa-circle-exclamation", label: "Đang chặn mở mới", tone: "text-amber-300" };
    const why = b.entry_block_reason || b.stop_reason;
    const dayLoss = Math.max(0, -b.day_pnl);
    const off = '<span class="text-gray-500">chưa đặt giới hạn</span>';
    return `<div>
      <div class="flex items-center justify-between gap-2">
        <span class="text-sm font-semibold text-white">Bot ${dashEsc(b.market)}</span>
        <span class="text-xs ${state.tone}"><i class="fa-solid ${state.icon} mr-1"></i>${state.label}</span>
      </div>
      <div class="text-[11px] text-gray-500 mt-0.5">${dashEsc(b.strategy)} · ${dashEsc(b.interval)} · ${b.leverage}x · hôm nay ${dashMoney(b.day_pnl)} · đang mở ${dashMoney(b.unrealized_pnl)}</div>
      ${why ? `<div class="text-[11px] text-amber-200/90 mt-1">${dashEsc(why)}</div>` : ""}
      <div class="space-y-2.5 mt-3">
        ${dashMeter("Lỗ hôm nay", dayLoss, b.max_daily_loss_usdt, b.max_daily_loss_usdt > 0 ? `${usd(dayLoss)} / ${usd(b.max_daily_loss_usdt)}` : `${usd(dayLoss)} · ${off}`)}
        ${dashMeter("Chuỗi thua hôm nay", b.loss_streak, b.max_consecutive_losses, b.max_consecutive_losses > 0 ? `${b.loss_streak} / ${b.max_consecutive_losses} lệnh` : `${b.loss_streak} lệnh · ${off}`)}
        ${dashMeter("Ký quỹ đang dùng", b.used_margin, b.max_total_margin_usdt, `${usd(b.used_margin)} / ${usd(b.max_total_margin_usdt)}`)}
        ${dashMeter("Lỗ nếu mọi SL cùng chạm", b.open_risk, b.max_open_risk_usdt, `${usd(b.open_risk)} / ${usd(b.max_open_risk_usdt)}`)}
        ${dashMeter("Vị thế đang mở", b.positions, b.max_positions, `${b.positions} / ${b.max_positions}`)}
      </div>
    </div>`;
  }).join('<div class="border-t border-[#232a3d]"></div>');
}

function renderDashEquity(points) {
  const container = document.getElementById("dash-equity");
  const empty = document.getElementById("dash-equity-empty");
  const readout = document.getElementById("dash-equity-readout");
  container.classList.toggle("hidden", !points.length);
  empty.classList.toggle("hidden", points.length > 0);
  if (!points.length) { readout.textContent = ""; return; }
  if (container.clientWidth === 0 || typeof LightweightCharts === "undefined") return;
  if (!dashEquityChart) {
    dashEquityChart = LightweightCharts.createChart(container, {
      width: container.clientWidth, height: 280,
      layout: { background: { color: "transparent" }, textColor: "#848e9c" },
      grid: { vertLines: { visible: false }, horzLines: { color: "#1c2230" } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderColor: "#262c3d", timeVisible: true, secondsVisible: false },
    });
    dashEquitySeries = dashEquityChart.addBaselineSeries({
      baseValue: { type: "price", price: 0 }, lineWidth: 2,
      topLineColor: DASH_GAIN, topFillColor1: "rgba(52,211,153,0.16)", topFillColor2: "rgba(52,211,153,0.02)",
      bottomLineColor: DASH_LOSS, bottomFillColor1: "rgba(248,113,113,0.02)", bottomFillColor2: "rgba(248,113,113,0.16)",
      priceFormat: { type: "price", precision: 2, minMove: 0.01 },
    });
    dashEquityChart.subscribeCrosshairMove(param => {
      const point = param.time ? param.seriesData.get(dashEquitySeries) : null;
      dashEquityReadout(point ? { time: param.time, value: point.value } : null);
    });
  } else {
    dashEquityChart.applyOptions({ width: container.clientWidth });
  }
  dashEquitySeries.setData([{ time: points[0].time - 1, value: 0 }, ...points]);
  dashEquityChart.timeScale().fitContent();
  dashEquityReadout(null);
}

function dashEquityReadout(point) {
  const points = dashData ? dashData.equity : [];
  const shown = point || points[points.length - 1];
  if (!shown) return;
  // Times were sent as server wall-clock, so read them back as UTC to print the same clock.
  const when = new Date(shown.time * 1000).toLocaleString("vi-VN", { timeZone: "UTC", hour12: false, day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  document.getElementById("dash-equity-readout").textContent = `${point ? when : "Hiện tại"} · ${dashMoney(shown.value)}`;
}

function dashDayList(daily) {
  // Every calendar day of the range, so quiet days show as gaps instead of disappearing.
  const byDate = new Map(daily.map(d => [d.date, d]));
  const iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const end = new Date(); end.setHours(12, 0, 0, 0);
  let span = dashFilter.days || DASH_MAX_DAILY_COLUMNS;
  if (daily.length) {
    const first = new Date(daily[0].date + "T12:00:00");
    span = Math.min(span, Math.round((end - first) / 86400000) + 1);
  }
  span = Math.max(1, Math.min(span, DASH_MAX_DAILY_COLUMNS));
  const days = [];
  for (let i = span - 1; i >= 0; i--) {
    const d = new Date(end); d.setDate(end.getDate() - i);
    const key = iso(d);
    days.push(byDate.get(key) || { date: key, pnl: 0, fills: 0 });
  }
  return days;
}

function renderDashDaily(daily) {
  const box = document.getElementById("dash-daily");
  const label = date => `${date.slice(8, 10)}/${date.slice(5, 7)}`;
  document.getElementById("dash-daily-table").innerHTML = daily.slice().reverse().map(d =>
    `<tr class="border-t border-[#232a3d]"><td class="p-1.5 text-gray-300">${dashEsc(d.date)}</td>
     <td class="p-1.5 text-right text-gray-300">${d.fills}</td>
     <td class="p-1.5 text-right ${dashTone(d.pnl)}">${dashMoney(d.pnl)}</td></tr>`).join("")
    || '<tr><td colspan="3" class="p-2 text-center text-gray-500 font-sans">Chưa có dữ liệu.</td></tr>';
  if (!daily.length) {
    box.innerHTML = '<p class="text-xs text-gray-500 text-center py-16">Chưa có lệnh đã đóng trong giai đoạn này.</p>';
    document.getElementById("dash-daily-range").textContent = "";
    return;
  }
  const width = box.clientWidth;
  if (!width) return;
  const days = dashDayList(daily);
  document.getElementById("dash-daily-range").textContent =
    `${label(days[0].date)} – ${label(days[days.length - 1].date)}` + (dashFilter.days === 0 && days.length === DASH_MAX_DAILY_COLUMNS ? ` (${DASH_MAX_DAILY_COLUMNS} ngày gần nhất)` : "");

  const H = 220, left = 48, right = 8, top = 10, bottom = 22;
  const plotW = width - left - right, plotH = H - top - bottom;
  const hi = Math.max(0, ...days.map(d => d.pnl)), lo = Math.min(0, ...days.map(d => d.pnl));
  const range = hi - lo || 1;
  const step = dashNiceStep(range / 4);
  const yMax = Math.ceil(hi / step) * step, yMin = Math.floor(lo / step) * step;
  const y = v => top + (yMax - v) / (yMax - yMin || 1) * plotH;
  const band = plotW / days.length;
  const barW = Math.max(1, Math.min(24, band - 2));
  const digits = step >= 1 ? 0 : step >= 0.1 ? 1 : 2;

  let grid = "";
  for (let v = yMin; v <= yMax + step / 2; v += step) {
    const zero = Math.abs(v) < step / 1000;
    grid += `<line x1="${left}" x2="${width - right}" y1="${y(v)}" y2="${y(v)}" stroke="${zero ? "#3b4661" : "#1c2230"}" stroke-width="1"/>
             <text x="${left - 6}" y="${y(v) + 3}" text-anchor="end" font-size="10" fill="#848e9c">${zero ? "0" : v.toFixed(digits)}</text>`;
  }
  let bars = "", hits = "";
  days.forEach((d, i) => {
    const cx = left + band * i + band / 2, x0 = cx - barW / 2;
    if (d.pnl) {
      const up = d.pnl > 0, h = Math.max(1, Math.abs(y(d.pnl) - y(0)));
      const r = Math.min(4, barW / 2, h);
      const base = y(0), tip = up ? base - h : base + h, s = up ? 1 : -1;
      // Rounded at the data end, square on the zero baseline.
      bars += `<path d="M${x0},${base} L${x0},${tip + s * r} Q${x0},${tip} ${x0 + r},${tip} L${x0 + barW - r},${tip} Q${x0 + barW},${tip} ${x0 + barW},${tip + s * r} L${x0 + barW},${base} Z" fill="${up ? DASH_GAIN : DASH_LOSS}"/>`;
    }
    hits += `<rect x="${left + band * i}" y="${top}" width="${band}" height="${plotH}" fill="transparent" data-i="${i}"/>`;
  });
  const ticks = [...new Set([0, Math.floor((days.length - 1) / 2), days.length - 1])].map(i =>
    `<text x="${left + band * i + band / 2}" y="${H - 6}" text-anchor="${i === 0 ? "start" : i === days.length - 1 ? "end" : "middle"}" font-size="10" fill="#848e9c">${label(days[i].date)}</text>`).join("");

  box.innerHTML = `<svg width="${width}" height="${H}" role="img" aria-label="PnL ròng theo ngày">${grid}${bars}${ticks}${hits}</svg><div class="dash-tip hidden"></div>`;
  const tip = box.querySelector(".dash-tip");
  box.querySelectorAll("rect[data-i]").forEach(rect => {
    rect.addEventListener("mousemove", () => {
      const i = Number(rect.dataset.i), d = days[i];
      tip.innerHTML = `<small>${label(d.date)} · ${d.fills} lần khớp đóng</small>${dashMoney(d.pnl)}`;
      tip.style.left = `${Math.min(width - 70, Math.max(70, left + band * i + band / 2))}px`;
      tip.style.top = `${Math.min(y(Math.max(d.pnl, 0)), y(0)) - 6}px`;
      tip.classList.remove("hidden");
    });
    rect.addEventListener("mouseleave", () => tip.classList.add("hidden"));
  });
}

function dashNiceStep(raw) {
  const power = Math.pow(10, Math.floor(Math.log10(raw || 1)));
  const unit = raw / power;
  return (unit <= 1 ? 1 : unit <= 2 ? 2 : unit <= 5 ? 5 : 10) * power;
}

// Table with a bar per row growing left (loss) or right (gain) from a shared zero line.
function renderDashBreakdown(id, rows, key, heading, labelOf) {
  const box = document.getElementById(id);
  if (!rows.length) {
    box.innerHTML = '<p class="text-xs text-gray-500 py-3">Chưa có lệnh đã đóng trong giai đoạn này.</p>';
    return;
  }
  const max = Math.max(...rows.map(r => Math.abs(r.net_pnl))) || 1;
  box.innerHTML = `<table class="w-full text-left text-xs">
    <thead class="text-gray-500"><tr>
      <th class="py-1.5 pr-2 font-normal">${heading}</th><th class="py-1.5 px-2 font-normal text-right">Lệnh</th>
      <th class="py-1.5 px-2 font-normal text-right">Thắng</th><th class="py-1.5 px-2 font-normal w-2/5"></th>
      <th class="py-1.5 pl-2 font-normal text-right">PnL ròng</th></tr></thead>
    <tbody>${rows.map(r => {
      const share = (Math.abs(r.net_pnl) / max * 50).toFixed(1);
      const bar = r.net_pnl > 0
        ? `<div class="absolute h-2 top-1/2 -translate-y-1/2 rounded-r" style="left:50%;width:${share}%;background:${DASH_GAIN}"></div>`
        : r.net_pnl < 0
          ? `<div class="absolute h-2 top-1/2 -translate-y-1/2 rounded-l" style="right:50%;width:${share}%;background:${DASH_LOSS}"></div>` : "";
      const name = labelOf(r[key]);
      return `<tr class="border-t border-[#232a3d]" title="${dashEsc(name)}: ${r.trades} lệnh, thắng ${r.winrate_pct}%, ${dashMoney(r.net_pnl)}">
        <td class="py-2 pr-2 text-gray-200">${dashEsc(name)}</td>
        <td class="py-2 px-2 text-right font-mono text-gray-300">${r.trades}</td>
        <td class="py-2 px-2 text-right font-mono text-gray-300">${r.winrate_pct}%</td>
        <td class="py-2 px-2"><div class="relative h-4"><div class="absolute inset-y-0 left-1/2 w-px bg-[#3b4661]"></div>${bar}</div></td>
        <td class="py-2 pl-2 text-right font-mono ${dashTone(r.net_pnl)}">${dashMoney(r.net_pnl)}</td></tr>`;
    }).join("")}</tbody></table>`;
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-dash-market]").forEach(button => button.addEventListener("click", () => {
    dashFilter.market = button.dataset.dashMarket;
    loadDashboard();
  }));
  document.querySelectorAll("[data-dash-days]").forEach(button => button.addEventListener("click", () => {
    dashFilter.days = Number(button.dataset.dashDays);
    loadDashboard();
  }));
  const visible = () => !document.getElementById("tab-content-dashboard").classList.contains("hidden");
  setInterval(() => { if (visible() && !document.hidden) loadDashboard(); }, 10000);
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (visible()) renderDashboard(); }, 150);
  });
});
