#!/usr/bin/env python3
"""
🎯 交易信號生成系統
基於計量篩選結果生成進場和出場信號
"""

from enum import Enum
from dataclasses import dataclass
from typing import List, Dict
from datetime import datetime


class SignalType(Enum):
    """信號類型"""
    ENTRY = "進場"
    EXIT = "出場"
    WATCH = "觀察"
    AVOID = "迴避"


class SignalStrength(Enum):
    """信號強度"""
    STRONG = "強"
    MEDIUM = "中"
    WEAK = "弱"


@dataclass
class TradingSignal:
    """交易信號"""
    code: str
    name: str
    signal_type: SignalType
    strength: SignalStrength
    price: float
    score: float

    # 進場條件
    foreign_chips_buy: bool = False
    revenue_growth: bool = False
    volume_signal: bool = False
    ma_cross_up: bool = False
    rsi_cross_up: bool = False

    # 出場條件
    ma_cross_down: bool = False
    rsi_cross_down: bool = False

    # 說明
    reasons: List[str] = None
    cautions: List[str] = None


class TradingSignalGenerator:
    """交易信號生成器"""

    @staticmethod
    def generate_entry_signal(stock_metrics) -> TradingSignal:
        """
        生成進場信號
        條件：
        1. 法人買超
        2. 營收成長
        3. 成交大量區
        4. MA交叉向上 或 RSI交叉向上
        """
        signal = TradingSignal(
            code=stock_metrics.code,
            name=stock_metrics.name,
            signal_type=SignalType.ENTRY,
            strength=SignalStrength.MEDIUM,
            price=stock_metrics.price,
            score=stock_metrics.score,
            reasons=[],
            cautions=[]
        )

        # 檢查進場條件
        signal.foreign_chips_buy = len([f for f in stock_metrics.pass_filters if "法人" in f]) > 0
        signal.revenue_growth = len([f for f in stock_metrics.pass_filters if "營收" in f]) > 0
        signal.volume_signal = len([f for f in stock_metrics.pass_filters if "成交" in f]) > 0
        signal.ma_cross_up = stock_metrics.ma_signal == "UP"
        signal.rsi_cross_up = stock_metrics.rsi_signal == "UP"

        # 判斷信號強度
        conditions_met = sum([
            signal.foreign_chips_buy,
            signal.revenue_growth,
            signal.volume_signal,
            signal.ma_cross_up,
            signal.rsi_cross_up
        ])

        if conditions_met >= 4:
            signal.strength = SignalStrength.STRONG
            signal.reasons.append("五大條件基本齊備")
        elif conditions_met >= 3:
            signal.strength = SignalStrength.MEDIUM
            signal.reasons.append("三項條件滿足")
        else:
            signal.strength = SignalStrength.WEAK
            signal.signal_type = SignalType.WATCH
            signal.reasons.append("條件不足，建議觀察")

        # 添加具體理由
        if signal.foreign_chips_buy:
            signal.reasons.append("法人買超確認")
        if signal.revenue_growth:
            signal.reasons.append("營收成長確認")
        if signal.ma_cross_up:
            signal.reasons.append("MA向上交叉")
        if signal.rsi_cross_up:
            signal.reasons.append("RSI向上交叉")

        # 風險提示
        if stock_metrics.foreign_holding > 20:
            signal.cautions.append("⚠️ 外資高持股（>20%），需注意減碼風險")
        if stock_metrics.score < 50:
            signal.cautions.append("⚠️ 綜合分數較低，勝率有限")

        return signal

    @staticmethod
    def generate_exit_signal(stock_metrics) -> TradingSignal:
        """
        生成出場信號
        條件：
        1. MA交叉向下
        2. RSI交叉向下
        """
        signal = TradingSignal(
            code=stock_metrics.code,
            name=stock_metrics.name,
            signal_type=SignalType.EXIT,
            strength=SignalStrength.MEDIUM,
            price=stock_metrics.price,
            score=stock_metrics.score,
            reasons=[],
            cautions=[]
        )

        # 檢查出場條件
        signal.ma_cross_down = stock_metrics.ma_signal == "DOWN"
        signal.rsi_cross_down = stock_metrics.rsi_signal == "DOWN"

        # 判斷信號強度
        if signal.ma_cross_down or signal.rsi_cross_down:
            signal.strength = SignalStrength.STRONG
            signal.reasons.append("技術面轉弱，應準備出場")

            if signal.ma_cross_down:
                signal.reasons.append("MA開始向下")
            if signal.rsi_cross_down:
                signal.reasons.append("RSI開始向下")

        return signal

    @staticmethod
    def generate_avoidance_signal(stock_metrics) -> TradingSignal:
        """
        生成迴避信號
        條件：
        1. 法人持續減碼 (>10%)
        2. 營收持續衰退
        """
        signal = TradingSignal(
            code=stock_metrics.code,
            name=stock_metrics.name,
            signal_type=SignalType.AVOID,
            strength=SignalStrength.STRONG,
            price=stock_metrics.price,
            score=stock_metrics.score,
            reasons=stock_metrics.fail_reasons,
            cautions=[]
        )

        if stock_metrics.foreign_holding > 10 and stock_metrics.foreign_net < 0:
            signal.cautions.append("🚫 外資減碼信號（>10%持股）")

        if stock_metrics.trust_holding > 10 and stock_metrics.trust_net < 0:
            signal.cautions.append("🚫 投信減碼信號（>10%持股）")

        return signal


