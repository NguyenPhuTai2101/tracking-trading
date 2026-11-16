# Trading Bot: quản lý vốn + nhận diện case thị trường (Binance Futures Demo)

Bot USDT-M Futures chạy trên **Binance Demo**. Hai nguyên tắc thiết kế:

- **Mô hình vốn quyết định, chiến lược chỉ đề xuất.** Chiến lược đưa ra hướng và điểm cắt lỗ; mô hình vốn
  quyết định có vào lệnh không và vào bao nhiêu.
- **Nhận diện nhiều case, chỉ giao dịch case có bằng chứng.** Bot quét và báo cáo mọi case nó biết; case nào
  chưa qua kiểm tra ngoài mẫu thì chỉ được quan sát.

> Đây là công cụ thử nghiệm trên tài khoản Demo, không phải lời khuyên đầu tư. Trong các case đã nghiên cứu chỉ
> có một case (phá vỡ có lực khung 4h) cho kỳ vọng dương, và mức dương đó **nhỏ, dồn vào những năm thị trường
> có xu hướng mạnh** (xem [Nghiên cứu tín hiệu](#nghiên-cứu-tín-hiệu)). Đừng dùng tiền thật.

## Cấu trúc

```
dữ liệu nến ─► strategy.py ─► risk.py ─► engine.py ─► binance_client.py
               (case/setup)  (duyệt +     (khớp lệnh,   (lớp sàn)
                              khối lượng)  SL trên sàn)
                                  │
backtest.py dùng chung strategy.py và risk.py      journal.py (SQLite) ─► metrics.py ─► giao diện
```

| File | Vai trò |
|---|---|
| `strategy.py` | Các case (setup): quy tắc nhận diện, khung thời gian, cách thoát lệnh. Hàm thuần, không gọi mạng |
| `risk.py` | Tính khối lượng theo rủi ro, trần danh mục, ngắt mạch, thang drawdown |
| `engine.py` | Quét từng case trên khung của nó, ghi lệnh trước khi gửi, đối soát, SL trên sàn |
| `backtest.py` | Mô phỏng danh mục với đúng `strategy.py` + `risk.py`, có phí, trượt giá, funding |
| `research.py` | Nghiên cứu tín hiệu với ba tập dữ liệu tách rời (train / kiểm định / holdout) |
| `journal.py` | SQLite `trading.db`: trạng thái bot, lệnh đã đóng, đường vốn, nhật ký |
| `notifier.py` | Gửi thông báo Telegram qua hàng đợi riêng, không làm chậm vòng giao dịch |
| `binance_client.py` | REST Binance Demo: lệnh, lệnh điều kiện (algo), vị thế, nến |
| `main.py`, `static/` | API FastAPI và giao diện một trang |

## Các case bot nhận diện

Mỗi case có khung tín hiệu, khung xu hướng và cách thoát lệnh riêng. Bot **luôn quét cả bốn**; chỉ case được
bật ở tab Thiết lập mới được vào lệnh. Case còn lại hiện ở bảng quét và nhật ký (`CASE`) để bạn theo dõi.

| Case | Khung | Nhận diện | Thoát lệnh | Bằng chứng | Mặc định |
|---|---|---|---|---|---|
| **Phá vỡ có lực** `THRUST_BREAKOUT` | 4h, xu hướng ngày | Nến đóng vượt đỉnh/đáy 20 nến **và** vượt dải Bollinger, không ngược xu hướng ngày | SL 2 ATR, trailing 4 ATR. Không hòa vốn, không chốt một phần | Dương ở cả 4 mẫu dữ liệu, +0,07 đến +0,25R mỗi lệnh | **Vào lệnh** |
| **Nhịp hồi trong xu hướng** `TREND_PULLBACK` | 1h, xu hướng 4h | Xu hướng rõ, giá hồi về EMA20 rồi có nến xác nhận | Hòa vốn ở 1R, chốt 50% ở 2R, trailing 3 ATR, time stop | Không có lợi thế (−0,055R train, ≈ 0R ngoài mẫu) | Quan sát |
| **Đảo chiều trong vùng đi ngang** `RANGE_REVERSION` | 1h, xu hướng 4h | Đi ngang, giá đóng ngoài dải Bollinger rồi đóng trở lại | Mục tiêu đường giữa dải, tối đa 24 nến | Âm trên train (−0,22R), ≈ 0R trên kiểm định | Quan sát |
| **Phá vỡ giả** `FAILED_BREAKOUT` | 1h, xu hướng 4h | Giá quét qua đỉnh/đáy 48 nến rồi đóng ngược lại | Mục tiêu 2R, tối đa 24 nến | Âm trên train (−0,13R), ≈ 0R trên kiểm định | Quan sát |

Bảng quét còn gắn nhãn trạng thái thị trường cho từng mã: **nén biến động** (độ rộng Bollinger thuộc 20% thấp
nhất của 120 nến), **sốc biến động** (ATR gấp đôi trung bình 100 nến), **quá mua / quá bán** (RSI ≥ 70 / ≤ 30).
Các nhãn này chỉ mô tả, không tạo lệnh: nghiên cứu cho thấy chúng không cải thiện kết quả khi dùng làm bộ lọc.

Xu hướng (TĂNG / GIẢM / ĐI NGANG) tính từ giá so với EMA200, độ dốc EMA200 và ADX ≥ 20 trên khung xu hướng
của từng case. Mỗi mã chỉ có một vị thế; nếu hai case cùng báo ở một nến thì case đứng trước trong bảng thắng.

## Mô hình quản lý vốn

Mọi giới hạn tính theo **% vốn tài khoản** (số dư + lãi/lỗ chưa chốt) và **không tắt được**; chỉ chỉnh trong
khoảng cho phép ở tab Thiết lập.

| Lớp | Mặc định | Ý nghĩa |
|---|---|---|
| Rủi ro mỗi lệnh | 0,75% vốn | Chạm SL thì mất chừng này, đã gồm phí + trượt giá ước tính (= 1R) |
| Khối lượng | tự tính | `rủi ro / (khoảng cách SL + chi phí khứ hồi)`. Đòn bẩy (5x) chỉ quyết định ký quỹ |
| Tổng rủi ro đang mở | 3% vốn | Nếu mọi SL cùng bị chạm. SL đã dời qua giá vào thì tính rủi ro = 0 |
| Số vị thế / cùng chiều | 4 / 3 | Các coin đi cùng nhau nên các lệnh cùng chiều gần như một lệnh lớn |
| Giá trị vị thế | 100% vốn mỗi lệnh, 300% tổng | Các trần này chỉ làm lệnh nhỏ đi |
| Lọc chi phí | 0,15R | Bỏ lệnh nếu phí + trượt giá khứ hồi lớn hơn 0,15R (SL quá sát) |
| Ngắt mạch ngày / tuần | −2% / −5% | Nghỉ mở lệnh tới 00:00 UTC / thứ Hai. Vị thế đang mở vẫn giữ SL |
| Chuỗi thua | 4 lệnh | Nghỉ tới hôm sau |
| Thang drawdown | −10% / −20% từ đỉnh | −10%: rủi ro mỗi lệnh giảm một nửa. −20%: bot dừng, chờ bạn bấm xác nhận |

## Chạy ứng dụng

```bash
python -m pip install -r requirements.txt
```

```bash
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Mở `http://127.0.0.1:8000`. Khóa Demo đặt trong `.env` cạnh mã nguồn:

```
BINANCE_API_KEY=...
BINANCE_SECRET_KEY=...
```

- Chạy **một worker**. Ứng dụng khóa file `trading.db.lock` để hai tiến trình không cùng giao dịch.
- `STATE_DIR` đổi nơi lưu `trading.db` (ví dụ volume trên Railway).
- `APP_USERNAME` + `APP_PASSWORD` bật trang đăng nhập. Trên Railway (hoặc `REQUIRE_AUTH=1`) mà thiếu hai
  biến này thì ứng dụng trả 503 thay vì mở công khai.
- Tài khoản Futures phải ở chế độ **One-way**. Bot không tự đổi chế độ.

### Thông báo Telegram

1. Trong Telegram, nhắn `/newbot` cho **@BotFather** để tạo bot và nhận token.
2. Mở cuộc trò chuyện với bot vừa tạo và gửi cho nó một tin bất kỳ (bot không thể nhắn trước cho bạn).
3. Lấy chat id: mở `https://api.telegram.org/bot<TOKEN>/getUpdates` trên trình duyệt và tìm `"chat":{"id":...}`.
4. Thêm vào `.env` (hoặc biến môi trường trên Railway) rồi khởi động lại ứng dụng:

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

5. Vào tab Thiết lập, mục "Thông báo Telegram", bấm **Gửi tin thử**. Nếu sai token hoặc chat id, lý do Telegram
   trả về sẽ hiện ngay cạnh nút.

Bot gửi (bật/tắt từng loại ở tab Thiết lập):

| Loại | Mặc định | Nội dung |
|---|---|---|
| Mở / đóng lệnh | Bật | Mở vị thế (case, khối lượng, SL, 1R), chốt một phần, đóng lệnh kèm lãi/lỗ và R, bot bắt đầu hoặc tự chạy lại |
| Rủi ro | Bật | Ngắt mạch ngày/tuần/chuỗi thua, dừng do drawdown, bot tự dừng, lỗi đặt SL trên sàn |
| Sức khỏe | Bật | Không đọc được sàn quá 5 phút (một tin), và khi kết nối lại (một tin) |
| Tổng kết ngày | Bật | Lúc 00:00 UTC: vốn, % trong ngày, số lệnh đã đóng, vị thế đang mở |
| Mọi case được nhận diện | Tắt | Cả case chỉ quan sát; khá nhiều tin |
| Cảnh báo lặp lại | Tắt | Ví dụ một mã không quét được |

Token chỉ nằm trong biến môi trường: không ghi vào `trading.db`, không trả qua API, không xuất hiện trong thông báo
lỗi. Tin giống hệt nhau không gửi lại trong 10 phút; gửi lỗi thì thử lại 3 lần rồi bỏ; hàng đợi tối đa 200 tin.
Telegram chậm hoặc không truy cập được không ảnh hưởng tới việc giao dịch.

### Bốn tab

- **Tổng quan:** vốn, lãi/lỗ ngày và tuần, drawdown, mức sử dụng từng giới hạn rủi ro, vị thế đang mở, bảng
  quét case (mỗi mã: từng case đang ở bước nào, ● được vào lệnh, ○ chỉ quan sát), nhật ký.
- **Hiệu suất:** lệnh đã đóng tính theo R và USDT với **phí và funding thật từ sàn**, tách theo case; chi phí chạy
  thật đặt cạnh giả định của backtest; kết quả từng case đặt cạnh khoảng kỳ vọng từ nghiên cứu; đường vốn lấy
  từ số dư thật.
- **Thiết lập:** bật/tắt từng case (kèm bằng chứng nghiên cứu), tham số vốn, ngắt mạch, danh mục. Phải dừng
  bot trước khi lưu.
- **Backtest:** chạy thử các giá trị đang hiện ở tab Thiết lập (kể cả chưa lưu), có bảng theo case.

### Hành vi an toàn của engine

- Mỗi lệnh được ghi vào `trading.db` **trước khi gửi**. Kết quả không rõ thì tra cứu theo mã lệnh, không gửi lại.
- Mỗi vị thế có một lệnh STOP_MARKET reduce-only theo Mark Price **trên sàn**, nên SL vẫn còn khi ứng dụng tắt.
  Khi kế hoạch dời SL (hòa vốn, trailing) hoặc sau khi chốt một phần, bot hủy lệnh SL cũ và đặt lại ở mức mới;
  trong lúc đó vòng giám sát 2 giây tự thực thi SL.
- Trailing, hòa vốn, chốt một phần, chạm mục tiêu và time stop **cần ứng dụng đang chạy**. Khi ứng dụng tắt chỉ
  còn SL trên sàn ở mức đã đặt gần nhất. Vị thế được quản lý theo nến của case đã mở nó (4h hoặc 1h).
- Đặt SL trên sàn thất bại: đóng vị thế đó. Thất bại 2 lần trong 1 giờ: bot dừng.
- Vị thế trên sàn mà bot không biết sẽ **chặn mở lệnh mới**. Bảng điều khiển có nút đóng riêng các vị thế đó;
  "Đóng tất cả" chỉ đóng vị thế của bot.
- **Đối soát lãi/lỗ với sàn.** Khi đóng, lệnh được ghi ngay với phí ước tính (dấu ≈). Khoảng một phút sau bot
  đọc lại các lần khớp của đúng những lệnh đã mở và đóng vị thế đó (`/fapi/v1/userTrades`) cùng các khoản
  funding trong thời gian giữ lệnh (`/fapi/v1/income`), rồi thay bằng phí, lãi/lỗ và funding thật. Chưa đủ dữ
  liệu thì thử lại mỗi phút, tối đa 3 ngày. Việc này chỉ sửa sổ sách, không ảnh hưởng lệnh đang mở.
- "Dừng mở lệnh" vẫn tiếp tục quản lý SL của vị thế đang mở. Bot đang chạy sẽ tự chạy lại sau khi máy chủ
  khởi động lại (thử tối đa 10 lần, mỗi 30 giây).

## Backtest

Trên giao diện (tab Backtest) hoặc dòng lệnh:

```bash
python backtest.py --symbols BTCUSDT ETHUSDT SOLUSDT BNBUSDT XRPUSDT --days 730 --robust
```

```bash
python backtest.py --setups THRUST_BREAKOUT TREND_PULLBACK --days 365
```

Mỗi case đọc nến của khung riêng; vị thế được quản lý theo nến của case đã mở nó. Vào ở giá mở nến kế tiếp
cộng trượt giá (bỏ nếu lệch quá 1%), phí taker mỗi lần khớp, funding lịch sử, nến chạm cả SL và mục tiêu tính
là SL, gap qua SL khớp ở giá mở. Chưa mô phỏng thanh lý, làm tròn khối lượng và độ sâu sổ lệnh. Dữ liệu nến
lấy từ API công khai của Binance, không gửi khóa API. Tối đa 1.095 ngày, 15 mã.

Báo cáo gồm kết quả ở **70% đầu và 30% cuối** giai đoạn, mức **mua & giữ**, kết quả theo case, số tín hiệu bị
mô hình vốn chặn, và (tùy chọn) **độ nhạy tham số**. Cấu hình chỉ lãi ở một phần, hoặc chỉ lãi ở đúng một giá
trị tham số, nhiều khả năng là khớp nhiễu.

### Kết quả backtest của cấu hình mặc định

Case "Phá vỡ có lực", 10 mã mặc định, 730 ngày tới 16/11/2026, vốn 1.000 USDT, mô hình vốn mặc định:
380 lệnh, thắng 31,1%, kỳ vọng +0,13R, **lợi nhuận +8,9%**, **drawdown tối đa 17,6%**. Mua & giữ cùng giai
đoạn: −50,2%. Phần lớn giai đoạn này nằm trong dữ liệu đã dùng để nghiên cứu, nên đây là mô tả chứ không phải
bằng chứng ngoài mẫu. Lợi nhuận mỏng so với drawdown: hãy coi đây là một case đáng theo dõi tiếp trên Demo.

## Nghiên cứu tín hiệu

`research.py` chạy đúng bộ backtest của project, gỡ các trần và ngắt mạch của mô hình vốn để đo riêng tín
hiệu: R mỗi lệnh sau phí, trượt giá và funding. Ba tập dữ liệu tách rời:

| Tập | Giai đoạn, coin | Dùng để |
|---|---|---|
| **Train** | 11/2023 → 12/2025, 10 coin của danh mục | Dò tìm thoải mái |
| **Kiểm định** | 12/2025 → 11/2026, 10 coin của danh mục | Kiểm tra điều train gợi ý |
| **Holdout H1** | 01/2021 → 11/2023, 20 coin | Chu kỳ thị trường trước đó, chưa từng dùng |
| **Holdout H2** | 11/2023 → 11/2026, 10 coin khác | Coin chưa từng xem (LTC, DOT, TRX, BCH, NEAR, UNI, ATOM, ETC, XLM, AAVE) |

```bash
python research.py fetch
```

```bash
python research.py fetch-holdout
```

```bash
python research.py train
```

```bash
python research.py validate
```

```bash
python research.py holdout
```

### Kết quả (kỳ vọng mỗi lệnh, t-statistic, số lệnh)

| Quy tắc | Train | Kiểm định | Holdout (H1 + H2) |
|---|---|---|---|
| **Phá vỡ có lực 4h** (case của bot) | +0,252R, t = 3,3 (795) | +0,079R, t = 0,6 (333) | **+0,103R, t = 2,7 (3.149)** |
| Như trên, chỉ sau nén biến động | +0,275R, t = 2,8 (487) | +0,144R, t = 0,9 (227) | +0,057R, t = 1,3 (2.079) |
| Donchian 40 nến 4h, không lọc | +0,159R, t = 2,4 (861) | −0,003R, t = 0,0 (369) | không chạy |
| Nhịp hồi trong xu hướng 1h | −0,055R, t = −1,9 (1.646) | +0,001R, t = 0,0 (641) | không chạy |
| Đối chứng: theo xu hướng 1h, không setup | +0,010R, t = 0,4 (4.166) | +0,093R, t = 2,1 (1.647) | không chạy |
| Đảo chiều trong vùng đi ngang 1h | −0,219R, t = −4,9 (638) | +0,004R, t = 0,1 (236) | không chạy |
| Phá vỡ giả 1h | −0,128R, t = −4,0 (1.433) | +0,004R, t = 0,1 (352) | không chạy |

Phá vỡ có lực trên holdout, tách ra: H1 +0,120R (t = 2,4; 2.029 lệnh), H2 +0,072R (t = 1,3; 1.120 lệnh).
Theo năm: 2021 +0,26R, 2022 +0,08R, 2023 −0,01R, 2024 +0,32R, 2025 −0,08R, 2026 +0,02R.
LONG +0,19R, SHORT +0,03R.

### Cách đọc, nói thẳng

- **Phá vỡ có lực 4h là case duy nhất dương ở cả bốn mẫu dữ liệu**, và trên train nó dương ở mọi biến thể tham
  số đã thử (số nến 10/20/40, SL 1,5–3 ATR, trailing 3–5 ATR: +0,17 đến +0,37R). Vì vậy nó là case mặc định.
- **Mức dương đó nhỏ và không đều.** Khoảng +0,1R mỗi lệnh, gần như toàn bộ đến từ hai năm có xu hướng mạnh
  (2021, 2024) và từ lệnh LONG; các năm còn lại quanh 0. Qua mô hình vốn thật, năm kiểm định chỉ cho +3,0%
  với drawdown 17,9%.
- **Con số t = 2,7 trên holdout cần được hiểu đúng.** Quy tắc tôi cam kết trước khi mở holdout là biến thể
  "chỉ sau nén biến động", và biến thể đó **không đạt** (+0,057R, t = 1,3). Phá vỡ có lực (cùng quy tắc nhưng bỏ
  điều kiện nén) là giả thuyết phụ chạy cùng lần đó. Kết quả của nó vẫn vững sau khi hiệu chỉnh cho hai phép
  thử, nhưng yếu hơn một giả thuyết chính được xác nhận.
- **Điều kiện "nén biến động" không thêm gì**: trên holdout nó làm kết quả kém đi. Nên "nén" chỉ là nhãn mô tả.
- **Đánh ngược không có lợi thế**: đảo chiều ở biên Bollinger và phá vỡ giả lỗ rõ rệt trên train, về 0 trên
  kiểm định. Bot nhận diện hai case này nhưng mặc định không giao dịch.
- **Donchian 40 nến** (ứng viên của vòng nghiên cứu trước) dương trên train nhưng về 0 trên kiểm định. Khác biệt
  với phá vỡ có lực là yêu cầu vượt cả dải Bollinger và không đi ngược xu hướng ngày.
- **Phí** ăn khoảng 0,05R mỗi lệnh ở khung 1h và 0,02–0,03R ở khung 4h. Đây là một lý do các case 1h khó dương.
- **Holdout đã bị nhìn một lần.** Ý tưởng mới cần dữ liệu mới (thêm coin, hoặc chờ thời gian trôi) thì kiểm tra
  ngoài mẫu mới còn ý nghĩa. Cách thu thêm bằng chứng không tốn dữ liệu: chạy bot trên Demo và theo dõi bảng
  "Theo case" ở tab Hiệu suất.

## Kiểm thử

```bash
python -m pip install -r requirements-dev.txt
```

```bash
python -m unittest discover -s tests -t .
```

Bộ test dùng sàn giả lập trong bộ nhớ (`tests/helpers.py`), không gọi mạng và không đụng tới `trading.db` thật.

Kiểm tra kết nối bằng khóa trong `.env` (không in khóa): `python demo_smoke_test.py`. Khi ứng dụng đã tắt và
tài khoản Demo không có vị thế hay lệnh chờ, `python demo_smoke_test.py --probe-stop` tạo rồi hủy một lệnh
STOP_MARKET reduce-only ở xa giá để kiểm tra vòng đời lệnh SL; không mở vị thế.

## Giới hạn đã biết

- Lãi/lỗ từng lệnh chỉ là **ước tính** (phí taker 0,05% mỗi chiều, chưa có funding) cho tới khi được đối soát
  với sàn. Nếu phí bị thu bằng tài sản khác USD (ví dụ BNB), phí giữ nguyên ước tính còn lãi/lỗ và funding vẫn
  lấy số thật. Các ngắt mạch không phụ thuộc vào những con số này vì chúng tính theo vốn thật của tài khoản.
- Luồng đối soát đã được kiểm thử với sàn giả lập và hai endpoint đã xác nhận có trên Binance Demo, nhưng
  **chưa chạy với khóa thật**. Funding được gán cho lệnh theo mã và khoảng thời gian giữ lệnh, đúng khi mỗi mã
  chỉ có vị thế của bot (bot vốn chặn mở lệnh nếu thấy vị thế ngoài bot).
- Thông báo Telegram đã được kiểm thử với Telegram giả lập; **chưa có tin nào được gửi tới Telegram thật**.
  Bước "Gửi tin thử" ở trên là cách xác nhận. Nếu ứng dụng tắt hẳn thì không có tin nào được gửi, kể cả cảnh
  báo sức khỏe: muốn biết máy chủ chết cần một dịch vụ giám sát bên ngoài gọi `/healthz`.
- Bảng "so với kỳ vọng từ nghiên cứu" có khoảng rất rộng khi ít lệnh (với 30 lệnh của case phá vỡ: ±0,77R).
  Nó dùng để phát hiện kết quả lệch hẳn (thường do khớp lệnh hoặc chi phí), không phải để chứng minh case.
- Luồng chốt một phần gửi lệnh reduce-only khi lệnh SL cũ còn trên sàn, rồi mới thay SL. Luồng này đã được
  kiểm thử với sàn giả lập, **chưa được xác nhận trên Binance Demo thật**; nếu sàn từ chối, bot bỏ qua bước
  chốt một phần và tiếp tục trailing. Case mặc định (phá vỡ có lực) không dùng chốt một phần.
- Mục tiêu chốt lời của các case đánh ngược do vòng giám sát thực thi (lệnh thị trường khi giá chạm), không
  có lệnh chốt lời đặt sẵn trên sàn.
- Ngày/tuần của ngắt mạch tính theo UTC. Nạp/rút tiền làm lệch các mốc vốn; drawdown tính lại từ hôm bạn bấm
  xác nhận.
- Dữ liệu của bản cũ (`bot_state*.json`) không được chuyển sang. Vị thế do bản cũ mở sẽ hiện là "vị thế ngoài bot".
