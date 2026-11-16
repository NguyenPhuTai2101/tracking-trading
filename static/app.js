"use strict";

// ---------------------------------------------------------------- helpers
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const isNum = (v) => typeof v === "number" && Number.isFinite(v);
const fmt = (v, digits = 2) => isNum(v) ? v.toLocaleString("vi-VN", { minimumFractionDigits: digits, maximumFractionDigits: digits }) : "–";
const price = (v) => isNum(v) ? v.toLocaleString("vi-VN", { maximumSignificantDigits: 6 }) : "–";
// Values that round to zero are shown as plain zero, never "-0,00".
const settle = (v, digits = 2) => isNum(v) && Math.abs(v) < 0.5 / 10 ** digits ? 0 : v;
const signed = (v, digits = 2, unit = "") => isNum(v) ? `${settle(v, digits) > 0 ? "+" : ""}${fmt(settle(v, digits), digits)}${unit}` : "–";
const tone = (v) => !isNum(v) || settle(v) === 0 ? "" : v > 0 ? "up" : "down";
const dateTime = (ms) => ms ? new Date(ms).toLocaleString("vi-VN", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "–";
const clock = (ms) => new Date(ms).toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });

const REASONS = {
  STOP_LOSS: "Cắt lỗ", BREAKEVEN_STOP: "SL hòa vốn", TRAILING_STOP: "Trailing stop", TIME_STOP: "Hết thời gian",
  PARTIAL_TP: "Chốt một phần", TAKE_PROFIT: "Chạm mục tiêu", MANUAL_CLOSE: "Đóng tay", MANUAL_CLOSE_ALL: "Đóng tất cả",
  PROTECTION_FAILED: "Không đặt được SL", INVALID_STOP: "Khớp sai phía SL", CANCEL_ENTRY: "Hủy lệnh vào",
  END_OF_TEST: "Hết giai đoạn thử",
};
const REGIMES = { UP: "↑ Tăng", DOWN: "↓ Giảm", RANGE: "→ Đi ngang" };
const STOP_KINDS = { INITIAL: "ban đầu", BREAKEVEN: "hòa vốn", TRAIL: "trailing" };
const TAGS = { SQUEEZE: "Nén biến động", SHOCK: "Sốc biến động", OVERBOUGHT: "Quá mua", OVERSOLD: "Quá bán" };
let SETUPS = {};      // key -> {label, interval, regime_interval, case, evidence}, from /api/config
const setupLabel = (key) => esc(SETUPS[key]?.label || key);

async function api(path, body) {
  const options = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const response = await fetch(path, options);
  if (response.status === 401) {
    location.href = "/login";
    throw new Error("Cần đăng nhập.");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(data.detail)
      ? data.detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ")
      : data.detail;
    throw new Error(detail || `Lỗi ${response.status}`);
  }
  return data;
}

let toastTimer;
function toast(message, bad = false) {
  const el = $("toast");
  el.className = `alert ${bad ? "bad" : ""}`;
  el.innerHTML = `<span class="icon">${bad ? "✕" : "✓"}</span><div class="body">${esc(message)}</div>`;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, bad ? 9000 : 4000);
}

