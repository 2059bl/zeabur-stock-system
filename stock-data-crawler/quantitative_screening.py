#!/usr/bin/env python3
"""
📊 計量篩選系統 - 近期強勢股識別
根據以下條件進行多層級篩選：
1. 法人買超 + 累計月營收正成長
2. 成交大量區或法人成本區成交
3. 技術指標信號（MA交叉、RSI交叉）
4. 法人持倉變化監控

使用場景：短線操作，提高勝率
"""

import asyncio
import json
import os
from datetime import datetime, timedelta
from collections import defaultdict
from dataclasses import dataclass
from typing import List, Dict, Tuple

try:
    import httpx
    import numpy as np
    import pandas as pd
except ImportError:
    print("安裝依賴：pip install httpx numpy pandas")
    exit(1)


# ══════════════════════════════════════════════════════════════════════════════════
# 數據模型
# ══════════════════════════════════════════════════════════════════════════════════

@dataclass
class StockMetrics:
    """股票指標"""
    code: str
    name: str
    price: float
    volume: float

    # 技術指標
    ma2: float = 0.0
    ma7: float = 0.0
    rsi6: float = 0.0
    rsi12: float = 0.0

    # 法人籌碼
    foreign_net: float = 0.0  # 外資淨買
    trust_net: float = 0.0    # 投信淨買
    dealer_net: float = 0.0   # 自營商淨買
    foreign_holding: float = 0.0  # 外資持股%
    trust_holding: float = 0.0    # 投信持股%

    # 營收
    revenue_growth: float = 0.0      # 年成長%
    revenue_monthly_growth: float = 0.0  # 月變動%
    margin_growth: float = 0.0       # 毛利率變動%

    # 交易信號
    ma_signal: str = "NONE"  # UP/DOWN/NONE
    rsi_signal: str = "NONE"  # UP/DOWN/NONE
    volume_signal: str = "NORMAL"  # HIGH/NORMAL/LOW

    # 篩選結果
    score: float = 0.0
    pass_filters: List[str] = None
    fail_reasons: List[str] = None


# ══════════════════════════════════════════════════════════════════════════════════
# 篩選邏輯
# ══════════════════════════════════════════════════════════════════════════════════

