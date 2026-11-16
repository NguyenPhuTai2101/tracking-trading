# Trading Bot: quản lý vốn + giao dịch tự động (Binance Futures Demo)

Bot USDT-M Futures chạy trên **Binance Demo**. Nguyên tắc thiết kế: **mô hình vốn quyết định, chiến lược
chỉ đề xuất**. Chiến lược đưa ra hướng và điểm cắt lỗ; mô hình vốn quyết định có vào lệnh không và vào bao nhiêu.

> Đây là công cụ thử nghiệm trên tài khoản Demo, không phải lời khuyên đầu tư. Nghiên cứu trên 3 năm dữ liệu
> cho thấy cả chiến lược mặc định lẫn ứng viên thay thế đều **không có lợi thế sau phí khi kiểm tra ngoài mẫu**
> (xem [Nghiên cứu tín hiệu](#nghiên-cứu-tín-hiệu)). Đừng dùng tiền thật.

## Cấu trúc

```
dữ liệu nến ─► strategy.py ─► risk.py ─► engine.py ─► binance_client.py
               (tín hiệu)    (duyệt +     (khớp lệnh,   (lớp sàn)
                              khối lượng)  SL trên sàn)
                                  │
backtest.py dùng chung strategy.py và risk.py      journal.py (SQLite) ─► metrics.py ─► giao diện
```

| File | Vai trò |
|---|---|
| `strategy.py` | Xu hướng 4h, tín hiệu pullback 1h, kế hoạch thoát lệnh. Hàm thuần, không gọi mạng |
| `risk.py` | Tính khối lượng theo rủi ro, trần danh mục, ngắt mạch, thang drawdown |
| `engine.py` | Vòng lặp bot, ghi lệnh trước khi gửi, đối soát, SL trên sàn |
| `backtest.py` | Mô phỏng danh mục với đúng `strategy.py` + `risk.py`, có phí, trượt giá, funding |
| `research.py` | Nghiên cứu tín hiệu trên 3 năm dữ liệu, tách train/test |
| `journal.py` | SQLite `trading.db`: trạng thái bot, lệnh đã đóng, đường vốn, nhật ký |
| `binance_client.py` | REST Binance Demo: lệnh, lệnh điều kiện (algo), vị thế, nến |
| `main.py`, `static/` | API FastAPI và giao diện một trang |

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

## Chiến lược: pullback thuận xu hướng

1. **Xu hướng (4h):** giá so với EMA200, độ dốc EMA200 và ADX ≥ 20 cho ra TĂNG / GIẢM / ĐI NGANG.
   Đi ngang thì đứng ngoài. Altcoin không đánh ngược xu hướng của BTC.
2. **Thiết lập (1h):** EMA20/EMA50 cùng chiều xu hướng, giá hồi về EMA20 và RSI nguội lại.
3. **Kích hoạt:** nến 1h đóng theo chiều xu hướng, vượt đỉnh/đáy nến trước, chưa chạy quá 1 ATR khỏi EMA20.
   Vào lệnh ở giá mở nến kế tiếp.
4. **Cắt lỗ:** ngoài đáy/đỉnh của nhịp hồi, tối thiểu 1,5 ATR. Xa hơn 3 ATR thì bỏ lệnh.
5. **Thoát:** đạt 1R dời SL về hòa vốn (cộng phí); đạt 2R chốt 50%; phần còn lại trailing 3 ATR sau giá
   tốt nhất; sau 24 nến chưa đạt 0,5R thì đóng.

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

### Bốn tab

- **Tổng quan:** vốn, lãi/lỗ ngày và tuần, drawdown, mức sử dụng từng giới hạn rủi ro, vị thế đang mở, bảng
  quét (vì sao mỗi mã có hay chưa có tín hiệu), nhật ký.
- **Hiệu suất:** lệnh đã đóng tính theo R và USDT sau phí ước tính, đường vốn lấy từ số dư thật.
- **Thiết lập:** tham số vốn, ngắt mạch, chiến lược, danh mục. Phải dừng bot trước khi lưu.
- **Backtest:** chạy thử các giá trị đang hiện ở tab Thiết lập (kể cả chưa lưu).

### Hành vi an toàn của engine

- Mỗi lệnh được ghi vào `trading.db` **trước khi gửi**. Kết quả không rõ thì tra cứu theo mã lệnh, không gửi lại.
- Mỗi vị thế có một lệnh STOP_MARKET reduce-only theo Mark Price **trên sàn**, nên SL vẫn còn khi ứng dụng tắt.
  Khi kế hoạch dời SL (hòa vốn, trailing) hoặc sau khi chốt một phần, bot hủy lệnh SL cũ và đặt lại ở mức mới;
  trong lúc đó vòng giám sát 2 giây tự thực thi SL.
- Chốt một phần, hòa vốn, trailing và time stop **cần ứng dụng đang chạy**. Khi ứng dụng tắt chỉ còn SL trên
  sàn ở mức đã đặt gần nhất.
- Đặt SL trên sàn thất bại: đóng vị thế đó. Thất bại 2 lần trong 1 giờ: bot dừng.
- Vị thế trên sàn mà bot không biết sẽ **chặn mở lệnh mới**. Bảng điều khiển có nút đóng riêng các vị thế đó;
  "Đóng tất cả" chỉ đóng vị thế của bot.
- "Dừng mở lệnh" vẫn tiếp tục quản lý SL của vị thế đang mở. Bot đang chạy sẽ tự chạy lại sau khi máy chủ
  khởi động lại (thử tối đa 10 lần, mỗi 30 giây).

## Backtest

Trên giao diện (tab Backtest) hoặc dòng lệnh:

```bash
python backtest.py --symbols BTCUSDT ETHUSDT SOLUSDT BNBUSDT XRPUSDT --days 180 --robust
```

Mô phỏng: vào ở giá mở nến kế tiếp cộng trượt giá (bỏ nếu lệch quá 1%), phí taker mỗi lần khớp, funding lịch
sử, nến chạm cả SL và mục tiêu tính là SL, gap qua SL khớp ở giá mở. Chưa mô phỏng thanh lý, làm tròn khối
lượng và độ sâu sổ lệnh. Dữ liệu nến lấy từ API công khai của Binance, không gửi khóa API.

Báo cáo gồm kết quả ở **70% đầu và 30% cuối** giai đoạn, mức **mua & giữ**, số tín hiệu bị mô hình vốn chặn, và
(tùy chọn) **độ nhạy tham số**. Chỉ chỉnh tham số dựa trên 70% đầu; cấu hình chỉ lãi ở một phần, hoặc chỉ lãi
ở đúng một giá trị tham số, nhiều khả năng là khớp nhiễu.

### Kết quả backtest của cấu hình mặc định

10 mã mặc định, 365 ngày tới 16/11/2026, vốn 1.000 USDT: 386 lệnh, thắng 29,3%, kỳ vọng −0,01R, lợi nhuận
+4,2%, drawdown tối đa 20,2% (chạm mức dừng). Mua & giữ cùng giai đoạn: −50,7%. Mô hình vốn làm đúng việc
(mỗi lần cắt lỗ ≈ −1R, drawdown dừng ở ngưỡng đã đặt), nhưng điểm vào lệnh không tạo ra lợi thế.

## Nghiên cứu tín hiệu

`research.py` chạy đúng bộ backtest của project trên 3 năm dữ liệu (17/11/2023 → 16/11/2026, 10 mã), gỡ các
trần và ngắt mạch của mô hình vốn để đo riêng tín hiệu: R mỗi lệnh sau phí, trượt giá và funding.

```bash
python research.py fetch
```

```bash
python research.py train
```

```bash
python research.py test
```

Kỷ luật: dò tìm thoải mái trên **70% đầu** (train, tới 22/12/2025); chọn trước **một** ứng viên rồi mới chạy
**30% cuối** (test). Mỗi lần nhìn vào test là một lần "tiêu" nó.

### Trên train (11/2023 – 12/2025)

| Quy tắc | Lệnh | Kỳ vọng | t | Nửa đầu / nửa sau |
|---|---|---|---|---|
| Pullback 1h mặc định | 1.646 | −0,055R | −1,9 | −0,011R / −0,100R |
| Pullback 1h, bỏ hòa vốn + chốt một phần | 1.597 | −0,039R | −1,1 | +0,009R / −0,088R |
| Đối chứng: theo xu hướng 4h, **không setup** | 4.166 | +0,010R | +0,4 | −0,003R / +0,023R |
| Breakout 24 / 96 nến 1h, lọc xu hướng 4h | 1.970 / 1.192 | −0,045R / −0,018R | −1,2 / −0,4 | |
| Breakout 40 nến 4h, SL 2 ATR, trailing 4 ATR, không lọc | 861 | +0,159R | +2,4 | +0,231R / +0,085R |

- **Setup pullback không thêm gì**: nó kém hơn cả việc vào lệnh theo xu hướng mà không cần setup. Đổi cách
  thoát lệnh cũng không cứu được.
- **Breakout 4h trông có triển vọng**: cả 24 ô trong lưới (N 20/40/80 × SL 2/3 ATR × trailing 3–6 ATR) đều
  dương, t từ 0,8 đến 2,6. Nhưng 10 lệnh lớn nhất (một lệnh XRP +97R cuối 2024) chiếm 226 trên 243R ở ô
  trailing 5 ATR, và lệnh SHORT âm (−0,10R).

Ứng viên chọn trước cho test: **breakout 40 nến 4h, SL 2 ATR, trailing 4 ATR, cả hai chiều, không lọc** (ô
giữa của vùng dương, không phải ô tốt nhất).

### Trên test (12/2025 – 11/2026), chạy một lần

| Quy tắc | Lệnh | Kỳ vọng | t |
|---|---|---|---|
| Ứng viên breakout 4h | 369 | −0,003R | −0,03 |
| Pullback 1h mặc định | 641 | +0,001R | +0,02 |

Qua mô hình vốn thật: ứng viên −0,7% (drawdown 18,6%), pullback mặc định +5,7% (drawdown 20,2%, bị dừng).

Cùng lần chạy đó cũng in ra, chỉ để tham khảo chứ không dùng để chọn: 16/24 ô của lưới breakout dương trên
test nhưng không ô nào có |t| ≥ 2; mẫu đối chứng "theo xu hướng, không setup" khung 1h đạt +0,093R (t = 2,1)
trên test trong khi chỉ +0,010R trên train, tức là không nhất quán giữa hai giai đoạn.

### Kết luận

- Lợi thế của breakout 4h trên train **không lặp lại ngoài mẫu**. Nó đến từ vài con sóng lớn của năm 2024,
  không phải từ quy tắc. Vì vậy chiến lược mặc định **không được đổi** sang breakout.
- Chưa có quy tắc giá đơn giản nào (pullback, breakout, 1h hay 4h) cho thấy lợi thế sau phí trên 10 coin này.
- Phí ăn khoảng 0,05R mỗi lệnh ở khung 1h và 0,02–0,03R ở khung 4h. Khung chậm rẻ hơn nhưng chưa đủ.
- Giai đoạn test đã bị nhìn một lần. Ý tưởng mới cần dữ liệu mới (thêm coin, thêm lịch sử, hoặc chờ thời gian
  trôi) thì kiểm tra ngoài mẫu mới còn ý nghĩa.

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

- Lãi/lỗ từng lệnh dùng **phí ước tính** (taker 0,05% mỗi chiều) và chưa gồm funding. Đường vốn thì lấy từ số
  dư thật nên đã gồm cả hai.
- Luồng chốt một phần gửi lệnh reduce-only khi lệnh SL cũ còn trên sàn, rồi mới thay SL. Luồng này đã được
  kiểm thử với sàn giả lập, **chưa được xác nhận trên Binance Demo thật**; nếu sàn từ chối, bot bỏ qua bước
  chốt một phần và tiếp tục trailing.
- Ngày/tuần của ngắt mạch tính theo UTC. Nạp/rút tiền làm lệch các mốc vốn; drawdown tính lại từ hôm bạn bấm
  xác nhận.
- Dữ liệu của bản cũ (`bot_state*.json`) không được chuyển sang. Vị thế do bản cũ mở sẽ hiện là "vị thế ngoài bot".
