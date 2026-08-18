"""
券商分點資料（TaiwanStockTradingDailyReport）
- DB-first：若 broker_daily_actions 已有當日+個股資料，不重複打 API
- 每次只拉單日（API 不支援多日）
- 並發限制：_broker_sem（最多 3）
"""
import logging
import asyncio
from datetime import date

from src.db import execute, fetch_all, fetch_one
from src.finmind_client import fm_get

logger = logging.getLogger(__name__)

# 外資券商關鍵字（辨識是否為外資分點）
_FOREIGN_BROKER_KW = [
    "高盛", "美林", "摩根", "野村", "瑞銀", "花旗", "匯豐", "德意志",
    "麥格理", "巴克萊", "法興", "里昂", "港豐", "新加坡", "港商",
    "Goldman", "Merrill", "Morgan", "Nomura", "UBS", "Citi", "HSBC",
]

_GOV_BANK_KW = ["兆豐", "合庫", "第一", "華南", "臺銀", "土銀", "彰銀", "台企銀", "合作金庫"]


def _broker_type(name: str) -> str:
    """辨識分點性質（粗分類）。"""
    if any(kw in name for kw in _FOREIGN_BROKER_KW):
        return "FOREIGN"
    if any(kw in name for kw in _GOV_BANK_KW):
        return "GOV_BANK"
    return "LOCAL"


async def _cached_broker_data(stock_code: str, trade_date: str) -> list[dict]:
    return await fetch_all(
        """SELECT broker_code, broker_name, buy_volume, sell_volume,
                  net_volume, net_amount, absorption_ratio, stock_volume
           FROM broker_daily_actions
           WHERE stock_code=$1 AND trade_date=$2
           ORDER BY net_volume DESC""",
        stock_code, date.fromisoformat(trade_date),
    )


async def fetch_broker_data(
    stock_code: str,
    trade_date: str,
    stock_volume: int = 0,
    change_pct: float = 0.0,
    is_blood_day: bool = False,
    top_n: int = 10,
) -> list[dict]:
    """
    取得個股單日分點資料（DB-first）。
    回傳 top_n 大淨買超分點。
    """
    cached = await _cached_broker_data(stock_code, trade_date)
    if cached:
        return cached[:top_n]

    rows = await fm_get(
        "TaiwanStockTradingDailyReport",
        stock_code,
        trade_date,
        use_broker_sem=True,
    )

    if not rows:
        return []

    # 過濾當日（API 只支援單日，但以防萬一）
    rows = [r for r in rows if r.get("date") == trade_date]

    # 彙總（同一分點可能有多筆）
    agg: dict[str, dict] = {}
    for r in rows:
        bid   = str(r.get("securities_trader_id", "")).strip()
        bname = str(r.get("securities_trader", "")).strip()
        if not bid:
            continue
        if bid not in agg:
            agg[bid] = {"broker_code": bid, "broker_name": bname, "buy": 0, "sell": 0}
        agg[bid]["buy"]  += int(r.get("buy") or 0)
        agg[bid]["sell"] += int(r.get("sell") or 0)

    # 計算 net、金額、吞噬率
    close_est = 0.0  # 無即時收盤，用近似估算
    result = []
    for bid, d in agg.items():
        net = d["buy"] - d["sell"]
        # 估算金額：需 close price，若無則 0
        net_amount = 0.0
        absorption = round(net / stock_volume * 100, 2) if stock_volume > 0 else None

        result.append({
            "broker_code":      bid,
            "broker_name":      d["broker_name"],
            "buy_volume":       d["buy"],
            "sell_volume":      d["sell"],
            "net_volume":       net,
            "net_amount":       net_amount,
            "stock_volume":     stock_volume,
            "absorption_ratio": absorption,
            "broker_type":      _broker_type(d["broker_name"]),
        })

    result.sort(key=lambda x: x["net_volume"], reverse=True)

    # 寫入 DB（只寫 top 20，節省空間）
    for r in result[:20]:
        try:
            await execute(
                """
                INSERT INTO broker_daily_actions
                  (trade_date, stock_code, broker_code, broker_name,
                   buy_volume, sell_volume, net_volume, net_amount,
                   stock_volume, absorption_ratio, is_blood_day, day_change_pct)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                ON CONFLICT(trade_date, stock_code, broker_code) DO NOTHING
                """,
                date.fromisoformat(trade_date), stock_code,
                r["broker_code"], r["broker_name"],
                r["buy_volume"], r["sell_volume"], r["net_volume"], r["net_amount"],
                stock_volume, r["absorption_ratio"],
                is_blood_day, change_pct,
            )
        except Exception as e:
            logger.debug(f"broker_daily_actions insert {stock_code}/{r['broker_code']}: {e}")

    return result[:top_n]


async def fetch_broker_batch(
    stocks: list[dict],
    trade_date: str,
    price_map: dict,
    is_blood_day: bool = False,
    top_n: int = 10,
) -> dict[str, list[dict]]:
    """批量取得多支個股的分點資料（有限並發）。"""
    sem = asyncio.Semaphore(3)

    async def _fetch_one(s: dict) -> tuple[str, list]:
        code = s["code"]
        price = price_map.get(code) or {}
        vol   = int(price.get("volume", 0) or 0)
        pct   = float(price.get("change_pct", 0) or 0)
        async with sem:
            data = await fetch_broker_data(code, trade_date, vol, pct, is_blood_day, top_n)
            await asyncio.sleep(0.3)   # 保護 API，讓伺服器喘息
            return code, data

    tasks   = [_fetch_one(s) for s in stocks]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    out = {}
    for item in results:
        if isinstance(item, Exception):
            logger.warning(f"broker_batch error: {item}")
            continue
        code, data = item
        out[code] = data

    return out
