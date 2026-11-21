"""
test_integration_risk.py: Kiểm thử tích hợp giữa bot Alpaca gốc và module risk_control_all.py.
Kiểm tra cơ chế Stop-loss theo giá khớp thực tế (filled_avg_price), khớp một phần,
tránh trùng lặp và hủy lệnh stop khi có lệnh Market SELL.
Dùng Mock/Fake API (không cần kết nối mạng hoặc API key thật).
"""
import json
import logging
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from unittest.mock import MagicMock

import pandas as pd
from zoneinfo import ZoneInfo

from config import TradingConfig
from models import TradingSignal
from risk_control_all import (
    BOT_PREFIX,
    MAX_POSITION_VALUE,
    NY,
    R_SL,
    cancel_open_stop_orders,
    has_open_stop_order,
    place_stop_loss_order,
    save_state,
)
from run_with_risk import RiskGuardedTradingBot

logging.basicConfig(level=logging.INFO)


class FakeOrder:
    def __init__(self, id, symbol, side, type, status="open", client_order_id=None, filled_at=None,
                 filled_qty=0, filled_avg_price=0.0, qty=0, stop_price=None, legs=None):
        self.id = id
        self.symbol = symbol
        self.side = side
        self.type = type
        self.status = status
        self.client_order_id = client_order_id or ""
        self.filled_at = filled_at
        self.filled_qty = filled_qty
        self.filled_avg_price = filled_avg_price
        self.qty = qty
        self.stop_price = stop_price
        self.legs = legs or []


class FakeAccount:
    def __init__(self, equity=10000.0, last_equity=10000.0, buying_power=20000.0):
        self.equity = equity
        self.last_equity = last_equity
        self.buying_power = buying_power
        self.status = "ACTIVE"


class FakeApi:
    def __init__(self, equity=10000.0, last_equity=10000.0, mock_fill_price=None, mock_partial_qty=None):
        self._acc = FakeAccount(equity, last_equity)
        self.orders = []
        self.submitted_orders = []
        self.cancelled_ids = []
        self.positions = []
        self.mock_fill_price = mock_fill_price
        self.mock_partial_qty = mock_partial_qty

    def get_account(self):
        return self._acc

    def list_orders(self, status="open"):
        if status == "open":
            return [o for o in self.orders if o.status in ("open", "new", "pending_new")]
        if status == "closed":
            return [o for o in self.orders if o.status in ("closed", "filled", "canceled", "expired")]
        return list(self.orders)

    def get_order(self, order_id):
        for o in self.orders:
            if o.id == order_id:
                return o
        return None

    def get_bars(self, symbol, timeframe, **kwargs):
        if hasattr(self, "mock_bars_dict") and symbol in self.mock_bars_dict:
            mock_res = MagicMock()
            mock_res.df = self.mock_bars_dict[symbol]
            return mock_res
        mock_res = MagicMock()
        mock_res.df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        return mock_res

    def cancel_order(self, order_id):
        self.cancelled_ids.append(order_id)
        for o in self.orders:
            if o.id == order_id:
                o.status = "canceled"

    def submit_order(self, **kwargs):
        self.submitted_orders.append(kwargs)
        side = kwargs.get("side")
        order_type = kwargs.get("type", "market")
        qty = kwargs.get("qty", 0)
        symbol = kwargs.get("symbol")
        cid = kwargs.get("client_order_id", "")
        stop_p = kwargs.get("stop_price")
        order_id = f"order-{len(self.submitted_orders)}"

        if side == "buy":
            fill_p = self.mock_fill_price if self.mock_fill_price is not None else 100.0
            fill_q = self.mock_partial_qty if self.mock_partial_qty is not None else qty
            status = "filled" if fill_q == qty else ("partially_filled" if fill_q > 0 else "new")
            o = FakeOrder(
                id=order_id,
                symbol=symbol,
                side=side,
                type=order_type,
                client_order_id=cid,
                status=status,
                filled_qty=fill_q,
                filled_avg_price=fill_p,
                qty=qty,
            )
        else:
            status = "open" if order_type == "stop" else "filled"
            o = FakeOrder(
                id=order_id,
                symbol=symbol,
                side=side,
                type=order_type,
                client_order_id=cid,
                status=status,
                qty=qty,
                stop_price=stop_p,
            )
        self.orders.append(o)
        return o

    def list_positions(self):
        return self.positions

    def get_clock(self):
        clock = MagicMock()
        clock.is_open = True
        return clock


