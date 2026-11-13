# Binance Demo Trading Hub & Học Viện Giao Dịch (Spot & Futures)

Hệ thống Web App toàn diện kết hợp giữa **Học viện hướng dẫn Trading thực chiến** và **Trading Bot tự động 2 chiều (Long/Short)** kết nối trực tiếp với sàn **Binance Demo (Mock Trading)**.

---

## 🌟 Tính Năng Mới & Toàn Diện

### 1. 🪙 Hỗ Trợ 500+ Đồng Coin (Tìm Kiếm & Đổi Coin Tức Thì)
* **Dải nút chọn nhanh HOT Coins:** Chuyển đổi 1-click giữa các coin hot nhất: **BTC, ETH, SOL, BNB, SUI, NEAR, PEPE, DOGE, XRP, AVAX, LINK, ADA, APT...**
* **Bộ tìm kiếm 500+ Cặp tiền Binance Demo:** Cho phép tìm theo tên hoặc lọc theo danh mục: **Layer 1, DeFi, Meme Coins, AI & Web3**.
* **Đồng bộ toàn diện khi đổi coin:**
  * Biểu đồ nến TradingView cập nhật ngay cấu trúc nến của coin đó.
  * Máy tính tín hiệu AI (RSI, EMA, MACD, Bollinger Bands) tính toán lại riêng cho coin đó.
  * Danh sách quét của bot được lưu riêng trong bảng cấu hình Bot Auto.
  * Bảng đặt lệnh tay chuyển sang coin đó.

### 2. ⚡ Giao Dịch Phái Sinh Futures (Đòn Bẩy 2 Chiều)
* **Kết nối trực tiếp Binance Futures Demo:** `https://demo-fapi.binance.com`
* **Số dư ví Futures độc lập:** Tích hợp ví ký quỹ **5,000 USDT & 5,000 USDC** của Binance Futures.
* **Tùy chỉnh Đòn bẩy (Leverage):** Hỗ trợ chuyển đổi nhanh **2x, 5x, 10x, 20x, 50x** chỉ bằng 1 cú click.
* **Bot Tự Động 2 Chiều (2-Way Trading):**
  * Tín hiệu thị trường TĂNG (RSI quá bán hoặc EMA Golden Cross) $\rightarrow$ Bot tự động mở vị thế **LONG**.
  * Tín hiệu thị trường GIẢM (RSI quá mua hoặc EMA Death Cross) $\rightarrow$ Bot tự động mở vị thế **SHORT** (Kiếm tiền ngay cả khi thị trường downtrend sập giá).
* **Quản trị rủi ro Futures:** TP/SL theo % ROE, giới hạn vốn và lỗ dự kiến; SL trên sàn dùng STOP_MARKET reduceOnly. Các cơ chế này không bảo đảm tránh thanh lý hoặc trượt giá.
* **Đặt lệnh tay 2 chiều:** Hỗ trợ nút **MỞ LONG** và **MỞ SHORT** trực tiếp lên sàn Binance Futures Demo.

### 3. 🟢 Giao Dịch Giao Ngay Spot (Truyền Thống)
* **Kết nối Binance Spot Demo:** `https://demo-api.binance.com`
* Mua thấp bán cao, không đòn bẩy, an toàn tích sản.

### 4. 📚 Học Viện Trading Thực Chiến (Academy)
* **Bài 1:** Khái niệm cốt lõi (Spot vs Futures, Market vs Limit, TP / SL).
* **Bài 2:** Nghệ thuật đọc nến Nhật (Hammer, Bullish Engulfing, Shooting Star, Hỗ trợ & Kháng cự).
* **Bài 3:** Làm chủ 4 chỉ báo kỹ thuật (RSI, EMA, MACD, Bollinger Bands).
* **Bài 4:** Quản trị vốn sống còn (Quy tắc 1-2%, Tỷ lệ R:R >= 1:2) kèm **Máy tính Position Size Calculator**.
* **Bài 5:** Tâm lý giao dịch và cách ứng dụng Bot để giữ kỷ luật 100%.
* **Bài 6:** Bí kíp đánh Futures & Đòn bẩy (Cách tính ROE, Quy tắc tránh cháy tài khoản).
* **Bài 7:** Luật sàn Binance cốt lõi: Min Notional, Lot Size/Step Size, Phí Funding Rate, Mark Price & Bảng mã lỗi.

