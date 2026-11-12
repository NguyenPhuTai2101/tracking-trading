// chart.js - TradingView Lightweight Charts Integration

let tvChart = null;
let candleSeries = null;
let volumeSeries = null;

function initChart() {
  const container = document.getElementById('tv-chart');
  if (!container) return;

  // Clear previous instance if any
  container.innerHTML = '<div id="chart-loading" class="absolute inset-0 flex items-center justify-center bg-[#131722]/80 z-10 text-gray-400"><i class="fa-solid fa-spinner fa-spin text-2xl text-yellow-400 mr-3"></i> Đang tải dữ liệu nến Binance Demo...</div>';

  try {
    tvChart = LightweightCharts.createChart(container, {
      width: container.clientWidth,
      height: 450,
      layout: {
        background: { color: '#131722' },
        textColor: '#848e9c',
      },
      grid: {
        vertLines: { color: '#1c2230' },
        horzLines: { color: '#1c2230' },
      },
      crosshair: {
        mode: LightweightCharts.CrosshairMode.Normal,
      },
      rightPriceScale: {
        borderColor: '#262c3d',
      },
      timeScale: {
        borderColor: '#262c3d',
        timeVisible: true,
        secondsVisible: false,
      },
    });

    // Add Candlestick Series
    candleSeries = tvChart.addCandlestickSeries({
      upColor: '#0ecb81',
      downColor: '#f6465d',
      borderVisible: false,
      wickUpColor: '#0ecb81',
      wickDownColor: '#f6465d',
    });

    // Add Volume Histogram Series
    volumeSeries = tvChart.addHistogramSeries({
      color: '#26a69a',
      priceFormat: {
        type: 'volume',
      },
      priceScaleId: '', // overlay
      scaleMargins: {
        top: 0.8,
        bottom: 0,
      },
    });

    // Window Resize Handler
    window.addEventListener('resize', () => {
      if (tvChart && container) {
        tvChart.applyOptions({ width: container.clientWidth });
      }
    });

  } catch (err) {
    console.error("Failed to initialize TradingView chart:", err);
  }
}

function updateChartData(candles) {
  if (!candleSeries || !candles || candles.length === 0) return;

  const candleData = candles.map(c => ({
    time: c.time,
    open: c.open,
    high: c.high,
    low: c.low,
    close: c.close
  }));

  const volData = candles.map(c => ({
    time: c.time,
    value: c.volume,
    color: c.close >= c.open ? 'rgba(14, 203, 129, 0.4)' : 'rgba(246, 70, 93, 0.4)'
  }));

  candleSeries.setData(candleData);
  volumeSeries.setData(volData);

  // Hide loading spinner
  const loading = document.getElementById('chart-loading');
  if (loading) loading.style.display = 'none';
}

function updateLastCandle(candle) {
  if (!candleSeries || !candle) return;
  candleSeries.update({
    time: candle.time,
    open: candle.open,
    high: candle.high,
    low: candle.low,
    close: candle.close
  });
  if (volumeSeries) {
    volumeSeries.update({
      time: candle.time,
      value: candle.volume,
      color: candle.close >= candle.open ? 'rgba(14, 203, 129, 0.4)' : 'rgba(246, 70, 93, 0.4)'
    });
  }
}
