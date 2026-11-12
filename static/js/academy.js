// academy.js - Interactive Trading Academy & Calculators

function switchLesson(lessonNum) {
  // Update buttons
  document.querySelectorAll('.lesson-nav-btn').forEach((btn, idx) => {
    const num = idx + 1;
    const badge = btn.querySelector('span');
    if (num === lessonNum) {
      btn.classList.add('border-l-4', 'border-l-yellow-500', 'bg-[#1f2433]');
      if (badge) {
        badge.className = 'w-7 h-7 rounded-full bg-yellow-500/20 text-yellow-400 font-bold text-xs flex items-center justify-center';
      }
    } else {
      btn.classList.remove('border-l-4', 'border-l-yellow-500', 'bg-[#1f2433]');
      if (badge) {
        badge.className = 'w-7 h-7 rounded-full bg-gray-700 text-gray-300 font-bold text-xs flex items-center justify-center';
      }
    }
  });

  // Update lesson content views
  document.querySelectorAll('.lesson-body').forEach((el, idx) => {
    if (idx + 1 === lessonNum) {
      el.classList.remove('hidden');
    } else {
      el.classList.add('hidden');
    }
  });
}

function calculatePositionSize() {
  const balance = parseFloat(document.getElementById('calc-balance').value) || 5000;
  const riskPct = parseFloat(document.getElementById('calc-risk-pct').value) || 2;
  const entry = parseFloat(document.getElementById('calc-entry').value) || 73500;
  const sl = parseFloat(document.getElementById('calc-sl').value) || 72000;
  const tp = parseFloat(document.getElementById('calc-tp').value) || 76500;

  if (entry <= 0 || sl <= 0 || tp <= 0) {
    alert("Vui lòng nhập các mức giá hợp lệ lớn hơn 0");
    return;
  }

  const riskAmount = (balance * riskPct) / 100.0;
  const priceRiskPerCoin = Math.abs(entry - sl);
  
  if (priceRiskPerCoin === 0) {
    alert("Giá Entry và Stop Loss không được trùng nhau!");
    return;
  }

  const qty = riskAmount / priceRiskPerCoin;
  const totalCost = qty * entry;
  const rewardPerCoin = Math.abs(tp - entry);
  const rrRatio = (rewardPerCoin / priceRiskPerCoin).toFixed(2);

  // Update UI
  document.getElementById('res-risk-usdt').textContent = `$${riskAmount.toFixed(2)}`;
  document.getElementById('res-qty').textContent = `${qty.toFixed(4)} Coin`;
  document.getElementById('res-total-cost').textContent = `$${totalCost.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

  const rrBadge = document.getElementById('res-rr-ratio');
  if (parseFloat(rrRatio) >= 2.0) {
    rrBadge.className = 'text-emerald-400 font-bold text-sm';
    rrBadge.textContent = `1 : ${rrRatio} (Đạt Chuẩn Tốt)`;
  } else if (parseFloat(rrRatio) >= 1.5) {
    rrBadge.className = 'text-yellow-400 font-bold text-sm';
    rrBadge.textContent = `1 : ${rrRatio} (Tạm Chấp Nhận)`;
  } else {
    rrBadge.className = 'text-red-400 font-bold text-sm';
    rrBadge.textContent = `1 : ${rrRatio} (Rủi Ro Quá Cao)`;
  }
}
