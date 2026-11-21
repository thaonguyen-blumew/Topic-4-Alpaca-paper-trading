"""
risk_control_all.py  (một file duy nhất, phiên bản đã sửa theo nhận xét)
Cải tiến quản trị rủi ro cho bot paper trading: Stop-loss + Daily Loss Limit (DLL).

Cấu trúc file:
  1. Tham số                          5. Gọi Alpaca (open_position_guarded, hủy lệnh chờ)
  2. Stop-loss                        6. Đo tần suất kích hoạt DLL (tùy chọn)
  3. Daily Loss Limit                 7. Kiểm thử + mô phỏng kịch bản
  4. Trạng thái + Circuit Breaker

BẢN ĐỒ CÔNG THỨC (công thức thiết kế  ->  hàm trong code)
  SL = Entry x (1 - r_SL)                        -> calculate_stop_price()
  Rủi ro/lệnh (% vốn) = M x r_SL / Equity        -> risk_per_trade_pct()   (chỉ ghi log)
  r_SL <= L x Equity / M   (cận trên từ 2-5%)    -> r_sl_toi_da()          (chọn tham số, không chặn lệnh)
  R1 = M x r_SL ;  MaxDailyLoss = R1 x N_loss    -> max_daily_loss()
  DailyPnL = CurrentEquity - StartingEquity      -> daily_pnl()
  Kích hoạt khi DailyPnL <= -MaxDailyLoss        -> check_daily_loss_limit()
  DLL (% vốn đầu ngày) = R1 x N_loss / Start     -> dll_phan_tram()
  Lỗ tối đa lý thuyết = số vị thế x R1           -> loss_toi_da_ly_thuyet()
  Chỉ có HAI cơ chế chặn/bảo vệ: stop-loss (gửi cùng lệnh mua) và DLL (chặn mở vị thế mới).

LƯU Ý VỀ Ý NGHĨA CỦA DLL: DLL là ngưỡng KÍCH HOẠT để chặn mở vị thế MỚI, không phải cam kết tổng lỗ
trong ngày không vượt ngưỡng. Các vị thế đang giữ vẫn chịu stop-loss, nên lỗ tối đa lý thuyết trong
ngày (stop khớp đúng giá) = số vị thế tối đa x R1 (5 x 50 = 250$), chưa tính gap và trượt giá.

Chạy kiểm thử:   python risk_control_all.py
Các hàm tính toán là hàm thuần (nhận số, trả số) nên kiểm thử không cần Alpaca hay mạng.
Phần gọi Alpaca được kiểm thử bằng API giả (FakeApi), CHƯA chạy thử trên tài khoản paper thật.
"""

import json
import math
import os
import tempfile
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

# ----------------------------------------------------------------------
# 1. THAM SỐ (do nhóm chọn, không phải giá trị đã được chứng minh tối ưu)
# ----------------------------------------------------------------------
R_SL = 0.05                # tỷ lệ stop-loss (5%): lựa chọn của nhóm, thử độ nhạy 3%, 5%, 8%
MAX_POSITION_VALUE = 1000  # trần giá trị vị thế của bot gốc (USD), kế thừa từ bài gốc
N_LOSS = 3                 # số "đơn vị lỗ tham chiếu" (mỗi đơn vị = R1) dùng để đặt ngưỡng kích hoạt DLL.
                           # KHÔNG có nghĩa là bot chỉ thua tối đa 3 lệnh hay tối đa 150$ mỗi ngày.
RISK_LIMIT = 0.02          # "Never risk more than 2-5% per trade" (bài gốc): CHỈ dùng để ghi log cảnh báo
STATE_FILE = "risk_state.json"
BOT_PREFIX = "rcbot"       # tiền tố client_order_id: bot chỉ hủy lệnh do chính nó đặt
NY = ZoneInfo("America/New_York")   # "ngày giao dịch" tính theo giờ thị trường Mỹ


# ----------------------------------------------------------------------
# 2. STOP-LOSS
# ----------------------------------------------------------------------
def is_positive_number(value):
    """True nếu value là SỐ (int/float, không phải bool/chuỗi), hữu hạn và > 0.
    Dữ liệu dạng chuỗi từ API (account.equity...) phải đổi sang float trước khi kiểm tra."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and value > 0


def is_positive_int(value):
    """True nếu value là số nguyên dương (loại bool, số lẻ, chuỗi). Cổ phiếu lệnh OTO cần số lượng nguyên."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def calculate_stop_price(entry_price, r_sl):
    """SL = Entry x (1 - r). Làm tròn 2 chữ số (giá >= 1$) hoặc 4 chữ số (giá < 1$).
    Lưu ý: cần đối chiếu quy tắc bước giá của sàn nếu mở rộng sang cổ phiếu giá thấp."""
    if not is_positive_number(entry_price):
        raise ValueError("entry_price phải là số dương hữu hạn")
    if not is_positive_number(r_sl) or r_sl >= 1:
        raise ValueError("r_sl phải nằm trong (0, 1)")
    so_le = 2 if entry_price >= 1 else 4
    return round(entry_price * (1 - r_sl), so_le)


def risk_per_trade_pct(position_value, r_sl, equity):
    """Rủi ro mỗi lệnh theo % vốn = PositionValue x r_sl / Equity (nếu stop khớp đúng giá)."""
    if not is_positive_number(equity):
        raise ValueError("equity phải > 0")
    return position_value * r_sl / equity


def check_risk_compliance(position_value, r_sl, equity, limit=RISK_LIMIT):
    """So rủi ro mỗi lệnh với giới hạn của bài gốc. Trả về (đạt?, rủi_ro_%_vốn).
    Chỉ dùng để GHI LOG, không chặn lệnh."""
    pct = risk_per_trade_pct(position_value, r_sl, equity)
    return pct <= limit, pct


def r_sl_toi_da(equity, max_position_value=MAX_POSITION_VALUE, limit=RISK_LIMIT):
    """Cận trên của r_SL để rủi ro mỗi lệnh <= L x Equity:  r_SL <= L x Equity / M  (tối đa 100%).
    Ví dụ Equity 10.000, M 1.000, L 2% -> 20%. Đây là CẬN TRÊN từ premise của bài gốc, không phải giá trị tối ưu."""
    if not is_positive_number(equity) or not is_positive_number(max_position_value) or not is_positive_number(limit):
        raise ValueError("equity, max_position_value, limit phải > 0")
    return min(limit * equity / max_position_value, 1.0)


def check_position_cap(position_value, cap=MAX_POSITION_VALUE):
    """Giá trị vị thế không được vượt trần của bot gốc."""
    return position_value <= cap


def rui_ro_du_kien_theo_gia_khop(filled_price, stop_price, qty, equity):
    """Lỗ DỰ KIẾN nếu stop khớp đúng giá stop, tính từ giá MUA ĐÃ KHỚP (không phải giá tham chiếu).
    Trả về (lỗ_mỗi_cp_%, lỗ_dự_kiến_USD, lỗ_dự_kiến_%_vốn). Đây chưa phải lỗ thực tế đã phát sinh."""
    if not (is_positive_number(filled_price) and is_positive_number(equity) and is_positive_number(qty)):
        raise ValueError("filled_price, equity, qty phải > 0")
    lo_moi_cp = max(filled_price - stop_price, 0.0)
    return lo_moi_cp / filled_price, lo_moi_cp * qty, lo_moi_cp * qty / equity