---

## 🚀 Hướng Dẫn Sử Dụng

Web App đang hoạt động tại địa chỉ:
👉 **`http://127.0.0.1:8000`**

### Khởi động lại ứng dụng:
```bash
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

### Cấu hình Binance Demo

Ứng dụng đọc `BINANCE_API_KEY` và `BINANCE_SECRET_KEY` từ `.env` cạnh mã nguồn.
Biến môi trường của tiến trình có ưu tiên cao hơn `.env`. Khởi động lại sau khi đổi khóa.
Phần dữ liệu thị trường vẫn dùng được khi chưa cấu hình khóa. Chạy một worker
và giữ ứng dụng trên localhost nếu chưa có lớp xác thực phía trước.

Hai bot lưu mã lệnh vào `bot_state_spot.json` và `bot_state_futures.json` trước khi gửi. Nếu mất phản hồi, bot
tra cứu lệnh theo mã và tạm ngừng mở vị thế mới. Khi khởi động lại, bot
đọc các lệnh chờ từ file và tiếp tục đối soát. Mã lệnh có trong
`pending_orders` của `/api/bot/status?market=SPOT` hoặc `?market=FUTURES`. Nếu Binance vẫn không xác nhận được
lệnh, hãy kiểm tra mã trên Binance Demo trước khi xử lý thủ công.
Bot Futures hỗ trợ chế độ One-way và kiểm tra các vị thế trên sàn trước khi mở mới.
Bot không tự đổi chế độ tài khoản.

### Điều khiển bot sau nâng cấp

- Bot SPOT và bot FUTURES có cấu hình, vị thế, lệnh chờ, PnL, nhật ký và worker riêng.
  Có thể chạy một hoặc cả hai. Dừng/đóng toàn bộ chỉ tác động bot đang chọn.
  Spot cố định 1x, Futures có đòn bẩy riêng. Trần vốn và lỗ tại SL áp dụng riêng từng bot.
- Chọn **Thiết lập** trên thẻ bot để chỉnh vốn mỗi lệnh, chiến lược, khung nến,
  watchlist, TP/SL, số vị thế và giới hạn rủi ro. Bản nháp chưa lưu được giữ riêng
  khi chuyển bot trong trang; cần Lưu trước khi chạy. Tải lại trang sẽ bỏ bản nháp.
- Thị trường và khung nến ở biểu đồ phục vụ việc xem dữ liệu và đặt lệnh tay.
  Việc đổi biểu đồ không đổi bot đang xem hoặc cấu hình hai bot.
- Dừng mở lệnh mới vẫn giữ giám sát TP/SL và đối soát lệnh chờ.
  Khởi động ứng dụng sẽ khôi phục giám sát vị thế đã lưu, nhưng không tự bật mở mới.
- Đóng tất cả dừng mở mới, hủy lệnh bot còn chờ và tiếp tục xử lý vị thế còn lại.
  Nếu Binance chưa xác nhận, trạng thái hiển thị đang xử lý.
- Mỗi bot có thị trường cố định. Chỉ đổi đòn bẩy Futures khi hết vị thế và lệnh chờ. Các thay đổi cấu hình
  yêu cầu tạm dừng mở mới. Lệnh tay không được can thiệp mã bot đang quản lý.
- Bot bỏ qua lệnh nếu không đủ mức tối thiểu của sàn, số dư không đủ,
  hoặc vượt trần tổng vốn/tổng lỗ dự kiến tại SL. Không tự nâng vốn.
  Khi nâng cấp dữ liệu cũ, trần vốn ban đầu = vốn mỗi lệnh × số vị thế tối đa,
  trần lỗ tại SL = trần vốn × SL %. Người dùng có thể điều chỉnh trong bảng bot.
- Giới hạn SL là ngưỡng dự kiến theo giá; khớp MARKET có thể trượt giá.
  Lệch giá quá 1% giữa tín hiệu và kiểm tra trước gửi sẽ khiến bot bỏ qua lệnh.
- PnL và win rate hiện tính trước commission và funding. Gợi ý ATR trên biểu đồ
  khác với TP/SL theo % đã cấu hình cho bot.
- Mỗi phần khớp Futures được gắn một STOP_MARKET reduceOnly theo Mark Price trên sàn.
  Lệnh đã được Binance xác nhận tiếp tục tồn tại khi ứng dụng tắt. Có một khoảng
  thời gian từ khi khớp entry đến khi sàn xác nhận SL; lỗi xác định khi đặt SL
  khiến bot dừng mở mới và yêu cầu đóng vị thế. Phản hồi không rõ được đối soát,
  không gửi trùng. Khi đóng bằng ứng dụng, bot xác nhận hủy/khớp SL trước khi đóng.
  Trạng thái và mã SL nằm trong `protective_orders` của `/api/bot/status?market=FUTURES`.
- TP Futures và TP/SL Spot vẫn cần ứng dụng chạy và kết nối. SL trên sàn chỉ dành
  cho vị thế bot quản lý, không tự bảo vệ các lệnh tay.
- Vị thế bị thay đổi bên ngoài sẽ chặn mở mới. Trường hợp giảm/đóng thủ công
  cần đối soát lịch sử; bot không tự suy đoán giá đóng hoặc PnL.
- Chạy một worker. Ứng dụng khóa file trạng thái để tránh hai tiến trình cùng
  giao dịch. Đường dẫn .env và các file trạng thái được xác định theo thư mục mã nguồn.

Lần đầu nâng cấp từ một bot, ứng dụng chuyển cấu hình hiện có sang bot đúng thị trường,
giữ nguyên vị thế, mã lệnh chờ, SL và lịch sử đã lưu. Bot còn lại nhận cấu hình mặc định.
`bot_state.json` cũ được giữ làm bản sao; những lần chạy sau đọc hai file riêng,
không ghi đè lại cấu hình đã tách. Cả hai bot khởi động ở chế độ giám sát, chờ người dùng bật mở lệnh.
Nếu lịch sử cũ đã trộn hai thị trường, tổng PnL cũ được giữ ở bot sở hữu cấu hình cũ
và gắn nhãn `legacy_combined`; không suy đoán lại số thắng/thua từ các phần khớp còn lưu.

API trạng thái tổng: `GET /api/bots/status`. Các API `/api/bot/status`, `/start`,
`/stop`, `/config`, `/close-position`, `/close-all` bắt buộc query `market=SPOT`
hoặc `market=FUTURES` để tránh thao tác nhầm bot. Ví dụ:
`POST /api/bot/stop?market=SPOT` không dừng bot Futures.

### Đăng nhập

Đặt `APP_USERNAME` và `APP_PASSWORD` để bật trang đăng nhập (`/login`). Mọi trang và
API (trừ `/healthz`) đều cần phiên đăng nhập; cookie `HttpOnly`, `SameSite=Strict`,
giữ 7 ngày. Sai 5 lần liên tiếp bị khóa 5 phút theo IP. Đổi mật khẩu sẽ đăng xuất mọi
trình duyệt (hoặc đặt `SESSION_SECRET` riêng để tách khóa phiên khỏi mật khẩu).

Trên Railway (hoặc khi đặt `REQUIRE_AUTH=1`) mà thiếu hai biến trên, ứng dụng trả 503
cho mọi yêu cầu thay vì mở công khai. Chạy local không đặt biến thì không cần đăng nhập.

### Bot tự chạy lại và lý do dừng

Bot nhớ trạng thái "đang chạy" trong file trạng thái. Sau khi máy chủ khởi động lại
(redeploy, crash) bot tự chạy lại với đúng các bước kiểm tra như bấm CHẠY (đối soát
lệnh, SL trên sàn, vị thế), thử lại mỗi 30 giây tối đa 10 lần. Bấm DỪNG hoặc "Đóng tất
cả" thì không tự chạy lại. Mỗi lần bot dừng, lý do và thời điểm được lưu và hiện dưới
trạng thái bot; 100 dòng log gần nhất cũng được giữ qua lần khởi động lại.

Đặt SL trên sàn bị từ chối một lần (thường do giá đã vượt SL khi SL quá sát): bot đóng
vị thế đó và tạm chặn mở mới cho tới khi đóng xong. Bị từ chối 2 lần trong 1 giờ, hoặc
lệnh SL trên sàn sai thông số, thì bot dừng hẳn và ghi lý do.

### Vào & chốt lệnh

| Tùy chọn | Ý nghĩa |
|---|---|
| Kiểu TP/SL `ROE` | TP/SL theo % ký quỹ (cũ): đòn bẩy càng cao khoảng cách giá càng hẹp |
| Kiểu TP/SL `ATR` | SL = hệ số × ATR, TP = hệ số × ATR của nến tín hiệu, không phụ thuộc đòn bẩy. Bỏ lệnh nếu SL gần giá thanh lý |
| Lọc EMA200 | Chỉ LONG khi giá trên EMA200, chỉ SHORT khi dưới (cùng khung nến, bot tải 261 nến) |
| Nghỉ sau khi thoát | Bỏ qua tín hiệu của N nến tính từ nến thoát lệnh trên cùng mã |
| Dời SL hòa vốn | Khi lãi N×R, bot tự đóng nếu giá quay về giá vào + phí; SL trên sàn giữ nguyên làm lưới an toàn |

### Quản trị rủi ro (0 = tắt, mặc định tắt)

| Tùy chọn | Ý nghĩa |
|---|---|
| Lỗ tối đa mỗi lệnh tại SL | Bot tính ký quỹ theo khoảng cách SL (đã cộng phí khứ hồi ước tính) để mỗi lệnh chạm SL lỗ gần bằng số này. "Vốn mỗi lệnh" trở thành trần ký quỹ. Lệnh dưới mức tối thiểu của sàn bị bỏ, không tự nâng vốn |
| Nghỉ khi lỗ trong ngày đạt | Khi kết quả đã chốt trong ngày (trừ phí ước tính) chạm mức này, bot ngừng mở lệnh mới đến ngày hôm sau theo giờ máy chủ. Vị thế đang mở vẫn được giám sát |
| Nghỉ sau số lệnh thua liên tiếp | Tương tự, theo chuỗi thua trong ngày; một lệnh thắng đặt lại chuỗi |

Các giới hạn này chỉ chặn mở lệnh mới; khớp MARKET vẫn có thể trượt giá nên lỗ thực có thể lớn hơn dự kiến.
Cơ sở và nguồn tham khảo nằm ở Bài 8 trong tab Học viện.

Nút **Cấu hình khuyến nghị** (bot Futures): RSI_SCALPING, khung 1h, 3x, ATR 1.5/3, lọc EMA200,
nghỉ 2 nến. Đây là tổ hợp có drawdown thấp nhất trong các backtest đã chạy, chưa phải lợi thế
đã được chứng minh; kiểm tra lại ở tab Backtest trước khi dùng.

### Dashboard

Tab **Dashboard** tổng hợp các lệnh bot đã đóng (API `GET /api/dashboard?market=ALL|SPOT|FUTURES&days=30`,
`days=0` là toàn bộ lịch sử đã lưu). Lọc theo bot và 7/30/90 ngày; tự làm mới mỗi 10 giây khi đang mở.

- Số liệu tính **sau phí taker ước tính** (0.1% giá trị lệnh khứ hồi với Futures, 0.2% với Spot), chưa gồm
  funding, nên có thể lệch với PnL trước phí ở tab Lịch sử và với số thực trên Binance.
- Các lần khớp đóng từng phần của cùng một vị thế được gộp thành một lệnh khi tính tỷ lệ thắng, profit factor
  và kỳ vọng. Vị thế mới đóng một phần chưa được tính thắng/thua.
- "Hôm nay & giới hạn rủi ro" cho biết bot đã dùng bao nhiêu so với giới hạn lỗ ngày, chuỗi thua, trần ký quỹ
  và trần lỗ tại SL.
- Bot lưu 500 lần khớp đóng gần nhất (trước đây chỉ 50 lần sau khi khởi động lại); lịch sử cũ hơn không có.
- Dưới 30 lệnh trong giai đoạn đang xem, dashboard nhắc rằng các tỷ lệ chưa có ý nghĩa thống kê.

### Backtest có tính phí

Tab **Backtest** trên web, hoặc dòng lệnh:

```powershell
python backtest.py --symbols BTCUSDT ETHUSDT SOLUSDT --days 90 --strategy HYBRID_SMART RSI_SCALPING EMA_CROSSOVER
python backtest.py --interval 1h --days 180 --leverage 3 --tp 12 --sl 6 --cooldown 2
```

Dùng đúng logic tín hiệu của bot (`strategy.py`), TP/SL theo % ROE, trần vị thế/ký quỹ/rủi ro.
Vào lệnh ở giá mở nến kế tiếp (bỏ qua nếu lệch >1%), phí taker mặc định 0.05% (Futures)
/ 0.1% (Spot) mỗi chiều cộng trượt giá. Nến chạm cả TP và SL tính là SL. Chưa mô phỏng
funding, thanh lý và làm tròn khối lượng. Dữ liệu nến lấy từ API công khai của Binance
(`--source demo` để dùng sàn Demo), không gửi khóa API.

Kết quả kèm bảng **Kiểm tra độ ổn định**: cùng cấu hình trên 70% đầu và 30% cuối của giai đoạn
(chia theo thời điểm vào lệnh) và mức "Mua & giữ" cùng giai đoạn. Chỉ chỉnh tham số dựa trên phần đầu;
cấu hình chỉ lãi ở một phần, hoặc có dưới 30 lệnh, sẽ được cảnh báo. Các tùy chọn rủi ro dùng được ở
dòng lệnh: `--risk 1 --daily-loss 3 --max-losses 3`.

### Kiểm tra hồi quy

```powershell
python -m pip install -r requirements-dev.txt
python -m unittest -v test_trading_upgrade test_bot_isolation test_backtest_auth test_entry_exit test_risk_controls test_dashboard
```

Bộ test dùng API giả lập và chặn kết nối mạng ngoài ý muốn. Kiểm tra API Demo
trực tiếp có thể dùng endpoint /order/test để xác nhận yêu cầu, không chứng minh
được hành vi khớp lệnh hoặc TP/SL khi thị trường biến động.

Kiểm tra kết nối bằng khóa `.env` (không in khóa hoặc chữ ký):

```powershell
python demo_smoke_test.py
```

Khi ứng dụng đã tắt, bot và tài khoản Futures không có vị thế/lệnh bot chờ,
có thể kiểm tra vòng đời SL Demo bằng `python demo_smoke_test.py --probe-stop`.
Script tạo một STOP_MARKET reduceOnly cách xa giá hiện tại, truy vấn và hủy
đúng mã của probe; không mở vị thế. Journal `demo_stop_probe.json` được giữ nếu
chưa xác nhận hủy để lần chạy sau tiếp tục đối soát. API có thể trả dữ liệu chậm
sau khi tạo/hủy, vì vậy script đợi xác nhận thay vì coi phản hồi đầu tiên là kết quả cuối.
Triển khai theo [tài liệu Algo Order của Binance](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Algo-Order).