class QuantitativeScreener:
    """計量篩選系統"""

    def __init__(self):
        self.stocks_data: Dict[str, StockMetrics] = {}
        self.finmind_token = os.getenv("FINMIND_TOKEN", "")

    async def fetch_stock_data(self, code: str, days: int = 30) -> Dict:
        """從 FinMind 獲取股票數據"""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                end_date = datetime.now().date().isoformat()
                start_date = (datetime.now() - timedelta(days=days)).date().isoformat()

                r = await client.get(
                    "https://api.finmindtrade.com/api/v4/data",
                    params={
                        "dataset": "TaiwanStockPrice",
                        "data_id": code,
                        "start_date": start_date,
                        "end_date": end_date,
                        "token": self.finmind_token
                    }
                )

                if r.status_code == 200:
                    data = r.json()
                    return data.get("data", [])
        except Exception as e:
            print(f"❌ {code} 數據獲取失敗: {e}")

        return []

    def calculate_ma(self, prices: List[float], period: int) -> float:
        """計算移動平均線"""
        if len(prices) < period:
            return 0.0
        return np.mean(prices[-period:])

    def calculate_rsi(self, prices: List[float], period: int = 14) -> float:
        """計算 RSI 指標"""
        if len(prices) < period + 1:
            return 0.0

        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)

        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])

        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 0.0

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def filter_by_foreign_chips(self, metrics: StockMetrics) -> Tuple[bool, str]:
        """
        篩選1：法人買超
        檢查外資、投信、自營商淨買情況
        """
        total_net = metrics.foreign_net + metrics.trust_net + metrics.dealer_net

        # 至少有一家法人買超
        if metrics.foreign_net > 0 or metrics.trust_net > 0:
            return True, "法人買超"

        # 三家合計買超也可接受
        if total_net > 0:
            return True, "法人買超（合計）"

        return False, "法人持續賣超"

    def filter_by_revenue(self, metrics: StockMetrics) -> Tuple[bool, str]:
        """
        篩選2：營收成長
        - 年成長20%以上
        - 月變動正成長
        - 毛利率正成長
        """
        reasons = []

        if metrics.revenue_growth < 20:
            return False, f"年成長不足 ({metrics.revenue_growth:.1f}%)"
        reasons.append(f"年成長{metrics.revenue_growth:.1f}%")

        if metrics.revenue_monthly_growth <= 0:
            return False, f"月營收負成長 ({metrics.revenue_monthly_growth:.1f}%)"
        reasons.append(f"月成長{metrics.revenue_monthly_growth:.1f}%")

        if metrics.margin_growth <= 0:
            return False, f"毛利率負成長 ({metrics.margin_growth:.1f}%)"
        reasons.append(f"毛利率{metrics.margin_growth:.1f}%")

        return True, "營收&毛利率均成長"

    def filter_by_volume(self, metrics: StockMetrics) -> Tuple[bool, str]:
        """
        篩選3：成交量
        檢查是否在大量區成交或法人成本區成交
        """
        # 簡化版本：檢查成交量是否高於平均
        if metrics.volume_signal == "HIGH":
            return True, "成交大量區"

        if metrics.volume_signal == "NORMAL" and metrics.foreign_net > 0:
            return True, "法人成本區成交"

        return False, "成交量不足"

    def check_ma_signal(self, metrics: StockMetrics) -> str:
        """
        進場信號1：2日均線交叉7日均線向上
        出場信號：交叉向下
        """
        if metrics.ma2 > metrics.ma7:
            return "UP"
        elif metrics.ma2 < metrics.ma7:
            return "DOWN"
        return "NONE"

    def check_rsi_signal(self, metrics: StockMetrics) -> str:
        """
        進場信號2：6RSI交叉12RSI向上
        出場信號：交叉向下
        """
        if metrics.rsi6 > metrics.rsi12:
            return "UP"
        elif metrics.rsi6 < metrics.rsi12:
            return "DOWN"
        return "NONE"

    def calculate_score(self, metrics: StockMetrics) -> float:
        """
        計算綜合分數 (0-100)
        """
        score = 0.0

        # 法人籌碼分 (40分)
        if metrics.foreign_net > 0:
            score += 20
        if metrics.trust_net > 0:
            score += 10
        if metrics.dealer_net > 0:
            score += 10

        # 營收成長分 (30分)
        revenue_score = min((metrics.revenue_growth / 20) * 10, 10)
        margin_score = min((metrics.revenue_monthly_growth / 10) * 10, 10)
        score += revenue_score + margin_score

        # 技術指標分 (30分)
        if metrics.ma_signal == "UP":
            score += 15
        if metrics.rsi_signal == "UP":
            score += 15

        return min(score, 100.0)

    async def screen_stock(self, code: str, name: str) -> StockMetrics:
        """
        篩選單支股票
        """
        metrics = StockMetrics(code=code, name=name, price=0.0, volume=0.0)
        metrics.pass_filters = []
        metrics.fail_reasons = []

        # 獲取數據
        daily_data = await self.fetch_stock_data(code, days=30)
        if not daily_data:
            metrics.fail_reasons.append("無法獲取數據")
            return metrics

        # 提取價格數據
        prices = [float(d.get("close", 0)) for d in daily_data]
        volumes = [float(d.get("Trading_Volume", 0)) for d in daily_data]

        if not prices:
            metrics.fail_reasons.append("價格數據不足")
            return metrics

        # 計算技術指標
        metrics.price = prices[-1]
        metrics.volume = volumes[-1] if volumes else 0
        metrics.ma2 = self.calculate_ma(prices, 2)
        metrics.ma7 = self.calculate_ma(prices, 7)
        metrics.rsi6 = self.calculate_rsi(prices, 6)
        metrics.rsi12 = self.calculate_rsi(prices, 12)

        # 檢查交叉信號
        metrics.ma_signal = self.check_ma_signal(metrics)
        metrics.rsi_signal = self.check_rsi_signal(metrics)

        # 進行篩選
        pass_foreign, reason_foreign = self.filter_by_foreign_chips(metrics)
        pass_revenue, reason_revenue = self.filter_by_revenue(metrics)
        pass_volume, reason_volume = self.filter_by_volume(metrics)

        # 記錄結果
        if pass_foreign:
            metrics.pass_filters.append(reason_foreign)
        else:
            metrics.fail_reasons.append(reason_foreign)

        if pass_revenue:
            metrics.pass_filters.append(reason_revenue)
        else:
            metrics.fail_reasons.append(reason_revenue)

        if pass_volume:
            metrics.pass_filters.append(reason_volume)
        else:
            metrics.fail_reasons.append(reason_volume)

        # 計算綜合分數
        metrics.score = self.calculate_score(metrics)

        return metrics

    async def screen_portfolio(self, stocks: List[Tuple[str, str]]) -> List[StockMetrics]:
        """
        篩選投資組合
        """
        print(f"🔍 開始篩選 {len(stocks)} 支股票...\n")

        tasks = [self.screen_stock(code, name) for code, name in stocks]
        results = await asyncio.gather(*tasks)

        # 按分數排序
        results = sorted(results, key=lambda x: x.score, reverse=True)

        return results

    def generate_report(self, results: List[StockMetrics]) -> str:
        """
        生成篩選報告
        """
        report = "╔" + "═" * 100 + "╗\n"
        report += "║" + "【計量篩選系統 - 強勢股識別報告】".center(100) + "║\n"
        report += "╚" + "═" * 100 + "╝\n\n"

        # 統計
        passed = [r for r in results if len(r.pass_filters) == 3]
        high_score = [r for r in results if r.score >= 70]

        report += f"📊 篩選統計\n"
        report += f"  總計：{len(results)} 支股票\n"
        report += f"  全部通過：{len(passed)} 支\n"
        report += f"  高分 (≥70)：{len(high_score)} 支\n\n"

        # 詳細結果
        report += "═" * 100 + "\n"
        report += "【進場信號強勢股】(符合 法人買超 + 營收成長 + 成交大量)\n"
        report += "═" * 100 + "\n\n"

        report += f"{'排序':<5} {'代號':<8} {'股名':<12} {'價格':>10} {'成交量':>12} {'分數':>8} {'MA信號':>8} {'RSI信號':>8}\n"
        report += "─" * 100 + "\n"

        for i, r in enumerate(passed[:20], 1):
            ma_signal = f"{'↗ UP' if r.ma_signal == 'UP' else '↘ DOWN' if r.ma_signal == 'DOWN' else 'NONE'}"
            rsi_signal = f"{'↗ UP' if r.rsi_signal == 'UP' else '↘ DOWN' if r.rsi_signal == 'DOWN' else 'NONE'}"
            report += f"{i:<5} {r.code:<8} {r.name:<12} {r.price:>10.2f} {r.volume:>12.0f} {r.score:>7.1f}% {ma_signal:>8} {rsi_signal:>8}\n"

        if len(passed) > 20:
            report += f"\n... 還有 {len(passed) - 20} 支\n"

        # 高分但未全部通過的股票
        if high_score:
            report += f"\n\n【高分觀察股】(分數 ≥ 70 但未全部通過篩選)\n"
            report += "─" * 100 + "\n"

            for r in high_score:
                if r not in passed:
                    report += f"\n{r.code} {r.name} - 分數 {r.score:.1f}%\n"
                    report += f"  通過篩選：{', '.join(r.pass_filters) if r.pass_filters else '無'}\n"
                    report += f"  失敗原因：{', '.join(r.fail_reasons) if r.fail_reasons else '無'}\n"

        return report