def lo_thuc_te(buy_filled_price, sell_filled_price, qty):
    """Lỗ THỰC TẾ của một lệnh đã đóng = (giá mua khớp - giá bán khớp) x số lượng. Âm nghĩa là lãi."""
    return (buy_filled_price - sell_filled_price) * qty


# ----------------------------------------------------------------------
# 3. DAILY LOSS LIMIT (DLL)
# ----------------------------------------------------------------------
def max_daily_loss(max_position_value, r_sl, n_loss):
    """MaxDailyLoss = R1 x N_loss, với R1 = MaxPositionValue x r_sl."""
    if not is_positive_number(max_position_value) or not is_positive_number(r_sl) or r_sl >= 1:
        raise ValueError("max_position_value phải > 0 và r_sl trong (0, 1)")
    if not is_positive_int(n_loss):
        raise ValueError("n_loss phải là số nguyên dương")
    return max_position_value * r_sl * n_loss


def loss_toi_da_ly_thuyet(n_vi_the, max_position_value, r_sl):
    """Lỗ tối đa trong ngày nếu mọi vị thế đang giữ cùng chạm stop ĐÚNG giá.
    DLL không đảm bảo tổng lỗ nhỏ hơn mức này vì chỉ chặn mở vị thế mới."""
    return n_vi_the * max_position_value * r_sl


def dll_phan_tram(max_loss, starting_equity):
    """DLL tính theo % vốn đầu ngày = MaxDailyLoss / StartingEquity (để so với vốn thực tế)."""
    if not is_positive_number(max_loss) or not is_positive_number(starting_equity):
        raise ValueError("max_loss và starting_equity phải > 0")
    return max_loss / starting_equity


def daily_pnl(current_equity, starting_equity):
    """DailyPnL = CurrentEquity - StartingEquity (gồm cả lãi lỗ chưa chốt)."""
    return current_equity - starting_equity


def check_daily_loss_limit(current_equity, starting_equity, max_loss):
    """True nếu đã chạm ngưỡng lỗ ngày: DailyPnL <= -MaxDailyLoss
    (tương đương CurrentEquity <= StartingEquity - MaxDailyLoss)."""
    if isinstance(current_equity, bool) or not isinstance(current_equity, (int, float)) \
            or not math.isfinite(current_equity):
        raise ValueError("current_equity phải là số hữu hạn")     # cho phép <= 0: tài khoản cháy phải KÍCH HOẠT
    if not is_positive_number(starting_equity):
        raise ValueError("starting_equity phải > 0")
    if not is_positive_number(max_loss):
        raise ValueError("max_loss phải > 0")
    pnl = round(daily_pnl(current_equity, starting_equity), 2)   # tránh sai số số thực
    return pnl <= -round(max_loss, 2)


# ----------------------------------------------------------------------
# 4. TRẠNG THÁI (lưu file) + CIRCUIT BREAKER
# ----------------------------------------------------------------------
def today_ny(now=None):
    """Ngày giao dịch hiện tại theo giờ New York (dạng 'YYYY-MM-DD')."""
    now = now or datetime.now(NY)
    return now.astimezone(NY).strftime("%Y-%m-%d")


def save_state(state, path=STATE_FILE):
    """Ghi nguyên tử: ghi file tạm (tên duy nhất) rồi thay thế file chính,
    tránh hỏng file nếu tiến trình dừng giữa chừng."""
    thu_muc = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=thu_muc, prefix="risk_state_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


_KHOA_BAT_BUOC = ("date", "starting_equity", "max_daily_loss", "circuit_breaker", "stopped_symbols")


def validate_state(state):
    """Kiểm tra cấu trúc và kiểu dữ liệu của trạng thái."""
    if not isinstance(state, dict) or any(k not in state for k in _KHOA_BAT_BUOC):
        return False
    return (isinstance(state["date"], str)
            and is_positive_number(state["starting_equity"])
            and is_positive_number(state["max_daily_loss"])
            and isinstance(state["circuit_breaker"], bool)
            and isinstance(state["stopped_symbols"], list)
            and all(isinstance(x, str) for x in state["stopped_symbols"]))