class TestRiskIntegration(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.state_file = os.path.join(self.test_dir, "test_risk_state.json")
        self.config = TradingConfig(
            api_key="mock_key",
            api_secret="mock_secret",
            max_position_size=1000.0,
            symbols=["AAPL", "NVDA", "MSFT"],
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_1_buy_and_stop_loss_on_actual_filled_price(self):
        """MUA: Đặt Stop-loss theo giá khớp thực tế (filled_avg_price), không dùng giá tham chiếu."""
        # Giả lập trượt giá: giá tham chiếu là 150.0, nhưng sàn Alpaca khớp ở giá 152.0
        api = FakeApi(equity=10000.0, last_equity=10000.0, mock_fill_price=152.0)
        bot = RiskGuardedTradingBot(
            self.config,
            api=api,
            dry_run=False,
            ignore_market_hours=True,
            khoi_tao_lan_dau=True,
            state_path=self.state_file,
        )

        signal = TradingSignal(
            symbol="AAPL",
            action="BUY",
            price=150.0,  # Giá tham chiếu
            quantity=2,
            timestamp=datetime.now(),
            reason="Test BUY",
            confidence=0.8,
        )
        res = bot.execute_signal_group("AAPL", "BUY", [signal])
        self.assertTrue(res)

        # Kiểm tra: Phải có 2 lệnh được gửi (1 lệnh MUA Market, và 1 lệnh Stop SELL)
        self.assertEqual(len(api.submitted_orders), 2)

        # Lệnh 1: MUA Market
        buy_order = api.submitted_orders[0]
        self.assertEqual(buy_order["symbol"], "AAPL")
        self.assertEqual(buy_order["side"], "buy")
        self.assertEqual(buy_order["type"], "market")

        # Lệnh 2: Stop SELL tính theo filled_avg_price 152.0 (không phải 150.0)
        stop_order = api.submitted_orders[1]
        self.assertEqual(stop_order["symbol"], "AAPL")
        self.assertEqual(stop_order["side"], "sell")
        self.assertEqual(stop_order["type"], "stop")
        self.assertEqual(stop_order["time_in_force"], "gtc")

        # stop_price = 152.0 * (1 - 0.05) = 144.40 (Nếu dùng 150.0 sẽ là 142.50)
        expected_sl = round(152.0 * (1 - R_SL), 2)
        self.assertEqual(stop_order["stop_price"], str(expected_sl))
        self.assertEqual(stop_order["stop_price"], "144.4")

    def test_2_partial_fill_stop_loss(self):
        """Khớp một phần: Chỉ đặt Stop SELL cho đúng số lượng cổ phiếu đã khớp (filled_qty)."""
        # Mua 5 cp, nhưng chỉ khớp một phần 3 cp @ 100$
        api = FakeApi(equity=10000.0, last_equity=10000.0, mock_fill_price=100.0, mock_partial_qty=3)
        bot = RiskGuardedTradingBot(
            self.config,
            api=api,
            dry_run=False,
            ignore_market_hours=True,
            khoi_tao_lan_dau=True,
            state_path=self.state_file,
        )

        signal = TradingSignal("AAPL", "BUY", 100.0, 5, datetime.now(), "BUY", 0.8)
        res = bot.execute_signal_group("AAPL", "BUY", [signal])
        self.assertTrue(res)

        self.assertEqual(len(api.submitted_orders), 2)
        stop_order = api.submitted_orders[1]
        self.assertEqual(stop_order["qty"], 3, "Stop SELL phải bảo vệ đúng 3 cp đã khớp")
        self.assertEqual(stop_order["stop_price"], "95.0")

    def test_3_prevent_duplicate_stop_orders(self):
        """Tránh trùng lặp: Không đặt thêm Stop SELL nếu đã có lệnh stop đang mở cho mã đó."""
        api = FakeApi(equity=10000.0, last_equity=10000.0, mock_fill_price=100.0)
        # Giả lập đã có 1 lệnh Stop SELL đang mở cho AAPL
        api.orders.append(FakeOrder("existing_stop", "AAPL", "sell", "stop", status="open"))

        bot = RiskGuardedTradingBot(
            self.config,
            api=api,
            dry_run=False,
            ignore_market_hours=True,
            khoi_tao_lan_dau=True,
            state_path=self.state_file,
        )

        signal = TradingSignal("AAPL", "BUY", 100.0, 2, datetime.now(), "BUY", 0.8)
        bot.execute_signal_group("AAPL", "BUY", [signal])

        # Chỉ có lệnh MUA được gửi, KHÔNG gửi thêm lệnh Stop trùng lặp
        self.assertEqual(len(api.submitted_orders), 1)
        self.assertEqual(api.submitted_orders[0]["side"], "buy")

    def test_4_market_sell_cancels_open_stop_to_avoid_conflict(self):
        """Tránh xung đột: Hủy lệnh Stop SELL đang chờ trước khi thực hiện lệnh Market SELL."""
        api = FakeApi(equity=10000.0, last_equity=10000.0)
        # Đang giữ 10 cp AAPL
        p = MagicMock()
        p.symbol = "AAPL"
        p.qty = "10"
        api.positions = [p]

        # Đang có 1 lệnh Stop SELL mở giữ 10 cp AAPL
        stop_order = FakeOrder("stop_1", "AAPL", "sell", "stop", status="open")
        api.orders.append(stop_order)

        bot = RiskGuardedTradingBot(
            self.config,
            api=api,
            dry_run=False,
            ignore_market_hours=True,
            khoi_tao_lan_dau=True,
            state_path=self.state_file,
        )

        signal = TradingSignal("AAPL", "SELL", 160.0, 10, datetime.now(), "SELL", 0.8)
        res = bot.execute_signal_group("AAPL", "SELL", [signal])
        self.assertTrue(res)

        # Lệnh stop_1 phải bị hủy trước để giải phóng cổ phiếu bị khóa
        self.assertIn("stop_1", api.cancelled_ids)
        self.assertEqual(stop_order.status, "canceled")

        # Sau đó lệnh Market SELL mới được đặt ra sàn
        market_sell = api.submitted_orders[-1]
        self.assertEqual(market_sell["symbol"], "AAPL")
        self.assertEqual(market_sell["side"], "sell")
        self.assertEqual(market_sell["type"], "market")

    def test_5_dll_triggered_blocks_buy_cancels_only_bot_orders(self):
        """DLL chạm: Chặn MUA, hủy lệnh MUA chờ của bot, KHÔNG hủy lệnh MUA thủ công hay lệnh BÁN."""
        api = FakeApi(equity=9800.0, last_equity=10000.0)

        o_bot_buy = FakeOrder("o1", "AAPL", "buy", "market", "open", client_order_id=f"{BOT_PREFIX}-AAPL-1")
        o_man_buy = FakeOrder("o2", "NVDA", "buy", "market", "open", client_order_id="manual-NVDA-99")
        o_bot_sell = FakeOrder("o3", "AAPL", "sell", "stop", "open", client_order_id=f"{BOT_PREFIX}-AAPL-stop")
        api.orders.extend([o_bot_buy, o_man_buy, o_bot_sell])

        bot = RiskGuardedTradingBot(
            self.config,
            api=api,
            dry_run=False,
            ignore_market_hours=True,
            khoi_tao_lan_dau=True,
            state_path=self.state_file,
        )

        bot.run_trading_cycle()

        self.assertIn("o1", api.cancelled_ids)
        self.assertNotIn("o2", api.cancelled_ids, "Lệnh MUA thủ công không được bị hủy")
        self.assertNotIn("o3", api.cancelled_ids, "Lệnh BÁN stop-loss không được bị hủy")

        signal = TradingSignal("MSFT", "BUY", 200.0, 5, datetime.now(), "BUY", 0.8)
        res = bot.execute_signal_group("MSFT", "BUY", [signal])
        self.assertFalse(res, "DLL chạm thì phải chặn MUA")

    def test_6_failsafe_on_missing_or_corrupted_state(self):
        """Mất file trạng thái giữa ngày (equity lệch): Chặn an toàn (Fail-safe)."""
        api = FakeApi(equity=9900.0, last_equity=10000.0)
        bot = RiskGuardedTradingBot(
            self.config,
            api=api,
            dry_run=False,
            ignore_market_hours=True,
            khoi_tao_lan_dau=False,
            state_path=self.state_file,
        )

        bot.run_trading_cycle()
        self.assertTrue(bot.current_state["circuit_breaker"])

        signal = TradingSignal("AAPL", "BUY", 150.0, 5, datetime.now(), "BUY", 0.8)
        res = bot.execute_signal_group("AAPL", "BUY", [signal])
        self.assertFalse(res)

    def test_7_invalid_qty_not_submitted(self):
        """qty không hợp lệ: Không gửi lệnh ra API."""
        api = FakeApi(equity=10000.0, last_equity=10000.0)
        bot = RiskGuardedTradingBot(
            self.config,
            api=api,
            dry_run=False,
            ignore_market_hours=True,
            khoi_tao_lan_dau=True,
            state_path=self.state_file,
        )
        bot.run_trading_cycle()

        signal = TradingSignal("AAPL", "BUY", 150.0, 5, datetime.now(), "BUY", 0.8)

        bot.portfolio_manager.calculate_position_size = MagicMock(return_value=0)
        res = bot.execute_signal_group("AAPL", "BUY", [signal])
        self.assertFalse(res)
        self.assertEqual(len(api.submitted_orders), 0)

        bot.portfolio_manager.calculate_position_size = MagicMock(return_value=-2)
        res = bot.execute_signal_group("AAPL", "BUY", [signal])
        self.assertFalse(res)
        self.assertEqual(len(api.submitted_orders), 0)

        bot.portfolio_manager.calculate_position_size = MagicMock(return_value=2.5)
        res = bot.execute_signal_group("AAPL", "BUY", [signal])
        self.assertFalse(res)
        self.assertEqual(len(api.submitted_orders), 0)

    def test_8_do_not_rebuy_stopped_symbol(self):
        """Không mua lại mã đã bị stop-loss trong ngày."""
        api = FakeApi(equity=10000.0, last_equity=10000.0)
        now_ny = datetime.now(NY)
        o_stop_filled = FakeOrder(
            id="stop_nvda_1",
            symbol="NVDA",
            side="sell",
            type="stop",
            status="filled",
            filled_at=now_ny.isoformat(),
        )
        api.orders.append(o_stop_filled)

        bot = RiskGuardedTradingBot(
            self.config,
            api=api,
            dry_run=False,
            ignore_market_hours=True,
            khoi_tao_lan_dau=True,
            state_path=self.state_file,
        )

        bot.run_trading_cycle()
        self.assertIn("NVDA", bot.current_state["stopped_symbols"])

        signal = TradingSignal("NVDA", "BUY", 120.0, 5, datetime.now(), "BUY", 0.8)
        res = bot.execute_signal_group("NVDA", "BUY", [signal])
        self.assertFalse(res, "Mã đã bị stop trong ngày tuyệt đối không được mua lại")
        self.assertEqual(len(api.submitted_orders), 0)

    def test_9_refuse_run_on_max_position_size_mismatch(self):
        """Từ chối chạy nếu config.max_position_size lệch với MAX_POSITION_VALUE."""
        mismatched_config = TradingConfig(
            api_key="mock_key",
            api_secret="mock_secret",
            max_position_size=2000.0,
            symbols=["AAPL"],
        )
        api = FakeApi()
        with self.assertRaises(ValueError) as ctx:
            RiskGuardedTradingBot(mismatched_config, api=api)
        self.assertIn("Lệch cấu hình", str(ctx.exception))

    def test_10_full_cycle_auto_signal_to_stop_loss(self):
        """CHU KỲ TỰ ĐỘNG: Tín hiệu nến (RSI < 30) -> MUA Market -> Khớp thật -> Đặt Stop SELL."""
        # Khớp thật ở 62.0$
        api = FakeApi(equity=10000.0, last_equity=10000.0, mock_fill_price=62.0)

        prices = [100.0] * 80 + [100.0 - i * 2.0 for i in range(20)]
        df_bars = pd.DataFrame({
            "open": prices,
            "high": [p + 1.0 for p in prices],
            "low": [p - 1.0 for p in prices],
            "close": prices,
            "volume": [1000] * 100
        })
        api.mock_bars_dict = {"AAPL": df_bars}

        bot_config = TradingConfig(
            api_key="mock_key",
            api_secret="mock_secret",
            max_position_size=1000.0,
            symbols=["AAPL"],
        )

        bot = RiskGuardedTradingBot(
            bot_config,
            api=api,
            dry_run=False,
            ignore_market_hours=True,
            khoi_tao_lan_dau=True,
            state_path=self.state_file,
        )

        bot.run_trading_cycle()

        # Kiểm tra: 1 lệnh MUA Market và 1 lệnh Stop SELL
        self.assertEqual(len(api.submitted_orders), 2)
        buy_sub = api.submitted_orders[0]
        self.assertEqual(buy_sub["side"], "buy")
        self.assertEqual(buy_sub["symbol"], "AAPL")

        stop_sub = api.submitted_orders[1]
        self.assertEqual(stop_sub["side"], "sell")
        self.assertEqual(stop_sub["type"], "stop")
        self.assertEqual(stop_sub["stop_price"], "58.9")  # 62.0 * 0.95 = 58.9
        self.assertEqual(stop_sub["qty"], 6)


if __name__ == "__main__":
    unittest.main()