def generate_signal_report(stock_results: List) -> str:
    """生成交易信號報告"""

    generator = TradingSignalGenerator()

    # 分類信號
    entry_strong = []
    entry_medium = []
    entry_weak = []
    exit_signals = []
    watch_signals = []
    avoid_signals = []

    for stock in stock_results:
        # 生成進場信號
        entry = generator.generate_entry_signal(stock)

        if entry.signal_type == SignalType.ENTRY:
            if entry.strength == SignalStrength.STRONG:
                entry_strong.append(entry)
            elif entry.strength == SignalStrength.MEDIUM:
                entry_medium.append(entry)
            else:
                entry_weak.append(entry)
        elif entry.signal_type == SignalType.WATCH:
            watch_signals.append(entry)

        # 生成出場信號
        exit_sig = generator.generate_exit_signal(stock)
        if exit_sig.ma_cross_down or exit_sig.rsi_cross_down:
            exit_signals.append(exit_sig)

        # 生成迴避信號
        if len(stock.fail_reasons) >= 2:
            avoid = generator.generate_avoidance_signal(stock)
            avoid_signals.append(avoid)

    # 生成報告
    report = "╔" + "═" * 100 + "╗\n"
    report += "║" + "【交易信號生成系統】".center(100) + "║\n"
    report += f"║" + f"生成時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}".center(100) + "║\n"
    report += "╚" + "═" * 100 + "╝\n\n"

    # 進場信號 - 強
    report += "═" * 100 + "\n"
    report += f"【🟢 進場信號 - 強】({len(entry_strong)} 支)\n"
    report += "═" * 100 + "\n\n"

    if entry_strong:
        report += f"{'代號':<8} {'股名':<12} {'價格':>10} {'分數':>8} {'信號原因':<50}\n"
        report += "─" * 100 + "\n"

        for signal in sorted(entry_strong, key=lambda x: x.score, reverse=True)[:10]:
            reasons = " + ".join(signal.reasons[:2])
            report += f"{signal.code:<8} {signal.name:<12} {signal.price:>10.2f} {signal.score:>7.1f}% {reasons:<50}\n"
            if signal.cautions:
                for caution in signal.cautions:
                    report += f"  {caution}\n"

        if len(entry_strong) > 10:
            report += f"\n... 還有 {len(entry_strong) - 10} 支\n"
    else:
        report += "暫無強進場信號\n"

    # 進場信號 - 中
    report += f"\n【🟡 進場信號 - 中】({len(entry_medium)} 支)\n"
    report += "─" * 100 + "\n\n"

    if entry_medium:
        report += f"{'代號':<8} {'股名':<12} {'價格':>10} {'分數':>8} {'信號原因':<50}\n"
        report += "─" * 100 + "\n"

        for signal in sorted(entry_medium, key=lambda x: x.score, reverse=True)[:5]:
            reasons = " + ".join(signal.reasons[:2])
            report += f"{signal.code:<8} {signal.name:<12} {signal.price:>10.2f} {signal.score:>7.1f}% {reasons:<50}\n"

    # 出場信號
    if exit_signals:
        report += f"\n\n【🔴 出場信號】({len(exit_signals)} 支)\n"
        report += "─" * 100 + "\n\n"

        report += f"{'代號':<8} {'股名':<12} {'價格':>10} {'MA':>8} {'RSI':>8} {'建議':20}\n"
        report += "─" * 100 + "\n"

        for signal in exit_signals[:10]:
            ma_status = "↘ DOWN" if signal.ma_cross_down else "HOLD"
            rsi_status = "↘ DOWN" if signal.rsi_cross_down else "HOLD"
            advice = "止損" if signal.score < 40 else "獲利了結"
            report += f"{signal.code:<8} {signal.name:<12} {signal.price:>10.2f} {ma_status:>8} {rsi_status:>8} {advice:20}\n"

    # 迴避信號
    if avoid_signals:
        report += f"\n\n【⛔ 迴避信號】({len(avoid_signals)} 支)\n"
        report += "─" * 100 + "\n\n"

        for signal in avoid_signals[:5]:
            report += f"{signal.code} {signal.name}\n"
            for reason in signal.cautions:
                report += f"  {reason}\n"

    # 交易建議
    report += "\n\n" + "═" * 100 + "\n"
    report += "【交易建議】\n"
    report += "═" * 100 + "\n\n"

    report += """
進場策略：
  • 優先選擇「強進場信號」個股
  • 以成交大量區或法人成本區作為進場價位
  • 確認 2 日均線交叉 7 日均線向上
  • 同時檢查 6 RSI 交叉 12 RSI 是否向上

風險控管：
  • 設置止損點：跌破 7 日線或 RSI 交叉向下
  • 分批進場，分批出場
  • 監控外資、投信持股變化
  • 關注營收月變動趨勢

進出場信號標準：
  進場：2日≥7日線 且 (法人買超 或 RSI向上)
  出場：2日<7日線 或 RSI<12RSI 或 商品被移除

適用市場環境：
  ✓ 短線行情 (1-5 天)
  ✓ 個股強勢表現
  ✓ 法人推升特定產業
  ✓ 技術面突破格局

"""

    return report


if __name__ == "__main__":
    print("🎯 交易信號系統已準備就緒")
    print("建議在計量篩選完成後調用此模塊生成交易信號")
