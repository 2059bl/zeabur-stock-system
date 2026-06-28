"""
台股板塊輪動資料計算
為 /api/sector-rotation 提供 flow20、accel5、change5 三項指標。

flow20  : 近 20 個交易日法人（外資+投信）累計淨買超，單位億元
accel5  : 近 5 日均買 − 前 5 日均買，單位億元/日
change5 : 代表股近 5 個交易日漲跌幅 %
"""
import asyncio
import logging
from datetime import date, timedelta
from typing import Optional

import httpx

import os

logger = logging.getLogger(__name__)

FINMIND_TOKEN = os.environ.get("FINMIND_API_KEY") or os.environ.get("FINMIND_TOKEN", "")
_BASE = "https://api.finmindtrade.com/api/v4/data"

# 外資 + 投信 name 集合（英中雙語）
_FOREIGN_NAMES = {
    "Foreign_Investor", "Foreign_Dealer_Self",
    "外資", "外資自營", "外資及陸資(不含外資自營商)", "外資自營商",
}
_TRUST_NAMES = {"Investment_Trust", "投信"}
_CHIP_NAMES  = _FOREIGN_NAMES | _TRUST_NAMES


# ── 板塊定義 ────────────────────────────────────────────────────────────────
# stocks: 代表股代碼（用於拉籌碼 & 價格；前 3 支用於漲跌幅均值）
# label : 顯示用名稱（stocks_label 是給前端 tooltip 顯示的可讀名稱）
SECTORS: list[dict] = [
    {
        "sector": "AI 伺服器組裝",   "theme": "AI 供應鏈",
        "stocks": ["2376","2356","3231","2353"],
        "stocks_label": "2376 技嘉、2356 英業達、3231 緯創、2353 宏碁",
    },
    {
        "sector": "EMS 電子代工",    "theme": "AI 供應鏈",
        "stocks": ["2317","2382","4938"],
        "stocks_label": "2317 鴻海、2382 廣達、4938 和碩",
    },
    {
        "sector": "散熱模組",        "theme": "AI 供應鏈",
        "stocks": ["3017","3324","3653"],
        "stocks_label": "3017 奇鋐、3324 雙鴻、3653 健策",
    },
    {
        "sector": "整合與委外",      "theme": "AI 供應鏈",
        "stocks": ["3658","2345","6438"],
        "stocks_label": "系統整合、委外服務",
    },
    {
        "sector": "CPO 光通訊",      "theme": "光通訊",
        "stocks": ["6803","6238","3081"],
        "stocks_label": "波若威、訊芯-KY、聯亞",
    },
    {
        "sector": "網通設備",        "theme": "通訊",
        "stocks": ["2345","5388"],
        "stocks_label": "2345 智邦、5388 中磊",
    },
    {
        "sector": "功率電感",        "theme": "被動元件",
        "stocks": ["2327","2456","6173"],
        "stocks_label": "2327 國巨、2456 奇力新、6173 信昌電",
    },
    {
        "sector": "PCB 高階板",      "theme": "電子零組件",
        "stocks": ["3037","3189","8046"],
        "stocks_label": "3037 欣興、3189 景碩、8046 南電",
    },
    {
        "sector": "智慧型手機",      "theme": "消費電子",
        "stocks": ["2317","2474","3008"],
        "stocks_label": "2317 鴻海、2474 可成、3008 大立光",
    },
    {
        "sector": "晶圓代工",        "theme": "半導體",
        "stocks": ["2330","2303","6770"],
        "stocks_label": "2330 台積電、2303 聯電、6770 力積電",
    },
    {
        "sector": "AI 先進封裝",     "theme": "半導體",
        "stocks": ["3711","3264","6271"],
        "stocks_label": "3711 日月光、3264 欣銓、6271 同欣電",
    },
    {
        "sector": "IC 設計",         "theme": "半導體",
        "stocks": ["2454","3443","3035"],
        "stocks_label": "2454 聯發科、3443 創意、3035 智原",
    },
    {
        "sector": "HBM 記憶體",      "theme": "記憶體",
        "stocks": ["4919","3443","2330"],
        "stocks_label": "記憶體供應鏈",
    },
    {
        "sector": "車用電子",        "theme": "車用",
        "stocks": ["1590","2207","1537"],
        "stocks_label": "車用零組件、電動車供應鏈",
    },
    {
        "sector": "工業電腦",        "theme": "工業電腦",
        "stocks": ["2395","6414"],
        "stocks_label": "2395 研華、6414 樺漢",
    },
    {
        "sector": "面板",            "theme": "顯示器",
        "stocks": ["2409","3481"],
        "stocks_label": "2409 友達、3481 群創",
    },
    {
        "sector": "金融控股",        "theme": "金融",
        "stocks": ["2881","2882","2891"],
        "stocks_label": "2881 富邦金、2882 國泰金、2891 中信金",
    },
    {
        "sector": "航運",            "theme": "傳產",
        "stocks": ["2603","2609","2615"],
        "stocks_label": "2603 長榮、2609 陽明、2615 萬海",
    },
    {
        "sector": "雲端與 MSP",      "theme": "雲端服務",
        "stocks": ["6488","3217","5203"],
        "stocks_label": "數位雲端、資服通路",
    },
    {
        "sector": "生技醫療",        "theme": "防禦",
        "stocks": ["4736","4743","1707"],
        "stocks_label": "醫材、新藥、保健",
    },
]

# ── FinMind 原始請求 ─────────────────────────────────────────────────────────