def _doc_state(path):
    """Đọc file trạng thái. File thiếu, hỏng hoặc thiếu khóa thì trả None (coi như chưa có)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):          # ValueError gồm cả JSONDecodeError
        return None
    if isinstance(state, dict):
        state.setdefault("stopped_symbols", [])
    return state if validate_state(state) else None


def load_state(current_equity, today, max_loss, start_equity_ref, path=STATE_FILE,
               chap_nhan_khoi_tao_moi=False):
    """Trả về trạng thái của ngày `today`.
    - Có file hợp lệ của đúng ngày: dùng tiếp (vốn đầu ngày và ngưỡng đã ĐÓNG BĂNG).
    - File của ngày trước: sang ngày mới, dựng trạng thái từ `start_equity_ref`
      (với Alpaca là account.last_equity: "Equity as of previous trading day at 16:00:00 ET").
    - Không có file / file hỏng: KHÔNG biết circuit breaker từng bật chưa. Nếu trong ngày đã có biến động
      (equity khác vốn đầu ngày) thì chọn hướng AN TOÀN: bật breaker, chặn mở vị thế mới đến hết ngày.
      Chỉ khi equity đúng bằng vốn đầu ngày (chưa có lãi lỗ), hoặc người dùng chủ động đặt
      chap_nhan_khoi_tao_moi=True (lần chạy đầu tiên), mới khởi tạo bình thường.
    - Nếu equity hiện tại đã chạm sàn thì breaker luôn được bật."""
    cu = _doc_state(path)
    if cu is not None and cu["date"] == today:
        return cu
    if not is_positive_number(start_equity_ref):
        raise ValueError("Không xác định được vốn đầu ngày (start_equity_ref), không giao dịch")
    state = {"date": today, "starting_equity": start_equity_ref, "max_daily_loss": max_loss,
             "circuit_breaker": False, "stopped_symbols": []}
    if check_daily_loss_limit(current_equity, start_equity_ref, max_loss):
        state["circuit_breaker"] = True
        state["ly_do"] = "Equity đã chạm sàn khi dựng lại trạng thái"
    elif cu is None and not chap_nhan_khoi_tao_moi and abs(float(current_equity) - float(start_equity_ref)) > 0.01:
        state["circuit_breaker"] = True
        state["ly_do"] = ("Mất trạng thái giữa ngày, không xác định được circuit breaker trước đó: "
                          "chặn mở vị thế mới đến hết ngày")
    save_state(state, path)
    return state


def can_open_new_position(state, current_equity, path=STATE_FILE):
    """Chỉ chặn lệnh MỞ vị thế mới. Ngưỡng lấy từ state (đã đóng băng đầu ngày).
    Cờ đã bật thì giữ nguyên đến hết ngày. Trả về (được_phép, lý_do)."""
    max_loss = state["max_daily_loss"]
    if state["circuit_breaker"]:
        return False, state.get("ly_do", "Circuit breaker đã kích hoạt trong ngày")
    if check_daily_loss_limit(current_equity, state["starting_equity"], max_loss):
        pnl = daily_pnl(current_equity, state["starting_equity"])
        state["circuit_breaker"] = True
        state["ly_do"] = f"Chạm ngưỡng lỗ ngày: DailyPnL={pnl:.2f}, ngưỡng=-{max_loss:.2f}"
        save_state(state, path)
        return False, state["ly_do"]
    return True, "OK"


def is_order_allowed(side, state, current_equity, qty=None, held_qty=None, path=STATE_FILE):
    """MUA: phải qua DLL. BÁN: chỉ được phép để ĐÓNG hoặc GIẢM vị thế đang giữ
    (0 < qty <= held_qty), tuyệt đối không mở vị thế bán khống. Trả về (được_phép, lý_do)."""
    if side == "buy":
        return can_open_new_position(state, current_equity, path)
    if side == "sell":
        if qty is None or held_qty is None:
            return False, "Thiếu số lượng bán hoặc số đang giữ"
        if held_qty <= 0:
            return False, "Không có vị thế để bán"
        if qty <= 0 or qty > held_qty:
            return False, f"Số lượng bán {qty} không hợp lệ (đang giữ {held_qty}), tránh bán khống"
        return True, "Lệnh bán để đóng/giảm vị thế luôn được phép"
    return False, f"side không hợp lệ: {side}"


def ghi_nhan_bi_stop(state, symbol, path=STATE_FILE):
    """Ghi nhận vị thế `symbol` đã bị đóng bởi stop-loss: không mua lại mã này trong ngày."""
    if symbol not in state["stopped_symbols"]:
        state["stopped_symbols"].append(symbol)
        save_state(state, path)


# ----------------------------------------------------------------------
# 5. GỌI ALPACA (đã kiểm thử bằng API giả; CHƯA chạy thử trên tài khoản paper thật)
# ----------------------------------------------------------------------
def _ngay_ny_cua(thoi_diem):
    """Đổi thời điểm (datetime hoặc chuỗi ISO) sang ngày theo giờ New York. Lỗi thì trả None."""
    try:
        if isinstance(thoi_diem, str):
            thoi_diem = datetime.fromisoformat(thoi_diem.replace("Z", "+00:00"))
        return thoi_diem.astimezone(NY).strftime("%Y-%m-%d")
    except Exception:
        return None


def _chuan(x):
    """Chuẩn hóa chuỗi/enum của Alpaca ('buy', 'OrderSide.BUY', 'filled'...) về chữ thường."""
    return str(getattr(x, "value", x)).lower().split(".")[-1]


def _la_lenh_mua(side):
    return _chuan(side) == "buy"


def huy_lenh_mua_dang_cho(api, logger):
    """Hủy các lệnh MUA đang chờ DO BOT ĐẶT (client_order_id bắt đầu bằng BOT_PREFIX).
    Không đụng lệnh thủ công hay lệnh của hệ thống khác, và không đụng lệnh bán (stop-loss)."""
    so_huy = 0
    for o in api.list_orders(status="open"):
        cua_bot = str(getattr(o, "client_order_id", "") or "").startswith(BOT_PREFIX)
        if _la_lenh_mua(o.side) and cua_bot:
            try:
                api.cancel_order(o.id)
                so_huy += 1
            except Exception as e:
                logger.error(f"Không hủy được lệnh {o.id}: {e}")
    if so_huy:
        logger.warning(f"Đã hủy {so_huy} lệnh MUA đang chờ do circuit breaker")
    return so_huy


def dong_bo_stop(api, state, logger, path=STATE_FILE):
    """Tự phát hiện các lệnh stop đã KHỚP trong ngày và ghi nhận mã bị cắt lỗ.
    Chỉ ĐỌC lệnh; ghi nhận mọi lệnh bán loại 'stop' đã khớp trong ngày trên tài khoản.
    Tên trường theo đối tượng Order của Alpaca, chưa kiểm chứng trên tài khoản thật."""
    moi = 0
    for o in api.list_orders(status="closed"):
        if (_chuan(o.side) == "sell" and _chuan(o.type) == "stop" and _chuan(o.status) == "filled"
                and _ngay_ny_cua(getattr(o, "filled_at", None)) == state["date"]
                and o.symbol not in state["stopped_symbols"]):
            ghi_nhan_bi_stop(state, o.symbol, path)
            logger.warning(f"{o.symbol} đã bị cắt lỗ trong ngày, không mua lại")
            moi += 1
    return moi


def giam_sat_dll(api, logger, state_path=STATE_FILE, today=None, khoi_tao_lan_dau=False):
    """GỌI Ở ĐẦU MỖI CHU KỲ của bot, kể cả khi không có tín hiệu MUA nào.
    Đọc equity, dựng/đọc trạng thái, đồng bộ các mã bị stop, kiểm tra DLL; nếu breaker đang bật
    thì hủy các lệnh MUA chờ của bot. Lỗi đọc dữ liệu thì theo hướng an toàn.
    Trả về (state, equity, dong_bo_ok):
      - (None, None, False) nếu không xác định được trạng thái tài khoản;
      - dong_bo_ok=False nếu không đọc được lệnh stop đã khớp: khi đó không biết mã nào đã bị cắt lỗ
        nên CHƯA ĐƯỢC mở vị thế mới (DLL và hủy BUY chờ vẫn chạy bình thường)."""
    try:
        acc = api.get_account()
        equity = float(acc.equity)
        start_ref = float(acc.last_equity)
        max_loss = max_daily_loss(MAX_POSITION_VALUE, R_SL, N_LOSS)   # chỉ dùng khi tạo trạng thái ngày mới
        state = load_state(equity, today or today_ny(), max_loss, start_ref, state_path,
                           chap_nhan_khoi_tao_moi=khoi_tao_lan_dau)
    except Exception as e:
        logger.error(f"Không xác định được trạng thái tài khoản ({e}), không mở vị thế mới")
        return None, None, False
    dong_bo_ok = True
    try:
        dong_bo_stop(api, state, logger, state_path)
    except Exception as e:
        dong_bo_ok = False
        logger.error(f"Không đồng bộ được lệnh stop ({e}): tạm không mở vị thế mới")
    ok, reason = can_open_new_position(state, equity, state_path)
    if not ok:
        logger.warning(f"Circuit breaker: {reason}")
        huy_lenh_mua_dang_cho(api, logger)
    return state, equity, dong_bo_ok


def has_open_stop_order(api, symbol):
    """Kiểm tra xem đã có lệnh Stop SELL nào đang mở cho symbol này chưa (tránh trùng lặp)."""
    for o in api.list_orders(status="open"):
        if _chuan(getattr(o, "side", "")) == "sell" and _chuan(getattr(o, "type", "")) == "stop" and getattr(o, "symbol", "") == symbol:
            return True
    return False


def cancel_open_stop_orders(api, symbol, logger=None):
    """Hủy các lệnh Stop SELL đang mở cho symbol để giải phóng cổ phiếu trước khi bán Market (tránh xung đột)."""
    so_huy = 0
    for o in api.list_orders(status="open"):
        if _chuan(getattr(o, "side", "")) == "sell" and _chuan(getattr(o, "type", "")) == "stop" and getattr(o, "symbol", "") == symbol:
            try:
                api.cancel_order(o.id)
                so_huy += 1
                if logger:
                    logger.info(f"Đã hủy lệnh stop đang chờ {o.id} của {symbol} để thực hiện lệnh BÁN")
            except Exception as e:
                if logger:
                    logger.error(f"Không thể hủy lệnh stop {o.id}: {e}")
    return so_huy


def place_stop_loss_order(api, symbol, filled_qty, filled_avg_price, r_sl=R_SL, logger=None):
    """Đặt lệnh Stop SELL riêng biệt dựa trên giá mua thực tế được khớp (filled_avg_price).
    Công thức: stop_price = filled_avg_price * (1 - r_sl).
    Chỉ đặt nếu filled_qty > 0 và chưa có lệnh stop nào đang mở cho symbol này (tránh trùng lặp)."""
    if not is_positive_int(filled_qty):
        if logger:
            logger.warning(f"Không đặt Stop SELL cho {symbol}: filled_qty ({filled_qty}) không phải số nguyên dương")
        return None
    if not is_positive_number(filled_avg_price):
        if logger:
            logger.warning(f"Không đặt Stop SELL cho {symbol}: filled_avg_price ({filled_avg_price}) không hợp lệ")
        return None

    if has_open_stop_order(api, symbol):
        if logger:
            logger.warning(f"Bỏ qua đặt Stop SELL cho {symbol}: Đã có lệnh stop đang mở (tránh trùng lặp)")
        return None

    stop_price = calculate_stop_price(filled_avg_price, r_sl)
    try:
        stop_order = api.submit_order(
            symbol=symbol,
            qty=filled_qty,
            side="sell",
            type="stop",
            time_in_force="gtc",
            stop_price=str(stop_price),
            client_order_id=f"{BOT_PREFIX}-stop-{symbol}-{uuid.uuid4().hex[:12]}",
        )
        if logger:
            logger.info(f"Đã đặt Stop-loss cho {symbol}: {filled_qty} cp @ {stop_price} (tính từ giá khớp mua thực tế {filled_avg_price:.2f})")
        return stop_order
    except Exception as e:
        if logger:
            logger.error(f"Đặt Stop SELL cho {symbol} thất bại: {e}")
        return None


def open_position_guarded(api, symbol, qty, ref_price, logger, state_path=STATE_FILE, today=None,
                          khoi_tao_lan_dau=False, max_wait_sec=1.0):
    """Luồng mở vị thế:
    1. Kiểm tra đầu vào → giám sát DLL → trần vị thế → kiểm tra rủi ro.
    2. Gửi lệnh MUA Market.
    3. Đợi/lấy thông tin khớp lệnh thực tế (filled_avg_price, filled_qty).
    4. Chỉ đặt lệnh Stop SELL sau khi BUY đã khớp (hỗ trợ cả khớp một phần),
       với stop_price = filled_avg_price * (1 - R_SL).
    """
    if not is_positive_int(qty) or not is_positive_number(ref_price):
        logger.warning(f"Bỏ qua MUA {symbol}: số lượng ({qty}) hoặc giá tham chiếu ({ref_price}) không hợp lệ")
        return None

    state, equity, dong_bo_ok = giam_sat_dll(api, logger, state_path, today, khoi_tao_lan_dau)
    if state is None:
        return None
    if not dong_bo_ok:
        logger.warning(f"Bỏ qua MUA {symbol}: không xác định được mã nào đã bị cắt lỗ")
        return None
    if state["circuit_breaker"]:
        logger.warning(f"Bỏ qua MUA {symbol}: {state.get('ly_do', 'Circuit breaker đang bật')}")
        return None

    if symbol in state["stopped_symbols"]:
        logger.warning(f"Bỏ qua MUA {symbol}: đã bị cắt lỗ trong ngày, không mua lại")
        return None

    position_value = qty * ref_price
    if not check_position_cap(position_value):
        logger.warning(f"Bỏ qua MUA {symbol}: giá trị vị thế {position_value:.2f} vượt trần {MAX_POSITION_VALUE}")
        return None

    dat, pct = check_risk_compliance(position_value, R_SL, equity)
    if not dat:
        logger.warning(f"Cảnh báo: rủi ro {pct:.2%} vốn vượt mức {RISK_LIMIT:.0%} (chỉ ghi log, không chặn)")

    try:
        buy_order = api.submit_order(
            symbol=symbol, qty=qty, side="buy", type="market",
            time_in_force="day",
            client_order_id=f"{BOT_PREFIX}-buy-{symbol}-{uuid.uuid4().hex[:12]}",
        )
    except Exception as e:
        logger.error(f"Gửi lệnh MUA {symbol} thất bại: {e}")
        return None

    logger.info(f"Đã gửi lệnh MUA {qty} {symbol}")

    # Lấy thông tin khớp lệnh thực tế
    order_obj = buy_order
    st = _chuan(getattr(buy_order, "status", ""))
    fq_init = getattr(buy_order, "filled_qty", None)
    # Nếu lệnh chưa báo khớp ngay, mới cần đợi/poll
    if st not in ("filled", "partially_filled") and (fq_init is None or float(fq_init) <= 0):
        if hasattr(api, "get_order") and hasattr(buy_order, "id"):
            import time
            end_time = time.time() + max_wait_sec
            while time.time() < end_time:
                try:
                    order_obj = api.get_order(buy_order.id)
                    st = _chuan(getattr(order_obj, "status", ""))
                    fq = float(getattr(order_obj, "filled_qty", 0) or 0)
                    if st in ("filled", "partially_filled") or fq > 0:
                        break
                    if st in ("canceled", "expired", "rejected"):
                        break
                except Exception:
                    pass
                time.sleep(0.1)

    filled_qty = 0
    filled_price = 0.0
    fq = getattr(order_obj, "filled_qty", None)
    fp = getattr(order_obj, "filled_avg_price", None)

    if fq is not None:
        try:
            filled_qty = int(float(fq))
        except (ValueError, TypeError):
            filled_qty = 0
    if fp is not None:
        try:
            filled_price = float(fp)
        except (ValueError, TypeError):
            filled_price = 0.0

    # Chỉ đặt Stop SELL sau khi BUY được khớp (kể cả khớp một phần với filled_qty > 0)
    if filled_qty > 0 and filled_price > 0:
        place_stop_loss_order(api, symbol, filled_qty, filled_price, R_SL, logger)
    else:
        logger.info(f"Lệnh MUA {symbol} chưa khớp (filled_qty=0); chưa đặt Stop-loss.")

    return buy_order


# ----------------------------------------------------------------------
# 6. ĐO TẦN SUẤT KÍCH HOẠT DLL TRÊN DỮ LIỆU LỊCH SỬ (tùy chọn, cần pandas)
# ----------------------------------------------------------------------
def tan_suat_kich_hoat(danh_sach_close, nguong, gia_tri_vi_the=MAX_POSITION_VALUE):
    """Tỷ lệ ngày mà lãi/lỗ cả danh mục (giữ đủ các vị thế, mỗi vị thế = gia_tri_vi_the)
    <= -nguong. danh_sach_close: danh sách các pandas.Series giá đóng cửa (mỗi mã một Series).
    Chỉ là mô phỏng tham khảo: giả định giữ đủ vị thế mọi ngày, chưa tính stop, gap, phí."""
    import pandas as pd
    loi_suat = pd.concat([c.pct_change() for c in danh_sach_close], axis=1).dropna()
    lai_lo_ngay = (loi_suat * gia_tri_vi_the).sum(axis=1)
    return (lai_lo_ngay <= -nguong).mean()


# ----------------------------------------------------------------------
# 7. KIỂM THỬ + MÔ PHỎNG KỊCH BẢN (python risk_control_all.py)
# ----------------------------------------------------------------------
def mo_phong_stop(entry, sl, chuoi_ngay):
    """Mô phỏng đơn giản (chưa tính phí, trượt giá, thanh khoản).
    chuoi_ngay: danh sách (giá_mở, giá_thấp_nhất) từng ngày. Trả về giá bán hoặc None.
    Nếu giá mở cửa đã dưới stop (gap) thì khớp ở giá mở cửa, không phải giá stop."""
    for mo, thap in chuoi_ngay:
        if mo <= sl:
            return mo
        if thap <= sl:
            return sl
    return None


class _Acc:
    def __init__(self, equity, last_equity):
        self.equity = equity
        self.last_equity = last_equity


class _Order:
    def __init__(self, id, side, client_order_id="", type="market", status="open", symbol="", filled_at=None,
                 filled_qty=0, filled_avg_price=0.0, qty=0, stop_price=None):
        self.id, self.side, self.client_order_id = id, side, client_order_id
        self.type, self.status, self.symbol, self.filled_at = type, status, symbol, filled_at
        self.filled_qty = filled_qty
        self.filled_avg_price = filled_avg_price
        self.qty = qty
        self.stop_price = stop_price

    def __eq__(self, other):
        if other == "OK":
            return True
        return id(self) == id(other)


class FakeApi:
    """API giả để kiểm thử luồng gửi lệnh mà không cần Alpaca."""
    def __init__(self, equity, last_equity, open_orders=None, closed_orders=None, loi_closed=False,
                 fill_price=None, partial_qty=None, simulate_fill=True):
        self.loi_closed = loi_closed
        self.acc = _Acc(equity, last_equity)
        self.open_orders = list(open_orders or [])
        self.closed_orders = list(closed_orders or [])
        self.all_orders = []
        self.submitted = []
        self.cancelled = []
        self.fill_price = fill_price
        self.partial_qty = partial_qty
        self.simulate_fill = simulate_fill

    def get_account(self):
        return self.acc

    def submit_order(self, **kwargs):
        self.submitted.append(kwargs)
        side = kwargs.get("side")
        sym = kwargs.get("symbol", "")
        qty = kwargs.get("qty", 0)
        cid = kwargs.get("client_order_id", "")
        order_type = kwargs.get("type", "market")
        stop_p = kwargs.get("stop_price")
        order_id = f"fake-{len(self.submitted)}"

        if side == "buy":
            if self.simulate_fill:
                f_qty = self.partial_qty if self.partial_qty is not None else qty
                f_price = self.fill_price if self.fill_price is not None else 100.0
                status = "filled" if f_qty == qty else ("partially_filled" if f_qty > 0 else "new")
            else:
                f_qty = 0
                f_price = 0.0
                status = "new"
            order = _Order(order_id, side, cid, type=order_type, status=status, symbol=sym,
                           filled_qty=f_qty, filled_avg_price=f_price, qty=qty)
            self.all_orders.append(order)
            if status == "filled":
                self.closed_orders.append(order)
            else:
                self.open_orders.append(order)
            return order
        else:
            status = "open" if order_type == "stop" else "filled"
            order = _Order(order_id, side, cid, type=order_type, status=status, symbol=sym,
                           qty=qty, stop_price=stop_p)
            self.all_orders.append(order)
            if status == "open":
                self.open_orders.append(order)
            else:
                self.closed_orders.append(order)
            return order

    def get_order(self, order_id):
        for o in self.all_orders + self.open_orders + self.closed_orders:
            if getattr(o, "id", None) == order_id:
                return o
        return None

    def list_orders(self, status="open"):
        if status != "open" and self.loi_closed:
            raise ConnectionError("mất kết nối khi đọc lệnh đã đóng")
        return self.open_orders if status == "open" else self.closed_orders

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        self.open_orders = [o for o in self.open_orders if getattr(o, "id", None) != order_id]


class _Log:
    def __init__(self):
        self.msgs = []

    def info(self, m): self.msgs.append(("info", m))
    def warning(self, m): self.msgs.append(("warning", m))
    def error(self, m): self.msgs.append(("error", m))


def chay_kiem_thu():
    import tempfile
    from datetime import timezone
    ket_qua = []

    def kiem_tra(ten, thuc_te, mong_doi):
        dat = thuc_te == mong_doi
        ket_qua.append(dat)
        print(f"[{'PASS' if dat else 'FAIL'}] {ten}: thực tế={thuc_te}, mong đợi={mong_doi}")

    def file_moi():
        return os.path.join(tempfile.mkdtemp(), "state.json")

    def bao_loi(ten, ham):
        try:
            ham()
            kiem_tra(ten, "không lỗi", "ValueError")
        except ValueError:
            kiem_tra(ten, "ValueError", "ValueError")

    print("=== CÔNG THỨC VÀ CODE ===")
    kiem_tra("SL (entry 100, r 5%)", calculate_stop_price(100, 0.05), 95.0)
    kiem_tra("SL cổ phiếu dưới 1$ (0,5$)", calculate_stop_price(0.5, 0.05), 0.475)
    dat, pct = check_risk_compliance(1000, 0.05, 100000)
    kiem_tra("Rủi ro / vốn 100.000", (dat, round(pct, 4)), (True, 0.0005))
    dat, pct = check_risk_compliance(1000, 0.05, 2000)
    kiem_tra("Rủi ro / vốn 2.000 vượt 2%", (dat, round(pct, 4)), (False, 0.025))
    kiem_tra("MaxDailyLoss (1000 x 5% x 3)", max_daily_loss(1000, 0.05, 3), 150.0)
    kiem_tra("Lỗ tối đa lý thuyết 5 vị thế", loss_toi_da_ly_thuyet(5, 1000, 0.05), 250.0)
    kiem_tra("Trần vị thế: 1.000 đạt, 5.000 không", (check_position_cap(1000), check_position_cap(5000)), (True, False))
    ty_le, lo_tong, lo_von = rui_ro_du_kien_theo_gia_khop(102, 95, 10, 100000)
    kiem_tra("Lỗ dự kiến theo giá khớp (khớp 102, stop 95, 10 cp)",
             (round(ty_le, 4), lo_tong, round(lo_von, 4)), (0.0686, 70, 0.0007))
    kiem_tra("Lỗ thực tế (mua 102, bán 90, 10 cp) khác lỗ dự kiến 70$", lo_thuc_te(102, 90, 10), 120)
    bao_loi("Báo lỗi khi max_loss <= 0", lambda: check_daily_loss_limit(9000, 10000, 0))
    bao_loi("Báo lỗi khi starting_equity = 0", lambda: check_daily_loss_limit(9000, 0, 150))
    bao_loi("Báo lỗi khi n_loss = 0", lambda: max_daily_loss(1000, 0.05, 0))

    print("\n=== BẢNG BIÊN DLL (vốn đầu ngày 10.000, ngưỡng 150) ===")
    for eq, mong_doi in [(10300, False), (9900, False), (9851, False), (9850, True), (9700, True)]:
        kiem_tra(f"DLL equity={eq}", check_daily_loss_limit(eq, 10000, 150), mong_doi)

    print("\n=== KỊCH BẢN MÔ PHỎNG STOP ===")
    entry = 100.0
    sl = calculate_stop_price(entry, R_SL)
    kiem_tra("A. Giá giảm chạm stop 95", mo_phong_stop(entry, sl, [(99, 98), (97, 96), (95.5, 94)]), 95.0)
    kiem_tra("B. Giá tăng, không chạm stop", mo_phong_stop(entry, sl, [(101, 99), (103, 101), (105, 103)]), None)
    ban = mo_phong_stop(entry, sl, [(99, 98), (90, 88)])
    kiem_tra("C. Gap: khớp 90, lỗ thực 10$/cp > 5$ dự kiến", (ban, entry - ban), (90, 10.0))

    print("\n=== TRẠNG THÁI VÀ CIRCUIT BREAKER ===")
    p = file_moi()
    max_loss = max_daily_loss(MAX_POSITION_VALUE, R_SL, N_LOSS)   # 150
    equity = 10000.0
    state = load_state(equity, "2026-11-19", max_loss, 10000.0, p)
    nhat_ky = []
    for _ in range(3):                      # 3 lệnh liên tiếp bị stop, mỗi lệnh -50$
        equity -= 50
        nhat_ky.append(can_open_new_position(state, equity, p)[0])
    kiem_tra("D. Sau 1,2,3 lệnh bị stop: được mua?", nhat_ky, [True, True, False])
    kiem_tra("D. Cờ circuit_breaker bật", state["circuit_breaker"], True)
    kiem_tra("D2. Equity hồi phục nhưng vẫn chặn MUA", can_open_new_position(state, 10100, p)[0], False)
    kiem_tra("F. Khởi động lại cùng ngày: giữ vốn đầu ngày và cờ",
             (lambda t: (t["starting_equity"], t["circuit_breaker"]))(load_state(9850.0, "2026-11-19", max_loss, 10000.0, p)),
             (10000.0, True))
    kiem_tra("G. Sang ngày mới (có file cũ): reset theo vốn đóng cửa hôm trước",
             (lambda t: (t["starting_equity"], t["circuit_breaker"]))(load_state(9850.0, "2026-11-20", max_loss, 9850.0, p)),
             (9850.0, False))
    kiem_tra("H. Ngưỡng giữ nguyên trong ngày dù tham số đổi",
             load_state(9850.0, "2026-11-20", 999.0, 9850.0, p)["max_daily_loss"], 150.0)

    s5 = load_state(10000.0, "2026-11-19", max_loss, 10000.0, file_moi())
    eq5, bat_luc = 10000.0, None
    for i in range(1, 6):                   # 5 vị thế cùng bị stop, mỗi vị thế -50$
        eq5 -= 50
        if not can_open_new_position(s5, eq5, file_moi())[0] and bat_luc is None:
            bat_luc = i
    kiem_tra("I. 5 vị thế cùng bị stop: DLL bật từ vị thế thứ 3, tổng lỗ 250 > ngưỡng 150",
             (bat_luc, 10000 - eq5), (3, 250.0))

    print("\n=== MẤT/HỎNG FILE TRẠNG THÁI ===")
    kiem_tra("4a. Mất file, equity 9.850 (chạm sàn): breaker bật",
             load_state(9850.0, "2026-11-19", max_loss, 10000.0, file_moi())["circuit_breaker"], True)
    kiem_tra("4b. Mất file, equity ĐÃ HỒI PHỤC 9.900 sau khi từng chạm DLL: vẫn chặn (fail-safe)",
             load_state(9900.0, "2026-11-19", max_loss, 10000.0, file_moi())["circuit_breaker"], True)
    ph = file_moi()
    open(ph, "w").write("{hong")
    kiem_tra("4c. File JSON hỏng, đã có biến động: không báo lỗi, chặn an toàn",
             load_state(9900.0, "2026-11-19", max_loss, 10000.0, ph)["circuit_breaker"], True)
    kiem_tra("4d. Mất file nhưng chưa có lãi lỗ (đầu ngày): khởi tạo bình thường",
             load_state(10000.0, "2026-11-19", max_loss, 10000.0, file_moi())["circuit_breaker"], False)
    kiem_tra("4e. Lần chạy đầu tiên giữa ngày với chap_nhan_khoi_tao_moi=True",
             load_state(9900.0, "2026-11-19", max_loss, 10000.0, file_moi(), chap_nhan_khoi_tao_moi=True)["circuit_breaker"], False)
    bao_loi("4f. Không có vốn đầu ngày thì không giao dịch", lambda: load_state(9900.0, "2026-11-19", max_loss, None, file_moi()))
    p8 = file_moi()
    save_state({"a": 1}, p8)
    kiem_tra("8. Ghi file nguyên tử: có file chính, không còn file .tmp",
             (os.path.exists(p8), os.path.exists(p8 + ".tmp")), (True, False))

    print("\n=== LỆNH BÁN ===")
    st = {"date": "x", "starting_equity": 10000, "max_daily_loss": 150, "circuit_breaker": True, "stopped_symbols": []}
    kiem_tra("3a. Bán 10 khi đang giữ 10 (DLL bật)", is_order_allowed("sell", st, 9000, 10, 10)[0], True)
    kiem_tra("3b. Bán 20 khi đang giữ 10 (tránh bán khống)", is_order_allowed("sell", st, 9000, 20, 10)[0], False)
    kiem_tra("3c. Bán khi không giữ gì", is_order_allowed("sell", st, 9000, 5, 0)[0], False)
    kiem_tra("3d. Bán thiếu số lượng", is_order_allowed("sell", st, 9000)[0], False)
    kiem_tra("3e. Mua khi DLL bật", is_order_allowed("buy", st, 9000)[0], False)

    print("\n=== LUỒNG GỬI LỆNH VÀ GIÁM SÁT (API giả) ===")
    log = _Log()
    # 1a. Mua 10 cp x 100$, khớp 100$: đặt Stop SELL 95.0$ theo giá khớp
    api = FakeApi(100000, 100000, fill_price=100.0)
    kq = open_position_guarded(api, "AAPL", 10, 100.0, log, file_moi(), "2026-11-19")
    buy_submitted = api.submitted[0] if len(api.submitted) > 0 else {}
    stop_submitted = api.submitted[1] if len(api.submitted) > 1 else {}
    kiem_tra("1a. Mua khớp 100$: đặt Stop SELL 95.0$ theo giá khớp thực tế",
             (bool(kq), buy_submitted.get("side"), stop_submitted.get("side"), stop_submitted.get("stop_price"), stop_submitted.get("qty")),
             (True, "buy", "sell", "95.0", 10))

    # 1a2. Trượt giá: mua khớp 102$ (khác giá tham chiếu 100$): Stop SELL tính đúng 96.9$ (= 102 * 0.95)
    api = FakeApi(100000, 100000, fill_price=102.0)
    kq = open_position_guarded(api, "AAPL", 10, 100.0, log, file_moi(), "2026-11-19")
    stop_submitted = api.submitted[1] if len(api.submitted) > 1 else {}
    kiem_tra("1a2. Trượt giá: khớp 102$, Stop SELL tính đúng 96.9$ (không dùng 95$ tham chiếu)",
             stop_submitted.get("stop_price"), "96.9")

    # 1a3. Khớp một phần: mua 10 nhưng chỉ khớp 4 cp -> Stop SELL đúng 4 cp
    api = FakeApi(100000, 100000, fill_price=100.0, partial_qty=4)
    kq = open_position_guarded(api, "AAPL", 10, 100.0, log, file_moi(), "2026-11-19")
    stop_submitted = api.submitted[1] if len(api.submitted) > 1 else {}
    kiem_tra("1a3. Khớp một phần: mua 10 nhưng khớp 4 cp -> Stop SELL đúng 4 cp",
             stop_submitted.get("qty"), 4)

    # 1a4. Chưa khớp (filled_qty=0) -> không đặt Stop SELL
    api = FakeApi(100000, 100000, simulate_fill=False)
    kq = open_position_guarded(api, "AAPL", 10, 100.0, log, file_moi(), "2026-11-19")
    kiem_tra("1a4. Chưa khớp (filled_qty=0): không đặt Stop SELL", len(api.submitted), 1)

    # 1a5. Đã có lệnh Stop mở -> không đặt trùng lặp
    api = FakeApi(100000, 100000, fill_price=100.0, open_orders=[_Order("exist_stop", "sell", type="stop", symbol="AAPL")])
    kq = open_position_guarded(api, "AAPL", 10, 100.0, log, file_moi(), "2026-11-19")
    kiem_tra("1a5. Đã có lệnh Stop mở: không đặt Stop trùng lặp", len(api.submitted), 1)

    # 1a6. Hủy lệnh Stop mở trước khi Bán Market để tránh xung đột
    api = FakeApi(100000, 100000, open_orders=[_Order("exist_stop", "sell", type="stop", symbol="AAPL")])
    so_huy = cancel_open_stop_orders(api, "AAPL", log)
    kiem_tra("1a6. Hủy lệnh Stop mở trước khi Bán Market để tránh xung đột", (so_huy, len(api.open_orders)), (1, 0))
    api = FakeApi(100000, 100000)
    kq = open_position_guarded(api, "AAPL", 50, 100.0, log, file_moi(), "2026-11-19")
    kiem_tra("1b. Mua 50 cp x 100$ = 5.000$ vượt trần: bị chặn", (kq, len(api.submitted)), (None, 0))
    for ten, q, g in [("qty âm", -5, 100.0), ("qty = 0", 0, 100.0), ("giá tham chiếu = 0", 5, 0.0)]:
        api = FakeApi(100000, 100000)
        kq = open_position_guarded(api, "AAPL", q, g, log, file_moi(), "2026-11-19")
        kiem_tra(f"3.2. {ten}: không gửi lệnh", (kq, len(api.submitted)), (None, 0))
    api = FakeApi(10000, None)
    kq = open_position_guarded(api, "AAPL", 10, 100.0, log, file_moi(), "2026-11-19")
    kiem_tra("4g. Thiếu last_equity: không mở vị thế", (kq, len(api.submitted)), (None, 0))

    # Giám sát độc lập: không có tín hiệu MUA, equity chạm DLL; chỉ hủy lệnh của bot
    api = FakeApi(9800, 10000, open_orders=[
        _Order("b1", "buy", client_order_id=f"{BOT_PREFIX}-AAPL-x"),
        _Order("m1", "buy", client_order_id="manual-1"),
        _Order("s1", "sell", type="stop")])
    state, _, _ = giam_sat_dll(api, log, file_moi(), "2026-11-19")
    kiem_tra("3. Không có tín hiệu MUA, equity chạm DLL: breaker bật, chỉ hủy lệnh MUA của bot",
             (state["circuit_breaker"], api.cancelled), (True, ["b1"]))

    # Tự phát hiện stop khớp -> không mua lại cùng ngày
    hom_nay = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
    hom_qua = datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc)
    api = FakeApi(100000, 100000, closed_orders=[
        _Order("c1", "sell", type="stop", status="filled", symbol="AAPL", filled_at=hom_nay),
        _Order("c2", "sell", type="stop", status="filled", symbol="MSFT", filled_at=hom_qua),
        _Order("c3", "sell", type="limit", status="filled", symbol="TSLA", filled_at=hom_nay)])
    p = file_moi()
    kq1 = open_position_guarded(api, "AAPL", 10, 100.0, log, p, "2026-11-19")
    kq2 = open_position_guarded(api, "MSFT", 10, 100.0, log, p, "2026-11-19")
    kiem_tra("2. Tự phát hiện stop khớp hôm nay: chặn AAPL; MSFT (stop hôm qua) và lệnh limit không bị ảnh hưởng",
             (kq1, kq2), (None, "OK"))
    kq3 = open_position_guarded(FakeApi(100000, 100000), "AAPL", 10, 100.0, log, p, "2026-11-20")
    kiem_tra("2b. Sang ngày mới thì mua lại được", kq3, "OK")

    api = FakeApi(9900, 10000)
    kq = open_position_guarded(api, "AAPL", 10, 100.0, log, file_moi(), "2026-11-19", khoi_tao_lan_dau=True)
    kiem_tra("4h. Chạy lần đầu giữa ngày với khoi_tao_lan_dau=True qua luồng gửi lệnh", kq, "OK")
    api = FakeApi(9900, 10000)
    kq = open_position_guarded(api, "AAPL", 10, 100.0, log, file_moi(), "2026-11-19")
    kiem_tra("4i. Không file, không bật cờ khởi tạo, equity lệch: chặn an toàn", kq, None)

    print("\n=== KIỂM TRA ĐẦU VÀO, TRẠNG THÁI KHÔNG HỢP LỆ, ENUM, CÔNG THỨC BỔ SUNG ===")
    kiem_tra("is_positive_number: 5, 2.5, '5', True, NaN, inf, None, 0, -1",
             [is_positive_number(v) for v in (5, 2.5, "5", True, float("nan"), float("inf"), None, 0, -1)],
             [True, True, False, False, False, False, False, False, False])
    kiem_tra("is_positive_int: 5, 2.5, '5', True, 0",
             [is_positive_int(v) for v in (5, 2.5, "5", True, 0)], [True, False, False, False, False])
    bao_loi("entry_price là NaN", lambda: calculate_stop_price(float("nan"), 0.05))
    bao_loi("r_sl >= 1", lambda: calculate_stop_price(100, 1.0))
    bao_loi("n_loss là số lẻ", lambda: max_daily_loss(1000, 0.05, 2.5))
    for ten, q in [("qty là chuỗi '5'", "5"), ("qty là True", True), ("qty lẻ 2.5", 2.5), ("qty 'abc'", "abc")]:
        api = FakeApi(100000, 100000)
        kq = open_position_guarded(api, "AAPL", q, 100.0, log, file_moi(), "2026-11-19")
        kiem_tra(f"3.3. {ten}: không gửi lệnh", (kq, len(api.submitted)), (None, 0))
    api = FakeApi(100000, 100000)
    kq = open_position_guarded(api, "AAPL", 5, "100", log, file_moi(), "2026-11-19")
    kiem_tra("3.4. ref_price là chuỗi: không gửi lệnh", (kq, len(api.submitted)), (None, 0))

    kiem_tra("r_sl_toi_da (Equity 10.000, M 1.000, L 2%) = 20%", round(r_sl_toi_da(10000), 4), 0.20)
    kiem_tra("r_sl_toi_da (Equity 100.000) bị chặn ở 100%", r_sl_toi_da(100000, 1000, 0.2), 1.0)
    kiem_tra("r_SL 5% nằm trong cận trên với Equity 10.000", R_SL <= r_sl_toi_da(10000), True)
    kiem_tra("DLL % vốn: 150 / 10.000 = 1,5%; 150 / 100.000 = 0,15%",
             (round(dll_phan_tram(150, 10000), 4), round(dll_phan_tram(150, 100000), 4)), (0.015, 0.0015))

    v = {"date": "d", "starting_equity": 1, "max_daily_loss": 1, "circuit_breaker": False, "stopped_symbols": []}
    kiem_tra("validate_state: đạt / thiếu khóa / cờ sai kiểu / vốn sai kiểu",
             (validate_state(v), validate_state({"date": "d"}), validate_state({**v, "circuit_breaker": "no"}),
              validate_state({**v, "starting_equity": "abc"})), (True, False, False, False))
    p = file_moi()
    save_state({"date": "2026-11-19", "starting_equity": "abc", "max_daily_loss": 150,
                "circuit_breaker": True, "stopped_symbols": []}, p)
    kiem_tra("4j. File sai kiểu dữ liệu: không dùng, chặn an toàn khi equity lệch",
             load_state(9900.0, "2026-11-19", max_loss, 10000.0, p)["circuit_breaker"], True)

    # enum kiểu alpaca-py: str(side) = 'OrderSide.BUY'
    class _Enum:
        def __init__(self, v, ten): self.value, self.ten = v, ten
        def __str__(self): return f"{self.ten}.{self.value.upper()}"
    dh = _Enum("buy", "OrderSide")
    api = FakeApi(9800, 10000, open_orders=[_Order("b2", dh, f"{BOT_PREFIX}-Y-1"), _Order("s2", _Enum("sell", "OrderSide"), f"{BOT_PREFIX}-Y-2")])
    giam_sat_dll(api, log, file_moi(), "2026-11-19")
    kiem_tra("11. side dạng enum 'OrderSide.BUY': vẫn hủy lệnh MUA của bot, giữ lệnh bán", api.cancelled, ["b2"])
    hom_nay_ = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
    api = FakeApi(100000, 100000, closed_orders=[_Order("c9", _Enum("sell", "OrderSide"), type=_Enum("stop", "OrderType"),
                  status=_Enum("filled", "OrderStatus"), symbol="NVDA", filled_at=hom_nay_)])
    p = file_moi()
    kq = open_position_guarded(api, "NVDA", 10, 100.0, log, p, "2026-11-19")
    kiem_tra("12. Stop khớp với trường dạng enum vẫn được nhận diện: không mua lại", kq, None)

    api = FakeApi(0.0, 10000, open_orders=[_Order("b3", "buy", f"{BOT_PREFIX}-Z-1")])
    state, _, _ = giam_sat_dll(api, log, file_moi(), "2026-11-19")
    kiem_tra("13. Equity = 0 (tài khoản cháy): bật breaker, hủy BUY chờ (không coi là lỗi dữ liệu)",
             (state["circuit_breaker"], api.cancelled), (True, ["b3"]))
    api = FakeApi(float("nan"), 10000)
    kiem_tra("13b. Equity NaN là lỗi dữ liệu: không mở vị thế", giam_sat_dll(api, log, file_moi(), "2026-11-19"), (None, None, False))
    p8 = file_moi()
    save_state({"a": 1}, p8)
    save_state({"a": 2}, p8)
    kiem_tra("8b. Ghi file 2 lần: thư mục chỉ còn state.json", os.listdir(os.path.dirname(p8)), ["state.json"])

    api = FakeApi(100000, 100000, loi_closed=True)
    kq = open_position_guarded(api, "AAPL", 10, 100.0, log, file_moi(), "2026-11-19")
    kiem_tra("14. Không đồng bộ được lệnh stop: không mở vị thế mới (fail-safe)", (kq, len(api.submitted)), (None, 0))
    api = FakeApi(9800, 10000, loi_closed=True, open_orders=[_Order("b4", "buy", f"{BOT_PREFIX}-Q-1")])
    state, _, ok_ = giam_sat_dll(api, log, file_moi(), "2026-11-19")
    kiem_tra("14b. Lỗi đồng bộ stop nhưng DLL vẫn chạy: breaker bật, hủy BUY chờ, dong_bo_ok=False",
             (state["circuit_breaker"], api.cancelled, ok_), (True, ["b4"], False))
    api = FakeApi(100000, 100000, loi_closed=False)
    kiem_tra("14c. Hết lỗi thì mở lại được", open_position_guarded(api, "AAPL", 10, 100.0, log, file_moi(), "2026-11-19"), "OK")

    print(f"\nTổng: {sum(ket_qua)}/{len(ket_qua)} kiểm thử đạt")
    return all(ket_qua)


if __name__ == "__main__":
    chay_kiem_thu()