# ══════════════════════════════════════════════════════════════════════════════════
# 主程式
# ══════════════════════════════════════════════════════════════════════════════════

async def main():
    """主程式"""

    # 83 支電子股清單
    stocks = [
        ("2301", "光寶科"), ("2303", "聯電"), ("2308", "台達電"),
        ("2317", "鴻海"), ("2324", "仁寶"), ("2330", "台積電"),
        ("2345", "智邦"), ("2356", "英業達"), ("2357", "華碩"),
        ("2376", "技嘉"), ("2417", "圓剛"), ("2449", "京元電"),
        ("2454", "聯發科"), ("2465", "麗臺"), ("2467", "志聖"),
        ("3029", "零壹"), ("3037", "欣興"), ("3048", "益登"),
        ("3081", "聯亞光"), ("3189", "景碩"), ("3231", "緯創"),
        ("3264", "欣銓"), ("3324", "雙鴻"), ("3443", "創意"),
        ("3661", "世芯-KY"), ("3693", "營邦"), ("3711", "日月光"),
        ("4938", "和碩"), ("5443", "均豪"), ("6197", "佳必琪"),
        ("6227", "茂綸"), ("6271", "同欣電"), ("6640", "均華"),
        ("6669", "緯穎"), ("8046", "南電"), ("8064", "東捷"),
        ("8210", "勤誠"), ("8234", "新漢"), ("8299", "群聯"),
    ]

    screener = QuantitativeScreener()
    results = await screener.screen_portfolio(stocks)

    # 生成報告
    report = screener.generate_report(results)
    print(report)

    # 保存報告
    with open("/Users/ning/Desktop/計量篩選報告.txt", "w", encoding="utf-8") as f:
        f.write(report)

    print("\n✅ 報告已保存到桌面")


if __name__ == "__main__":
    asyncio.run(main())