async def _fm_get(sem: asyncio.Semaphore, dataset: str, data_id: str, start_date: str) -> list[dict]:
    async with sem:
        params = {"dataset": dataset, "data_id": data_id, "start_date": start_date, "token": FINMIND_TOKEN}
        try:
            async with httpx.AsyncClient(timeout=25) as c:
                r = await c.get(_BASE, params=params)
                r.raise_for_status()
                body = r.json()
                if body.get("status") != 200:
                    logger.debug(f"FinMind {dataset} {data_id}: {body.get('msg')}")
                    return []
                return body.get("data", [])
        except Exception as e:
            logger.warning(f"FinMind {dataset} {data_id}: {e}")
            return []


# ── 板塊法人淨買超計算 ───────────────────────────────────────────────────────

async def _chip_for_stock(sem: asyncio.Semaphore, code: str, start_date: str) -> dict[str, int]:
    """回傳 {date: net_buy_shares} 外資+投信合計"""
    rows = await _fm_get(sem, "TaiwanStockInstitutionalInvestorsBuySell", code, start_date)
    daily: dict[str, int] = {}
    for row in rows:
        if row.get("name") not in _CHIP_NAMES:
            continue
        d   = row.get("date", "")
        net = int(row.get("buy") or 0) - int(row.get("sell") or 0)
        daily[d] = daily.get(d, 0) + net
    return daily


async def _price_for_stock(sem: asyncio.Semaphore, code: str, start_date: str) -> list[dict]:
    """回傳 [{date, close}, ...] 排序"""
    rows = await _fm_get(sem, "TaiwanStockPrice", code, start_date)
    return sorted([{"date": r["date"], "close": float(r["close"])} for r in rows if "close" in r],
                  key=lambda x: x["date"])


# ── 指標計算 ─────────────────────────────────────────────────────────────────

def _calc_flow_accel(daily_nets: dict[str, int], trading_dates: list[str]) -> tuple[float, float]:
    """
    trading_dates: 最近 20 個交易日（由新到舊）
    flow20 : 合計，除以 1000（股→張）再除以 100（萬→億）= /100000
    accel5 : (last5_avg − prev5_avg)，同單位
    """
    unit = 100_000  # 股 → 億元（估算，以淨買超股數 / 均價 換算；此處僅比例，用股數代替）
    # 以「千張」為單位（1 張=1000 股 → 千張=百萬股）；flow20 單位億元需乘均價估算
    # 簡化：以「億股」代替億元（比例正確，量級不同），前端只看相對大小與方向
    vals = [daily_nets.get(d, 0) / 1_000_000 for d in trading_dates]  # 單位：百萬股
    flow20 = round(sum(vals), 2)
    last5  = sum(vals[:5]) / 5 if vals[:5] else 0
    prev5  = sum(vals[5:10]) / 5 if vals[5:10] else 0
    accel5 = round(last5 - prev5, 2)
    return flow20, accel5


def _calc_change5(prices: list[dict]) -> float:
    """近 5 個交易日收盤漲跌幅 %"""
    if len(prices) < 6:
        return 0.0
    cur  = prices[-1]["close"]
    base = prices[-6]["close"]
    if base == 0:
        return 0.0
    return round((cur - base) / base * 100, 2)


# ── 板塊聚合 ─────────────────────────────────────────────────────────────────

async def _calc_sector(sem: asyncio.Semaphore, sector: dict,
                       start_chip: str, start_price: str,
                       trading_dates: list[str]) -> dict:
    codes = sector["stocks"]

    # 並行拉各股資料
    chip_tasks  = [_chip_for_stock(sem, c, start_chip)  for c in codes]
    price_tasks = [_price_for_stock(sem, c, start_price) for c in codes]
    chip_results, price_results = await asyncio.gather(
        asyncio.gather(*chip_tasks),
        asyncio.gather(*price_tasks),
    )

    # 合計各股籌碼
    merged_chips: dict[str, int] = {}
    for daily in chip_results:
        for d, v in daily.items():
            merged_chips[d] = merged_chips.get(d, 0) + v

    flow20, accel5 = _calc_flow_accel(merged_chips, trading_dates)

    # 價格用代表股（第一支有足夠資料的）
    change5 = 0.0
    for prices in price_results:
        if len(prices) >= 6:
            change5 = _calc_change5(prices)
            break

    return {
        "sector":  sector["sector"],
        "theme":   sector["theme"],
        "flow20":  flow20,
        "accel5":  accel5,
        "change5": change5,
        "stocks":  sector["stocks_label"],
    }


# ── 公開入口 ─────────────────────────────────────────────────────────────────

async def build_sector_rotation(today: Optional[str] = None) -> list[dict]:
    """
    計算所有板塊輪動指標，回傳 list[dict]。
    today: ISO 日期字串（預設今天）。
    """
    td = date.fromisoformat(today) if today else date.today()

    # 往前 35 曆日以確保有 20 個交易日
    start_chip  = (td - timedelta(days=35)).isoformat()
    start_price = (td - timedelta(days=35)).isoformat()

    # 先抓任一指數（0050）的日期序列，作為交易日曆
    sem = asyncio.Semaphore(5)  # 控制並發，避免 FinMind 限流
    calendar_rows = await _fm_get(sem, "TaiwanStockPrice", "0050", start_chip)
    trading_dates = sorted(
        {r["date"] for r in calendar_rows if r["date"] <= td.isoformat()},
        reverse=True
    )[:20]

    if not trading_dates:
        logger.warning("無法取得交易日曆（0050 資料為空）")
        trading_dates = []

    # 並行計算所有板塊（使用同一個 semaphore 控制總並發）
    tasks = [_calc_sector(sem, s, start_chip, start_price, trading_dates) for s in SECTORS]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    output = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            logger.error(f"板塊 {SECTORS[i]['sector']} 計算失敗: {r}")
        else:
            output.append(r)

    return output