async function action(button, run) {
  button.disabled = true;
  try {
    const result = await run();
    if (result && result.message) toast(result.message);
    await refreshStatus();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function table(columns, rows, emptyText) {
  if (!rows.length) return `<div class="empty">${esc(emptyText)}</div>`;
  const head = columns.map((c) => `<th class="${c.cls || ""}">${esc(c.title)}</th>`).join("");
  const body = rows.map((row) => `<tr>${columns.map((c) => `<td class="${c.cls || ""}">${c.cell(row)}</td>`).join("")}</tr>`).join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function tile(label, value, sub = "", cls = "") {
  return `<div class="tile"><div class="label">${esc(label)}</div><div class="value ${cls}">${value}</div><div class="sub">${sub || "&nbsp;"}</div></div>`;
}

// ---------------------------------------------------------------- line chart (one series)
function lineChart(container, points) {
  if (points.length < 2) {
    container.innerHTML = '<div class="empty">Chưa đủ dữ liệu để vẽ đường vốn.</div>';
    return;
  }
  // Drawn at the container's own width so text is never stretched.
  const W = Math.max(container.clientWidth, 320), H = 260, pad = { l: 8, r: 64, t: 12, b: 24 };
  const xs = points.map((p) => p.time), ys = points.map((p) => p.equity);
  let lo = Math.min(...ys), hi = Math.max(...ys);
  if (hi === lo) { hi += 1; lo -= 1; }
  const margin = (hi - lo) * 0.08;
  lo -= margin; hi += margin;
  const x = (t) => pad.l + (t - xs[0]) / (xs[xs.length - 1] - xs[0] || 1) * (W - pad.l - pad.r);
  const y = (v) => pad.t + (hi - v) / (hi - lo) * (H - pad.t - pad.b);
  const path = points.map((p, i) => `${i ? "L" : "M"}${x(p.time).toFixed(1)},${y(p.equity).toFixed(1)}`).join("");
  const ticks = [0, 1, 2, 3].map((i) => lo + (hi - lo) * (i + 0.5) / 4);
  const grid = ticks.map((v) => `<line class="gridline" x1="${pad.l}" x2="${W - pad.r}" y1="${y(v)}" y2="${y(v)}"/>
    <text class="axis-label" x="${W - pad.r + 6}" y="${y(v) + 4}">${fmt(v, hi - lo < 20 ? 2 : 0)}</text>`).join("");
  const day = (ms) => new Date(ms).toLocaleDateString("vi-VN", { day: "2-digit", month: "2-digit" });
  const labels = [0, 0.5, 1].map((f, i) => {
    const t = xs[0] + (xs[xs.length - 1] - xs[0]) * f;
    return `<text class="axis-label" x="${x(t)}" y="${H - 6}" text-anchor="${["start", "middle", "end"][i]}">${day(t)}</text>`;
  }).join("");
  const start = `<line class="base" x1="${pad.l}" x2="${W - pad.r}" y1="${y(ys[0])}" y2="${y(ys[0])}"/>`;
  container.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Đường vốn theo thời gian">
    ${grid}${start}<path class="line" d="${path}"/>${labels}
    <line class="cross" y1="${pad.t}" y2="${H - pad.b}" visibility="hidden"/>
    <circle class="dot" r="4" visibility="hidden"/></svg><div class="tip" hidden></div>`;
  const svg = container.querySelector("svg"), cross = svg.querySelector(".cross"), dot = svg.querySelector(".dot");
  const tip = container.querySelector(".tip");
  svg.onmousemove = (event) => {
    const box = svg.getBoundingClientRect();
    const t = xs[0] + ((event.clientX - box.left) / box.width * W - pad.l) / (W - pad.l - pad.r) * (xs[xs.length - 1] - xs[0]);
    let best = 0;
    for (let i = 1; i < points.length; i++) if (Math.abs(xs[i] - t) < Math.abs(xs[best] - t)) best = i;
    const p = points[best], change = (p.equity / ys[0] - 1) * 100;
    cross.setAttribute("x1", x(p.time)); cross.setAttribute("x2", x(p.time));
    dot.setAttribute("cx", x(p.time)); dot.setAttribute("cy", y(p.equity));
    cross.setAttribute("visibility", "visible"); dot.setAttribute("visibility", "visible");
    tip.hidden = false;
    tip.innerHTML = `<span>${dateTime(p.time)}</span>${fmt(p.equity)} USDT (${signed(change, 2, "%")})`;
    const left = x(p.time) / W * box.width;
    tip.style.left = `${Math.min(Math.max(left - tip.offsetWidth / 2, 0), box.width - tip.offsetWidth)}px`;
  };
  svg.onmouseleave = () => {
    cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); tip.hidden = true;
  };
}

// ---------------------------------------------------------------- overview
let status = null;

function renderPill(s) {
  const pill = $("bot-pill");
  if (s.is_running) { pill.className = "pill on"; pill.textContent = s.entries_enabled ? "Đang chạy" : "Đang chạy · tạm chờ đối soát"; }
  else if (s.resume_pending) { pill.className = "pill warn"; pill.textContent = "Đang tự chạy lại…"; }
  else if (s.close_all_requested) { pill.className = "pill warn"; pill.textContent = "Đang đóng toàn bộ…"; }
  else { pill.className = "pill off"; pill.textContent = s.positions.length ? "Đã dừng · vẫn giữ SL" : "Đã dừng"; }
  $("btn-start").hidden = s.is_running;
  $("btn-stop").hidden = !s.is_running;
  $("settings-lock").hidden = !s.is_running;
}

function renderAlerts(s) {
  const items = [];
  const add = (cls, icon, html) => items.push(`<div class="alert ${cls}"><span class="icon">${icon}</span><div class="body">${html}</div></div>`);
  if (!s.has_credentials) add("warn", "!", "Chưa cấu hình <b>BINANCE_API_KEY</b> / <b>BINANCE_SECRET_KEY</b> trong file .env. Backtest vẫn dùng được; bot chưa thể chạy.");
  if (s.state_error) add("bad", "✕", "Không đọc hoặc lưu được trạng thái bot. Kiểm tra ổ đĩa rồi khởi động lại ứng dụng.");
  if (s.risk.halted) {
    add("bad", "✕", `<b>Bot dừng do drawdown.</b> ${esc(s.risk.block_reason)}. Hãy xem lại chiến lược trước khi tiếp tục.
      <div class="actions" style="margin-top:8px"><button class="btn small" id="btn-ack">Tôi đã xem lại, tính đỉnh vốn từ hôm nay</button></div>`);
  } else if (s.risk.block_reason) {
    add("warn", "⏸", `<b>Đang nghỉ mở lệnh.</b> ${esc(s.risk.block_reason)}. Vị thế đang mở vẫn giữ SL.`);
  }
  if (!s.is_running && s.stop_reason && !s.risk.halted) add("", "ℹ", `Bot đã dừng lúc ${dateTime(s.stopped_at)}: ${esc(s.stop_reason)}`);
  const mismatches = Object.entries(s.position_mismatches || {});
  if (mismatches.length) {
    const closable = mismatches.filter(([, d]) => !d.error && !d.bot_quantity).map(([sym]) => sym);
    const text = mismatches.map(([sym, d]) => d.error ? esc(d.error) : `${esc(sym)} (sàn ${fmt(d.exchange_quantity, 4)}, bot ${fmt(d.bot_quantity, 4)})`).join(", ");
    add("warn", "!", `<b>Vị thế trên sàn khác sổ của bot:</b> ${text}. Bot không mở lệnh mới cho tới khi khớp.` +
      (closable.length ? `<div class="actions" style="margin-top:8px"><button class="btn small danger" id="btn-untracked" data-symbols="${esc(closable.join(","))}">Đóng vị thế ngoài bot: ${esc(closable.join(", "))}</button></div>` : ""));
  }
  if (s.last_error) add("warn", "!", `Lỗi gần nhất: ${esc(s.last_error)}`);
  $("alerts").innerHTML = items.join("");
  const ack = $("btn-ack");
  if (ack) ack.onclick = () => action(ack, () => api("/api/bot/acknowledge-drawdown", {}));
  const untracked = $("btn-untracked");
  if (untracked) untracked.onclick = () => {
    const symbols = untracked.dataset.symbols.split(",");
    if (!confirm(`Đóng theo giá thị trường các vị thế ngoài bot: ${symbols.join(", ")}?`)) return;
    action(untracked, async () => {
      const result = await api("/api/bot/close-untracked", { symbols });
      const failed = result.results.filter((r) => !r.success).map((r) => `${r.symbol}: ${r.error}`);
      if (failed.length) throw new Error(failed.join("; "));
      return { message: "Đã đóng các vị thế ngoài bot." };
    });
  };
}

function renderTiles(s) {
  const r = s.risk, a = s.account, cfg = s.config.risk;
  $("t-equity").textContent = a ? fmt(a.equity) : "–";
  $("t-equity-sub").textContent = a ? `Khả dụng ${fmt(a.available)} USDT` : "Chưa đọc được tài khoản";
  const set = (id, value, sub) => {
    $(id).textContent = signed(value, 2, "%");
    $(id).className = `value ${tone(value)}`;
    $(`${id}-sub`).textContent = sub;
  };
  set("t-day", r.day_pnl_pct, `Giới hạn lỗ ${fmt(cfg.daily_loss_pct, 1)}%`);
  set("t-week", r.week_pnl_pct, `Giới hạn lỗ ${fmt(cfg.weekly_loss_pct, 1)}%`);
  $("t-dd").textContent = isNum(r.drawdown_pct) ? `${fmt(r.drawdown_pct)}%` : "–";
  $("t-dd-sub").textContent = isNum(r.peak_equity) ? `Đỉnh ${fmt(r.peak_equity)} USDT` : " ";
  $("t-risk").textContent = isNum(r.risk_per_trade_usdt) ? `${fmt(r.risk_per_trade_usdt)} USDT` : "–";
  $("t-risk-sub").textContent = r.risk_scale === 0.5 ? `${fmt(cfg.risk_pct)}% × 0,5 do drawdown` : `${fmt(cfg.risk_pct)}% vốn`;
}

function meter(label, used, limit, text, marker) {
  const ratio = isNum(used) && limit > 0 ? Math.max(0, used) / limit : 0;
  const cls = ratio >= 1 ? "bad" : ratio >= 0.7 ? "warn" : "";
  const state = ratio >= 1 ? "✕ Đã chạm giới hạn" : ratio >= 0.7 ? "! Gần giới hạn" : "✓ Trong giới hạn";
  const mark = marker ? `<div class="mark" style="left:${Math.min(marker.at / limit, 1) * 100}%" title="${esc(marker.title)}"></div>` : "";
  return `<div class="meter ${cls}"><div class="row"><span>${esc(label)}</span><span>${esc(text)}</span></div>
    <div class="track"><div class="fill" style="width:${Math.min(ratio, 1) * 100}%"></div>${mark}</div>
    <div class="state">${state}${marker ? ` · vạch: ${esc(marker.title)}` : ""}</div></div>`;
}

function renderMeters(s) {
  const r = s.risk, cfg = s.config.risk;
  if (!isNum(r.equity)) {
    $("meters").innerHTML = '<div class="empty">Chưa đọc được vốn tài khoản nên chưa tính được mức sử dụng giới hạn.</div>';
    return;
  }
  const longs = s.positions.filter((p) => p.direction === "LONG").length;
  const shorts = s.positions.length - longs;
  $("meters").innerHTML = [
    meter("Lỗ trong ngày", -r.day_pnl_pct, cfg.daily_loss_pct, `${fmt(Math.max(0, -r.day_pnl_pct))}% / ${fmt(cfg.daily_loss_pct, 1)}%`),
    meter("Lỗ trong tuần", -r.week_pnl_pct, cfg.weekly_loss_pct, `${fmt(Math.max(0, -r.week_pnl_pct))}% / ${fmt(cfg.weekly_loss_pct, 1)}%`),
    meter("Drawdown từ đỉnh", r.drawdown_pct, cfg.dd_halt_pct, `${fmt(r.drawdown_pct)}% / ${fmt(cfg.dd_halt_pct, 0)}%`,
      { at: cfg.dd_reduce_pct, title: `giảm nửa rủi ro từ ${fmt(cfg.dd_reduce_pct, 0)}%` }),
    meter("Tổng rủi ro đang mở", r.open_risk_pct, cfg.max_open_risk_pct, `${fmt(r.open_risk_pct)}% / ${fmt(cfg.max_open_risk_pct, 1)}%`),
    meter("Số vị thế", s.positions.length, cfg.max_positions, `${s.positions.length} / ${cfg.max_positions} (LONG ${longs}, SHORT ${shorts})`),
    meter("Tổng giá trị vị thế", r.gross_exposure_pct, cfg.max_gross_exposure_pct, `${fmt(r.gross_exposure_pct, 0)}% / ${fmt(cfg.max_gross_exposure_pct, 0)}% vốn`),
    meter("Chuỗi thua hôm nay", r.loss_streak, cfg.max_consecutive_losses, `${r.loss_streak} / ${cfg.max_consecutive_losses} lệnh`),
  ].join("");
}

function renderPositions(s) {
  $("positions").innerHTML = table([
    { title: "Mã", cell: (p) => `<b>${esc(p.symbol)}</b>` },
    { title: "Chiều", cell: (p) => `<span class="tag">${p.direction === "LONG" ? "↑ LONG" : "↓ SHORT"}</span>` },
    { title: "Case", cell: (p) => `${setupLabel(p.setup)} <span class="muted">${esc(p.interval)}</span>` },
    { title: "Khối lượng", cls: "r", cell: (p) => price(p.quantity) },
    { title: "Giá vào", cls: "r", cell: (p) => price(p.entry_price) },
    { title: "Giá hiện tại", cls: "r", cell: (p) => price(p.current_price) },
    { title: "SL", cls: "r", cell: (p) => `${price(p.plan.stop)} <span class="muted">(${STOP_KINDS[p.plan.stop_kind] || ""})</span>` },
    { title: "Lãi/lỗ", cls: "r", cell: (p) => `<span class="${tone(p.pnl_usdt)}">${signed(p.pnl_usdt)}</span>` },
    { title: "R", cls: "r", cell: (p) => `<span class="${tone(p.r_now)}">${signed(p.r_now, 2, "R")}</span>` },
    { title: "Rủi ro còn lại", cls: "r", cell: (p) => `${fmt(p.open_risk_usdt)} USDT` },
    { title: "SL trên sàn", cell: (p) => p.stop_on_exchange ? "✓ Đã đặt" : '<span class="muted">… đang cập nhật</span>' },
    { title: "", cell: (p) => `<button class="btn small danger" data-close="${esc(p.symbol)}">Đóng</button>` },
  ], s.positions, "Chưa có vị thế nào.");
  document.querySelectorAll("[data-close]").forEach((button) => {
    button.onclick = () => {
      if (!confirm(`Đóng vị thế ${button.dataset.close} theo giá thị trường?`)) return;
      action(button, () => api("/api/bot/close-position", { symbol: button.dataset.close }));
    };
  });
}

function caseLine(c) {
  const mark = c.traded ? "●" : "○";
  const head = `${mark} <b>${setupLabel(c.setup)}</b> <span class="muted">${esc(c.interval)} · ${REGIMES[c.regime] || esc(c.regime)}</span>`;
  const signal = c.signal ? `<span class="tag">${c.signal === "LONG" ? "↑ LONG" : "↓ SHORT"}</span> ` : "";
  return `<div class="case ${c.signal ? "fired" : ""}">${head}<br>${signal}<span class="${c.signal ? "" : "muted"}">${esc(c.note)}</span></div>`;
}

function renderScanBoard(s) {
  const tagText = (row) => [...new Set(Object.values(row.tags || {}).flat())].map((t) => `<span class="tag">${esc(TAGS[t] || t)}</span>`).join(" ");
  $("scan-board").innerHTML = table([
    { title: "Mã", cell: (r) => `<b>${esc(r.symbol)}</b><br><span class="muted num">${price(r.price)}</span><br>${tagText(r)}` },
    { title: "Case (● vào lệnh · ○ quan sát)", cls: "wrap", cell: (r) => r.cases.map(caseLine).join("") },
  ], s.scan_board, s.is_running ? "Đang chờ nến kế tiếp đóng để quét (1h và 4h)." : "Bảng này hiện kết quả quét khi bot chạy.");
}

function renderLog(s) {
  $("log").innerHTML = s.logs.length
    ? s.logs.map((l) => `<div><time>${clock(l.time)}</time><b>${esc(l.level)}</b><span>${esc(l.message)}</span></div>`).join("")
    : '<div class="empty">Chưa có sự kiện.</div>';
}

async function refreshStatus() {
  try {
    status = await api("/api/status");
  } catch (error) {
    $("bot-pill").className = "pill warn";
    $("bot-pill").textContent = "Mất kết nối máy chủ";
    return;
  }
  renderPill(status);
  renderAlerts(status);
  renderTiles(status);
  renderMeters(status);
  renderPositions(status);
  renderScanBoard(status);
  renderLog(status);
}

// ---------------------------------------------------------------- performance
let perfDays = 30;

function statTiles(m, extra = []) {
  return [
    tile("Số lệnh", m.trades, `${m.wins} thắng · ${m.losses} thua`),
    tile("Tỷ lệ thắng", isNum(m.win_rate_pct) ? `${fmt(m.win_rate_pct, 1)}%` : "–", `Chuỗi thua dài nhất ${m.max_consecutive_losses}`),
    tile("Kỳ vọng mỗi lệnh", signed(m.expectancy_r, 2, "R"), `Thắng TB ${signed(m.avg_win_r, 2, "R")} · thua TB ${signed(m.avg_loss_r, 2, "R")}`, tone(m.expectancy_r)),
    tile("Profit factor", isNum(m.profit_factor) ? fmt(m.profit_factor) : "–", "Tổng lãi / tổng lỗ"),
    tile("Lãi/lỗ ròng", signed(m.net_pnl), `Phí ước tính ${fmt(m.fees)} USDT`, tone(m.net_pnl)),
    ...extra,
  ].join("");
}

const groupColumns = (key, title, label = (v) => esc(v)) => [
  { title, cell: (r) => label(r[key]) },
  { title: "Lệnh", cls: "r", cell: (r) => r.trades },
  { title: "Thắng", cls: "r", cell: (r) => r.wins },
  { title: "R trung bình", cls: "r", cell: (r) => `<span class="${tone(r.avg_r)}">${signed(r.avg_r, 2, "R")}</span>` },
  { title: "Ròng (USDT)", cls: "r", cell: (r) => `<span class="${tone(r.net_pnl)}">${signed(r.net_pnl)}</span>` },
];
const reasonLabel = (v) => esc(REASONS[v] || v);

const tradeColumns = [
  { title: "Đóng lúc", cell: (t) => dateTime(t.exit_time) },
  { title: "Mã", cell: (t) => `<b>${esc(t.symbol)}</b>` },
  { title: "Chiều", cell: (t) => t.direction === "LONG" ? "↑ LONG" : "↓ SHORT" },
  { title: "Case", cell: (t) => setupLabel(t.setup) },
  { title: "Giá vào", cls: "r", cell: (t) => price(t.entry_price) },
  { title: "Giá ra", cls: "r", cell: (t) => price(t.exit_price) },
  { title: "Ròng (USDT)", cls: "r", cell: (t) => `<span class="${tone(t.net_pnl)}">${signed(t.net_pnl)}</span>` },
  { title: "R", cls: "r", cell: (t) => `<span class="${tone(t.r_multiple)}">${signed(t.r_multiple, 2, "R")}</span>` },
  { title: "Số nến", cls: "r", cell: (t) => t.bars },
  { title: "Lý do thoát", cell: (t) => reasonLabel(t.reason) },
];

async function refreshPerformance() {
  let data;
  try {
    data = await api(`/api/dashboard?days=${perfDays}`);
  } catch (error) {
    toast(error.message, true);
    return;
  }
  const m = data.summary;
  $("perf-note").innerHTML = m.low_sample
    ? `<div class="alert"><span class="icon">ℹ</span><div class="body">Mới có ${m.trades} lệnh trong giai đoạn này. Dưới 30 lệnh thì tỷ lệ thắng và kỳ vọng chưa nói lên điều gì.</div></div>` : "";
  $("perf-tiles").innerHTML = statTiles(m, [tile("Drawdown tối đa", `${fmt(data.max_drawdown_pct)}%`, "Theo đường vốn tài khoản")]);
  lineChart($("perf-chart"), data.equity_curve);
  $("perf-symbols").innerHTML = table(groupColumns("symbol", "Mã"), data.per_symbol, "Chưa có lệnh đã đóng.");
  $("perf-reasons").innerHTML = table(groupColumns("reason", "Lý do", reasonLabel), data.by_reason, "Chưa có lệnh đã đóng.");
  $("perf-setups").innerHTML = table(groupColumns("setup", "Case", setupLabel), data.by_setup, "Chưa có lệnh đã đóng.");
  $("perf-trades").innerHTML = table(tradeColumns, data.trades, "Chưa có lệnh đã đóng.");
}

// ---------------------------------------------------------------- settings
const FIELDS = {
  "form-capital": [
    ["risk.risk_pct", "Rủi ro mỗi lệnh (% vốn)", "Phần vốn mất khi một lệnh chạm SL, đã gồm phí. Khối lượng tự tính từ đây.", 0.05],
    ["risk.max_open_risk_pct", "Tổng rủi ro đang mở (% vốn)", "Mức lỗ nếu mọi SL đang mở cùng bị chạm.", 0.5],
    ["risk.max_positions", "Số vị thế tối đa", "", 1],
    ["risk.max_same_direction", "Vị thế cùng chiều tối đa", "Các coin thường đi cùng nhau nên các lệnh cùng chiều gần như là một lệnh lớn.", 1],
    ["risk.leverage", "Đòn bẩy trên sàn (x)", "Chỉ quyết định ký quỹ. Khối lượng do rủi ro và khoảng cách SL quyết định.", 1],
    ["risk.max_position_notional_pct", "Giá trị một vị thế tối đa (% vốn)", "", 10],
    ["risk.max_gross_exposure_pct", "Tổng giá trị vị thế tối đa (% vốn)", "", 10],
    ["risk.max_cost_r", "Chi phí tối đa (R)", "Bỏ lệnh nếu phí + trượt giá khứ hồi vượt mức này so với 1R.", 0.01],
    ["risk.fee_rate", "Phí taker mỗi chiều (tỷ lệ)", "0,0005 = 0,05%.", 0.0001],
    ["risk.slippage_rate", "Trượt giá ước tính mỗi lần khớp", "0,0002 = 0,02%.", 0.0001],
  ],
  "form-breakers": [
    ["risk.daily_loss_pct", "Giới hạn lỗ ngày (% vốn)", "Chạm mức này thì nghỉ mở lệnh đến 00:00 UTC.", 0.5],
    ["risk.weekly_loss_pct", "Giới hạn lỗ tuần (% vốn)", "Chạm mức này thì nghỉ đến thứ Hai (UTC).", 0.5],
    ["risk.max_consecutive_losses", "Chuỗi thua tối đa trong ngày", "Thua liên tiếp chừng này lệnh thì nghỉ đến hôm sau.", 1],
    ["risk.dd_reduce_pct", "Drawdown giảm nửa rủi ro (%)", "Tính từ đỉnh vốn.", 1],
    ["risk.dd_halt_pct", "Drawdown dừng bot (%)", "Bot dừng mở lệnh và chờ bạn xác nhận.", 1],
    ["risk.cooldown_candles", "Nghỉ sau khi thoát (nến 1h)", "Bỏ qua tín hiệu của mã vừa đóng lệnh.", 1],
  ],
  "form-strategy": [
    ["strategy.allow_short", "Cho phép SHORT", "", "bool"],
    ["strategy.adx_min", "ADX tối thiểu của xu hướng", "Dưới mức này khung xu hướng (4h hoặc ngày) được coi là đi ngang.", 1],
    ["strategy.breakout_lookback", "Phá vỡ: số nến 4h", "Giá phải đóng vượt đỉnh/đáy của chừng này nến.", 1],
    ["strategy.breakout_stop_atr", "Phá vỡ: SL (ATR)", "", 0.1],
    ["strategy.breakout_trail_atr", "Phá vỡ: trailing (ATR)", "Không dời hòa vốn, không chốt một phần: để lệnh thắng chạy.", 0.1],
  ],
  "form-pullback": [
    ["strategy.pullback_lookback", "Số nến tìm nhịp hồi (1h)", "", 1],
    ["strategy.rsi_pullback", "RSI nhịp hồi", "LONG cần RSI lùi về dưới mức này; SHORT đối xứng qua 50.", 1],
    ["strategy.stop_atr_min", "SL tối thiểu (ATR)", "", 0.1],
    ["strategy.stop_atr_max", "SL tối đa (ATR)", "SL theo đáy/đỉnh hồi xa hơn thì bỏ lệnh.", 0.1],
    ["strategy.breakeven_r", "Dời SL hòa vốn tại (R)", "", 0.1],
    ["strategy.partial_r", "Chốt một phần tại (R)", "", 0.1],
    ["strategy.partial_fraction", "Phần chốt (0–0,9)", "0 = không chốt một phần.", 0.1],
    ["strategy.trail_atr", "Trailing (ATR)", "Khoảng cách SL bám theo giá tốt nhất.", 0.1],
    ["strategy.time_stop_bars", "Time stop (nến 1h)", "Đóng lệnh nếu sau chừng này nến chưa đạt 0,5R.", 1],
  ],
};
let defaults = null;

function buildSetupList() {
  $("form-setups").innerHTML = Object.values(SETUPS).map((s) => `
    <div class="setup">
      <div class="field check"><input type="checkbox" id="setup-${s.key}" data-setup="${s.key}">
        <label for="setup-${s.key}">${esc(s.label)} <span class="muted">· tín hiệu ${esc(s.interval)}, xu hướng ${esc(s.regime_interval)}</span></label></div>
      <div class="hint">${esc(s.case)}</div>
      <div class="evidence"><b>Nghiên cứu:</b> ${esc(s.evidence)}</div>
    </div>`).join("");
}

function buildForms() {
  for (const [formId, fields] of Object.entries(FIELDS)) {
    $(formId).innerHTML = fields.map(([path, label, hint, step]) => {
      const id = `f-${path.replace(".", "-")}`;
      if (step === "bool") {
        return `<div class="field check"><input type="checkbox" id="${id}" data-path="${path}"><label for="${id}">${esc(label)}</label></div>`;
      }
      return `<div class="field"><label for="${id}">${esc(label)}</label><div class="hint">${esc(hint)}</div>
        <input type="number" id="${id}" data-path="${path}" step="${step}"></div>`;
    }).join("");
  }
}

function fillForms(config) {
  document.querySelectorAll("[data-path]").forEach((input) => {
    const [group, key] = input.dataset.path.split(".");
    if (input.type === "checkbox") input.checked = Boolean(config[group][key]);
    else input.value = config[group][key];
  });
  document.querySelectorAll("[data-setup]").forEach((box) => { box.checked = config.strategy.setups.includes(box.dataset.setup); });
  $("f-watchlist").value = config.watchlist.join(", ");
  $("bt-symbols").value = config.watchlist.join(", ");
}

const splitSymbols = (text) => text.toUpperCase().split(/[\s,;]+/).filter(Boolean);

function readForms() {
  const config = { watchlist: splitSymbols($("f-watchlist").value), scan_interval_sec: status?.config.scan_interval_sec ?? 20, risk: {}, strategy: {} };
  document.querySelectorAll("[data-path]").forEach((input) => {
    const [group, key] = input.dataset.path.split(".");
    config[group][key] = input.type === "checkbox" ? input.checked : Number(input.value);
  });
  config.strategy.setups = [...document.querySelectorAll("[data-setup]")].filter((box) => box.checked).map((box) => box.dataset.setup);
  return config;
}

async function loadConfig() {
  const data = await api("/api/config");
  defaults = data.defaults;
  SETUPS = Object.fromEntries(data.setups.map((s) => [s.key, s]));
  buildSetupList();
  fillForms(data.config);
}

// ---------------------------------------------------------------- backtest
function renderBacktest(r) {
  const s = r.summary;
  $("bt-result").hidden = false;
  $("bt-warnings").innerHTML = r.warnings.map((w) => `<div class="alert warn"><span class="icon">!</span><div class="body">${esc(w)}</div></div>`).join("");
  $("bt-tiles").innerHTML = statTiles(s, [
    tile("Lợi nhuận", signed(s.return_pct, 2, "%"), `Mua & giữ ${signed(r.buy_hold.average_pct, 2, "%")}`, tone(s.return_pct)),
    tile("Drawdown tối đa", `${fmt(s.max_drawdown_pct)}%`, `Funding ${signed(s.funding)} USDT`),
  ]);
  lineChart($("bt-chart"), r.equity_curve);
  const rows = [["70% đầu (để chỉnh tham số)", r.in_sample], ["30% cuối (để kiểm tra)", r.out_of_sample], ["Toàn giai đoạn", s]];
  $("bt-split").innerHTML = table([
    { title: "Giai đoạn", cell: (x) => esc(x[0]) },
    { title: "Lệnh", cls: "r", cell: (x) => x[1].trades },
    { title: "Thắng", cls: "r", cell: (x) => isNum(x[1].win_rate_pct) ? `${fmt(x[1].win_rate_pct, 1)}%` : "–" },
    { title: "Kỳ vọng", cls: "r", cell: (x) => `<span class="${tone(x[1].expectancy_r)}">${signed(x[1].expectancy_r, 2, "R")}</span>` },
    { title: "PF", cls: "r", cell: (x) => isNum(x[1].profit_factor) ? fmt(x[1].profit_factor) : "–" },
    { title: "Ròng (USDT)", cls: "r", cell: (x) => `<span class="${tone(x[1].net_pnl)}">${signed(x[1].net_pnl)}</span>` },
  ], rows, "");
  $("bt-skipped").innerHTML = table([
    { title: "Lý do", cls: "wrap", cell: (x) => esc(x.label) },
    { title: "Số tín hiệu", cls: "r", cell: (x) => x.count },
  ], r.skipped, "Không có tín hiệu nào bị chặn.");
  $("bt-symbols-table").innerHTML = table(groupColumns("symbol", "Mã"), r.per_symbol, "Không có lệnh.");
  $("bt-reasons").innerHTML = table(groupColumns("reason", "Lý do", reasonLabel), r.by_reason, "Không có lệnh.");
  $("bt-setups").innerHTML = table(groupColumns("setup", "Case", setupLabel), r.by_setup, "Không có lệnh.");
  $("bt-robust-card").hidden = !r.robustness;
  if (r.robustness) {
    $("bt-robust-table").innerHTML = table([
      { title: "Biến thể", cell: (x) => esc(x.label) },
      { title: "Lệnh", cls: "r", cell: (x) => x.trades },
      { title: "Lợi nhuận", cls: "r", cell: (x) => `<span class="${tone(x.return_pct)}">${signed(x.return_pct, 2, "%")}</span>` },
      { title: "Drawdown", cls: "r", cell: (x) => `${fmt(x.max_drawdown_pct)}%` },
      { title: "Kỳ vọng", cls: "r", cell: (x) => `<span class="${tone(x.expectancy_r)}">${signed(x.expectancy_r, 2, "R")}</span>` },
      { title: "PF", cls: "r", cell: (x) => isNum(x.profit_factor) ? fmt(x.profit_factor) : "–" },
    ], r.robustness, "");
  }
  $("bt-trades").innerHTML = table(tradeColumns, [...r.trades].reverse(), "Không có lệnh.");
}

async function runBacktest() {
  const button = $("btn-backtest"), message = $("bt-msg");
  const draft = readForms();
  button.disabled = true;
  message.className = "msg muted";
  message.textContent = "Đang tải dữ liệu và chạy mô phỏng…";
  try {
    const result = await api("/api/backtest", {
      symbols: splitSymbols($("bt-symbols").value), days: Number($("bt-days").value),
      initial_balance: Number($("bt-balance").value), risk: draft.risk, strategy: draft.strategy,
      robustness: $("bt-robust").checked,
    });
    renderBacktest(result);
    message.textContent = `Xong: ${result.total_trades} lệnh từ ${dateTime(result.period.start)} đến ${dateTime(result.period.end)}.`;
  } catch (error) {
    message.className = "msg down";
    message.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

// ---------------------------------------------------------------- wiring
function showTab(name) {
  document.querySelectorAll("nav [data-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  for (const tab of ["overview", "performance", "settings", "backtest"]) $(`tab-${tab}`).hidden = tab !== name;
  if (name === "performance") refreshPerformance();
  try { localStorage.setItem("tab", name); } catch (_) { /* private mode */ }
}

document.querySelectorAll("nav [data-tab]").forEach((button) => { button.onclick = () => showTab(button.dataset.tab); });
$("btn-start").onclick = () => action($("btn-start"), () => api("/api/bot/start", {}));
$("btn-stop").onclick = () => action($("btn-stop"), () => api("/api/bot/stop", {}));
$("btn-close-all").onclick = () => {
  if (!confirm("Dừng bot và đóng TẤT CẢ vị thế của bot theo giá thị trường?")) return;
  action($("btn-close-all"), () => api("/api/bot/close-all", {}));
};
$("btn-logout").onclick = async () => { await api("/api/auth/logout", {}); location.href = "/login"; };
$("perf-range").querySelectorAll("button").forEach((button) => {
  button.onclick = () => {
    perfDays = Number(button.dataset.days);
    $("perf-range").querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
    refreshPerformance();
  };
});
$("btn-save").onclick = async () => {
  const message = $("settings-msg");
  try {
    const result = await api("/api/config", readForms());
    fillForms(result.config);
    message.className = "msg up";
    message.textContent = "✓ Đã lưu.";
    refreshStatus();
  } catch (error) {
    message.className = "msg down";
    message.textContent = `✕ ${error.message}`;
  }
};
$("btn-defaults").onclick = () => {
  if (defaults) fillForms(defaults);
  $("settings-msg").className = "msg muted";
  $("settings-msg").textContent = "Đã điền giá trị mặc định. Bấm Lưu để áp dụng.";
};
$("btn-backtest").onclick = runBacktest;

(async function init() {
  buildForms();
  let saved = "overview";
  try { saved = localStorage.getItem("tab") || saved; } catch (_) { /* private mode */ }
  await loadConfig().catch((error) => toast(error.message, true));
  await refreshStatus();
  showTab(["overview", "performance", "settings", "backtest"].includes(saved) ? saved : "overview");
  api("/api/auth/status").then((auth) => { $("btn-logout").hidden = !auth.enabled; }).catch(() => {});
  setInterval(() => { if (!document.hidden) refreshStatus(); }, 3000);
  setInterval(() => { if (!document.hidden && !$("tab-performance").hidden) refreshPerformance(); }, 30000);
})();
