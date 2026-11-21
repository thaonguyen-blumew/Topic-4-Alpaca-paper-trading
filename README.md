# Alpaca Paper Trading Bot

Xay theo bai "Build a Python Paper Trading Bot with Alpaca API" (Anil Gaddam, Medium, 04/11/2025).
Khong tim thay GitHub cua tac gia, nen day la ban tu viet lai theo code va kien truc trong bai.
Bot **luon chay paper trading** (URL co dinh `https://paper-api.alpaca.markets`).

## Kien truc (khop bai viet)
| Thanh phan | File | Vai tro |
|---|---|---|
| `TradingConfig` | `config.py` | Cai dat tap trung (dataclass) |
| `TradingStrategy` (ABC) | `strategies/base.py` | Lop cha cho moi chien luoc |
| MovingAverageCrossover, RSI | `strategies/` | 2 chien luoc chinh |
| BollingerBands, MACD | `strategies/` | Vi du mo rong trong bai |
| `DataManager` | `data_manager.py` | Lay & chuan hoa du lieu |
| `PortfolioManager` | `portfolio_manager.py` | Vi the + position sizing |
| `OrderManager` | `order_manager.py` | Lenh market / limit |
| `TradingBot` | `trading_bot.py` | Dieu phoi, loc confidence, bao cao |

## Cai dat
```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env     # dien PAPER key tu https://app.alpaca.markets
```

## Chay
```bash
python main.py --once --dry-run --ignore-market-hours   # thu, khong dat lenh
python main.py --once                                   # 1 chu ky (chi khi thi truong mo)
python main.py --interval 30                            # chay lien tuc moi 30 phut
python run_like_article.py                               # chay y het muc 'Running the Bot' trong bai
python main.py --bollinger                              # bat them Bollinger Bands
pytest -q                                               # test offline
```

## Khac biet so voi bai (ghi ro)
- Bai de trong "Execute buy/sell logic" va than Bollinger: toi tu dien (Bollinger: gia ngoai dai +-2 sigma, mac dinh TAT, bat bang `--bollinger`).
- Bao cao: dung `account.unrealized_pl` nhu bai; neu tai khoan khong co truong nay thi tu dong dung `equity - last_equity`.
- `TradingConfig.data_feed = "iex"` (goi mien phi). Neu API bao loi SIP/feed, thu doi sang `None` hoac `"sip"`.
- TradingView: chi co KHUNG `TradingViewDataManager` (NotImplementedError), bai dung `TradingViewClient` nhung khong noi thu vien nao (co the la tvDatafeed, chua kiem chung).
- `PortfolioManager.calculate_position_size`: dung cong thuc cua bai, them 1 buoc: gia > buying power thi tra ve 0.
- RSI: dung cong thuc rolling cua bai, them xu ly loss=0 (RSI=100) de tranh chia cho 0.
- `add_macd_indicator` la ham rieng (`strategies/indicators.py`), bai viet no nhu method co `self`.
- Bai liet ke "Stop losses" trong Best Practices nhung khong co code; ban nay chua lam.
- Dung URL API paper `https://paper-api.alpaca.markets` (bai ghi URL trang docs).
- requirements.txt them python-dotenv (doc .env) va pytest; bai chi dung bien moi truong `export`.
- Neu BUY va SELL cung xuat hien cho 1 ma, chi lay nhom co confidence trung binh cao hon.
- Chua chay voi Alpaca that. Chi dung de hoc tap.
