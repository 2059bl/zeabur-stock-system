"""
股票池管理 — 從 YAML 載入並寫入 DB
"""
import os
import logging
from pathlib import Path

import yaml

from src.db import execute, fetch_all

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/app/config"))


def _load_yaml(filename: str) -> dict:
    path = CONFIG_DIR / filename
    if not path.exists():
        path = Path(__file__).parent.parent / "config" / filename
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


async def sync_universe():
    """將 YAML 股票池同步至 DB（upsert）。"""
    stocks = _load_yaml("stocks.yaml").get("stocks", [])
    etfs   = _load_yaml("etfs.yaml").get("etfs", [])

    for s in stocks:
        await execute(
            """
            INSERT INTO growth_stock_universe(stock_code, stock_name, sector)
            VALUES($1,$2,$3)
            ON CONFLICT(stock_code) DO UPDATE
              SET stock_name=EXCLUDED.stock_name,
                  sector=EXCLUDED.sector
            """,
            s["code"], s["name"], s.get("sector"),
        )

    for e in etfs:
        await execute(
            """
            INSERT INTO etf_universe(etf_code, etf_name, etf_type)
            VALUES($1,$2,$3)
            ON CONFLICT(etf_code) DO UPDATE
              SET etf_name=EXCLUDED.etf_name,
                  etf_type=EXCLUDED.etf_type
            """,
            e["code"], e["name"], e.get("type"),
        )

    logger.info(f"Universe 同步完成：{len(stocks)} 支成長股 + {len(etfs)} 支 ETF")


async def get_all_codes() -> list[dict]:
    """回傳所有追蹤代碼（成長股 + ETF）。"""
    stocks = await fetch_all(
        "SELECT stock_code AS code, stock_name AS name, sector FROM growth_stock_universe WHERE active=TRUE"
    )
    etfs = await fetch_all(
        "SELECT etf_code AS code, etf_name AS name, etf_type AS sector FROM etf_universe WHERE active=TRUE"
    )
    return stocks + etfs


async def get_stock_info(code: str) -> dict | None:
    rows = await fetch_all(
        "SELECT stock_code AS code, stock_name AS name, sector FROM growth_stock_universe WHERE stock_code=$1",
        code,
    )
    if rows:
        return rows[0]
    rows = await fetch_all(
        "SELECT etf_code AS code, etf_name AS name, etf_type AS sector FROM etf_universe WHERE etf_code=$1",
        code,
    )
    return rows[0] if rows else None


def load_stocks_from_yaml() -> list[dict]:
    """同步版（供初始化用）。"""
    data = _load_yaml("stocks.yaml").get("stocks", [])
    etfs = _load_yaml("etfs.yaml").get("etfs", [])
    stocks = [{"code": s["code"], "name": s["name"], "sector": s.get("sector", "")} for s in data]
    etfs_clean = [{"code": e["code"], "name": e["name"], "sector": e.get("type", "ETF")} for e in etfs]
    return stocks + etfs_clean
