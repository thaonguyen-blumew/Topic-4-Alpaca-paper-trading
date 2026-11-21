"""
run_with_risk.py: Chạy bot Alpaca tích hợp module quản trị rủi ro risk_control_all.py (Stop-loss + DLL).
KHÔNG sửa đổi bất kỳ dòng nào của code gốc.
"""
import argparse
import logging
import os
import sys
from typing import List, Optional

from dotenv import load_dotenv

from config import TradingConfig
from logging_setup import setup_logging
from models import TradingSignal
from risk_control_all import (
    MAX_POSITION_VALUE,
    R_SL,
    STATE_FILE,
    cancel_open_stop_orders,
    giam_sat_dll,
    is_order_allowed,
    is_positive_int,
    open_position_guarded,
    place_stop_loss_order,
)
from trading_bot import TradingBot


class RiskGuardedTradingBot(TradingBot):
    """Lớp con bọc TradingBot gốc, tích hợp Stop-loss (lệnh OTO) và DLL (Daily Loss Limit)."""

    def __init__(
        self,
        config: TradingConfig,
        api=None,
        dry_run: bool = False,
        use_bollinger: bool = False,
        ignore_market_hours: bool = False,
        khoi_tao_lan_dau: bool = False,
        state_path: str = STATE_FILE,
    ):
        # Kiểm tra MAX_POSITION_VALUE của module rủi ro khớp với config.max_position_size gốc
        if config.max_position_size != MAX_POSITION_VALUE:
            raise ValueError(
                f"Lệch cấu hình: config.max_position_size ({config.max_position_size}) "
                f"!= MAX_POSITION_VALUE ({MAX_POSITION_VALUE}) trong risk_control_all.py. Từ chối chạy."
            )

        super().__init__(
            config,
            api=api,
            dry_run=dry_run,
            use_bollinger=use_bollinger,
            ignore_market_hours=ignore_market_hours,
        )
        self.khoi_tao_lan_dau = khoi_tao_lan_dau
        self.state_path = state_path
        self.current_state = None
        self.current_equity = None
        self.dong_bo_ok = False

    def run_trading_cycle(self) -> None:
        """Thực hiện một chu kỳ giao dịch có bảo vệ rủi ro:
        1. Gọi giam_sat_dll(...) ngay ở đầu chu kỳ, bất kể có tín hiệu hay không.
        2. Cờ khoi_tao_lan_dau chỉ dùng đúng 1 lần cho chu kỳ đầu tiên.
        3. Tiếp tục quét tín hiệu và thực thi theo logic kiểm soát rủi ro.
        """
        state, equity, dong_bo_ok = giam_sat_dll(
            self.api,
            self.logger,
            state_path=self.state_path,
            khoi_tao_lan_dau=self.khoi_tao_lan_dau,
        )
        # Tắt cờ khởi tạo lần đầu sau lần gọi đầu tiên
        self.khoi_tao_lan_dau = False
        self.current_state = state
        self.current_equity = equity
        self.dong_bo_ok = dong_bo_ok

        if not self.ignore_market_hours and not self.is_market_open():
            self.logger.info("Thi truong dong cua - tam dung giao dich")
            return

        for symbol in self.config.symbols:
            data = self.data_manager.get_historical_data(symbol, self.config.history_days)
            if data.empty:
                continue
            signals: List[TradingSignal] = []
            for strategy in self.strategies:
                for s in strategy.generate_signals(data, symbol):
                    self.logger.info(
                        "[%s] %s %s conf=%.2f | %s",
                        strategy.name,
                        s.action,
                        symbol,
                        s.confidence,
                        s.reason,
                    )
                    signals.append(s)
            self._process_signals(symbol, signals)

    def execute_signal_group(self, symbol: str, action: str, signals: List[TradingSignal]) -> bool:
        """Thực thi nhóm tín hiệu với cơ chế Stop-loss và DLL:
        - MUA: tính qty bằng logic bot gốc, qua open_position_guarded(...) đặt lệnh OTO.
        - BÁN: giữ logic gốc nhưng kiểm tra qua is_order_allowed('sell', ...).
        """
        avg_confidence = sum(s.confidence for s in signals) / len(signals)
        if avg_confidence < self.config.min_confidence:
            self.logger.info("Skipping %s for %s due to low confidence", action, symbol)
            return False

        current_qty = self.portfolio_manager.get_positions().get(symbol, 0)
        price = signals[0].price

        if action == "BUY" and current_qty <= 0:
            # 1. Bot gốc tự tính số lượng
            qty = self.portfolio_manager.calculate_position_size(price)

            # 2. Ràng buộc: qty phải là số nguyên dương
            if not is_positive_int(qty):
                self.logger.warning("Bỏ qua MUA %s: qty=%s không phải số nguyên dương", symbol, qty)
                return False

            # Chế độ chạy thử khô (dry-run)
            if self.order_manager.dry_run:
                stop_price = round(price * (1 - R_SL), 2 if price >= 1 else 4)
                self.logger.info(
                    "[DRY-RUN] Mua được bảo vệ: MUA %d %s ~ %.2f (Stop-loss tính theo giá khớp: ~%.2f)",
                    qty,
                    symbol,
                    price,
                    stop_price,
                )
                return True

            # 3. Lệnh MUA đi qua open_position_guarded (gửi lệnh MUA, sau khi khớp đặt Stop SELL theo filled_avg_price)
            order = open_position_guarded(
                self.api,
                symbol,
                qty,
                price,
                self.logger,
                state_path=self.state_path,
                khoi_tao_lan_dau=False,
            )
            return order is not None

        if action == "SELL" and current_qty > 0:
            held_qty = current_qty
            sell_qty = int(current_qty)

            # Đảm bảo có state và equity trước khi kiểm tra
            if self.current_state is None:
                self.current_state, self.current_equity, self.dong_bo_ok = giam_sat_dll(
                    self.api, self.logger, state_path=self.state_path
                )

            allowed, reason = is_order_allowed(
                "sell",
                self.current_state,
                self.current_equity,
                qty=sell_qty,
                held_qty=held_qty,
                path=self.state_path,
            )
            if not allowed:
                self.logger.warning("Bỏ qua SELL %s: %s", symbol, reason)
                return False

            # QUAN TRỌNG: Hủy các lệnh Stop SELL đang mở cho symbol này để giải phóng cổ phiếu bị khóa (tránh xung đột)
            cancel_open_stop_orders(self.api, symbol, self.logger)

            return self.order_manager.place_market_order(symbol, sell_qty, "sell")

        self.logger.info("Bo qua %s %s (vi the hien tai: %s)", action, symbol, current_qty)
        return False


