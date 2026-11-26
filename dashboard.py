"""dashboard.py: Streamlit Dashboard truc quan hoa Historical Data & Stock Screening.

Chuc nang:
A. Overview: Tong quan so luong ma, trang thai sang loc.
B. Historical Data: Xem bieu do gia, volume va ket qua kiem dinh tung ma.
C. Financial Metrics: Bang tong hop ADV va Volatility (thuong nien hoa).
D. Correlation Analysis: Ma tran tuong quan va Heatmap giua cac co phieu.
E. Screening Results: Danh sach dat tieu chuan, danh sach bi loai va ly do.
F. Before vs After Comparison: So sanh toan dien he thong truoc va sau cai tien.
"""
import os
from datetime import datetime

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from config import TradingConfig, PAPER_BASE_URL
from data_manager import DataManager
from stock_screener import StockScreener, DataQualityValidator

# Cấu hình trang Streamlit
st.set_page_config(
    page_title="Stock Screening & Trading Bot Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

load_dotenv()


def get_alpaca_api():
    """Ket noi an toan voi Alpaca API ma khong lo API key tren giao dien."""
    api_key = os.getenv("ALPACA_API_KEY", "")
    api_secret = os.getenv("ALPACA_API_SECRET", "")
    if not api_key or not api_secret:
        return None
    try:
        import alpaca_trade_api as tradeapi
        return tradeapi.REST(api_key, api_secret, base_url=PAPER_BASE_URL, api_version="v2")
    except Exception as e:
        st.error(f"Lỗi khởi tạo Alpaca API: {e}")
        return None


# =====================================================================
# SIDEBAR: CẤU HÌNH THAM SỐ SÀNG LỌC
# =====================================================================
st.sidebar.title("⚙️ Cấu hình Sàng lọc")
st.sidebar.markdown("---")

default_universe = "AAPL, MSFT, GOOGL, NVDA, TSLA, JPM, BAC, XOM, CVX, JNJ"
universe_input = st.sidebar.text_area(
    "Vũ trụ cổ phiếu đầu vào (cách nhau bởi dấu phẩy):",
    value=default_universe,
    height=80,
)
universe_symbols = [s.strip().upper() for s in universe_input.split(",") if s.strip()]

if not universe_symbols:
    st.sidebar.warning("⚠️ Vui lòng nhập ít nhất một mã cổ phiếu hợp lệ.")
    st.stop()

is_crypto = any("/" in s for s in universe_symbols)

if is_crypto:
    st.sidebar.info("🪙 **Chế độ: Tiền điện tử (Crypto)**")
else:
    st.sidebar.info("🏛️ **Chế độ: Cổ phiếu Mỹ (Sàn IEX)**")

lookback_days = st.sidebar.slider(
    "Khoảng thời gian lịch sử (ngày giao dịch):",
    min_value=30, max_value=120, value=60, step=5,
    help="Chuẩn 60 ngày (~3 tháng) phản ánh ổn định thanh khoản và tương quan gần nhất.",
)

min_adv_input = st.sidebar.number_input(
    "Ngưỡng thanh khoản tối thiểu ADV ($):",
    min_value=0.0,
    max_value=10000000000.0,  # Cho phép nhập tự do lên tới 10 tỷ USD (không còn bị giới hạn 5M)
    value=200000.0,
    step=100000.0,
    format="%.0f",
    help="Average Dollar Volume trên sàn IEX. Mặc định $200k. Bạn có thể nhập tự do tùy ý từ 0 đến vài tỷ USD.",
)

col_vol1, col_vol2 = st.sidebar.columns(2)
with col_vol1:
    min_vol = st.number_input("Biến động min:", value=0.15, step=0.05, format="%.2f",
                              help="15%/năm - ngang biến động chỉ số S&P 500.")
with col_vol2:
    max_vol = st.number_input("Biến động max:", value=0.65, step=0.05, format="%.2f",
                              help="65%/năm - chặn cổ phiếu rác biến động quá lớn.")

max_corr = st.sidebar.slider(
    "Ngưỡng tương quan tối đa (Max Correlation):",
    min_value=0.50, max_value=0.95, value=0.75, step=0.05,
    help="Nếu |r| giữa hai mã vượt ngưỡng này, mã có ADV thấp hơn sẽ bị loại để tránh các chuỗi lợi suất liên hệ quá chặt.",
)

target_n = st.sidebar.number_input(
    "Số mã mục tiêu chọn vào TradingBot:",
    min_value=1, max_value=10, value=3, step=1,
)

run_button = st.sidebar.button("🚀 Thực hiện Sàng lọc Cổ phiếu", use_container_width=True)


# =====================================================================
# MAIN APP HEADER
# =====================================================================
st.title("📈 Alpaca Paper Trading Bot: Stock Screening Dashboard")
st.markdown("""
**Hệ thống Sàng lọc Cổ phiếu Dựa trên Dữ liệu Lịch sử (Historical Data-based Stock Screening)**
Phát triển theo nghiên cứu bài báo *“Build a Python Paper Trading Bot with Alpaca API” – Anil Gaddam*.
Module đánh giá toàn diện **Data Completeness, Liquidity (ADV), Volatility và Correlation** trước khi chuyển giao danh mục cho `TradingBot`.
""")
st.markdown("---")

api = get_alpaca_api()

if api is None:
    st.warning("⚠️ Không tìm thấy `ALPACA_API_KEY` hoặc `ALPACA_API_SECRET` hợp lệ trong file `.env`. Vui lòng cấu hình file `.env`.")
    st.stop()


# Khởi tạo hoặc chạy Screening
@st.cache_data(ttl=300, show_spinner="Đang tải dữ liệu lịch sử từ Alpaca và thực hiện sàng lọc...")
def execute_screening(symbols, days, min_adv, min_v, max_v, m_corr, top_n, crypto_mode=False):
    cfg = TradingConfig(
        api_key="internal", api_secret="internal",
        is_crypto=crypto_mode,
        screening_universe=symbols,
        screening_lookback_days=days,
        min_adv=min_adv,
        min_volatility=min_v,
        max_volatility=max_v,
        max_correlation=m_corr,
        target_top_n=top_n,
    )
    dm = DataManager(api, feed="crypto" if crypto_mode else "iex")
    screener = StockScreener(dm, cfg)
    res = screener.run_screening(symbols)

    # Tải chi tiết DataFrame cho từng mã để vẽ chart
    raw_dfs = {}
    for s in symbols:
        df = dm.get_daily_bars(s, days=days)
        if not df.empty:
            raw_dfs[s] = df

    return res, raw_dfs


# Thực thi screening
res, raw_dfs = execute_screening(
    universe_symbols, lookback_days, min_adv_input, min_vol, max_vol, max_corr, target_n, crypto_mode=is_crypto
)


# =====================================================================
# TABS HIỂN THỊ 6 KHU VỰC THEO YÊU CẦU
# =====================================================================
tab_overview, tab_history, tab_metrics, tab_corr, tab_results, tab_compare = st.tabs([
    "📊 A. Overview",
    "📜 B. Historical Data",
    "💰 C. Financial Metrics",
    "🔗 D. Correlation Matrix",
    "🎯 E. Screening Results",
    "⚖️ F. Before vs After",
])


# ---------------------------------------------------------------------
# TAB A: OVERVIEW
# ---------------------------------------------------------------------
with tab_overview:
    st.subheader("Tổng quan Quá trình Sàng lọc")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Tổng mã đầu vào", len(res.universe))
    c2.metric("Dữ liệu hợp lệ", len(res.valid_data_symbols))
    c3.metric("Đạt tiêu chuẩn sơ bộ", len(res.qualified_symbols))
    c4.metric("Bị loại / Cắt tỉa", len(res.universe) - len(res.selected_symbols))
    c5.metric("Mã được chọn vào Bot", len(res.selected_symbols), delta=f"Top {target_n}")

    st.write("---")
    st.write("#### 🌊 Dòng chảy 4 Lớp Sàng lọc (4-Layer Screening Funnel):")
    l1_col, l2_col, l3_col, l4_col = st.columns(4)
    p_l1 = getattr(res, "passed_layer_1", res.valid_data_symbols)
    p_l2 = getattr(res, "passed_layer_2", res.qualified_symbols)
    p_l3 = getattr(res, "passed_layer_3", res.qualified_symbols)
    p_l4 = getattr(res, "passed_layer_4", res.selected_symbols)

    l1_col.metric("Lớp 1: Data Completeness", f"{len(p_l1)}/{len(res.universe)} mã", "100% hợp lệ")
    l2_col.metric("Lớp 2: Thanh khoản ADV", f"{len(p_l2)}/{len(p_l1)} mã", f"ADV >= ${min_adv_input:,.0f}")
    l3_col.metric("Lớp 3: Độ biến động", f"{len(p_l3)}/{len(p_l2)} mã", f"{min_vol*100:.0f}%-{max_vol*100:.0f}%/năm")
    l4_col.metric("Lớp 4: Tương quan & Top-N", f"{len(p_l4)} mã chọn", f"Top {target_n} (|r| <= {max_corr})")

    st.info(f"""
    * **Nguồn cấp dữ liệu**: Alpaca Market Data API (Feed: `IEX` Free Tier).
    * **Khung thời gian (Timeframe)**: Nến Ngày (1 Day) — Lookback: **{lookback_days} ngày giao dịch gần nhất**.
    * **Thời điểm quét**: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`.
    * **Mục tiêu**: Lọc từ {len(res.universe)} mã ban đầu qua 4 lớp định lượng để chọn ra **{len(res.selected_symbols)} mã tối ưu** có thanh khoản tốt, biến động thích hợp và tương quan thấp.
    """)

    st.write("#### Danh mục cổ phiếu sau sàng lọc được chuyển vào TradingBot:")
    st.success(f"👉 **Danh sách chính thức:** `{'`, `'.join(res.selected_symbols)}`")


# ---------------------------------------------------------------------
# TAB B: HISTORICAL DATA
# ---------------------------------------------------------------------
with tab_history:
    st.subheader("Khảo sát & Kiểm định Dữ liệu Lịch sử OHLCV")
    if not universe_symbols:
        st.warning("Danh sách cổ phiếu trống.")
    else:
        selected_sym = st.selectbox("Chọn mã cổ phiếu để xem chi tiết nến lịch sử:", universe_symbols)
        if selected_sym in raw_dfs:
            df_sym = raw_dfs[selected_sym]
            m_sym = res.metrics.get(selected_sym)

            # Thống kê nhanh phiên gần nhất
            if len(df_sym) >= 2:
                last_bar = df_sym.iloc[-1]
                prev_bar = df_sym.iloc[-2]
                pct_chg = ((last_bar["close"] - prev_bar["close"]) / prev_bar["close"]) * 100
                last_date_str = df_sym.index[-1].strftime("%Y-%m-%d")
                day_range = last_bar["high"] - last_bar["low"]

                k1, k2, k3, k4 = st.columns(4)
                k1.metric("Phiên gần nhất", last_date_str)
                k2.metric("Giá Đóng cửa (Close)", f"${last_bar['close']:,.2f}", f"{pct_chg:+.2f}%")
                k3.metric("Khối lượng (Volume)", f"{last_bar['volume']:,.0f} cp")
                k4.metric("Giá trị GD (Dollar Vol)", f"${last_bar['close']*last_bar['volume']:,.0f}")

            # Trạng thái Validation
            if m_sym and m_sym.data_valid:
                st.success(
                    f"✅ **Dữ liệu Hợp lệ**: Thu thập đủ **{len(df_sym)} phiên giao dịch thực tế** trong {lookback_days} ngày lịch gần nhất "
                    f"(đã loại trừ thứ 7, CN và ngày nghỉ lễ). Dữ liệu 100% không có NaN, không trùng timestamp, $High \\ge Low > 0$."
                )
            else:
                issues_str = "; ".join(m_sym.data_issues) if m_sym else "Không xác định"
                st.error(f"❌ **Dữ liệu Không Đạt Chuẩn**: {issues_str}")

            # Biểu đồ giá và volume
            col_chart1, col_chart2 = st.columns(2)
            with col_chart1:
                st.write(f"**Biểu đồ Giá Đóng cửa (Close) - {selected_sym}**")
                chart_price = df_sym[["close"]].copy()
                chart_price.index = chart_price.index.strftime("%Y-%m-%d")
                st.line_chart(chart_price)
            with col_chart2:
                st.write(f"**Biểu đồ Khối lượng Giao dịch (Volume) - {selected_sym}**")
                chart_vol = df_sym[["volume"]].copy()
                chart_vol.index = chart_vol.index.strftime("%Y-%m-%d")
                st.bar_chart(chart_vol)

            st.write("---")
            # Chuẩn bị bảng hiển thị nến chuyên nghiệp
            df_display = df_sym.copy()
            # 1. Tính % thay đổi và Dollar Volume theo trình tự thời gian
            df_display["Change (%)"] = (df_display["close"].pct_change() * 100).round(2)
            df_display["Dollar Volume ($)"] = df_display["close"] * df_display["volume"]

            # 2. Định dạng ngày chuẩn YYYY-MM-DD
            df_display.index = df_display.index.strftime("%Y-%m-%d")
            df_display.index.name = "Ngày Giao dịch (Date)"

            # 3. Đưa ngày mới nhất lên TRÊN CÙNG để người dùng dễ tra cứu
            df_display = df_display.sort_index(ascending=False)

            # Tùy chọn số lượng nến xem để tránh mâu thuẫn
            col_opt1, col_opt2 = st.columns([2, 1])
            with col_opt1:
                st.write(f"### 📋 Bảng Chi Tiết Nến OHLCV ({selected_sym})")
            with col_opt2:
                view_mode = st.radio(
                    "Số nến hiển thị:",
                    ["15 nến gần nhất", f"Toàn bộ {len(df_sym)} nến"],
                    horizontal=True,
                    key=f"view_mode_{selected_sym}"
                )

            table_data = df_display.head(15) if "15" in view_mode else df_display

            st.dataframe(
                table_data.style.format({
                    "open": "${:,.2f}",
                    "high": "${:,.2f}",
                    "low": "${:,.2f}",
                    "close": "${:,.2f}",
                    "volume": "{:,.0f}",
                    "Dollar Volume ($)": "${:,.0f}",
                    "Change (%)": "{:+.2f}%",
                }),
                use_container_width=True,
            )
            feed_source = "Crypto" if is_crypto else "sàn IEX"
            st.caption(
                f"ℹ️ Bảng được sắp xếp theo thứ tự **phiên gần nhất ở trên cùng**. "
                f"Tổng cộng có {len(df_sym)} phiên giao dịch hợp lệ từ {feed_source}."
            )
        else:
            st.warning(f"Không có dữ liệu trả về từ Alpaca cho mã {selected_sym}.")


# ---------------------------------------------------------------------
# TAB C: FINANCIAL METRICS
# ---------------------------------------------------------------------
with tab_metrics:
    st.subheader("Bảng Chỉ số Tài chính & Xếp hạng Thanh khoản (ADV)")
    st.markdown("""
    * **Average Dollar Volume (ADV)**: $ADV = \\text{Mean}(Close \\times Volume)$ — Đo lường giá trị giao dịch trung bình mỗi ngày bằng USD.
    * **Độ biến động thường niên hóa (Annualized Volatility)**: $\\sigma_{annual} = \\text{std}(R_t) \\times \\sqrt{252}$ — Quy đổi độ lệch chuẩn lợi suất ngày theo chuẩn 252 ngày giao dịch/năm của thị trường Mỹ.
    """)

    # Tiêu đề kết quả lựa chọn giống slide trình chiếu
    if res.selected_symbols:
        top_selected_str = ", ".join(res.selected_symbols)
        st.markdown(f"### 📊 **{top_selected_str} được chọn vì có thanh khoản cao nhất trong {len(res.universe)} mã**")
    else:
        st.markdown(f"### 📊 **Xếp hạng Thanh khoản (ADV) của {len(res.universe)} mã đầu vào**")

    # Bố cục 2 cột: Cột trái biểu đồ thanh khoản ngang, Cột phải thẻ "Được chọn"
    col_adv_chart, col_adv_card = st.columns([1.7, 1])

    with col_adv_chart:
        # Chuẩn bị dữ liệu an toàn, phòng ngừa trường hợp tập hợp rỗng
        adv_rows = []
        if hasattr(res, "summary_df") and not res.summary_df.empty and "ADV ($)" in res.summary_df.columns:
            for _, row in res.summary_df.iterrows():
                try:
                    sym = str(row["Symbol"])
                    adv_val = float(row["ADV ($)"])
                    vol_raw = str(row.get("Annual Volatility", "0")).replace("%", "")
                    vol_val = float(vol_raw) if vol_raw else 0.0
                    adv_rows.append({
                        "Symbol": sym,
                        "ADV_M": adv_val / 1e6,
                        "Vol": vol_val
                    })
                except Exception:
                    pass
        elif hasattr(res, "metrics") and res.metrics:
            for s in res.universe:
                if s in res.metrics:
                    adv_rows.append({
                        "Symbol": s,
                        "ADV_M": res.metrics[s].adv / 1e6,
                        "Vol": res.metrics[s].volatility_annual * 100
                    })

        if adv_rows:
            adv_df = pd.DataFrame(adv_rows).sort_values(by="ADV_M", ascending=True).reset_index(drop=True)
        else:
            adv_df = pd.DataFrame(columns=["Symbol", "ADV_M", "Vol"])

        if not adv_df.empty:
            fig_adv, ax_adv = plt.subplots(figsize=(8, 5.2))
            fig_adv.patch.set_facecolor("#ffffff")
            ax_adv.set_facecolor("#ffffff")

            # Vẽ thanh màu vàng cam (golden yellow) chuẩn Canva
            bars = ax_adv.barh(adv_df["Symbol"], adv_df["ADV_M"], color="#ffc107", edgecolor="#e0a800", height=0.62)

            feed_name = "IEX" if not is_crypto else "Crypto"
            ax_adv.set_title(f"ADV trung bình (triệu USD), {lookback_days} ngày giao dịch, dữ liệu {feed_name}",
                             fontsize=11.5, fontweight="bold", pad=15, color="#222222")

            max_adv = adv_df["ADV_M"].max()
            for bar in bars:
                w = bar.get_width()
                label_text = f"{w:,.1f}"
                if w > max_adv * 0.12:
                    ax_adv.text(w - (max_adv * 0.02), bar.get_y() + bar.get_height() / 2,
                                label_text, va="center", ha="right", fontsize=9, fontweight="bold", color="#222222")
                else:
                    ax_adv.text(w + (max_adv * 0.015), bar.get_y() + bar.get_height() / 2,
                                label_text, va="center", ha="left", fontsize=9, fontweight="bold", color="#222222")

            ax_adv.set_xlim(0, max(max_adv * 1.15, 1.0))
            ax_adv.spines["top"].set_visible(False)
            ax_adv.spines["right"].set_visible(False)
            ax_adv.spines["left"].set_color("#cccccc")
            ax_adv.spines["bottom"].set_color("#cccccc")
            ax_adv.tick_params(axis="both", labelsize=9.5)
            ax_adv.grid(axis="x", color="#f0f0f0", linestyle="--", alpha=0.7)

            plt.tight_layout()
            st.pyplot(fig_adv)
            plt.close(fig_adv)
        else:
            st.info("ℹ️ Chưa có đủ dữ liệu thanh khoản để vẽ biểu đồ.")

    with col_adv_card:
        # Card "Được chọn" theo chuẩn thiết kế slide trình chiếu
        n_sel = len(res.selected_symbols)
        n_rej = len(res.universe) - n_sel

        if n_sel > 0:
            selected_items_html = ""
            for sym in res.selected_symbols:
                vol = res.metrics[sym].volatility_annual * 100 if (hasattr(res, "metrics") and sym in res.metrics) else 0.0
                vol_str = f"{vol:.2f}%".replace(".", ",")
                selected_items_html += f"""
                <div style="font-size: 15px; font-weight: 600; color: #2d3748; margin-bottom: 12px; display: flex; align-items: center;">
                    <span style="display: inline-block; width: 6px; height: 6px; background-color: #ffc107; border-radius: 50%; margin-right: 10px;"></span>
                    <span><strong>{sym}</strong> &nbsp;·&nbsp; biến động {vol_str}</span>
                </div>
                """

            st.markdown(f"""
            <div style="background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 22px; box-shadow: 0 4px 10px rgba(0,0,0,0.04); margin-top: 15px;">
                <div style="background-color: #ffc107; color: #1a1a1a; font-weight: 700; text-align: center; padding: 10px; border-radius: 6px; font-size: 15px; letter-spacing: 0.5px; margin-bottom: 22px;">
                    Được chọn ({n_sel} mã)
                </div>
                {selected_items_html}
                <div style="margin-top: 22px; padding-top: 16px; border-top: 1px solid #edf2f7; font-size: 13.5px; color: #4a5568; line-height: 1.6;">
                    Các mã nằm trong dải {min_vol*100:.0f}–{max_vol*100:.0f}%. {n_rej} mã còn lại bị loại vì đã đủ {target_n} mã mục tiêu (hoặc tương quan).
                </div>
            </div>
            """, unsafe_allow_html=True)
        else:
            st.markdown(f"""
            <div style="background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 22px; box-shadow: 0 4px 10px rgba(0,0,0,0.04); margin-top: 15px;">
                <div style="background-color: #f1f5f9; color: #475569; font-weight: 700; text-align: center; padding: 10px; border-radius: 6px; font-size: 15px; letter-spacing: 0.5px; margin-bottom: 16px;">
                    Chưa có mã được chọn
                </div>
                <div style="font-size: 13.5px; color: #64748b; line-height: 1.6;">
                    Không có mã nào trong {len(res.universe)} mã đầu vào vượt qua đồng thời cả 4 lớp sàng lọc (thanh khoản, biến động, tương quan). Bạn có thể điều chỉnh ngưỡng ở Sidebar bên trái.
                </div>
            </div>
            """, unsafe_allow_html=True)

    st.write("---")
    st.write("### 📋 Bảng Số Liệu Chi Tiết Chỉ Số Tài Chính (ADV & Volatility)")
    st.dataframe(
        res.summary_df.sort_values(by="ADV ($)", ascending=False),
        use_container_width=True,
    )


# ---------------------------------------------------------------------
# TAB D: CORRELATION ANALYSIS
# ---------------------------------------------------------------------
with tab_corr:
    st.subheader("Phân tích Ma trận Tương quan Pearson (Correlation Analysis)")
    st.markdown(f"""
    * Đo mức độ và chiều liên hệ tuyến tính giữa các chuỗi lợi suất ngày $R_t$.
    * **Ngưỡng loại trừ (Pruning Threshold)**: $|\\rho| > {max_corr}$. Nếu trị tuyệt đối tương quan giữa hai mã vượt ngưỡng này, hệ thống chỉ giữ mã có thanh khoản cao hơn.
    """)

    if not res.correlation_matrix.empty:
        col_corr1, col_corr2 = st.columns([1.2, 1])

        with col_corr1:
            st.write("**Bản đồ Nhiệt Tương quan (Correlation Heatmap):**")
            fig, ax = plt.subplots(figsize=(7, 5))
            cax = ax.matshow(res.correlation_matrix, cmap="coolwarm", vmin=-1.0, vmax=1.0)
            fig.colorbar(cax)

            ticks = np.arange(len(res.correlation_matrix.columns))
            ax.set_xticks(ticks)
            ax.set_yticks(ticks)
            ax.set_xticklabels(res.correlation_matrix.columns, rotation=45, ha="left")
            ax.set_yticklabels(res.correlation_matrix.columns)

            # In giá trị số lên từng ô heatmap
            for i in range(len(res.correlation_matrix)):
                for j in range(len(res.correlation_matrix)):
                    val = res.correlation_matrix.iloc[i, j]
                    ax.text(j, i, f"{val:.2f}", ha="center", va="center",
                            color="white" if abs(val) > 0.6 else "black", fontsize=8)

            plt.tight_layout()
            st.pyplot(fig)

        with col_corr2:
            st.write("**Ma trận Tương quan dạng số liệu:**")
            st.dataframe(res.correlation_matrix, use_container_width=True)

            st.write("**Giải thích các cặp có tương quan cao:**")
            high_corr_pairs = []
            cols = list(res.correlation_matrix.columns)
            for i in range(len(cols)):
                for j in range(i + 1, len(cols)):
                    val = res.correlation_matrix.iloc[i, j]
                    if abs(val) > max_corr:
                        high_corr_pairs.append((cols[i], cols[j], val))

            if high_corr_pairs:
                for c1, c2, val in high_corr_pairs:
                    st.warning(f"⚠️ **{c1} - {c2}**: Tương quan mạnh ($|r| = {abs(val):.2f} > {max_corr}$, r = {val:.2f}). Hệ thống sẽ chỉ giữ lại mã có thanh khoản lớn hơn.")
            else:
                st.info("Không có cặp nào có tương quan vượt ngưỡng rủi ro.")


# ---------------------------------------------------------------------
# TAB E: SCREENING RESULTS
# ---------------------------------------------------------------------
with tab_results:
    st.subheader("Kết quả Chi tiết Từng Mã Cổ phiếu Qua 4 Lớp Sàng Lọc")

    # Bảng tổng hợp 4 lớp
    st.write("### 📋 Bảng Theo Dõi Trạng Thái 4 Lớp & Chỉ Số Tài Chính")
    st.dataframe(res.summary_df, use_container_width=True)

    st.write("---")
    st.write("### 🔍 Phân Tích Chi Tiết Từng Lớp Sàng Lọc (Layer Breakdown)")

    p_l1 = getattr(res, "passed_layer_1", res.valid_data_symbols)
    p_l2 = getattr(res, "passed_layer_2", res.qualified_symbols)
    p_l3 = getattr(res, "passed_layer_3", res.qualified_symbols)
    p_l4 = getattr(res, "passed_layer_4", res.selected_symbols)

    with st.expander(f"🟢 **Lớp 1: Data Completeness ({len(p_l1)}/{len(res.universe)} mã qua)**", expanded=False):
        st.write(f"• **Danh sách mã qua Lớp 1:** `{'`, `'.join(p_l1)}`")
        st.write("• **Tiêu chí kiểm định:** Không rỗng, đủ $\ge 70\%$ số nến lookback, không chứa NaN, không trùng timestamp, $High \ge Low$, $Volume \ge 0$, giá $> 0$.")

    with st.expander(f"🟢 **Lớp 2: Liquidity Filter — ADV ({len(p_l2)}/{len(p_l1)} mã qua)**", expanded=False):
        st.write(f"• **Danh sách mã qua Lớp 2:** `{'`, `'.join(p_l2)}`")
        st.write(f"• **Tiêu chí kiểm định:** Average Dollar Volume $ADV = \\text{{Mean}}(Close \\times Volume) \\ge \\${min_adv_input:,.0f}$.")
        adv_details = [f"**{s}**: ${res.metrics[s].adv:,.0f}" for s in p_l2 if s in res.metrics]
        st.caption(" | ".join(adv_details))

    with st.expander(f"🟢 **Lớp 3: Volatility Filter — Độ biến động ({len(p_l3)}/{len(p_l2)} mã qua)**", expanded=False):
        st.write(f"• **Danh sách mã qua Lớp 3:** `{'`, `'.join(p_l3)}`")
        st.write(f"• **Tiêu chí kiểm định:** Độ biến động năm hóa $\\sigma_{{annual}} = \\text{{std}}(R_t) \\times \\sqrt{{252}}$ trong dải $[{min_vol*100:.0f}\\%, {max_vol*100:.0f}\\%]$.")
        vol_details = [f"**{s}**: {res.metrics[s].volatility_annual*100:.1f}%/năm" for s in p_l3 if s in res.metrics]
        st.caption(" | ".join(vol_details))

    with st.expander(f"🏆 **Lớp 4: Correlation & Selection ({len(p_l4)} mã được chọn)**", expanded=True):
        st.write(f"• **Danh sách mã chính thức được nạp vào TradingBot:** `{'`, `'.join(p_l4)}`")
        st.write(f"• **Tiêu chí kiểm định:** Xếp hạng ADV giảm dần, áp dụng thuật toán Greedy Pruning loại bỏ mã có $|\\rho| > {max_corr}$, chọn tối đa Top {target_n} mã.")

    st.write("---")
    col_res1, col_res2 = st.columns(2)

    with col_res1:
        st.write("### ✅ Mã Đạt Tiêu Chuẩn & Được Chọn")
        for sym in res.selected_symbols:
            m = res.metrics[sym]
            st.success(f"**{sym}** | ADV: `${m.adv:,.0f}` | Biến động: `{m.volatility_annual*100:.1f}%/năm`")
            st.caption(f"Lý do: {m.reason}")

    with col_res2:
        st.write("### ❌ Mã Bị Loại & Lý Do Cụ Thể")
        for sym in res.universe:
            if sym not in res.selected_symbols:
                m = res.metrics.get(sym)
                if m:
                    st.error(f"**{sym}** (Trạng thái: `{m.status}`)")
                    st.caption(f"Lý do: {m.reason}")


# ---------------------------------------------------------------------
# TAB F: BEFORE VS AFTER COMPARISON
# ---------------------------------------------------------------------
with tab_compare:
    st.subheader("So sánh Hệ thống Trước và Sau Cải tiến (Before vs After)")

    comparison_data = [
        {
            "Tiêu chí": "Danh sách Cổ phiếu Đầu vào",
            "Before (Ban đầu)": f"{len(universe_symbols)} mã cấu hình cứng ({', '.join(universe_symbols[:4])}...)",
            "After (Cải tiến)": f"{len(res.selected_symbols)} mã tối ưu sau sàng lọc ({', '.join(res.selected_symbols)})",
        },
        {
            "Tiêu chí": "Kiểm tra Dữ liệu (Data Validation)",
            "Before (Ban đầu)": "Chỉ kiểm tra đơn giản nến rỗng hoặc lỗi API",
            "After (Cải tiến)": "Kiểm định 5 lớp: số nến, NaN, trùng timestamp, $High < Low$, giá âm, volume âm",
        },
        {
            "Tiêu chí": "Thanh khoản (Liquidity Evaluation)",
            "Before (Ban đầu)": "Hoàn toàn chưa đánh giá thanh khoản",
            "After (Cải tiến)": f"Lọc Average Dollar Volume ($ADV \\ge {min_adv_input:,.0f}$ USD trên IEX)",
        },
        {
            "Tiêu chí": "Độ biến động (Volatility Evaluation)",
            "Before (Ban đầu)": "Chưa đánh giá rủi ro biến động giá",
            "After (Cải tiến)": f"Dải biến động thường niên hóa $[{min_vol*100:.0f}\\%, {max_vol*100:.0f}\\%]$",
        },
        {
            "Tiêu chí": "Tương quan Danh mục (Correlation Analysis)",
            "Before (Ban đầu)": "Không có ma trận tương quan, dễ dồn toàn bộ vốn vào 1 ngành",
            "After (Cải tiến)": f"Ma trận Pearson & thuật toán Greedy Pruning loại bỏ mã $|r| > {max_corr}$",
        },
        {
            "Tiêu chí": "Đầu vào cho TradingBot",
            "Before (Ban đầu)": "Nhận toàn bộ danh sách cứng ban đầu",
            "After (Cải tiến)": "Chỉ nhận danh mục tinh gọn đã loại trừ rủi ro tập trung",
        },
    ]

    st.table(pd.DataFrame(comparison_data))
