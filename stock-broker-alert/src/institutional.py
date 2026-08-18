"""
三大法人 + 八大公股 資料抓取與快取
八大公股：單次全市場 call（TaiwanStockGovernmentBankBuySell），再依代碼過濾
"""
import logging
from datetime import date

from src.db import execute, fetch_one, fetch_all
from src.finmind_client import fm_get

logger = logging.getLogger(__name__)

# FinMind TaiwanStockGovernmentBankBuySell 中的公股銀行名稱關鍵字
# 注意：不自行擴充定義，只使用 API 實際回傳的名稱
_GOV_BANK_KEYWORDS = ["兆豐", "合庫", "第一", "華南", "臺銀", "土銀", "彰銀", "台企銀",
                       "合作金庫", "臺灣銀行", "臺灣土地", "彰化銀行"]

_FOREIGN_NAMES = {
    "Foreign_Investor", "Foreign_Dealer_Self",
    "外資", "外資自營", "外資及陸資(不含外資自營商)", "外資自營商",
}
_TRUST_NAMES   = {"Investment_Trust", "投信"}
_DEALER_NAMES  = {"Dealer_self", "Dealer_Hedging", "自營商", "自營商(自行買賣)", "自營商(避險)"}


def _is_gov_bank(name: str) -> bool:
    return any(kw in name for kw in _GOV_BANK_KEYWORDS)


async def fetch_institutional(stock_code: str, trade_date: str) -> dict | None:
    """三大法人（DB-first）。"""
    cached = await fetch_one(
        """SELECT foreign_net, trust_net, dealer_net FROM institutional_daily
           WHERE stock_code=$1 AND trade_date=$2""",
        stock_code, date.fromisoformat(trade_date),
    )
    if cached:
        return {
            "foreign_net": cached["foreign_net"] or 0,
            "trust_net":   cached["trust_net"]   or 0,
            "dealer_net":  cached["dealer_net"]  or 0,
        }

    rows = await fm_get("TaiwanStockInstitutionalInvestorsBuySell", stock_code, trade_date)
    if not rows:
        return None

    result = {"foreign_net": 0, "trust_net": 0, "dealer_net": 0}
    found = False
    for r in rows:
        if r.get("date") != trade_date:
            continue
        name = r.get("name", "")
        net  = int(r.get("buy") or 0) - int(r.get("sell") or 0)
        if name in _FOREIGN_NAMES or ("外資" in name or "Foreign" in name):
            result["foreign_net"] += net; found = True
        elif name in _TRUST_NAMES or "投信" in name:
            result["trust_net"] += net; found = True
        elif name in _DEALER_NAMES or "自營" in name:
            result["dealer_net"] += net; found = True

    if found:
        try:
            await execute(
                """
                INSERT INTO institutional_daily(stock_code, trade_date, foreign_net, trust_net, dealer_net,
                    total_net, foreign_buy, foreign_sell)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8)
                ON CONFLICT(stock_code, trade_date) DO NOTHING
                """,
                stock_code, date.fromisoformat(trade_date),
                result["foreign_net"], result["trust_net"], result["dealer_net"],
                result["foreign_net"] + result["trust_net"] + result["dealer_net"],
                0, 0,
            )
        except Exception as e:
            logger.debug(f"institutional_daily insert {stock_code}: {e}")

    return result if found else None


async def fetch_gov_bank_all(trade_date: str) -> dict[str, dict]:
    """
    八大公股全市場單次 call。
    回傳: {stock_code: {gov_bank_net, gov_bank_detail, pub_bank_status}}
    """
    # 先檢查 DB 是否已有當日資料
    count = await fetch_all(
        "SELECT COUNT(*) AS cnt FROM gov_bank_daily WHERE trade_date=$1",
        date.fromisoformat(trade_date),
    )
    if count and count[0].get("cnt", 0) > 0:
        logger.info(f"公股資料從 DB 載入 (date={trade_date})")
        rows = await fetch_all(
            "SELECT stock_code, bank_name, buy_shares, sell_shares FROM gov_bank_daily WHERE trade_date=$1",
            date.fromisoformat(trade_date),
        )
    else:
        # 全市場一次 call（不帶 data_id）
        rows_raw = await fm_get("TaiwanStockGovernmentBankBuySell", None, trade_date)
        if not rows_raw:
            logger.warning(f"公股資料無回傳 (date={trade_date})")
            return {}

        # 過濾當日資料
        rows_raw = [r for r in rows_raw if r.get("date") == trade_date]

        # 確認 API 實際回傳的銀行名稱（用於判斷是否為公股）
        actual_names = {r.get("securities_trader", "") for r in rows_raw}
        gov_names = {n for n in actual_names if _is_gov_bank(n)}
        if not gov_names:
            logger.warning("公股資料無法精確辨識，標記 PUBLIC_BANK_DATA_UNAVAILABLE")
            return {}

        logger.info(f"確認公股名稱：{gov_names}")

        # 寫入 DB
        for r in rows_raw:
            bank = r.get("securities_trader", "")
            if not _is_gov_bank(bank):
                continue
            code = r.get("stock_id", "")
            try:
                await execute(
                    """
                    INSERT INTO gov_bank_daily(stock_code, trade_date, bank_name, buy_shares, sell_shares)
                    VALUES($1,$2,$3,$4,$5)
                    ON CONFLICT(stock_code, trade_date, bank_name) DO NOTHING
                    """,
                    code, date.fromisoformat(trade_date), bank,
                    int(r.get("buy") or 0), int(r.get("sell") or 0),
                )
            except Exception as e:
                logger.debug(f"gov_bank_daily insert {code}: {e}")

        rows = await fetch_all(
            "SELECT stock_code, bank_name, buy_shares, sell_shares FROM gov_bank_daily WHERE trade_date=$1",
            date.fromisoformat(trade_date),
        )

    # 彙總成 {stock_code: {...}}
    result: dict[str, dict] = {}
    for r in rows:
        code = r["stock_code"]
        if code not in result:
            result[code] = {"gov_bank_net": 0, "gov_bank_detail": {}, "pub_bank_status": "OK"}
        net = int(r.get("buy_shares", 0) or 0) - int(r.get("sell_shares", 0) or 0)
        result[code]["gov_bank_net"] += net
        result[code]["gov_bank_detail"][r["bank_name"]] = net

    return result
