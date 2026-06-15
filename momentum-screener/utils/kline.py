"""
K 線圖生成（matplotlib，dark theme）
"""
import io
import logging
import datetime
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

from utils.price_fetcher import _yahoo_ohlcv

logger = logging.getLogger(__name__)

_BG    = "#1a1a2e"
_PANEL = "#16213e"
_RED   = "#e74c3c"   # 上漲（台灣紅）
_GREEN = "#2ecc71"   # 下跌（台灣綠）
_WHITE = "#e0e0e0"
_GRAY  = "#555566"


def _sma(lst: list[float], n: int) -> list[float]:
    out = []
    for i in range(len(lst)):
        window = lst[max(0, i - n + 1): i + 1]
        out.append(sum(window) / len(window))
    return out


async def generate_kline_chart(
    stock_code: str,
    stock_name: str = "",
    days: int = 10,
) -> bytes | None:
    """
    生成近 days 日 K 線圖（含 MA5/10/20 及成交量），回傳 PNG bytes。
    失敗時回傳 None。
    """
    try:
        rows = await _yahoo_ohlcv(stock_code)
    except Exception as e:
        logger.warning(f"K線資料抓取失敗 {stock_code}: {e}")
        return None

    if not rows:
        return None

    rows = sorted(rows, key=lambda x: x["date"])[-days:]
    n    = len(rows)
    xs   = list(range(n))

    opens  = [r["open"]   for r in rows]
    highs  = [r["high"]   for r in rows]
    lows   = [r["low"]    for r in rows]
    closes = [r["close"]  for r in rows]
    vols   = [r["volume"] for r in rows]
    dates  = [r["date"]   for r in rows]

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(9, 5.5),
        gridspec_kw={"height_ratios": [3.5, 1]},
    )
    fig.patch.set_facecolor(_BG)
    for ax in (ax1, ax2):
        ax.set_facecolor(_PANEL)
        ax.tick_params(colors=_WHITE, labelsize=8)
        for spine in ax.spines.values():
            spine.set_color(_GRAY)

    # ── 蠟燭圖 ────────────────────────────────────────────────────────────────
    for i in range(n):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        color = _RED if c >= o else _GREEN
        ax1.plot([i, i], [l, h], color=color, linewidth=0.9, zorder=2)
        body_h = max(abs(c - o), 0.01)
        rect = mpatches.FancyBboxPatch(
            (i - 0.32, min(o, c)), 0.64, body_h,
            boxstyle="square,pad=0",
            facecolor=color, edgecolor=color, zorder=3,
        )
        ax1.add_patch(rect)

    # ── MA 線 ────────────────────────────────────────────────────────────────
    ma5  = _sma(closes, 5)
    ma10 = _sma(closes, 10)
    ma20 = _sma(closes, 20)
    ax1.plot(xs, ma5,  color="#f39c12", linewidth=1.1, label="MA5",  zorder=4)
    ax1.plot(xs, ma10, color="#3498db", linewidth=1.1, label="MA10", zorder=4)
    ax1.plot(xs, ma20, color="#9b59b6", linewidth=1.1, label="MA20", zorder=4)
    ax1.legend(
        fontsize=7.5, loc="upper left",
        facecolor=_BG, edgecolor=_GRAY, labelcolor=_WHITE,
    )

    price_pad = (max(highs) - min(lows)) * 0.08
    ax1.set_ylim(min(lows) - price_pad, max(highs) + price_pad)
    ax1.set_xlim(-0.5, n - 0.5)
    ax1.set_xticks(xs)
    ax1.set_xticklabels([""] * n)
    ax1.yaxis.tick_right()
    ax1.yaxis.set_label_position("right")
    ax1.set_ylabel("價格 (NT$)", color=_WHITE, fontsize=8)

    # ── 成交量柱 ────────────────────────────────────────────────────────────
    bar_colors = [_RED if closes[i] >= opens[i] else _GREEN for i in range(n)]
    ax2.bar(xs, vols, color=bar_colors, width=0.7, zorder=3)
    ax2.set_xlim(-0.5, n - 0.5)
    ax2.set_xticks(xs)
    ax2.set_xticklabels(
        [d.strftime("%m/%d") for d in dates],
        rotation=45, ha="right", fontsize=7.5,
    )
    ax2.yaxis.tick_right()
    ax2.yaxis.set_label_position("right")
    ax2.set_ylabel("量(張)", color=_WHITE, fontsize=8)

    title = f"{stock_code}  {stock_name}  近{n}日K線"
    fig.suptitle(title, color=_WHITE, fontsize=11, y=0.98)
    plt.tight_layout(rect=[0, 0, 1, 0.96])

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=110, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.read()