def main() -> None:
    p = argparse.ArgumentParser(description="Alpaca Paper Trading Bot with Stop-loss + DLL")
    p.add_argument("--once", action="store_true", help="Chạy 1 chu kỳ rồi thoát")
    p.add_argument("--dry-run", action="store_true", help="Chạy thử nghiệm, không đặt lệnh thật")
    p.add_argument("--interval", type=int, default=30, help="Số phút giữa các chu kỳ (mặc định: 30)")
    p.add_argument("--bollinger", action="store_true", help="Bật thêm chiến lược Bollinger Bands")
    p.add_argument("--ignore-market-hours", action="store_true", help="Bỏ qua kiểm tra giờ thị trường")
    p.add_argument(
        "--khoi-tao-lan-dau",
        action="store_true",
        help="Chấp nhận khởi tạo trạng thái rủi ro mới nếu chạy giữa ngày",
    )
    p.add_argument("--symbols", type=str, default="", help="Danh sách mã cổ phiếu (phân tách bởi dấu phẩy)")
    args = p.parse_args()

    load_dotenv()
    log = setup_logging()

    raw_symbols = args.symbols or os.getenv("SYMBOLS", "")
    symbols = [s.strip().upper() for s in raw_symbols.split(",") if s.strip()] or None

    api_key = os.getenv("ALPACA_API_KEY", "")
    api_secret = os.getenv("ALPACA_API_SECRET", "")

    config = TradingConfig(api_key=api_key, api_secret=api_secret, symbols=symbols)

    bot = RiskGuardedTradingBot(
        config,
        dry_run=args.dry_run,
        use_bollinger=args.bollinger,
        ignore_market_hours=args.ignore_market_hours,
        khoi_tao_lan_dau=args.khoi_tao_lan_dau,
    )

    try:
        if args.once:
            bot.run_trading_cycle()
            log.info("Report: %s", bot.generate_performance_report())
        else:
            bot.run_continuous(interval_minutes=args.interval)
    except KeyboardInterrupt:
        log.info("Đã dừng bot.")


if __name__ == "__main__":
    main()
